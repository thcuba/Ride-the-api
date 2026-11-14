"""
Learning Orchestrator - Async worker and queue management per physical connected device.
Handles: per-device buffers, mode routing (learning, hybrid, production, passthrough),
background flush scheduling, and pattern sync.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl

from sqlalchemy import select

from core.capture_state import describe_capture
from core.database import (
    DatabaseManager,
    DeviceRegistry,
    get_db_manager,
)
from core.match_tracker import MatchRateTracker, MatchResult
from core.pattern_db.pattern_engine import PatternEngine
from core.pattern_db.schemas import (
    PatternDB,
    merge_pattern_dbs,
)
from core.pipeline.pipeline import (
    ContextBuffer,
    FlushCoordinator,
    LearningPipeline,
    PipelineMode,
    _llm_validate_weak_match,
)

if TYPE_CHECKING:
    from core.buffer.store import BufferStore
    from core.llm_decipher import LLMDecipherService

logger = logging.getLogger(__name__)


class LearningOrchestrator:
    """Orchestrates the full learning/production pipeline per device."""

    def __init__(self, db_manager: DatabaseManager) -> None:
        self.db_manager = db_manager
        self.llm_decipher = None
        self.buffer: dict[str, ContextBuffer] = {}
        self.engine = None
        self.tracker = None
        self.pipeline = None
        # Outlives pipeline rebuilds (buffer backend switch), see FlushCoordinator.
        self.flushes = FlushCoordinator()

    async def cancel_background_flushes(self) -> None:
        """Cancel automatic flushes still running (called at shutdown)."""
        await self.flushes.cancel_all()

    def initialize(self, llm_decipher: LLMDecipherService):
        """Initialize the orchestrator with required services.

        Sets up the PatternEngine and MatchRateTracker.
        Auto-loads any .ride-pattern.json files found in the patterns/
        or data/ directories so pre-seeded or exported patterns are
        available immediately for production/hybrid serving.
        """
        self.llm_decipher = llm_decipher
        self.engine = PatternEngine(self.db_manager)
        self._auto_load_patterns()
        self.tracker = MatchRateTracker(self.db_manager)

    async def sync_engine(self, device_id: str) -> None:
        """Make the engine serve what the device DB (plus seed files) holds.

        Call after changing a device's patterns outside a learning cycle (API
        edit, delete, import): the engine serves its in-memory set exclusively
        when it has one, so a DB change alone kept serving the old patterns.
        """
        if self.engine is None:
            return
        pipeline = await self._ensure_pipeline(device_id)
        await pipeline.export_and_sync_patterns(device_id)

    def _pattern_search_dirs(self) -> list[Path]:
        """Directories scanned at startup, each once; the export dir included."""
        candidates = [Path("patterns"), Path("data"), Path("data/patterns")]
        with contextlib.suppress(TypeError, AttributeError):
            # Where export_and_sync_patterns writes; it used to be skipped, so
            # a restart lost the learned set's state variables/sensors.
            candidates.insert(0, Path(self.db_manager.device_db_dir).parent / "patterns")
        unique: dict[Path, Path] = {}
        for directory in candidates:
            unique.setdefault(directory.resolve(), directory)
        return list(unique.values())

    @staticmethod
    def _is_own_export(pattern_file: Path, device_id: str) -> bool:
        """Whether the sync wrote this file (it names files by device-id hash)."""
        name = pattern_file.name.removesuffix(".ride-pattern.json")
        return name == hashlib.sha256(device_id.encode("utf-8")).hexdigest()

    def _auto_load_patterns(self):
        """Load .ride-pattern.json files into the engine at startup.

        A file the sync wrote is a snapshot of the device DB: loading it warms
        the in-memory set. Any other file is a user-supplied seed: it is kept
        and merged into every later re-sync from the DB (the first learning
        flush used to replace it). Snapshots load first so a seed for the same
        device is merged on top of its snapshot.
        """
        files = [
            (pattern_file, self._device_id_from_pattern_file(pattern_file))
            for search_dir in self._pattern_search_dirs()
            if search_dir.exists()
            for pattern_file in sorted(search_dir.glob("*.ride-pattern.json"))
        ]
        files.sort(key=lambda item: not self._is_own_export(*item))
        snapshots: dict[str, PatternDB] = {}
        loaded = 0
        for pattern_file, device_id in files:
            seed = not self._is_own_export(pattern_file, device_id)
            try:
                pattern_db = self.engine.load_pattern_file(device_id, str(pattern_file), seed=seed)
            except Exception as e:  # noqa: BLE001 - one bad file must not stop startup
                logger.warning("Failed to load %s: %s", pattern_file.name, e)
                continue
            if not seed:
                snapshots[device_id] = pattern_db
            elif device_id in snapshots:
                self.engine.apply_pattern_db(
                    device_id, merge_pattern_dbs(snapshots[device_id], pattern_db)
                )
            loaded += 1
            logger.info("Auto-loaded patterns for %s from %s", device_id, pattern_file.name)
        if loaded:
            logger.info("Pattern DB: auto-loaded %d pattern files", loaded)
        else:
            logger.debug("Pattern DB: no .ride-pattern.json files found to auto-load")

    @staticmethod
    def _device_id_from_pattern_file(pattern_file: Path) -> str:
        """Recover the real device_id a .ride-pattern.json was exported for.

        Files written by :meth:`LearningPipeline.export_and_sync_patterns` are named after the
        SHA256 hash of the device_id (for filesystem-path safety), so the hash
        alone cannot be used as the runtime cache key. The export embeds the
        actual device_id in ``meta.pattern_id`` using the
        ``<device_id>-patterns`` convention; when present we recover it here,
        falling back to the filename stem otherwise (covers manually shared
        files that do not follow the convention).
        """
        device_id = pattern_file.stem
        if device_id.endswith(".ride-pattern"):
            device_id = device_id.replace(".ride-pattern", "")
        try:
            data = json.loads(pattern_file.read_text(encoding="utf-8"))
            pattern_id = data.get("meta", {}).get("pattern_id", "")
            suffix = "-patterns"
            if pattern_id.endswith(suffix) and len(pattern_id) > len(suffix):
                device_id = pattern_id[: -len(suffix)]
        except (json.JSONDecodeError, OSError):
            pass
        return device_id

    async def ensure_buffer(self, device_id: str, buffer_size: int = 524288) -> ContextBuffer:
        """Get or create a context buffer for a device with the right size."""
        if device_id not in self.buffer or self.buffer[device_id].max_size_bytes != buffer_size:
            self.buffer[device_id] = ContextBuffer(self.db_manager, buffer_size)
        return self.buffer[device_id]

    def reset_buffers(self) -> None:
        """Drop all cached context buffers so they rebuild with the new backend.

        Called when the runtime buffer backend changes (disk <-> memory) so the
        next ``ensure_buffer`` creates stores honoring the active backend.
        """
        self.buffer.clear()
        # The lazy LearningPipeline captured its ContextBuffer at construction;
        # drop it too so a backend switch cannot keep writing to the stale store.
        self.pipeline = None

    def rebind_buffer_store(self, store: BufferStore) -> None:
        """Repoint every live context buffer at ``store`` (a buffer backend switch).

        The pairs in flight have already been carried into the new backend by
        ``core.buffer.switch_backend``; rebinding is what stops the next capture from
        landing in the store the proxy just left, which otherwise only took effect after
        a restart.
        """
        for buffer in self.buffer.values():
            buffer.rebind_store(store)
        # The lazy pipeline captured the store at construction: rebuild it on next use.
        self.pipeline = None

    async def prune_stores(self) -> int:
        """Apply TTL/cap pruning to the persisted training store for every device.

        Reads ``store_pairs`` / ``max_pairs_per_device`` / ``pair_ttl_hours``
        from config. When pair storage is disabled the on-disk history for each
        device is pruned to its cap so the store cannot grow without bound.
        """
        try:
            from core.config import get_config  # noqa: PLC0415

            cfg = get_config()
            corr = cfg.correlation
        except Exception:
            logger.debug("Prune: config unavailable; using defaults")

            corr = SimpleNamespace(store_pairs=True, max_pairs_per_device=10000, pair_ttl_hours=168)

        if not getattr(corr, "store_pairs", True):
            logger.info("Store-pairs disabled; no correlation persistence to prune")
            return 0

        max_pairs = int(getattr(corr, "max_pairs_per_device", 10000))
        ttl_hours = int(getattr(corr, "pair_ttl_hours", 168))
        devices = await self.db_manager.list_devices()
        total = 0
        for device in devices:
            dev_id = device["device_id"]
            try:
                buffer = await self.ensure_buffer(dev_id)
                deleted = await buffer.prune(
                    dev_id, max_pairs_per_device=max_pairs, pair_ttl_hours=ttl_hours
                )
                total += deleted
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Prune failed for device %s: %s", dev_id, e)
        if total:
            logger.info("Pruned %d stale correlation rows across %d devices", total, len(devices))
        return total

    async def handle_request(  # noqa: PLR0913, PLR0917
        self,
        device_id: str,
        vendor: str,  # noqa: ARG002
        protocol: str,
        method: str,
        path: str,
        headers: dict,
        body: Any,  # noqa: ANN401
        query_params: dict,
        enrichment: dict | None = None,
    ) -> dict:
        """Main entry point: handle an incoming request. Returns response info."""
        # A transport may hand us the raw request target with the query string
        # still attached to the path (the TLS listener forwards the h11 target
        # verbatim). Split it here so (a) pattern matching compares on the path,
        # (b) the query is matched/learned through query_params, and (c) raw
        # query values — which can carry tokens — never land in the persisted
        # path or correlation key (they go through redact_query instead).
        if "?" in path:
            base_path, _, query_string = path.partition("?")
            path = base_path
            if not query_params:
                query_params = dict(parse_qsl(query_string, keep_blank_values=True))

        # Get device mode (cached lookup to avoid a registry query per request)
        device = await self.db_manager.get_device(device_id)
        if not device:
            return {"action": "forward", "reason": "device_not_found"}

        handler = {
            PipelineMode.PRODUCTION.value: self._handle_production,
            PipelineMode.HYBRID.value: self._handle_hybrid,
        }.get(device.mode, self._handle_learning)
        # Passthrough mode short-circuits: forward straight to the cloud with
        # no buffering, LLM analysis or local match. No handler is needed.
        if device.mode == PipelineMode.PASSTHROUGH.value:
            return {"action": "forward", "reason": "passthrough"}
        return await handler(
            device, protocol, method, path, headers, body, query_params, enrichment
        )

    async def _handle_production(  # noqa: PLR0913, PLR0917
        self,
        device: DeviceRegistry,
        protocol: str,
        method: str,
        path: str,
        headers: dict,
        body: Any,  # noqa: ANN401
        query_params: dict,
        enrichment: dict | None = None,
    ) -> dict:
        """Production mode: try local match, fall back to cloud forwarding + learning.

        When ``production_no_fallback`` is enabled on the device, unmatched
        requests return a conclusive ``no_fallback`` action instead of
        forwarding to the cloud.
        """
        pattern, template, score = await self.engine.find_best_match(
            device.device_id, method, path, headers, body, query_params, protocol=protocol
        )

        confident = bool(pattern and template and score >= device.match_threshold)
        if pattern and template and not confident:
            # Weak match: the local engine found a pattern, but below the device
            # threshold. Let the LLM (``correlation`` purpose) confirm it before
            # falling back to the cloud.
            confident = await _llm_validate_weak_match(
                self.llm_decipher,
                device,
                pattern,
                {"method": method, "path": path, "headers": headers, "body": body},
            )
        if confident:
            # Local hit -- build with state-aware engine
            response = await self.engine.build_local_response(
                device.device_id,
                template,
                {"body": body, "headers": headers, "query_params": query_params},
            )
            await self.engine.persist_state(device.device_id)
            await self.tracker.record_result(device.device_id, MatchResult.LOCAL_HIT)
            return {
                "action": "local_response",
                "response": response,
                "match_score": score,
                "pattern_id": getattr(pattern, "pattern_id", None),
                "intent": getattr(pattern, "intent", None),
            }
        # Miss
        await self.tracker.record_result(device.device_id, MatchResult.CLOUD_MISS)

        # Check production_no_fallback flag from device config
        if device.config.get("production_no_fallback", False):
            return {
                "action": "no_fallback",
                "reason": "below_threshold" if pattern else "no_pattern",
                "match_score": score,
            }

        # Forward to cloud + capture for learning
        corr_key = await self._register_for_learning(
            device, protocol, method, path, headers, body, query_params, enrichment
        )
        return {
            "action": "forward",
            "correlation_key": corr_key,
            "match_score": score,
            "reason": "below_threshold" if pattern else "no_pattern",
        }

    async def _handle_hybrid(  # noqa: PLR0913, PLR0917
        self,
        device: DeviceRegistry,
        protocol: str,
        method: str,
        path: str,
        headers: dict,
        body: Any,  # noqa: ANN401
        query_params: dict,
        enrichment: dict | None = None,
    ) -> dict:
        """Hybrid mode: try local match first; if confident serve locally, otherwise
        forward to cloud + learn."""
        pattern, template, score = await self.engine.find_best_match(
            device.device_id, method, path, headers, body, query_params, protocol=protocol
        )

        confident = bool(pattern and template and score >= device.match_threshold)
        if pattern and template and not confident:
            # Weak match: the local engine found a pattern, but below the device
            # threshold. Let the LLM (``correlation`` purpose) confirm it before
            # falling back to the cloud.
            confident = await _llm_validate_weak_match(
                self.llm_decipher,
                device,
                pattern,
                {"method": method, "path": path, "headers": headers, "body": body},
            )
        if confident:
            # Confident local hit -- build with state-aware engine
            response = await self.engine.build_local_response(
                device.device_id,
                template,
                {"body": body, "headers": headers, "query_params": query_params},
            )
            await self.engine.persist_state(device.device_id)
            await self.tracker.record_result(device.device_id, MatchResult.LOCAL_HIT)
            return {
                "action": "local_response",
                "response": response,
                "match_score": score,
                "pattern_id": getattr(pattern, "pattern_id", None),
                "intent": getattr(pattern, "intent", None),
                "mode": "hybrid",
            }
        # Not confident -- forward to cloud but also capture for learning
        corr_key = await self._register_for_learning(
            device, protocol, method, path, headers, body, query_params, enrichment
        )
        await self.tracker.record_result(device.device_id, MatchResult.CLOUD_MISS)
        return {
            "action": "forward",
            "correlation_key": corr_key,
            "match_score": score,
            "reason": "below_threshold" if pattern else "no_pattern",
            "mode": "hybrid",
        }

    async def _handle_learning(  # noqa: PLR0913, PLR0917
        self,
        device: DeviceRegistry,
        protocol: str,
        method: str,
        path: str,
        headers: dict,
        body: Any,  # noqa: ANN401
        query_params: dict,
        enrichment: dict | None = None,
    ) -> dict:
        """Learning mode: forward all to cloud, correlate, and build patterns.

        The request is always forwarded and captured. We also run a *shadow*
        match against the patterns learned so far and record whether the local
        engine would have served it. That is what makes the match rate
        meaningful in learning mode: it climbs toward the auto-switch threshold
        as coverage improves, instead of being pinned at 0% by a forced miss on
        every request (which made auto-switch unreachable).
        """
        corr_key = await self._register_for_learning(
            device, protocol, method, path, headers, body, query_params, enrichment
        )
        try:
            pattern, template, score = await self.engine.find_best_match(
                device.device_id, method, path, headers, body, query_params, protocol=protocol
            )
            would_serve = bool(pattern and template and score >= device.match_threshold)
        except Exception:  # noqa: BLE001 - a shadow-match failure must not break capture
            logger.debug("Shadow match failed for %s", device.device_id, exc_info=True)
            would_serve = False
        await self.tracker.record_result(
            device.device_id,
            MatchResult.LOCAL_HIT if would_serve else MatchResult.CLOUD_MISS,
        )
        return {
            "action": "forward",
            "correlation_key": corr_key,
            "mode": "learning",
        }

    async def _ensure_pipeline(self, device_id: str) -> LearningPipeline:
        """Lazily build the LearningPipeline bound to this device's buffer."""
        if not self.pipeline:
            buffer = await self.ensure_buffer(device_id)
            self.pipeline = LearningPipeline(
                self.db_manager,
                self.llm_decipher,
                buffer,
                self.tracker,
                self.engine,
                flushes=self.flushes,
            )
        return self.pipeline

    async def flush_and_learn(  # noqa: PLR0913
        self, device_id: str, pair_ids: list[int] | None = None, context_notes: str | None = None
    ) -> dict:
        """Flush the device buffer: LLM-analyse the pairs and persist patterns.

        Delegate to the ``LearningPipeline`` (the public route hits
        ``orchestrator.flush_and_learn``; the logic lives on the pipeline).
        """
        pipeline = await self._ensure_pipeline(device_id)
        return await pipeline.flush_and_learn(
            device_id, pair_ids=pair_ids, context_notes=context_notes
        )

    async def preview_analysis(
        self, device_id: str, pair_ids: list[int] | None = None, context_notes: str | None = None
    ) -> dict:
        """Run LLM analysis WITHOUT saving patterns (for user review)."""
        pipeline = await self._ensure_pipeline(device_id)
        return await pipeline.preview_analysis(
            device_id, pair_ids=pair_ids, context_notes=context_notes
        )

    async def _register_for_learning(  # noqa: PLR0913, PLR0917
        self,
        device: DeviceRegistry,
        protocol: str,
        method: str,
        path: str,
        headers: dict,
        body: Any,  # noqa: ANN401
        query_params: dict,
        enrichment: dict | None = None,
    ) -> str:
        """Register a request for learning capture with its real protocol."""
        if not self.pipeline:
            buffer = await self.ensure_buffer(device.device_id, device.context_buffer_size)
            self.pipeline = LearningPipeline(
                self.db_manager,
                self.llm_decipher,
                buffer,
                self.tracker,
                self.engine,
                flushes=self.flushes,
            )
        return await self.pipeline.register_request(
            device.device_id,
            device.vendor,
            protocol,
            method,
            path,
            headers,
            body,
            query_params,
            enrichment,
        )

    async def discard_request(
        self, device_id: str, correlation_key: str, *, count_as_error: bool = True
    ) -> bool:
        """Discard a pending capture whose response will never be correlated."""
        if not self.pipeline:
            return False
        return await self.pipeline.discard_request(
            device_id, correlation_key, count_as_error=count_as_error
        )

    async def handle_response(  # noqa: PLR0913, PLR0917
        self,
        device_id: str,
        vendor: str,
        protocol: str,
        status_code: int,
        headers: dict,
        body: Any,  # noqa: ANN401
        correlation_key: str | None = None,
    ) -> dict:
        """Process a response from the cloud (for learning mode)."""
        if not self.pipeline:
            return {"action": "ignored", "reason": "no_pipeline"}

        pair = await self.pipeline.match_response(
            device_id, vendor, protocol, status_code, headers, body, correlation_key=correlation_key
        )

        if not pair:
            return {"action": "ignored", "reason": "no_correlation"}

        # Get device for mode
        async with self.db_manager.core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            if not device:
                return {"action": "ignored", "reason": "device_not_found"}

        if device.mode == PipelineMode.LEARNING.value:
            needs_flush = await self.pipeline.process_learning_pair(
                device_id, pair, device.context_buffer_size
            )
            return {
                "action": "buffered_for_learning",
                "pair_id": pair.pair_id,
                "buffer_flushed": needs_flush,
            }
        # Passthrough mode: never buffer or learn from the response — the
        # traffic was forwarded untouched.
        if device.mode == PipelineMode.PASSTHROUGH.value:
            return {"action": "ignored", "reason": "passthrough"}
        # Production/hybrid mode: learn from the miss. The CLOUD_MISS is NOT
        # recorded again here - it was already counted once when the request
        # was forwarded (_handle_production/_handle_hybrid record it at
        # request time). Recording it again on the response would double-count
        # total_requests/cloud_misses and understate the real match rate.
        needs_flush = await self.pipeline.process_learning_pair(
            device_id, pair, device.context_buffer_size
        )
        return {
            "action": "learned_from_miss",
            "pair_id": pair.pair_id,
            "buffer_flushed": needs_flush,
        }

    async def get_device_stats(self, device_id: str) -> dict:
        """Get comprehensive stats for a device."""
        stats = await self.tracker.get_stats(device_id)
        async with self.db_manager.core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            if device:
                stats["mode"] = device.mode
                stats["match_threshold"] = device.match_threshold
                stats["context_buffer_size"] = device.context_buffer_size
                stats["vendor"] = device.vendor
                stats["device_type"] = device.device_type
                stats["name"] = device.name
                stats["resolved_protocol"] = await self.db_manager.resolve_device_protocol(
                    device_id
                )
                ips = list(device.ip_addresses or [])
                stats["ip_addresses"] = ips
                stats["primary_ip"] = ips[0] if ips else None
                # Per-device runtime config surfaced for the dashboard (so the
                # detail panel can render editable controls from one fetch).
                stats["connection"] = (device.extra_attributes or {}).get("connection", "auto")
                stats["tls_passthrough"] = (device.extra_attributes or {}).get(
                    "tls_passthrough", False
                )
                stats["tls_pinning_bypass"] = (device.extra_attributes or {}).get(
                    "tls_pinning_bypass", "mitm_proxy"
                )
                # Why the device is or is not producing pairs, in one field: the reasons
                # otherwise live in three different switches (per-IP bypass, tls
                # passthrough, device mode) and reading the wrong one is how "no data in
                # the buffer" turns into a false alarm.
                attributes = device.extra_attributes or {}
                stats["tls_untrusted_reason"] = attributes.get("tls_untrusted_reason")
                stats["seen_hostnames"] = list(attributes.get("seen_hostnames") or [])
                # How many times the device refused our certificate, and when. Written on every
                # refusal, so a count that keeps growing means a device still trying to negotiate
                # instead of one that refused once and is relayed from then on. Read defensively:
                # extra_attributes is free-form JSON, and a device view must not 500 over it.
                refusals = attributes.get("tls_refusals")
                stats["tls_refusals"] = refusals if isinstance(refusals, int) else 0
                stats["tls_first_refusal_at"] = attributes.get("tls_first_refusal_at")
                stats["tls_last_refusal_at"] = attributes.get("tls_last_refusal_at")
                stats["llm_base_url"] = device.llm_base_url
                stats["llm_model_id"] = device.llm_model_id
                stats["llm_profile_name"] = device.llm_profile_name
                stats["database_url"] = device.database_url
                stats["database_name"] = device.database_name
                # Bypass state (per-IP: from any profile IP, else config default).
                bypass_flags = [await self.db_manager.is_ips_bypassed(ip) for ip in ips]
                stats["bypass"] = any(bypass_flags)
                stats["capture"] = describe_capture(
                    bypass=stats["bypass"],
                    mode=device.mode,
                    tls_passthrough=bool(stats["tls_passthrough"]),
                    untrusted_reason=stats["tls_untrusted_reason"],
                    refusals=stats["tls_refusals"],
                    last_refusal_at=stats["tls_last_refusal_at"],
                )
        return stats


# Global instance
_orchestrator: LearningOrchestrator | None = None


def get_orchestrator() -> LearningOrchestrator:
    """Get or create global orchestrator instance."""
    global _orchestrator  # noqa: PLW0603
    if _orchestrator is None:
        import core.pipeline as _cp  # noqa: PLC0415

        db_mgr_fn = getattr(_cp, "get_db_manager", get_db_manager)
        _orchestrator = LearningOrchestrator(db_mgr_fn())
    return _orchestrator

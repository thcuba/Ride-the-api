Warning: truncated output (original token count: 20494)
Total output lines: 1898

"""
Learning/Production Pipeline - Core engine that manages device learning and local response serving.
Handles: correlation, buffer management, LLM deciphering, pattern matching, and match rate tracking.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import and_, delete, select, update

from core.atomic_io import _SAFE_FILENAME_RE
from core.buffer import create_buffer_store
from core.database import (
    DatabaseManager,
    DeviceRegistry,
    FieldMapping,
    LLMContextBuffer,
    MatchStats,
    RequestPattern,
    ResponseTemplate,
    SessionCache,
    get_db_manager,
)
from core.llm_decipher import LLMDecipherService, LLMProfile, _parse_llm_json
from core.pattern_db import decipher_ingest
from core.pattern_db.pattern_engine import (
    PatternEngine,
)
from core.pattern_db.schemas import DeviceModel, PatternDB, ProtocolInfo
from core.redaction import redact_body, redact_headers, redact_query

if TYPE_CHECKING:
    from core.buffer.store import BufferStore

logger = logging.getLogger(__name__)


# Pre-computed tuple mapping field names to redaction handlers for LLM prompt pair preparation
_REDACT_MAPPINGS = (
    ("request_headers", redact_headers),
    ("response_headers", redact_headers),
    ("request_query", redact_query),
    ("request_body", redact_body),
    ("response_body", redact_body),
)


def _redact_pairs_for_prompt(pairs: list) -> list:
    """Return a copy of capture pairs with sensitive fields redacted.

    Used as defense-in-depth before building an LLM prompt, so pairs loaded
    from persistent storage or user imports (which predate capture-time
    redaction) are also scrubbed before being sent to a possibly-external LLM.
    """
    if not isinstance(pairs, list):
        return pairs
    result = []
    for pair in pairs:
        if not isinstance(pair, dict):
            result.append(pair)
            continue
        clone = dict(pair)
        for field, redact in _REDACT_MAPPINGS:
            if field in clone:
                clone[field] = redact(clone[field])
        result.append(clone)
    return result


# Whitelisted protocol tokens that a device can be attributed with. Anything the
# LLM reports outside this set is dropped so arbitrary/untrusted strings cannot
# be persisted into the stable device header.
_SAFE_PROTOCOLS = {
    "http",
    "https",
    "tls",
    "tcp",
    "tcpip",
    "mqtt",
    "mqtt_connect",
    "modbus",
    "coap",
    "matter",
    "websocket",
    "ws",
    "zigbee",
    "zwave",
    "raw",
}

# Whitelisted ingress connection modes for the device header.
_SAFE_CONNECTION_MODES = {"auto", "tls", "http", "mqtt", "coap", "modbus"}


class PipelineMode(StrEnum):
    """Operation mode for the pipeline."""

    LEARNING = "learning"
    PRODUCTION = "production"
    HYBRID = "hybrid"
    PASSTHROUGH = "passthrough"


class MatchResult(StrEnum):
    """Result of a pattern match attempt."""

    LOCAL_HIT = "local_hit"  # Served from local database
    CLOUD_MISS = "cloud_miss"  # Forwarded to cloud, will learn
    ERROR = "error"  # Processing error


@dataclass
class ScoringInput:
    """Inputs required for calculating request pattern similarity scores."""

    method: str
    path: str
    headers: dict
    body: Any  # noqa: ANN401
    query_params: dict


@dataclass
class CorrelatedPair:
    """A correlated request/response pair ready for buffer and analysis."""

    pair_id: str
    device_id: str
    vendor: str
    protocol: str
    method: str
    path: str
    request_headers: dict
    request_body: Any
    request_query: dict
    response_status: int
    response_headers: dict
    response_body: Any
    latency_ms: float
    correlation_confidence: float
    timestamp: datetime
    # Observation enrichment (D2): transport/security/identity/kind carried from
    # the protocol server. Absent (None) for HTTP/TLS-passthrough pairs and for
    # legacy paths, so the buffer JSON stays backward-compatible.
    enrichment: dict | None = None


class ContextBuffer:
    """Sliding-window context buffer per device.

    A thin wrapper over a :class:`BufferStore`; the store decides whether the
    buffered pairs live on durable storage (``disk``) or in-process RAM
    (``memory``), selected at construction time via the current runtime
    backend.
    """

    def __init__(
        self,
        db_manager: DatabaseManager,
        max_size_bytes: int = 524288,
        store: BufferStore | None = None,
    ) -> None:
        self.db_manager = db_manager
        self.max_size_bytes = max_size_bytes
        self.store: BufferStore = store or create_buffer_store(db_manager)

    async def add_pair(self, device_id: str, pair: CorrelatedPair) -> int:
        """Add a pair and return the device's current buffered byte size."""
        pair_json = {
            "pair_id": pair.pair_id,
            "protocol": pair.protocol,
            "method": pair.method,
            "path": pair.path,
            "request_headers": pair.request_headers,
            "request_body": pair.request_body,
            "request_query": pair.request_query,
            "response_status": pair.response_status,
            "response_headers": pair.response_headers,
            "response_body": pair.response_body,
            "latency_ms": pair.latency_ms,
            "timestamp": pair.timestamp.isoformat(),
        }
        if pair.enrichment:
            pair_json["enrichment"] = pair.enrichment
        # Performance optimization: pair_json values (strings, numbers, dicts,
        # lists, booleans, None, and timestamp via .isoformat()) are natively
        # JSON serializable. Removing default=str avoids fallback handler lookup.
        # Since json.dumps defaults to ensure_ascii=True, all non-ASCII characters
        # are escaped as \uHHHH ASCII sequences, so string character count
        # len(serialized) exactly equals its UTF-8 byte size len(serialized.encode('utf-8'))
        # without allocating intermediate bytes objects (~1.20x speedup).
        serialized = json.dumps(pair_json)
        estimated_size = len(serialized)

        return await self.store.add_pair(device_id, pair_json, estimated_size)

    async def get_buffer_pairs(self, device_id: str) -> list[dict]:
        """Get all unflushed buffer entries for a device."""
        return await self.store.get_buffer_pairs(device_id)

    async def flush(self, device_id: str) -> int:
        """Mark all buffer entries for a device as flushed and clear buffer size."""
        return await self.store.flush(device_id)

    async def flush_selected(self, device_id: str, entry_ids: list[int]) -> int:
        """Mark only the specified buffer entries as flushed."""
        return await self.store.flush_selected(device_id, entry_ids)

    async def delete_entry(self, device_id: str, entry_id: int) -> bool:
        """Delete a single buffer entry."""
        return await self.store.delete_entry(device_id, entry_id)

    async def clear_cache(self, device_id: str):
        """Clear session cache for a device after flush."""
        await self.store.clear_cache(device_id)

    async def get_current_size(self, device_id: str) -> int:
        """Get current buffer size for a device."""
        return await self.store.get_current_size(device_id)

    async def prune(
        self,
        device_id: str,
        max_pairs_per_device: int = 10000,
        pair_ttl_hours: int = 168,
    ) -> int:
        """Delete persisted buffer rows that are too old or exceed the cap.

        Applies ``pair_ttl_hours`` to a device's flushed rows and caps the
        retained pairs per device at ``max_pairs_per_device`` so the on-disk
        training store cannot grow without bound. Returns rows deleted.
        """
        cutoff = datetime.now(UTC) - timedelta(hours=pair_ttl_hours)
        total = 0
        async with self.db_manager.device_session(device_id) as session:
            result = await session.execute(
                delete(LLMContextBuffer).where(
                    and_(
                        LLMContextBuffer.device_id == device_id,
                        LLMContextBuffer.flushed == True,  # noqa: E712
                        LLMContextBuffer.flushed_at != None,  # noqa: E711
                        LLMContextBuffer.flushed_at < cutoff,
                    )
                )
            )
            total += result.rowcount or 0

            retained = await session.execute(
                select(LLMContextBuffer.sequence)
                .where(
                    and_(
                        LLMContextBuffer.device_id == device_id,
                        LLMContextBuffer.flushed == True,  # noqa: E712
                    )
                )
                .order_by(LLMContextBuffer.sequence.desc())
                .offset(max_pairs_per_device)
                .limit(1)
            )
            oldest_kept = retained.scalar_one_or_none()
            if oldest_kept is not None:
                # ``oldest_kept`` is the (max_pairs+1)th-newest sequence; drop it
                # and anything older so only the newest ``max_pairs`` survive.
                result = await session.execute(
                    delete(LLMContextBuffer).where(
                        and_(
                            LLMContextBuffer.device_id == device_id,
                            LLMContextBuffer.flushed == True,  # noqa: E712
                            LLMContextBuffer.sequence <= oldest_kept,
                        )
                    )
                )
                total += result.rowcount or 0
        return total


class MatchRateTracker:
    """Tracks real-time match hit/miss rate per device."""

    def __init__(self, db_manager: DatabaseManager) -> None:
        self.db_manager = db_manager
        self._rolling_window = 1000
        # F-12: per-device locks serialize MatchStats updates.
        self._locks: dict[str, asyncio.Lock] = {}
        # Performance optimization: cache stats in memory and flush periodically
        self._stats_cache: dict[str, dict] = {}
        self._flush_threshold = 50

    async def _flush_cache(self, device_id: str):
        """Flush in-memory stats cache to the database."""
        cache = self._stats_cache.get(device_id)
        if not cache or cache["updates_pending"] == 0:
            return

        lock = self._locks.setdefault(device_id, asyncio.Lock())
        async with lock, self.db_manager.device_session(device_id) as session:
            result_obj = await session.execute(
                select(MatchStats).where(MatchStats.device_id == device_id)
            )
            stats = result_obj.scalar_one_or_none()

            if not stats:
                stats = MatchStats(
                    device_id=device_id,
                    total_requests=0,
                    local_hits=0,
                    cloud_misses=0,
                    errors=0,
                    match_rate_pct=0.0,
                    patterns_learned=0,
                    templates_created=0,
                    buffer_flushes=0,
                    current_buffer_size_bytes=0,
                )
                session.add(stats)
                await session.flush()

            stats.total_requests += cache["updates_pending"]
            stats.local_hits += cache["local_hits"]
            stats.cloud_misses += cache["cloud_misses"]
            stats.errors += cache["errors"]

            # Recalculate match rate
            total_attempted = stats.local_hits + stats.cloud_misses
            stats.match_rate_pct = round(
                (stats.local_hits / total_attempted * 100) if total_attempted > 0 else 0.0,
                2,
            )

            # Rolling window for recent results
            recent = list(stats.recent_results or [])
            recent.extend(cache["recent_results"])
            if len(recent) > self._rolling_window:
                recent = recent[-self._rolling_window :]
            stats.recent_results = recent

            # Reset cache
            cache["updates_pending"] = 0
            cache["local_hits"] = 0
            cache["cloud_misses"] = 0
            cache["errors"] = 0
            cache["recent_results"] = []

    async def record_result(self, device_id: str, match_result: MatchResult):
        """Record a match result and update stats."""
        cache = self._stats_cache.setdefault(
            device_id,
            {
                "updates_pending": 0,
                "local_hits": 0,
                "cloud_misses": 0,
                "errors": 0,
                "recent_results": [],
            },
        )

        cache["updates_pending"] += 1

        if match_result == MatchResult.LOCAL_HIT:
            cache["local_hits"] += 1
        elif match_result == MatchResult.CLOUD_MISS:
            cache["cloud_misses"] += 1
        else:
            cache["errors"] += 1

        cache["recent_results"].append(
            {
                "result": match_result.value,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

        if len(cache["recent_results"]) > self._rolling_window * 2:
            cache["recent_results"] = cache["recent_results"][-self._rolling_window :]

        if cache["updates_pending"] >= self._flush_threshold:
            await self._flush_cache(device_id)

    async def get_stats(self, device_id: str) -> dict:
        """Get current match stats for a device."""
        await self._flush_cache(device_id)

        async with self.db_manager.device_session(device_id) as session:
            result = await session.execute(
                select(MatchStats).where(MatchStats.device_id == device_id)
            )
            stats = result.scalar_one_or_none()
            if not stats:
                return {
                    "total_requests": 0,
                    "local_hits": 0,
                    "cloud_misses": 0,
                    "match_rate_pct": 0.0,
                    "errors": 0,
                    "patterns_learned": 0,
                    "buffer_flushes": 0,
                    "current_buffer_size_bytes": 0,
                    "recent_results": [],
                }
            return {
                "total_requests": stats.total_requests,
                "local_hits": stats.local_hits,
                "cloud_misses": stats.cloud_misses,
                "errors": stats.errors,
                "match_rate_pct": stats.match_rate_pct,
                "patterns_learned": stats.patterns_learned,
                "templates_created": stats.templates_created,
                "buffer_flushes": stats.buffer_flushes,
                "current_buffer_size_bytes": stats.current_buffer_size_bytes,
                "last_flush_at": stats.last_flush_at.isoformat() if stats.last_flush_at else None,
                "recent_results": (stats.recent_results or [])[-100:],  # Last 100
            }


def _llm_task_profile(llm_decipher: Any, task: str) -> str | None:  # noqa: ANN401
    """Return the profile bound to a purpose, or ``None`` when unbound.

    Defensive by design: an unbound purpose, a double without the resolver or
    any lookup error yields ``None`` so the caller falls back to the device
    profile — a profile lookup must never break a flush.
    """
    resolver = getattr(llm_decipher, "resolve_task_profile", None)
    if not callable(resolver):
        return None
    try:
        name = resolver(task)
    except Exception:  # noqa: BLE001 - never block on profile lookup
        logger.debug("Could not resolve the LLM profile for task %s", task)
        return None
    return name if isinstance(name, str) and name else None


async def _llm_validate_weak_match(
    llm_decipher: Any,  # noqa: ANN401 - the service, or a test double
    device: DeviceRegistry,
    pattern: Any,  # noqa: ANN401 - pattern object from the match engine
    request: dict,
) -> bool:
    """Let the LLM confirm a below-threshold pattern (``correlation`` purpose).

    Returns True only when a profile is bound to the purpose AND the model
    confirms that the learned pattern applies to the incoming request. Any
    failure returns False, so the request keeps its normal miss behaviour
    (cloud forward + learning): validation can never make matching worse than
    it was before the purpose was bound.
    """
    profile = _llm_task_profile(llm_decipher, "correlation")
    if not profile:
        return False
    try:
        verdict = await llm_decipher.validate_match(pattern, request, profile)
    except Exception:  # noqa: BLE001 - validation is best-effort
        logger.exception("LLM match validation failed for %s", getattr(device, "device_id", "?"))
        return False
    if not isinstance(verdict, dict):
        return False
    applicable = bool(verdict.get("applicable"))
    logger.info(
        "LLM %s the weak match for %s (confidence=%s, %s)",
        "confirmed" if applicable else "rejected",
        getattr(device, "device_id", "?"),
        verdict.get("confidence"),
        verdict.get("reason", ""),
    )
    return applicable


class LearningPipeline:
    """Orchestrates the learning flow: correlate buffer, read LLM, save patterns."""

    def __init__(  # noqa: PLR0913, PLR0917
        self,
        db_manager: DatabaseManager,
        llm_decipher: LLMDecipherService,
        buffer: ContextBuffer,
        tracker: MatchRateTracker,
        engine=None,
    ) -> None:
        self.db_manager = db_manager
        self.llm_decipher = llm_decipher
        self.buffer = buffer
        self.tracker = tracker
        self.engine = engine
        self._correlation_cache: dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))

    def _correlation_max_age(self) -> timedelta:
        """Max age for a pending request to be correlated.

        Mirrors the configured ``pair_ttl_hours`` so that un-correlated rows
        from a previous process run are never phantom-matched with an
        unrelated later response. Defaults to 7 days.
        """
        try:
            from core.config import get_config  # noqa: PLC0415

            cfg = get_config()
            ttl_hours = cfg.correlation.pair_ttl_hours
            return timedelta(hours=max(ttl_hours, 1))
        except Exception as e:  # pragma: no cover - config read is best-effort
            logger.debug("Correlation config unavailable, using 7-day TTL: %s", e)
            return timedelta(days=7)

    async def register_request(  # noqa: PLR0913, PLR0917
        self,
        device_id: str,
        vendor: str,
        protocol: str,
        method: str,
        path: str,
        headers: dict,
        body: Any,  # noqa: ANN401
        query_params: dict,
        enrichment: dict | None = None,
    ) -> str:
        """Register an outgoing request and generate a correlation key.

        ``enrichment`` carries transport/security/identity/kind emitted by
        protocol servers (D2); it is attached to the correlated pair so the
        buffer/observation layer can reconstruct the contextual metadata.

        Returns: correlation_key for later matching with response.
        """
        # Credential redaction before persistence/prompt (F-02): strip
        # Authorization/cookie/token headers, sensitive query params and secret
        # body fields from the capture. The live device-cloud forwarding path
        # uses the original request object directly; this copy is capture-only.
        headers = redact_headers(headers)
        query_params = redact_query(query_params)
        body = redact_body(body)

        corr_key = f"{device_id}:{method}:{path}:{uuid4().hex[:8]}"

        entry = {
            "correlation_key": corr_key,
            "device_id": device_id,
            "vendor": vendor,
            "protocol": protocol,
            "method": method,
            "path": path,
            "headers": headers,
            "body": body,
            "query_params": query_params,
            "enrichment": enrichment,
            "timestamp": datetime.now(UTC),
        }

        self._correlation_cache[device_id].append(entry)

        # Also store in DB for persistence
        async with self.db_manager.device_session(device_id) as session:
            cache_entry = SessionCache(
                device_id=device_id,
                correlation_key=corr_key,
                method=method,
                path=path,
                headers=headers,
                body=body,
                query_params=query_params,
                protocol=protocol,
            )
            session.add(cache_entry)

        return corr_key

    async def match_response(  # noqa: PLR0913, C901, PLR0917
        self,
        device_id: str,
        vendor: str,
        protocol: str,
        status_code: int,
        headers: dict,
        body: Any,  # noqa: ANN401
    ) -> CorrelatedPair | None:
        """Match an incoming response to a pending request. Returns correlated pair."""
        # Credential redaction (F-02): never persist raw response secrets.
        headers = redact_headers(headers)
        body = redact_body(body)

        # Try memory cache first
        pending = self._correlation_cache.get(device_id, deque())
        matched = None

        matched_key = None
        for entry in pending:
            if entry["protocol"] == protocol:
                matched = entry
                matched_key = entry.get("correlation_key")
                pending.remove(entry)
                break

        # When a request is matched from the in-memory cache, mark its
        # persisted SessionCache row as correlated too. Otherwise the original
        # row stays `correlated=False` permanently, and after a restart the DB
        # fallback re-selects these stale rows and re-correlates an already
        # consumed request with an unrelated later response (phantom match).
        if matched_key:
            async with self.db_manager.device_session(device_id) as session:
                await session.execute(
                    update(SessionCache)
                    .where(SessionCache.correlation_key == matched_key)
                    .values(
                        correlated=True,
                        correlated_at=datetime.now(UTC),
                        response_status=status_code,
                        response_headers=headers,
                        response_body=body,
                        response_latency_ms=0.0,
                    )
                )

        if not matched:
            # Try DB cache, guarding against stale rows: a pending request left
            # from a previous process (or an old, un-correlated row) must not be
            # wrongly paired with an unrelated later response (phantom match).
            cutoff = datetime.now(UTC) - self._correlation_max_age()
            async with self.db_manager.device_session(device_id) as session:
                result = await session.execute(
                    select(SessionCache)
                    .where(
                        and_(
                            SessionCache.device_id == device_id,
                            SessionCache.correlated == False,  # noqa: E712
                            SessionCache.created_at >= cutoff,
                        )
                    )
                    .order_by(SessionCache.created_at.desc())
                    .limit(10)
                )
                for cache_entry in result.scalars().all():
                    # `protocol` may be absent on SQLite databases created
                    # before this column existed. Default to "" so legacy rows
                    # still flow through t…8494 tokens truncated…stry, analysis: dict
    ) -> None:
        """Persist the stable per-device header on the first flush.

        Derives ``protocols`` and ``connection_mode`` from the LLM analysis
        (handles the structured PatternDB ``client.protocols`` shape, a flat
        ``protocols``/``connection_mode`` answer, or a full ``protocol_info``
        object produced in mode="auto"), then writes the header via
        :meth:`DatabaseManager.write_device_meta`. Only ever called when the
        header does not exist yet, so later flushes leave it stable.
        """
        # A structured protocol_info (first-flush mode="auto") is the richest
        # source: it carries transport/security/proprietary/identity/ports/
        # confidence that the flat protocols/connection_mode answer lacks.
        protocol_info = analysis.get("protocol_info")
        protocol_dict: dict = {}
        if isinstance(protocol_info, dict):
            try:
                protocol_dict = ProtocolInfo.model_validate(protocol_info).model_dump()
            except Exception:  # noqa: BLE001 - best-effort, header must not fail
                logger.debug("protocol_info failed validation, using flat answer")

        # protocols: prefer the structured singular protocol, then a flat list,
        # then the structured PatternDB client.protocols shape.
        protocols = analysis.get("protocols")
        if not protocols and protocol_dict.get("protocol"):
            protocols = [protocol_dict["protocol"]]
        if not protocols:
            client = analysis.get("client") or {}
            protocols = client.get("protocols") or []
        if isinstance(protocols, str):
            protocols = [protocols]
        # Whitelist the LLM-derived protocols so arbitrary strings are dropped.
        protocols = [p for p in (str(p).lower().strip() for p in protocols) if p in _SAFE_PROTOCOLS]

        connection_mode = analysis.get("connection_mode") or protocol_dict.get("handler")
        if not connection_mode:
            # Derive the ingress mode from the first reported protocol.
            connection_mode = protocols[0] if protocols else "auto"
        connection_mode = str(connection_mode).lower().strip()
        if connection_mode not in _SAFE_CONNECTION_MODES:
            connection_mode = "auto"

        meta = {
            "vendor": device.vendor or "unknown",
            "device_type": device.device_type or "unknown",
            "model": protocol_dict.get("identity", "") or "",
            "protocols": protocols or [""],
            "connection_mode": connection_mode,
            "source": "llm",
            "transport": protocol_dict.get("transport", "") or "",
            "security": protocol_dict.get("security", "") or "",
            "proprietary": bool(protocol_dict.get("proprietary", False)),
            "identity": protocol_dict.get("identity", "") or "",
            "ports": protocol_dict.get("ports") or [],
            "confidence": float(protocol_dict.get("confidence", 0.0) or 0.0),
        }
        try:
            stored = await self.db_manager.write_device_meta(device_id, meta)
            logger.info(
                "Wrote device_meta header for %s: connection_mode=%s protocols=%s",
                device_id,
                stored.get("connection_mode"),
                stored.get("protocols"),
            )
        except Exception as e:  # noqa: BLE001 - header is best-effort, never blocks learning
            logger.warning("Failed to write device_meta for %s: %s", device_id, e)


class LearningOrchestrator:
    """Orchestrates the full learning/production pipeline per device."""

    def __init__(self, db_manager: DatabaseManager) -> None:
        self.db_manager = db_manager
        self.llm_decipher = None
        self.buffer: dict[str, ContextBuffer] = {}
        self.engine = None
        self.tracker = None
        self.pipeline = None

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

    def _auto_load_patterns(self):
        """Scan for .ride-pattern.json files and load them into the engine."""
        search_dirs = [
            Path("patterns"),
            Path("data"),
            Path("data/patterns"),
            Path.cwd() / "patterns",
            Path.cwd() / "data",
        ]
        loaded = 0
        for search_dir in search_dirs:
            if not search_dir.exists():
                continue
            for pattern_file in search_dir.glob("*.ride-pattern.json"):
                device_id = self._device_id_from_pattern_file(pattern_file)
                try:
                    self.engine.load_pattern_file(device_id, str(pattern_file))
                    loaded += 1
                    logger.info("Auto-loaded patterns for %s from %s", device_id, pattern_file.name)
                except Exception as e:
                    logger.warning("Failed to load %s: %s", pattern_file.name, e)
        if loaded:
            logger.info("Pattern DB: auto-loaded %d pattern files", loaded)
        else:
            logger.debug("Pattern DB: no .ride-pattern.json files found to auto-load")

    @staticmethod
    def _device_id_from_pattern_file(pattern_file: Path) -> str:
        """Recover the real device_id a .ride-pattern.json was exported for.

        Files written by :meth:`_export_and_sync_patterns` are named after the
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
        """Learning mode: forward all to cloud, correlate, and build patterns."""
        corr_key = await self._register_for_learning(
            device, protocol, method, path, headers, body, query_params, enrichment
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

    async def handle_response(  # noqa: PLR0913, PLR0917
        self,
        device_id: str,
        vendor: str,
        protocol: str,
        status_code: int,
        headers: dict,
        body: Any,  # noqa: ANN401
    ) -> dict:
        """Process a response from the cloud (for learning mode)."""
        if not self.pipeline:
            return {"action": "ignored", "reason": "no_pipeline"}

        pair = await self.pipeline.match_response(
            device_id, vendor, protocol, status_code, headers, body
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
                stats["llm_base_url"] = device.llm_base_url
                stats["llm_model_id"] = device.llm_model_id
                stats["llm_profile_name"] = device.llm_profile_name
                stats["database_url"] = device.database_url
                stats["database_name"] = device.database_name
                # Bypass state (per-IP: from any profile IP, else config default).
                bypass_flags = [await self.db_manager.is_ips_bypassed(ip) for ip in ips]
                stats["bypass"] = any(bypass_flags)
        return stats


# Global instance
_orchestrator: LearningOrchestrator | None = None


def get_orchestrator() -> LearningOrchestrator:
    """Get or create global orchestrator instance."""
    global _orchestrator  # noqa: PLW0603
    if _orchestrator is None:
        _orchestrator = LearningOrchestrator(get_db_manager())
    return _orchestrator

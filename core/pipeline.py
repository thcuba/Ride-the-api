"""
Learning/Production Pipeline - Core engine that manages device learning and local response serving.
Handles: correlation, buffer management, LLM deciphering, pattern matching, and match rate tracking.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import and_, delete, select, update

from core.buffer import create_buffer_store
from core.database import (
    DatabaseManager,
    DeviceRegistry,
    FieldMapping,
    MatchStats,
    RequestPattern,
    ResponseTemplate,
    SessionCache,
    get_db_manager,
)
from core.llm_decipher import LLMDecipherService, LLMProfile
from core.pattern_db import decipher_ingest
from core.pattern_db.pattern_engine import PatternEngine
from core.pattern_db.schemas import PatternDB

if TYPE_CHECKING:
    from core.buffer.store import BufferStore

logger = logging.getLogger(__name__)


class PipelineMode(StrEnum):
    """Operation mode for the pipeline."""

    LEARNING = "learning"
    PRODUCTION = "production"
    HYBRID = "hybrid"


class MatchResult(StrEnum):
    """Result of a pattern match attempt."""

    LOCAL_HIT = "local_hit"  # Served from local database
    CLOUD_MISS = "cloud_miss"  # Forwarded to cloud, will learn
    ERROR = "error"  # Processing error


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

    async def add_pair(self, device_id: str, pair: CorrelatedPair) -> bool:
        """Add a correlated pair to the buffer. Returns True if buffer is full and needs flush."""
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
        serialized = json.dumps(pair_json, default=str)
        estimated_size = len(serialized.encode("utf-8"))

        size = await self.store.add_pair(device_id, pair_json, estimated_size)
        return size >= self.max_size_bytes

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


class PatternMatcher:
    """Matches incoming requests against learned patterns and builds local responses."""

    def __init__(self, db_manager: DatabaseManager) -> None:
        self.db_manager = db_manager

    async def find_best_match(  # noqa: PLR0913
        self,
        device_id: str,
        method: str,
        path: str,
        headers: dict,
        body: Any,  # noqa: ANN401
        query_params: dict,
    ) -> tuple:
        """Find the best matching request pattern for an incoming request.

        Returns: (pattern, response_template, similarity_score) or (None, None, 0)
        """
        async with self.db_manager.device_session(device_id) as session:
            result = await session.execute(select(RequestPattern))
            patterns = result.scalars().all()

        best_score = 0.0
        best_pattern = None
        best_template = None

        for pattern in patterns:
            score = self._calculate_similarity(pattern, method, path, headers, body, query_params)
            if score > best_score:
                best_score = score
                best_pattern = pattern

        if best_pattern and best_score >= 0.5:  # noqa: PLR2004
            async with self.db_manager.device_session(device_id) as session:
                result = await session.execute(
                    select(ResponseTemplate).where(
                        ResponseTemplate.pattern_id == best_pattern.pattern_id
                    )
                )
                best_template = result.scalar_one_or_none()

        return best_pattern, best_template, best_score

    def _calculate_similarity(  # noqa: PLR0913
        self,
        pattern: RequestPattern,
        method: str,
        path: str,
        headers: dict,
        body: Any,  # noqa: ANN401
        query_params: dict,
    ) -> float:
        """Calculate similarity between a request and a learned pattern (0.0 to 1.0)."""
        score = 0.0
        total_weight = 0.0

        # Method match (exact)
        total_weight += 30.0
        if pattern.method == method:
            score += 30.0

        # Path match (partial allowed)
        total_weight += 30.0
        path_score = self._path_similarity(pattern.path_pattern, path)
        score += 30.0 * path_score

        # Header key presence
        total_weight += 15.0
        if pattern.required_headers:
            present = sum(1 for h in pattern.required_headers if h in headers)
            score += 15.0 * (present / len(pattern.required_headers))

        # Query param key presence
        total_weight += 10.0
        if pattern.query_param_keys:
            present = sum(1 for q in pattern.query_param_keys if q in query_params)
            score += 10.0 * (present / len(pattern.query_param_keys))

        # Body structure match
        if body and pattern.body_schema:
            total_weight += 15.0
            body_score = self._body_similarity(pattern.body_schema, body)
            score += 15.0 * body_score
        elif not body and not pattern.body_schema:
            total_weight += 15.0
            score += 15.0

        if total_weight == 0:
            return 0.0
        return score / total_weight

    def _path_similarity(self, pattern: str, actual: str) -> float:
        """Compare path pattern (may contain {placeholders}) vs actual path."""
        p_parts = pattern.strip("/").split("/")
        a_parts = actual.strip("/").split("/")

        if len(p_parts) != len(a_parts):
            return 0.3 if abs(len(p_parts) - len(a_parts)) <= 1 else 0.0

        matches = 0
        for p, a in zip(p_parts, a_parts):
            if p.startswith("{") and p.endswith("}"):
                matches += 1  # Placeholder matches anything
            elif p == a:
                matches += 1

        return matches / len(p_parts) if p_parts else 1.0

    def _body_similarity(self, schema: dict, body: dict) -> float:
        """Check if body keys match schema keys."""
        if not schema or not body:
            return 0.5
        schema_keys = set(schema.keys())
        body_keys = set(body.keys())
        if not schema_keys:
            return 1.0
        intersection = schema_keys & body_keys
        return len(intersection) / len(schema_keys)

    async def build_local_response(
        self, device_id: str, template: ResponseTemplate, original_request: dict
    ) -> dict:
        """Build a local response from a template, filling in variables from the request."""
        # Start with template
        body = dict(template.body_template)

        # Apply field mappings
        async with self.db_manager.device_session(device_id) as session:
            result = await session.execute(select(FieldMapping))
            mappings = result.scalars().all()

        req = original_request.get("body", {})
        req_headers = original_request.get("headers", {})
        req_query = original_request.get("query_params", {})

        for mapping in mappings:
            if mapping.transform == "direct":
                val = self._resolve_field(mapping.request_field, req, req_headers, req_query)
                if val is not None:
                    self._set_nested(body, mapping.response_field, val)
            elif mapping.transform == "enum_map" and mapping.enum_values:
                val = self._resolve_field(mapping.request_field, req, req_headers, req_query)
                if val is not None and str(val) in mapping.enum_values:
                    self._set_nested(body, mapping.response_field, mapping.enum_values[str(val)])

        # Fill expected variables with placeholder values
        for var in template.expected_variables:
            if not self._get_nested(body, var):
                self._set_nested(body, var, 0)

        return {
            "status_code": template.status_code,
            "headers": dict(template.headers_template),
            "body": body,
        }

    def _resolve_field(self, field_path: str, body: dict, headers: dict, query: dict):
        """Resolve a field path like 'body.temp_set' or 'headers.X-Request-ID'."""
        parts = field_path.split(".", 1)
        source = parts[0]
        key = parts[1] if len(parts) > 1 else ""

        if source == "body" and isinstance(body, dict):
            return self._get_nested(body, key)
        if source == "headers":
            return headers.get(key)
        if source == "query":
            return query.get(key)
        return None

    def _get_nested(self, d: dict, path: str):
        parts = path.split(".")
        for p in parts:
            if isinstance(d, dict):
                d = d.get(p)
            else:
                return None
        return d

    def _set_nested(self, d: dict, path: str, value: Any) -> None:  # noqa: ANN401
        parts = path.split(".")
        for p in parts[:-1]:
            if p not in d:
                d[p] = {}
            d = d[p]
        d[parts[-1]] = value


class MatchRateTracker:
    """Tracks real-time match hit/miss rate per device."""

    def __init__(self, db_manager: DatabaseManager) -> None:
        self.db_manager = db_manager
        self._rolling_window = 1000

    async def record_result(self, device_id: str, match_result: MatchResult):
        """Record a match result and update stats."""
        async with self.db_manager.device_session(device_id) as session:
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

            stats.total_requests += 1

            if match_result == MatchResult.LOCAL_HIT:
                stats.local_hits += 1
            elif match_result == MatchResult.CLOUD_MISS:
                stats.cloud_misses += 1
            else:
                stats.errors += 1

            # Recalculate match rate
            total_attempted = stats.local_hits + stats.cloud_misses
            stats.match_rate_pct = round(
                (stats.local_hits / total_attempted * 100) if total_attempted > 0 else 0.0,
                2,
            )

            # Rolling window
            recent = list(stats.recent_results or [])
            recent.append(
                {
                    "result": match_result.value,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )
            if len(recent) > self._rolling_window:
                recent = recent[-self._rolling_window :]
            stats.recent_results = recent

    async def get_stats(self, device_id: str) -> dict:
        """Get current match stats for a device."""
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


class LearningPipeline:
    """Orchestrates the learning flow: correlate ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ buffer ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ LLM ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ save patterns."""

    def __init__(  # noqa: PLR0913
        self,
        db_manager: DatabaseManager,
        llm_decipher: LLMDecipherService,
        buffer: ContextBuffer,
        matcher: PatternMatcher,
        tracker: MatchRateTracker,
        engine=None,
    ) -> None:
        self.db_manager = db_manager
        self.llm_decipher = llm_decipher
        self.buffer = buffer
        self.matcher = matcher
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
            from core.config import get_config

            cfg = get_config()
            ttl_hours = cfg.correlation.pair_ttl_hours
            return timedelta(hours=max(ttl_hours, 1))
        except Exception:
            return timedelta(days=7)

    async def register_request(  # noqa: PLR0913
        self,
        device_id: str,
        vendor: str,
        protocol: str,
        method: str,
        path: str,
        headers: dict,
        body: Any,  # noqa: ANN401
        query_params: dict,
    ) -> str:
        """Register an outgoing request and generate a correlation key.

        Returns: correlation_key for later matching with response.
        """
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


    async def match_response(  # noqa: PLR0913
        self,
        device_id: str,
        vendor: str,
        protocol: str,
        status_code: int,
        headers: dict,
        body: Any,  # noqa: ANN401
    ) -> CorrelatedPair | None:
        """Match an incoming response to a pending request. Returns correlated pair."""
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
                    # still flow through the fallback instead of crashing.
                    if getattr(cache_entry, "protocol", None) == protocol:
                        matched = {
                            "correlation_key": cache_entry.correlation_key,
                            "device_id": device_id,
                            "vendor": vendor,
                            "protocol": protocol,
                            "method": cache_entry.method,
                            "path": cache_entry.path,
                            "headers": cache_entry.headers,
                            "body": cache_entry.body,
                            "query_params": cache_entry.query_params,
                            "timestamp": cache_entry.created_at,
                        }
                        cache_entry.correlated = True
                        cache_entry.correlated_at = datetime.now(UTC)
                        cache_entry.response_status = status_code
                        cache_entry.response_headers = headers
                        cache_entry.response_body = body
                        cache_entry.response_latency_ms = 0.0
                        break


        if not matched:
            return None

        # Normalize timestamp to timezone-aware (SQLite/aiosqlite may return a
        # naive datetime despite DateTime(timezone=True)); a naive value crashes
        # the aware - naive subtraction below.
        ts = matched.get("timestamp")
        if not isinstance(ts, datetime):
            ts = datetime.now(UTC)
        elif ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        latency_ms = (datetime.now(UTC) - ts).total_seconds() * 1000

        pair = CorrelatedPair(
            pair_id=str(uuid4()),
            device_id=device_id,
            vendor=vendor,
            protocol=protocol,
            method=matched["method"],
            path=matched["path"],
            request_headers=matched["headers"],
            request_body=matched["body"],
            request_query=matched.get("query_params", {}),
            response_status=status_code,
            response_headers=headers,
            response_body=body,
            latency_ms=latency_ms,
            correlation_confidence=0.8,
            timestamp=datetime.now(UTC),
        )

        # Store in the _correlation_cache for the pipeline
        async with self.db_manager.device_session(device_id) as session:
            cache_entry = SessionCache(
                device_id=device_id,
                correlation_key=f"response:{pair.pair_id}",
                method="RESPONSE",
                path=pair.path,
                headers=headers,
                body=body,
                correlated=True,
                correlated_at=datetime.now(UTC),
                response_status=status_code,
                response_headers=headers,
                response_body=body,
                response_latency_ms=latency_ms,
                in_buffer=True,
            )

        return pair

    async def process_learning_pair(
        self, device_id: str, pair: CorrelatedPair, _context_buffer_max: int
    ) -> bool:
        """Process a correlated pair in learning mode.

        Returns: True if buffer was flushed (LLM analysis triggered).
        """
        needs_flush = await self.buffer.add_pair(device_id, pair)

        if needs_flush:
            await self._flush_and_learn(device_id)
            return True
        return False

    async def _load_device(self, device_id: str) -> DeviceRegistry | None:
        """Load device from registry."""
        async with self.db_manager.core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            return result.scalar_one_or_none()

    async def _build_context(
        self,
        device_id: str,
        device: DeviceRegistry,
        pairs: list[dict],
        context_notes: str | None = None,
    ) -> dict:
        """Build context dict for LLM analysis."""
        return {
            "device_id": device_id,
            "vendor": device.vendor,
            "device_type": device.device_type,
            "pairs": [p["pair"] for p in pairs],
            "total_pairs": len(pairs),
            "total_size_bytes": sum(p["size"] for p in pairs),
            "context_notes": context_notes or device.llm_context_notes or "",
        }

    async def flush_and_learn(
        self, device_id: str, pair_ids: list[int] | None = None, context_notes: str | None = None
    ) -> dict:
        """Public flush: filter by optional pair_ids, inject context_notes, save patterns.

        Returns dict with success, pairs_count, patterns_count.
        """
        logger.info(f"Flushing context buffer for device {device_id}")

        all_pairs = await self.buffer.get_buffer_pairs(device_id)
        if not all_pairs:
            return {
                "success": False,
                "error": "No buffer entries to flush",
                "pairs_count": 0,
                "patterns_count": 0,
            }

        if pair_ids is not None:
            pairs = [p for p in all_pairs if p["id"] in pair_ids]
            if not pairs:
                return {
                    "success": False,
                    "error": "No matching buffer entries found",
                    "pairs_count": 0,
                    "patterns_count": 0,
                }
        else:
            pairs = all_pairs

        device = await self._load_device(device_id)
        if not device:
            return {
                "success": False,
                "error": "Device not found",
                "pairs_count": 0,
                "patterns_count": 0,
            }

        context = await self._build_context(device_id, device, pairs, context_notes)

        profile_name = device.llm_profile_name or "default"
        llm_analysis = await self._analyze_with_llm(
            context, profile_name, device.llm_base_url, device.llm_model_id
        )

        if not llm_analysis:
            return {
                "success": False,
                "error": "LLM analysis failed",
                "pairs_count": len(pairs),
                "patterns_count": 0,
            }

        await self._save_patterns(device_id, llm_analysis)
        await self._export_and_sync_patterns(device_id)

        if pair_ids is not None:
            await self.buffer.flush_selected(device_id, pair_ids)
        else:
            await self.buffer.flush(device_id)
        await self.buffer.clear_cache(device_id)

        patterns_count = len(llm_analysis.get("patterns", llm_analysis.get("decoded_patterns", [])))
        logger.info(
            f"Learning cycle complete for device {device_id}: {len(pairs)} pairs, "
            f"{patterns_count} patterns"
        )
        return {"success": True, "pairs_count": len(pairs), "patterns_count": patterns_count}

    async def preview_analysis(
        self, device_id: str, pair_ids: list[int] | None = None, context_notes: str | None = None
    ) -> dict:
        """Run LLM analysis WITHOUT saving patterns. Returns raw analysis for user review."""
        logger.info(f"Preview analysis for device {device_id}")

        all_pairs = await self.buffer.get_buffer_pairs(device_id)
        if not all_pairs:
            return {"success": False, "error": "No buffer entries to analyze", "pairs_count": 0}

        if pair_ids is not None:
            pairs = [p for p in all_pairs if p["id"] in pair_ids]
            if not pairs:
                return {
                    "success": False,
                    "error": "No matching buffer entries found",
                    "pairs_count": 0,
                }
        else:
            pairs = all_pairs

        device = await self._load_device(device_id)
        if not device:
            return {"success": False, "error": "Device not found", "pairs_count": 0}

        context = await self._build_context(device_id, device, pairs, context_notes)

        profile_name = device.llm_profile_name or "default"
        llm_analysis = await self._analyze_with_llm(
            context, profile_name, device.llm_base_url, device.llm_model_id
        )

        if not llm_analysis:
            return {"success": False, "error": "LLM analysis failed", "pairs_count": len(pairs)}

        return {
            "success": True,
            "pairs_count": len(pairs),
            "analysis": llm_analysis,
            "context": context,
        }

    async def _flush_and_learn(self, device_id: str):
        """Internal flush: all pairs, no context notes override."""
        await self.flush_and_learn(device_id)

    async def _export_and_sync_patterns(self, device_id: str) -> None:
        """Export learned patterns to .ride-pattern.json and sync with PatternEngine.

        Called after each learning cycle so the PatternEngine's in-memory cache
        has the latest patterns for subsequent production/hybrid requests.
        """
        if not self.engine:
            return
        try:
            ingester = decipher_ingest.DecipherIngest(self.db_manager)

            device_info = await self._load_device(device_id)
            vendor = device_info.vendor if device_info else "unknown"
            device_type = device_info.device_type if device_info else "unknown"
            pattern_db = await ingester.export_patterns(device_id, vendor, device_type)

            if not pattern_db.client.endpoints:
                logger.debug("No patterns to export for %s", device_id)
                return

            patterns_dir = Path("patterns")
            patterns_dir.mkdir(parents=True, exist_ok=True)

            filepath = patterns_dir / f"{device_id}.ride-pattern.json"
            self.engine.save_pattern_file(pattern_db, str(filepath))
            self.engine.apply_pattern_db(device_id, pattern_db)
            # Restore any previously persisted device state so the refreshed
            # pattern set starts from where the device left off.
            await self.engine.load_state(device_id)
            logger.info(
                "Exported and synced %d patterns for %s to %s",
                len(pattern_db.client.endpoints),
                device_id,
                filepath.name,
            )
        except Exception as e:
            logger.warning("Failed to export/sync patterns for %s: %s", device_id, e)

    async def _analyze_with_llm(
        self, context: dict, profile_name: str, base_url: str | None, model_id: str | None
    ) -> dict | None:
        """Send context to LLM for protocol analysis."""
        try:
            profile = self.llm_decipher.get_profile(profile_name)
            if not profile:
                logger.warning(f"LLM profile '{profile_name}' not found, skipping analysis")
                return None

            # Use the profile-rotation fallback chain when no per-device
            # overrides are set; the breaker excludes degraded providers and
            # the chain tries healthy alternatives before giving up.
            if not base_url and not model_id:
                result = await self.llm_decipher.call_profile_chain(
                    prompt=self._build_learning_prompt(profile, context),
                    preferred=profile_name,
                )
            else:
                # Override profile parameters if device-specific config provided
                effective_profile = LLMProfile(
                    name=profile_name,
                    base_url=base_url or profile.base_url,
                    api_key=profile.api_key,
                    model_id=model_id or profile.model_id,
                    prompt_template=profile.prompt_template,
                    enabled=profile.enabled,
                    timeout=profile.timeout,
                    max_retries=profile.max_retries,
                )
                prompt = self._build_learning_prompt(effective_profile, context)
                result = await self.llm_decipher.call_llm(effective_profile, prompt)

            if result["success"]:
                try:
                    content = result["content"]
                    if "```json" in content:
                        content = content.split("```json")[1].split("```")[0]
                    elif "```" in content:
                        content = content.split("```")[1].split("```")[0]
                    return json.loads(content)
                except json.JSONDecodeError:
                    logger.exception("Failed to parse LLM analysis result")
                    return None
            else:
                logger.error(f"LLM analysis failed: {result.get('error')}")
                return None
        except Exception:
            logger.exception("Error in LLM analysis")
            return None

    def _build_learning_prompt(self, profile, context: dict) -> str:
        """Build prompt for LLM batch analysis."""
        pairs_json = json.dumps(context["pairs"], indent=2, default=str)
        prompt = profile.prompt_template
        replacements = {
            "{vendor}": context.get("vendor", "unknown"),
            "{device_type}": context.get("device_type", "unknown"),
            "{pairs}": pairs_json,
            "{device_id}": context.get("device_id", "unknown"),
            "{context_notes}": context.get("context_notes", ""),
        }
        for placeholder, value in replacements.items():
            prompt = prompt.replace(placeholder, value)
        return prompt

    async def _save_patterns(self, device_id: str, analysis: dict):  # noqa: C901, PLR0912
        """Save LLM-decoded patterns to the device database.

        Uses DecipherIngest for structured output; falls back to the
        original dict-based save for simpler analyses.
        """
        # Try structured ingest (PatternDB format) first
        if "meta" in analysis and "client" in analysis and "server" in analysis:
            try:
                pattern_db = PatternDB.model_validate(analysis)
                ingester = decipher_ingest.DecipherIngest(self.db_manager)
                count = await ingester.import_patterns(device_id, pattern_db)
                logger.info("DecipherIngest saved %d patterns for %s", count, device_id)
            except Exception as e:
                logger.warning("Structured ingest failed, falling back: %s", e)
            else:
                return

        patterns = analysis.get("patterns", analysis.get("decoded_patterns", []))
        if not patterns:
            # Try to extract from top-level fields
            patterns = [analysis]

        async with self.db_manager.device_session(device_id) as session:
            stats = await session.execute(
                select(MatchStats).where(MatchStats.device_id == device_id)
            )
            stats_obj = stats.scalar_one_or_none()
            if stats_obj is None:
                stats_obj = MatchStats(device_id=device_id)
                session.add(stats_obj)

            for pattern_data in patterns:
                intent = pattern_data.get("intent", "unknown")
                method = pattern_data.get("method", pattern_data.get("request_method", "POST"))
                path = pattern_data.get("path", pattern_data.get("request_path", "/unknown"))

                # Create or update request pattern
                # Use a deterministic hash so pattern IDs are stable across processes.
                path_digest = hashlib.md5(path.encode("utf-8")).hexdigest()[:8]
                pattern_id = f"{device_id}_{intent}_{path_digest}"
                existing = await session.execute(
                    select(RequestPattern).where(RequestPattern.pattern_id == pattern_id)
                )
                pattern = existing.scalar_one_or_none()

                if not pattern:
                    pattern = RequestPattern(
                        pattern_id=pattern_id,
                        method=method,
                        path_pattern=path,
                        protocol=pattern_data.get("protocol", "http"),
                        required_headers=pattern_data.get("required_headers", []),
                        body_schema=pattern_data.get("body_schema", {}),
                        query_param_keys=pattern_data.get("query_param_keys", []),
                        intent=intent,
                        confidence=pattern_data.get("confidence", 0.5),
                    )
                    session.add(pattern)
                    stats_obj.patterns_learned += 1

                # Create response template
                resp_data = pattern_data.get("response", pattern_data.get("response_template", {}))
                if resp_data:
                    template_id = f"tpl_{pattern_id}"
                    existing_tpl = await session.execute(
                        select(ResponseTemplate).where(ResponseTemplate.template_id == template_id)
                    )
                    template = existing_tpl.scalar_one_or_none()
                    if not template:
                        template = ResponseTemplate(
                            template_id=template_id,
                            pattern_id=pattern_id,
                            status_code=resp_data.get("status_code", 200),
                            headers_template=resp_data.get("headers", {}),
                            body_template=resp_data.get("body", {}),
                            field_mappings=resp_data.get("field_mappings", {}),
                            expected_variables=resp_data.get("expected_variables", []),
                            confidence=resp_data.get("confidence", 0.5),
                        )
                        session.add(template)
                        stats_obj.templates_created += 1

                # Save field mappings
                mappings = pattern_data.get("field_mappings", {})
                if isinstance(mappings, dict):
                    for req_field, resp_info in mappings.items():
                        if isinstance(resp_info, str):
                            resp_info = {"response_field": resp_info, "type": "string"}  # noqa: PLW2901
                        mapping_id = f"map_{device_id}_{intent}_{req_field.replace('.', '_')}"
                        existing_map = await session.execute(
                            select(FieldMapping).where(FieldMapping.mapping_id == mapping_id)
                        )
                        if not existing_map.scalar_one_or_none():
                            fmap = FieldMapping(
                                mapping_id=mapping_id,
                                request_field=req_field,
                                request_type=resp_info.get("request_type", "string"),
                                response_field=resp_info.get("response_field", req_field),
                                response_type=resp_info.get("type", "string"),
                                transform=resp_info.get("transform", "direct"),
                                enum_values=resp_info.get("enum_values"),
                                intent=intent,
                                confidence=resp_info.get("confidence", 0.5),
                            )
                            session.add(fmap)


class LearningOrchestrator:
    """Orchestrates the full learning/production pipeline per device."""

    def __init__(self, db_manager: DatabaseManager) -> None:
        self.db_manager = db_manager
        self.llm_decipher = None
        self.buffer: dict[str, ContextBuffer] = {}
        self.matcher = None
        self.engine = None
        self.tracker = None
        self.pipeline = None

    def initialize(self, llm_decipher: LLMDecipherService):
        """Initialize the orchestrator with required services.

        Sets up the PatternEngine, PatternMatcher, and MatchRateTracker.
        Auto-loads any .ride-pattern.json files found in the patterns/
        or data/ directories so pre-seeded or exported patterns are
        available immediately for production/hybrid serving.
        """
        self.llm_decipher = llm_decipher
        self.engine = PatternEngine(self.db_manager)
        self._auto_load_patterns()
        self.matcher = PatternMatcher(self.db_manager)
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
                device_id = pattern_file.stem
                if device_id.endswith(".ride-pattern"):
                    device_id = device_id.replace(".ride-pattern", "")
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

    async def prune_stores(self) -> int:
        """Apply TTL/cap pruning to the persisted training store for every device.

        Reads ``store_pairs`` / ``max_pairs_per_device`` / ``pair_ttl_hours``
        from config. When pair storage is disabled the on-disk history for each
        device is pruned to its cap so the store cannot grow without bound.
        """
        try:
            from core.config import get_config

            cfg = get_config()
            corr = cfg.correlation
        except Exception:
            logger.debug("Prune: config unavailable; using defaults")
            from types import SimpleNamespace

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
                deleted = await buffer.prune(dev_id, max_pairs_per_device=max_pairs, pair_ttl_hours=ttl_hours)
                total += deleted
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Prune failed for device %s: %s", dev_id, e)
        if total:
            logger.info("Pruned %d stale correlation rows across %d devices", total, len(devices))
        return total

    async def handle_request(  # noqa: PLR0913
        self,
        device_id: str,
        vendor: str,  # noqa: ARG002
        protocol: str,  # noqa: ARG002
        method: str,
        path: str,
        headers: dict,
        body: Any,  # noqa: ANN401
        query_params: dict,
    ) -> dict:
        """Main entry point: handle an incoming request. Returns response info."""
        # Get device mode
        async with self.db_manager.core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            if not device:
                return {"action": "forward", "reason": "device_not_found"}

        if device.mode == PipelineMode.PRODUCTION.value:
            return await self._handle_production(device, method, path, headers, body, query_params)
        if device.mode == PipelineMode.HYBRID.value:
            return await self._handle_hybrid(device, method, path, headers, body, query_params)
        return await self._handle_learning(device, method, path, headers, body, query_params)

    async def _handle_production(  # noqa: PLR0913
        self,
        device: DeviceRegistry,
        method: str,
        path: str,
        headers: dict,
        body: Any,  # noqa: ANN401
        query_params: dict,
    ) -> dict:
        """Production mode: try local match, fall back to cloud forwarding + learning.

        When ``production_no_fallback`` is enabled on the device, unmatched
        requests return a conclusive ``no_fallback`` action instead of
        forwarding to the cloud.
        """
        pattern, template, score = await self.engine.find_best_match(
            device.device_id, method, path, headers, body, query_params
        )

        if pattern and template and score >= device.match_threshold:
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
            device, method, path, headers, body, query_params
        )
        return {
            "action": "forward",
            "correlation_key": corr_key,
            "match_score": score,
            "reason": "below_threshold" if pattern else "no_pattern",
        }

    async def _handle_hybrid(  # noqa: PLR0913
        self,
        device: DeviceRegistry,
        method: str,
        path: str,
        headers: dict,
        body: Any,  # noqa: ANN401
        query_params: dict,
    ) -> dict:
        """Hybrid mode: try local match first; if confident serve locally, otherwise
        forward to cloud + learn."""
        pattern, template, score = await self.engine.find_best_match(
            device.device_id, method, path, headers, body, query_params
        )

        if pattern and template and score >= device.match_threshold:
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
            device, method, path, headers, body, query_params
        )
        await self.tracker.record_result(device.device_id, MatchResult.CLOUD_MISS)
        return {
            "action": "forward",
            "correlation_key": corr_key,
            "match_score": score,
            "reason": "below_threshold" if pattern else "no_pattern",
            "mode": "hybrid",
        }

    async def _handle_learning(  # noqa: PLR0913
        self,
        device: DeviceRegistry,
        method: str,
        path: str,
        headers: dict,
        body: Any,  # noqa: ANN401
        query_params: dict,
    ) -> dict:
        """Learning mode: forward all to cloud, correlate, and build patterns."""
        corr_key = await self._register_for_learning(
            device, method, path, headers, body, query_params
        )
        return {
            "action": "forward",
            "correlation_key": corr_key,
            "mode": "learning",
        }

    async def _register_for_learning(  # noqa: PLR0913
        self,
        device: DeviceRegistry,
        method: str,
        path: str,
        headers: dict,
        body: Any,  # noqa: ANN401
        query_params: dict,
    ) -> str:
        """Register a request for learning capture."""
        if not self.pipeline:
            buffer = await self.ensure_buffer(device.device_id, device.context_buffer_size)
            self.pipeline = LearningPipeline(
                self.db_manager,
                self.llm_decipher,
                buffer,
                self.matcher,
                self.tracker,
                self.engine,
            )
        return await self.pipeline.register_request(
            device.device_id,
            device.vendor,
            "http",
            method,
            path,
            headers,
            body,
            query_params,
        )

    async def handle_response(  # noqa: PLR0913
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
        # Production/hybrid mode: learn from the miss. The CLOUD_MISS is NOT
        # recorded again here ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â it was already counted once when the request
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
        return stats


# Global instance
_orchestrator: LearningOrchestrator | None = None


def get_orchestrator() -> LearningOrchestrator:
    """Get or create global orchestrator instance."""
    global _orchestrator  # noqa: PLW0603
    if _orchestrator is None:
        _orchestrator = LearningOrchestrator(get_db_manager())
    return _orchestrator

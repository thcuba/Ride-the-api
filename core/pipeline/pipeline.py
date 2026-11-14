"""
Learning Pipeline - Core engine that manages device learning and pattern matching.
Handles: correlation, buffer management, LLM deciphering, pattern matching, and match rate tracking.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import and_, delete, select, update

from core.atomic_io import _SAFE_FILENAME_RE
from core.database import (
    DatabaseManager,
    DeviceRegistry,
    FieldMapping,
    MatchStats,
    RequestPattern,
    ResponseTemplate,
    SessionCache,
)
from core.llm_decipher import LLMDecipherService, LLMProfile, _parse_llm_json
from core.pattern_db import decipher_ingest
from core.pattern_db.schemas import (
    DeviceModel,
    PatternDB,
    ProtocolInfo,
    merge_pattern_dbs,
    normalize_connection_mode,
    normalize_protocol,
)
from core.redaction import redact_body, redact_headers, redact_query

if TYPE_CHECKING:
    from collections.abc import Coroutine

logger = logging.getLogger(__name__)

# After a failed automatic flush, wait this long before trying again.
_FLUSH_RETRY_COOLDOWN_S = 60


# Re-exported. These stay below the module-level constants on purpose: importing
# core.buffer.context_buffer at the top makes core.buffer and this module import each
# other while both are still initialising, which fails with a partially initialised
# module error.
from core.buffer.context_buffer import ContextBuffer, CorrelatedPair  # noqa: E402
from core.match_tracker import MatchRateTracker, MatchResult  # noqa: E402


def _monotonic() -> float:
    """Return monotonic time, respecting monkeypatched time on the parent package."""
    parent = sys.modules.get("core.pipeline")
    parent_time = getattr(parent, "time", None) if parent is not None else None
    if parent_time is not None and parent_time is not time:
        return parent_time.monotonic()
    return time.monotonic()

class FlushCoordinator:
    """Per-device flush bookkeeping, shared by every pipeline an orchestrator builds.

    One flush runs per device at a time; the automatic flush is a background
    task (never on a device's response path); after a failure, automatic
    flushes wait out a cooldown instead of re-sending the whole buffer on every
    response. The orchestrator rebuilds its pipeline when the buffer backend
    changes, so this state lives outside the pipeline: a rebuild must neither
    allow a second concurrent flush nor lose a running one at shutdown.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._retry_at: dict[str, float] = {}

    def lock(self, device_id: str) -> asyncio.Lock:
        lock = self._locks.get(device_id)
        if lock is None:
            lock = self._locks[device_id] = asyncio.Lock()
        return lock

    def is_running(self, device_id: str) -> bool:
        task = self._tasks.get(device_id)
        return task is not None and not task.done()

    def cooling_down(self, device_id: str) -> bool:
        return _monotonic() < self._retry_at.get(device_id, 0.0)

    def start(self, device_id: str, flush: Coroutine[Any, Any, None]) -> None:
        self._tasks[device_id] = asyncio.create_task(flush, name=f"flush:{device_id}")

    def record(self, device_id: str, *, failed: bool) -> None:
        if failed:
            self._retry_at[device_id] = _monotonic() + _FLUSH_RETRY_COOLDOWN_S
        else:
            self._retry_at.pop(device_id, None)

    async def wait(self, device_id: str) -> None:
        task = self._tasks.get(device_id)
        if task is not None:
            await task

    async def cancel_all(self) -> None:
        """Cancel the background flushes still running (shutdown).

        Running flushes will raise CancelledError when they yield to the event loop.
        """
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()


def _older_than(timestamp: Any, cutoff: datetime) -> bool:  # noqa: ANN401
    """True when ``timestamp`` is a datetime earlier than ``cutoff``.

    Naive values (SQLite can return them) are read as UTC. A missing or
    non-datetime value is never considered stale.
    """
    if not isinstance(timestamp, datetime):
        return False
    aware = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=UTC)
    return aware < cutoff


def _as_key_list(value: Any) -> list:  # noqa: ANN401
    """Normalise an LLM-supplied header/query spec into a list of key names.

    Accepts a list of names, a ``{name: ...}`` mapping (keys are taken) or
    anything else (treated as empty).
    """
    if isinstance(value, dict):
        return list(value.keys())
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _pattern_headers(pattern_data: dict) -> list:
    """Required-header keys from an LLM pattern, tolerating key-name drift.

    The shipped prompt asks the model for ``request_headers``; earlier code
    only read ``required_headers``, so learned patterns always had an empty
    header list. Accept either name.
    """
    return _as_key_list(pattern_data.get("required_headers") or pattern_data.get("request_headers"))


def _pattern_query_keys(pattern_data: dict) -> list:
    """Query-parameter keys from an LLM pattern, tolerating key-name drift."""
    return _as_key_list(
        pattern_data.get("query_param_keys")
        or pattern_data.get("query_params")
        or pattern_data.get("query_keys")
    )


def _listed_patterns(analysis: dict) -> list[dict]:
    """The pattern list of an LLM analysis, tolerating shape drift.

    A single pattern object instead of a list counts as one pattern; entries
    that are not objects (and a non-list value) are dropped.
    """
    patterns = analysis.get("patterns", analysis.get("decoded_patterns", []))
    if isinstance(patterns, dict):
        return [patterns]
    if isinstance(patterns, list):
        return [pattern for pattern in patterns if isinstance(pattern, dict)]
    return []


def _confidence(value: Any, default: float = 0.5) -> float:  # noqa: ANN401
    """An LLM-supplied confidence as a float in [0, 1].

    Models send ``"high"``, ``"0.9"`` or ``null`` as often as a number; the
    raw value used to be stored as-is in a float column.
    """
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return min(max(number, 0.0), 1.0)


_MIN_STATUS, _MAX_STATUS = 100, 599


def _status_code(value: Any, default: int = 200) -> int:  # noqa: ANN401
    """An LLM-supplied HTTP status as an int in 100-599, else ``default``.

    ``"OK"`` or ``"200 OK"`` used to be stored verbatim and broke the local
    response built from the template.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, str):
        value = value.strip().split(" ", 1)[0]
    try:
        code = int(value)
    except (TypeError, ValueError):
        return default
    return code if _MIN_STATUS <= code <= _MAX_STATUS else default


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


class PipelineMode(StrEnum):
    """Operation mode for the pipeline."""

    LEARNING = "learning"
    PRODUCTION = "production"
    HYBRID = "hybrid"
    PASSTHROUGH = "passthrough"


@dataclass
class ScoringInput:
    """Inputs required for calculating request pattern similarity scores."""

    method: str
    path: str
    headers: dict
    body: Any  # noqa: ANN401
    query_params: dict


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
        *,
        flushes: FlushCoordinator | None = None,
    ) -> None:
        self.db_manager = db_manager
        self.llm_decipher = llm_decipher
        self.buffer = buffer
        self.tracker = tracker
        self.engine = engine
        self._correlation_cache: dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
        self.flushes = flushes or FlushCoordinator()

    def _schedule_flush(self, device_id: str) -> bool:
        """Start a background flush for ``device_id``; True if one is running.

        Single-flight: while a flush runs, later triggers do not start another
        (previously every response arriving during the LLM call ran a full
        flush of its own, inline). After a failed flush, triggers wait out a
        cooldown instead of re-sending the whole buffer on every response.
        """
        if self.flushes.is_running(device_id):
            return True
        if self.flushes.cooling_down(device_id):
            return False
        self.flushes.start(device_id, self._background_flush(device_id))
        return True

    async def _background_flush(self, device_id: str) -> None:
        try:
            result = await self.flush_and_learn(device_id)
            # pairs_count 0: nothing reached the LLM (empty buffer, unknown
            # device), so there is nothing to back off from.
            failed = not result.get("success") and bool(result.get("pairs_count"))
            reason = result.get("error", "error")
        except Exception:  # noqa: BLE001 - a background task must log, not die silently
            logger.exception("Background flush failed for %s", device_id)
            failed, reason = True, "exception"
        self.flushes.record(device_id, failed=failed)
        if failed:
            logger.warning(
                "Flush for %s failed (%s); next automatic attempt in %ds",
                device_id,
                reason,
                _FLUSH_RETRY_COOLDOWN_S,
            )

    async def wait_for_flush(self, device_id: str) -> None:
        """Wait for the background flush of ``device_id`` to finish, if any."""
        await self.flushes.wait(device_id)

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

    async def discard_request(
        self, device_id: str, correlation_key: str, *, count_as_error: bool = True
    ) -> bool:
        """Remove a captured request that will never receive a correlated response.

        Failed forwards must not remain eligible for a later response: the
        correlation fallback matches by device and protocol, so an orphan can
        otherwise contaminate a future learning pair. ``count_as_error=False``
        is for a request that was handed off successfully but whose response
        never comes back here (e.g. nginx forwards it), which is not a failure.
        """
        pending = self._correlation_cache.get(device_id)
        removed_from_memory = False
        if pending is not None:
            for entry in tuple(pending):
                if entry.get("correlation_key") == correlation_key:
                    pending.remove(entry)
                    removed_from_memory = True
                    break
        if removed_from_memory and count_as_error:
            # A forward that never came back is an error, and it has to be visible in the
            # counters: otherwise a device whose upstream is failing looks idle.
            await self.tracker.record_result(device_id, MatchResult.ERROR)

        async with self.db_manager.device_session(device_id) as session:
            result = await session.execute(
                delete(SessionCache).where(
                    and_(
                        SessionCache.device_id == device_id,
                        SessionCache.correlation_key == correlation_key,
                        SessionCache.correlated == False,  # noqa: E712
                    )
                )
            )

        return removed_from_memory or bool(result.rowcount)

    def _pop_pending(
        self,
        device_id: str,
        protocol: str,
        correlation_key: str | None,
        cutoff: datetime,
    ) -> dict | None:
        """Remove and return the in-memory pending request a response answers.

        With a key, only that exact request qualifies. Without one, the oldest
        request of the same protocol that is still inside the correlation window
        is used — never one older than ``cutoff`` (a lost orphan).
        """
        pending = self._correlation_cache.get(device_id)
        if not pending:
            return None
        for entry in pending:
            if correlation_key is not None:
                if entry.get("correlation_key") != correlation_key:
                    continue
            elif entry["protocol"] != protocol or _older_than(entry.get("timestamp"), cutoff):
                continue
            pending.remove(entry)
            return entry
        return None

    async def _claim_keyed_row(  # noqa: PLR0913, PLR0917
        self,
        device_id: str,
        vendor: str,
        protocol: str,
        correlation_key: str,
        status_code: int,
        headers: dict,
        body: Any,  # noqa: ANN401
    ) -> dict | None:
        """Claim exactly the persisted request with ``correlation_key`` (atomic).

        A key whose row is gone (already consumed, or never persisted) yields
        ``None`` rather than falling back to an unrelated request.
        """
        async with self.db_manager.device_session(device_id) as session:
            claim = await session.execute(
                update(SessionCache)
                .where(
                    and_(
                        SessionCache.correlation_key == correlation_key,
                        SessionCache.correlated == False,  # noqa: E712
                    )
                )
                .values(
                    correlated=True,
                    correlated_at=datetime.now(UTC),
                    response_status=status_code,
                    response_headers=headers,
                    response_body=body,
                    response_latency_ms=0.0,
                )
            )
            if not claim.rowcount:
                return None
            row = (
                await session.execute(
                    select(SessionCache).where(SessionCache.correlation_key == correlation_key)
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "correlation_key": row.correlation_key,
            "device_id": device_id,
            "vendor": vendor,
            "protocol": protocol,
            "method": row.method,
            "path": row.path,
            "headers": row.headers,
            "body": row.body,
            "query_params": row.query_params,
            "timestamp": row.created_at,
        }

    async def match_response(  # noqa: PLR0913, C901, PLR0917
        self,
        device_id: str,
        vendor: str,
        protocol: str,
        status_code: int,
        headers: dict,
        body: Any,  # noqa: ANN401
        correlation_key: str | None = None,
    ) -> CorrelatedPair | None:
        """Match an incoming response to a pending request. Returns correlated pair.

        When ``correlation_key`` is given (the caller owns both sides of the
        exchange, e.g. the TLS listener), the response is matched to *exactly*
        that request and to nothing else: if it is no longer pending, no pair is
        produced. Without a key, the oldest pending request of the same protocol
        that is still within the correlation window is used — concurrent requests
        from one device, or a stale orphan, can otherwise be mis-paired.
        """
        # Credential redaction (F-02): never persist raw response secrets.
        headers = redact_headers(headers)
        body = redact_body(body)
        cutoff = datetime.now(UTC) - self._correlation_max_age()

        # Try memory cache first
        matched = self._pop_pending(device_id, protocol, correlation_key, cutoff)
        matched_key = matched.get("correlation_key") if matched else None

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

        if not matched and correlation_key is not None:
            matched = await self._claim_keyed_row(
                device_id, vendor, protocol, correlation_key, status_code, headers, body
            )

        if not matched and correlation_key is None:
            # Try DB cache, guarding against stale rows: a pending request left
            # from a previous process (or an old, un-correlated row) must not be
            # wrongly paired with an unrelated later response (phantom match).
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
                        # F-05: claim the row atomically so two concurrent
                        # responses cannot both consume the same pending
                        # request (select-then-mutate was racy).
                        claim = await session.execute(
                            update(SessionCache)
                            .where(
                                and_(
                                    SessionCache.correlation_key == cache_entry.correlation_key,
                                    SessionCache.correlated == False,  # noqa: E712
                                )
                            )
                            .values(
                                correlated=True,
                                correlated_at=datetime.now(UTC),
                                response_status=status_code,
                                response_headers=headers,
                                response_body=body,
                                response_latency_ms=0.0,
                            )
                        )
                        if claim.rowcount == 0:
                            # Another coroutine already claimed this row.
                            continue
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
            enrichment=matched.get("enrichment"),
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
        self, device_id: str, pair: CorrelatedPair, context_buffer_max: int
    ) -> bool:
        """Process a correlated pair in learning mode.

        The flush threshold comes from the *current* device's
        ``context_buffer_size`` (not the shared store's fixed max), so each
        device honours its own configured limit.

        Returns: True if a flush was started (or is already running). The flush
        itself runs in the background -- the LLM call never delays the device's
        response; use :meth:`wait_for_flush` to wait for it.
        """
        size = await self.buffer.add_pair(device_id, pair)
        if size >= context_buffer_max:
            return self._schedule_flush(device_id)
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
        ctx = {
            "device_id": device_id,
            "vendor": device.vendor,
            "device_type": device.device_type,
            "pairs": [p["pair"] for p in pairs],
            "total_pairs": len(pairs),
            "total_size_bytes": sum(p["size"] for p in pairs),
            "context_notes": context_notes or device.llm_context_notes or "",
        }
        # Ground subsequent flushes with the current learned model so the LLM
        # can produce a *delta* (C1) instead of re-deriving everything from
        # scratch. Only present once a device header exists (i.e. not the
        # very first flush). Best-effort: if the model cannot be exported the
        # flush still proceeds without grounding.
        if await self.db_manager.read_device_meta(device_id) is not None:
            try:
                ingester = decipher_ingest.DecipherIngest(self.db_manager)
                current = await ingester.export_device_model(device_id)
                ctx["current_model"] = current.model_dump(mode="json", by_alias=False)
            except Exception:  # noqa: BLE001 - grounding must never block learning
                logger.debug(
                    "Could not export current DeviceModel for grounding of %s",
                    device_id,
                )
        return ctx

    async def flush_and_learn(
        self, device_id: str, pair_ids: list[int] | None = None, context_notes: str | None = None
    ) -> dict:
        """Public flush: filter by optional pair_ids, inject context_notes, save patterns.

        One flush runs per device at a time: a manual flush waits for a
        background one to finish, so the same pairs are never sent twice.

        Returns dict with success, pairs_count, patterns_count.
        """
        async with self.flushes.lock(device_id):
            return await self._run_flush(device_id, pair_ids, context_notes)

    async def _run_flush(  # noqa: C901
        self, device_id: str, pair_ids: list[int] | None, context_notes: str | None
    ) -> dict:
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

        # First flush: the device header is not written yet (protocol still
        # auto). Ask the LLM to also report the protocol(s) so we can persist
        # the stable device_meta header; later flushes keep it untouched.
        is_first_flush = await self.db_manager.read_device_meta(device_id) is None
        if is_first_flush:
            context["first_flush"] = True

        # A purpose-bound profile wins over the device profile and the service
        # default. The grounded pass (a learned model already exists, so the LLM
        # works on a delta) is its own purpose: an operator can spend a stronger
        # model on the update and a cheaper one on the first pass.
        task = "learning_context" if context.get("current_model") else "flush"
        profile_name = (
            _llm_task_profile(self.llm_decipher, task) or device.llm_profile_name or "default"
        )
        llm_analysis = await self._analyze_with_llm(
            context, profile_name, device.llm_base_url, device.llm_model_id
        )
        # A model that ignores the requested object shape may answer with a
        # bare list of patterns (or a scalar): accept the list, reject the rest,
        # instead of crashing in _save_patterns on ``analysis.get``.
        if isinstance(llm_analysis, list):
            llm_analysis = {"patterns": llm_analysis}
        elif not isinstance(llm_analysis, dict):
            llm_analysis = None

        if not llm_analysis:
            return {
                "success": False,
                "error": "LLM analysis failed",
                "pairs_count": len(pairs),
                "patterns_count": 0,
            }

        await self._save_patterns(device_id, llm_analysis)
        if is_first_flush:
            await self._persist_device_meta(device_id, device, llm_analysis)
        await self.export_and_sync_patterns(device_id)

        # Mark exactly the pairs this cycle analysed. Pairs that arrived while
        # the LLM was working were not in the snapshot: marking "everything
        # unflushed" (buffer.flush) dropped them without ever analysing them.
        analysed_ids = pair_ids if pair_ids is not None else [p["id"] for p in pairs]
        await self.buffer.flush_selected(device_id, analysed_ids)
        await self.buffer.clear_cache(device_id)

        patterns_count = len(_listed_patterns(llm_analysis))
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

        # A purpose-bound profile wins over the device profile and the service
        # default. The grounded pass (a learned model already exists, so the LLM
        # works on a delta) is its own purpose: an operator can spend a stronger
        # model on the update and a cheaper one on the first pass.
        task = "learning_context" if context.get("current_model") else "flush"
        profile_name = (
            _llm_task_profile(self.llm_decipher, task) or device.llm_profile_name or "default"
        )
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

    async def export_and_sync_patterns(self, device_id: str) -> None:  # noqa: C901
        """Rebuild what the engine serves for a device from its DB, and export it.

        Called after each learning cycle and after any API change to the
        device's patterns, so the engine's in-memory set (used exclusively
        when present) never serves a deleted or outdated pattern. Patterns a
        user supplied as a file are not in the DB: they are merged back in.
        """
        if not self.engine:
            return
        try:
            ingester = decipher_ingest.DecipherIngest(self.db_manager)

            device_info = await self._load_device(device_id)
            vendor = device_info.vendor if device_info else "unknown"
            device_type = device_info.device_type if device_info else "unknown"
            applied = self.engine.cached_patterns(device_id)
            pattern_db = await ingester.export_patterns(
                device_id, vendor, device_type, applied=applied
            )
            seed = self.engine.seed_patterns(device_id)
            served = merge_pattern_dbs(pattern_db, seed) if seed else pattern_db

            if not served.client.endpoints:
                # Nothing left (e.g. the last pattern was deleted): stop serving
                # the stale in-memory set; matching falls back to the empty DB.
                self.engine.forget_patterns(device_id)
                return
            if not pattern_db.client.endpoints:
                # Only file-supplied patterns: serve them, nothing to export.
                self.engine.apply_pattern_db(device_id, served)
                await self.engine.load_state(device_id)
                return

            # Anchor the patterns base dir next to the device databases instead
            # of the process CWD, so exporting works the same regardless of
            # where the daemon was started from.
            patterns_dir = self.db_manager.device_db_dir.parent / "patterns"
            patterns_dir.mkdir(parents=True, exist_ok=True)

            # Derive a filesystem-safe deterministic filename component from
            # the (potentially untrusted) device id, instead of using raw
            # request-derived data directly in the path expression.
            safe_device_key = hashlib.sha256(device_id.encode("utf-8")).hexdigest()
            if not _SAFE_FILENAME_RE.fullmatch(safe_device_key):
                raise ValueError(f"Unsafe pattern path for device {device_id!r}")  # noqa: TRY301
            # Confine the write target explicitly under the patterns base dir.
            base = patterns_dir.resolve()
            filepath = (base / f"{safe_device_key}.ride-pattern.json").resolve()
            if base != filepath and base not in filepath.parents:
                raise ValueError(f"Unsafe pattern path for device {device_id!r}")  # noqa: TRY301
            # Write the portable v2 DeviceModel when the device has a learned
            # protocol header, so the on-disk .ride-pattern.json is a complete
            # clone (goal #11). Fall back to the v1 PatternDB for legacy
            # devices that never completed a first-flush AUTO identification.
            header = await self.db_manager.read_device_meta(device_id)
            if header and (header.get("protocols") or header.get("transport")):
                device_model = await ingester.export_device_model(
                    device_id, vendor, device_type, applied=applied
                )
                self.engine.save_pattern_file(device_model, str(filepath))
            else:
                self.engine.save_pattern_file(pattern_db, str(filepath))
            self.engine.apply_pattern_db(device_id, served)
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
                    return _parse_llm_json(result["content"])
                except Exception:  # noqa: BLE001 - defensive parse
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
        # Defense-in-depth (F-02): pairs may come from persistent storage or
        # imports that predate capture-time redaction, so re-redact before the
        # payload ever reaches the LLM.
        pairs = _redact_pairs_for_prompt(context.get("pairs"))
        pairs_json = json.dumps(pairs, indent=2, default=str)
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

        # On the very first flush the protocol is still unknown (auto). Ask the
        # LLM to identify the device/protocol in mode="auto" and return a full
        # structured protocol_info object (transport/security/proprietary/
        # handler/identity/ports/confidence) plus the legacy flat protocols/
        # connection_mode so we can persist the stable device header. Later
        # flushes skip this.
        if context.get("first_flush"):
            prompt += (
                "\n\nThis is the FIRST contact with the device: identify its "
                'protocol in mode="auto" (initial device/protocol '
                "identification, not a protocol message). Based on the pairs "
                'above, also answer in JSON with an object "protocol_info": '
                '{"transport": "tcp|udp|websocket", "protocol": "mqtt|http|'
                'modbus|coap|websocket|proprietary|...", "proprietary": '
                'true|false, "security": "none|tls|mqtts|dtls|...", '
                '"handler": "the protocol server / MITM handler to use", '
                '"identity": "vendor/model identity if any", "ports": [list '
                'of ports], "confidence": 0.0..1.0}. Also include "protocols": '
                '[list of protocols the device speaks, e.g. ["mqtt"], '
                '["http"], ["modbus"], ["coap"], ["tls"]], '
                '"connection_mode": one of auto | tls | http | mqtt | coap | '
                "modbus (the ingress decision for this device). "
                'Use "http" for plain HTTP(S) traffic.'
            )
        # Subsequent flushes: the device is already known. Ground the LLM with
        # the current learned model and have it return a structured *delta*
        # (commands/responses/interactions/?), which is then merged
        # idempotently into the DeviceModel (C1) instead of re-deriving SQL.
        elif context.get("current_model"):
            current_model_json = json.dumps(context["current_model"], indent=2, default=str)
            prompt += (
                "\n\nThis device has ALREADY been learned (a DeviceModel "
                "exists). The device model is:\n"
                f"{current_model_json}\n\n"
                "Based ONLY on the NEW pairs above, return a JSON DELTA of the "
                'device model: an object with "commands" (list of Command: '
                '"id" (stable, reuse the existing id when the command is '
                'already known), "kind", "protocol", "method", "path" or '
                '"topic", "headers", "body_schema", "confidence"), '
                '"responses" (list of ServerResponse: "id", "triggers" '
                '(intents), "status_code", "headers_template", "body_template", '
                '"field_mappings"), "interactions" (list of FieldMapping: '
                '"source", "target", "transform", "mapping"), and optionally '
                '"state_variables" or "virtual_sensors". Do NOT repeat '
                "commands already in the model above unchanged; only new or "
                "changed knowledge. If nothing is new, return "
                '{"commands": [], "responses": [], "interactions": []}.'
            )
        return prompt

    async def _save_patterns(self, device_id: str, analysis: dict):  # noqa: C901, PLR0912, PLR0915
        """Save LLM-decoded patterns to the device database.

        Routes structured output through DecipherIngest: a v2 delta (with
        ``commands``/``responses``/``interactions``) is merged idempotently via
        :meth:`DecipherIngest.merge_device_model` (C1 learning update), a full
        v1 PatternDB (``meta``/``client``/``server``) via ``import_patterns``,
        and simpler analyses fall back to the original dict-based loop.
        """
        # Skip the cascade of early `return`: each branch returns on success.
        if isinstance(analysis.get("commands"), list) or isinstance(
            analysis.get("responses"), list
        ):
            # v2 model delta (C1): only the fields the LLM actually returned,
            # merged idempotently so re-learning converges instead of duping.
            try:
                ingester = decipher_ingest.DecipherIngest(self.db_manager)
                pruned: dict = {}
                for key in (
                    "meta",
                    "protocol",
                    "commands",
                    "responses",
                    "interactions",
                    "state_variables",
                    "virtual_sensors",
                    "observation_history",
                ):
                    if key in analysis:
                        pruned[key] = analysis[key]
                # DeviceModel requires meta; default it when the delta omits it.
                if "meta" not in pruned:
                    header = await self.db_manager.read_device_meta(device_id) or {}
                    pruned["meta"] = {
                        "pattern_id": f"{device_id}-patterns",
                        "vendor": header.get("vendor", "unknown"),
                        "device_type": header.get("device_type", "unknown"),
                        "model": header.get("model", ""),
                    }
                # Command/ServerResponse ids are required by the schema but an
                # LLM delta may omit them: derive stable ids exactly like
                # merge_device_model does, so re-flush maps to the same row.
                for cmd in pruned.get("commands", []):
                    if not cmd.get("id"):
                        path = cmd.get("path") or cmd.get("path_pattern") or cmd.get("topic") or ""
                        digest = (
                            hashlib.md5(path.encode("utf-8")).hexdigest()[:8] if path else "none"
                        )
                        cmd["id"] = f"{device_id}_{cmd.get('kind', 'cmd')}_{digest}"
                for resp in pruned.get("responses", []):
                    if not resp.get("id"):
                        tpl_prefix = f"tpl_{device_id}_{resp.get('triggers', ['resp'])[0]}"
                        resp["id"] = tpl_prefix  # matched by template_id in merge
                model = DeviceModel.model_validate(pruned)
                count = await ingester.merge_device_model(device_id, model)
                logger.info(
                    "Merged %d model command rows for %s (v2 delta)",
                    count,
                    device_id,
                )
            except Exception as e:  # noqa: BLE001 - fall back to legacy path
                logger.warning("v2 delta merge failed, falling back: %s", e)
            else:
                return

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

        listed = "patterns" in analysis or "decoded_patterns" in analysis
        patterns = _listed_patterns(analysis)
        if not patterns and not listed:
            # No pattern list at all: the reply may be one pattern at top level.
            # An explicit empty list means nothing was learned -- do not turn
            # the reply itself into a bogus "POST /unknown" pattern.
            patterns = [analysis]
        if not patterns:
            logger.info("LLM reported no patterns for %s", device_id)
            return

        # Pre-compute IDs for batch lookup to eliminate N+1 queries
        pattern_ids = []
        template_ids = []
        mapping_ids = []

        prepared_patterns = []
        for pattern_data in patterns:
            # `or` (not a .get default): a model often sends explicit nulls,
            # and "path": null used to crash on path.encode().
            intent = str(pattern_data.get("intent") or "unknown")
            method = str(pattern_data.get("method") or pattern_data.get("request_method") or "POST")
            path = str(pattern_data.get("path") or pattern_data.get("request_path") or "/unknown")
            path_digest = hashlib.md5(path.encode("utf-8")).hexdigest()[:8]
            pattern_id = f"{device_id}_{intent}_{path_digest}"
            pattern_ids.append(pattern_id)

            resp_data = pattern_data.get("response", pattern_data.get("response_template", {}))
            if resp_data and not isinstance(resp_data, dict):
                # e.g. "response": "OK" -- no status/headers/body structure to
                # build a template from; learn the request shape only.
                logger.warning(
                    "Pattern %s: response is not an object, no template created", pattern_id
                )
                resp_data = {}
            template_id = f"tpl_{pattern_id}" if resp_data else None
            if template_id:
                template_ids.append(template_id)

            mappings = pattern_data.get("field_mappings", {})
            prepared_mappings = []
            if isinstance(mappings, dict):
                for req_field, raw_resp_info in mappings.items():
                    resp_info = (
                        {"response_field": raw_resp_info, "type": "string"}
                        if isinstance(raw_resp_info, str)
                        else raw_resp_info
                    )
                    if not isinstance(resp_info, dict):
                        continue  # e.g. a number or list: nothing to map
                    mapping_id = f"map_{device_id}_{intent}_{req_field.replace('.', '_')}"
                    mapping_ids.append(mapping_id)
                    prepared_mappings.append((mapping_id, req_field, resp_info))

            prepared_patterns.append(
                {
                    "pattern_data": pattern_data,
                    "intent": intent,
                    "method": method,
                    "path": path,
                    "pattern_id": pattern_id,
                    "resp_data": resp_data,
                    "template_id": template_id,
                    "prepared_mappings": prepared_mappings,
                }
            )

        async with self.db_manager.device_session(device_id) as session:
            stats = await session.execute(
                select(MatchStats).where(MatchStats.device_id == device_id)
            )
            stats_obj = stats.scalar_one_or_none()
            if stats_obj is None:
                stats_obj = MatchStats(device_id=device_id)
                session.add(stats_obj)

            existing_patterns = {}
            if pattern_ids:
                res = await session.execute(
                    select(RequestPattern).where(RequestPattern.pattern_id.in_(pattern_ids))
                )
                existing_patterns = {p.pattern_id: p for p in res.scalars().all()}

            existing_templates = {}
            if template_ids:
                res = await session.execute(
                    select(ResponseTemplate).where(ResponseTemplate.template_id.in_(template_ids))
                )
                existing_templates = {t.template_id: t for t in res.scalars().all()}

            existing_mappings = set()
            if mapping_ids:
                res = await session.execute(
                    select(FieldMapping.mapping_id).where(FieldMapping.mapping_id.in_(mapping_ids))
                )
                existing_mappings = set(res.scalars().all())

            for item in prepared_patterns:
                pattern_data = item["pattern_data"]
                intent = item["intent"]
                method = item["method"]
                path = item["path"]
                pattern_id = item["pattern_id"]
                resp_data = item["resp_data"]
                template_id = item["template_id"]

                pattern = existing_patterns.get(pattern_id)
                if not pattern:
                    pattern = RequestPattern(
                        pattern_id=pattern_id,
                        method=method,
                        path_pattern=path,
                        protocol=pattern_data.get("protocol", "http"),
                        required_headers=_pattern_headers(pattern_data),
                        body_schema=pattern_data.get("body_schema", {}),
                        query_param_keys=_pattern_query_keys(pattern_data),
                        intent=intent,
                        confidence=_confidence(pattern_data.get("confidence")),
                    )
                    session.add(pattern)
                    existing_patterns[pattern_id] = pattern
                    stats_obj.patterns_learned += 1
                else:
                    # The LLM may revise a pattern on a later flush: keep the
                    # learned fields fresh instead of leaving the old row stale.
                    pattern.method = method
                    pattern.path_pattern = path
                    pattern.protocol = pattern_data.get("protocol", "http")
                    pattern.required_headers = _pattern_headers(pattern_data)
                    pattern.body_schema = pattern_data.get("body_schema", {})
                    pattern.query_param_keys = _pattern_query_keys(pattern_data)
                    pattern.intent = intent
                    pattern.confidence = _confidence(pattern_data.get("confidence"))

                if resp_data and template_id:
                    template = existing_templates.get(template_id)
                    if not template:
                        template = ResponseTemplate(
                            template_id=template_id,
                            pattern_id=pattern_id,
                            status_code=_status_code(resp_data.get("status_code")),
                            headers_template=resp_data.get("headers", {}),
                            body_template=resp_data.get("body", {}),
                            field_mappings=resp_data.get("field_mappings", {}),
                            expected_variables=resp_data.get("expected_variables", []),
                            confidence=_confidence(resp_data.get("confidence")),
                        )
                        session.add(template)
                        existing_templates[template_id] = template
                        stats_obj.templates_created += 1
                    else:
                        template.status_code = _status_code(resp_data.get("status_code"))
                        template.headers_template = resp_data.get("headers", {})
                        template.body_template = resp_data.get("body", {})
                        template.field_mappings = resp_data.get("field_mappings", {})
                        template.expected_variables = resp_data.get("expected_variables", [])
                        template.confidence = _confidence(resp_data.get("confidence"))

                for mapping_id, req_field, resp_info in item["prepared_mappings"]:
                    if mapping_id not in existing_mappings:
                        fmap = FieldMapping(
                            mapping_id=mapping_id,
                            request_field=req_field,
                            request_type=resp_info.get("request_type", "string"),
                            response_field=resp_info.get("response_field", req_field),
                            response_type=resp_info.get("type", "string"),
                            transform=resp_info.get("transform", "direct"),
                            formula=decipher_ingest.stored_formula(resp_info.get("formula")),
                            enum_values=resp_info.get("enum_values"),
                            intent=intent,
                            confidence=_confidence(resp_info.get("confidence")),
                        )
                        session.add(fmap)
                        existing_mappings.add(mapping_id)

    async def _persist_device_meta(
        self, device_id: str, device: DeviceRegistry, analysis: dict
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
        # Whitelist the LLM-derived protocols so arbitrary strings are dropped
        # (DeviceMeta enforces the same sets on every header write).
        if not isinstance(protocols, (list, tuple)):
            protocols = []
        protocols = [p for p in map(normalize_protocol, protocols) if p]

        connection_mode = analysis.get("connection_mode") or protocol_dict.get("handler")
        if not connection_mode:
            # Derive the ingress mode from the first reported protocol.
            connection_mode = protocols[0] if protocols else "auto"
        connection_mode = normalize_connection_mode(connection_mode) or "auto"

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



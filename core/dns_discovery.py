"""DNS discovery: what each client asks for, and what the proxy did with it.

The DNS interception port is the first place a device reveals itself: the name it
resolves is the name it is about to talk to. Recording that turns "which domains
should we intercept?" from a guess into a list read off real traffic -- including
traffic the proxy is *not* intercepting yet.

Recording must not slow the DNS path down, so observations are counted in memory
and written to the database in batches by a background task: one DNS answer never
waits for SQLite.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: How often the in-memory counters reach the database.
DEFAULT_FLUSH_INTERVAL_S = 5.0

#: How often the log may be pruned. The row cap is a working-set bound, not a hard
#: one, and counting the table on every flush would be a query every few seconds
#: for nothing.
DEFAULT_PRUNE_INTERVAL_S = 60.0

#: Name of the three outcomes a DNS answer can have.
STATUS_INTERCEPTED = "intercepted"
STATUS_EXCLUDED = "excluded"
STATUS_FORWARDED = "forwarded"


class DNSDiscoverySink(Protocol):
    """What the recorder needs to persist observations (``DatabaseManager``).

    Declared as a protocol so the recorder never imports the database layer --
    the DNS path stays free of that dependency and a test can pass a fake.
    """

    async def record_dns_queries(self, observations: list[dict]) -> int:
        """Upsert a batch of (client, hostname, qtype) observations."""
        ...

    async def prune_dns_queries(self, max_rows: int | None = None) -> int:
        """Drop the oldest rows so the log stays bounded."""
        ...

    async def clear_dns_queries(self) -> int:
        """Empty the log and return how many rows were removed."""
        ...


@dataclass
class _PairCounters:
    """In-memory counters for one (client_ip, hostname, qtype) pair."""

    # Bound by the recorder when the pair is created. Kept with defaults so the
    # dataclass stays cheap to build on the DNS hot path.
    client_ip: str = ""
    hostname: str = ""
    qtype: str = "A"
    hit_count: int = 0
    first_seen: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_seen: datetime = field(default_factory=lambda: datetime.now(UTC))
    status: str = STATUS_FORWARDED

    def as_observation(self) -> dict[str, Any]:
        """Shape the counters exactly like the ``dns_query_log`` row."""
        return {
            "client_ip": self.client_ip,
            "hostname": self.hostname,
            "qtype": self.qtype,
            "status": self.status,
            "hit_count": self.hit_count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }


class DNSDiscoveryRecorder:
    """Aggregate observed DNS queries in memory and flush them in batches.

    ``sink`` is anything exposing ``record_dns_queries(observations)`` and
    ``prune_dns_queries(max_rows)`` (``DatabaseManager`` in the running service, a
    fake in tests) and may be ``None``: the recorder still counts in memory, so
    the DNS server never has to care whether persistence exists.
    """

    def __init__(
        self,
        sink: DNSDiscoverySink | None = None,
        *,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
        prune_interval_s: float = DEFAULT_PRUNE_INTERVAL_S,
        max_rows: int = 5000,
        enabled: bool = True,
    ) -> None:
        self.sink = sink
        self.flush_interval_s = float(flush_interval_s)
        self.prune_interval_s = float(prune_interval_s)
        self.max_rows = int(max_rows)
        self.enabled = bool(enabled)
        self.pairs: dict[tuple[str, str, str], _PairCounters] = {}
        self.flushed_pairs = 0
        self.flush_errors = 0
        self.dropped_pairs = 0
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._next_prune = 0.0

    # ── hot path ─────────────────────────────────────────────────────────────

    def note(self, client_ip: str, hostname: str, qtype: str, status: str) -> None:
        """Count one observed query. Synchronous, allocation-light, never raises."""
        if not self.enabled or not hostname:
            return
        key = (client_ip or "unknown", hostname.lower().rstrip("."), (qtype or "A").upper())
        now = datetime.now(UTC)
        counters = self.pairs.get(key)
        if counters is None:
            if len(self.pairs) >= self.max_rows:
                # Bounded even with no sink to prune: the live view is a working set,
                # not an archive, so a device resolving random names cannot grow this
                # process's memory. Counted, never silent.
                self.dropped_pairs += 1
                return
            counters = _PairCounters(
                client_ip=key[0],
                hostname=key[1],
                qtype=key[2],
                hit_count=1,
                status=status,
                first_seen=now,
                last_seen=now,
            )
            self.pairs[key] = counters
            return
        counters.hit_count += 1
        counters.last_seen = now
        counters.status = status

    # ── persistence ──────────────────────────────────────────────────────────

    async def flush(self) -> int:
        """Write the pending counters to the sink.

        The in-memory buffer is swapped out before the write, so queries answered
        while the flush is in flight are counted, not lost -- and put back if the
        write did not go through, so a database that is down never loses
        observations. How many pairs the sink reported is what comes back.

        The write itself happens under the lock, which is what keeps a clear() from
        racing it: a batch taken before the operator emptied the log must not land
        after it and repopulate what they just cleared. ``note()`` is synchronous and
        never takes the lock, so the DNS path stays free.
        """
        async with self._lock:
            if not self.pairs:
                return 0
            pending = self.pairs
            self.pairs = {}

            if self.sink is None:
                # Nothing to persist to: keep counting in memory instead of dropping.
                self._merge_back(pending)
                return 0

            observations = [counters.as_observation() for counters in pending.values()]
            try:
                written = await self.sink.record_dns_queries(observations)
            except BaseException as exc:  # noqa: BLE001
                # BaseException on purpose: a cancellation (shutdown, task cancel)
                # must put the counters back too, or they are lost silently.
                self._merge_back(pending)
                if not isinstance(exc, Exception):
                    raise
                self.flush_errors += 1
                logger.warning(
                    "DNS discovery: flush of %d pairs failed: %s", len(observations), exc
                )
                return 0

        self.flushed_pairs += len(observations)
        if written:
            await self.prune()
        return int(written)

    def _merge_back(self, pending: dict[tuple[str, str, str], _PairCounters]) -> None:
        """Re-aggregate counters that were not persisted into the live buffer.

        Synchronous and never awaits: the live buffer cannot be mutated while it
        runs, so no lock is needed on this path.
        """
        for key, counters in pending.items():
            merged = self.pairs.get(key)
            if merged is None:
                self.pairs[key] = counters
                continue
            merged.hit_count += counters.hit_count
            merged.first_seen = min(merged.first_seen, counters.first_seen)
            merged.last_seen = max(merged.last_seen, counters.last_seen)
            merged.status = counters.status

    async def prune(self) -> int:
        """Drop the oldest pairs past the cap, at most once per prune interval.

        Called after a successful write rather than on every flush: pruning
        cannot fix a failed write, and it must never be mistaken for one.
        """
        if self.sink is None:
            return 0
        now = time.monotonic()
        if now < self._next_prune:
            return 0
        self._next_prune = now + self.prune_interval_s
        try:
            return int(await self.sink.prune_dns_queries(self.max_rows))
        except Exception as exc:  # noqa: BLE001 - discovery must never break DNS
            self.flush_errors += 1
            logger.warning("DNS discovery: could not prune the log: %s", exc)
            return 0

    async def clear(self) -> int:
        """Forget every recorded pair, in memory and (through the sink) on disk.

        Memory and storage are emptied under the same lock ``flush`` uses, so a flush
        in flight cannot write its batch after the clear and bring the rows back.
        """
        async with self._lock:
            self.pairs.clear()
            if self.sink is None:
                return 0
            try:
                return int(await self.sink.clear_dns_queries())
            except Exception as exc:  # noqa: BLE001 - clearing must not raise into the API
                self.flush_errors += 1
                logger.warning("DNS discovery: could not clear the log: %s", exc)
                return 0

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.flush_interval_s)
            with contextlib.suppress(Exception):
                await self.flush()

    async def start(self) -> None:
        """Start the periodic flush task (idempotent)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        """Stop the flush task after writing whatever is still pending."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.flush()

    @property
    def pending_pairs(self) -> int:
        return len(self.pairs)

    def snapshot(self) -> list[dict[str, Any]]:
        """The in-memory counters: the API merges them into the discovery view, and
        tests read them directly."""
        return [counters.as_observation() for counters in self.pairs.values()]


# ── singleton ────────────────────────────────────────────────────────────────

_recorder: DNSDiscoveryRecorder | None = None


def get_dns_recorder() -> DNSDiscoveryRecorder | None:
    """Return the process-wide recorder, if one was created."""
    return _recorder


def set_dns_recorder(recorder: DNSDiscoveryRecorder | None) -> DNSDiscoveryRecorder | None:
    """Install the process-wide recorder and return the previous one."""
    global _recorder  # noqa: PLW0603
    previous = _recorder
    _recorder = recorder
    return previous

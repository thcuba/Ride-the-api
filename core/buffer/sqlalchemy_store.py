"""SQLAlchemy-backed buffer store shared by disk and in-memory modes."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.buffer.store import BufferStore
from core.database import LLMContextBuffer, MatchStats, SessionCache

logger = logging.getLogger(__name__)

# A callable that, given a device id, yields an async context manager
# providing an AsyncSession living in the buffer's database (file or RAM).
SessionProvider = Callable[[str], AbstractAsyncContextManager[AsyncSession]]


async def _get_or_create_stats(session: AsyncSession, device_id: str) -> MatchStats:
    """Get or create the per-device MatchStats row in the given database."""
    result = await session.execute(select(MatchStats).where(MatchStats.device_id == device_id))
    stats = result.scalar_one_or_none()
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
    return stats


class SqlAlchemyBufferStore(BufferStore):
    """Buffer store over SQLAlchemy session(s).

    The session source decides whether data lands on durable storage
    (``DatabaseManager.device_session`` -- the default "disk" mode) or on the
    process-shared in-memory engine (RAM mode).

    ``durable_stats_provider`` is optional: when set (RAM mode), after a
    flush/delete the durable ``MatchStats`` row on the file DB is synced so
    on-disk statistics stay coherent while the hot path stays RAM-only.

    Read-modify-write operations (``add_pair``, ``flush``, ``flush_selected``,
    ``delete_entry``) are serialized per device with an ``asyncio.Lock``,
    preventing lost update / duplicate-row races even though each database
    backend nominally has a single writer per device.
    """

    def __init__(
        self,
        session_provider: SessionProvider,
        durable_stats_provider: SessionProvider | None = None,
    ) -> None:
        self._session = session_provider
        self._durable_stats = durable_stats_provider
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, device_id: str) -> asyncio.Lock:
        """Return (creating if needed) the per-device serialization lock."""
        with self._locks_guard:
            lock = self._locks.get(device_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[device_id] = lock
            return lock

    async def add_pair(self, device_id: str, pair: dict, estimated_size: int) -> int:
        async with self._lock_for(device_id), self._session(device_id) as session:
            seq = await self._next_sequence(session, device_id)

            entry = LLMContextBuffer(
                device_id=device_id,
                correlated_pair=pair,
                estimated_size_bytes=estimated_size,
                sequence=seq,
            )
            session.add(entry)

            stats = await _get_or_create_stats(session, device_id)
            stats.current_buffer_size_bytes += estimated_size
            return stats.current_buffer_size_bytes

    async def get_buffer_pairs(self, device_id: str) -> list[dict]:
        async with self._session(device_id) as session:
            result = await session.execute(
                select(LLMContextBuffer)
                .where(
                    and_(
                        LLMContextBuffer.device_id == device_id,
                        LLMContextBuffer.flushed == False,  # noqa: E712
                    )
                )
                .order_by(LLMContextBuffer.sequence)
            )
            return [
                {"id": e.id, "pair": e.correlated_pair, "size": e.estimated_size_bytes}
                for e in result.scalars().all()
            ]

    async def flush(self, device_id: str) -> int:
        async with self._lock_for(device_id):
            async with self._session(device_id) as session:
                result = await session.execute(
                    select(LLMContextBuffer).where(
                        and_(
                            LLMContextBuffer.device_id == device_id,
                            LLMContextBuffer.flushed == False,  # noqa: E712
                        )
                    )
                )
                now = datetime.now(UTC)
                count = 0
                for entry in result.scalars().all():
                    entry.flushed = True
                    entry.flushed_at = now
                    count += 1

                stats = await _get_or_create_stats(session, device_id)
                stats.current_buffer_size_bytes = 0
                stats.last_flush_at = now
                stats.buffer_flushes += 1

            if count and self._durable_stats:
                await self._sync_durable_flush(device_id, now)
            return count

    async def flush_selected(self, device_id: str, entry_ids: list[int]) -> int:
        async with self._lock_for(device_id):
            async with self._session(device_id) as session:
                result = await session.execute(
                    select(LLMContextBuffer).where(
                        and_(
                            LLMContextBuffer.device_id == device_id,
                            LLMContextBuffer.flushed == False,  # noqa: E712
                            LLMContextBuffer.id.in_(entry_ids),
                        )
                    )
                )
                now = datetime.now(UTC)
                flushed_size = 0
                count = 0
                for entry in result.scalars().all():
                    entry.flushed = True
                    entry.flushed_at = now
                    flushed_size += entry.estimated_size_bytes
                    count += 1

                stats = await _get_or_create_stats(session, device_id)
                stats.current_buffer_size_bytes = max(
                    0, stats.current_buffer_size_bytes - flushed_size
                )
                stats.last_flush_at = now
                stats.buffer_flushes += 1

            if count and self._durable_stats:
                await self._sync_durable_flush(device_id, now, delta=flushed_size)
            return count

    async def delete_entry(self, device_id: str, entry_id: int) -> bool:
        async with self._lock_for(device_id):
            async with self._session(device_id) as session:
                result = await session.execute(
                    select(LLMContextBuffer).where(
                        and_(
                            LLMContextBuffer.id == entry_id,
                            LLMContextBuffer.device_id == device_id,
                        )
                    )
                )
                entry = result.scalar_one_or_none()
                if not entry:
                    return False
                size = entry.estimated_size_bytes
                await session.delete(entry)
                stats = await _get_or_create_stats(session, device_id)
                stats.current_buffer_size_bytes = max(0, stats.current_buffer_size_bytes - size)

            if self._durable_stats:
                await self._sync_durable_delete(device_id, size)
            return True

    async def get_current_size(self, device_id: str) -> int:
        async with self._session(device_id) as session:
            result = await session.execute(
                select(MatchStats).where(MatchStats.device_id == device_id)
            )
            stats = result.scalar_one_or_none()
            return stats.current_buffer_size_bytes if stats else 0

    async def clear_cache(self, device_id: str) -> None:
        async with self._session(device_id) as session:
            await session.execute(delete(SessionCache).where(SessionCache.device_id == device_id))
            logger.info("Cleared session cache for device %s", device_id)

    async def close(self) -> None:
        """Nothing to release for SQLAlchemy-backed stores."""

    # -- Internal helpers -----------------------------------------------------

    async def _next_sequence(self, session: AsyncSession, device_id: str) -> int:
        """Sequence within the device.

        ``SELECT MAX(sequence) + 1`` is safe in both backends today: disk mode
        uses one SQLite writer per device DB and RAM mode funnels every write
        through a single shared connection (``StaticPool``), and callers
        serialize per device via ``_lock_for``.
        """
        result = await session.execute(
            select(func.max(LLMContextBuffer.sequence)).where(
                LLMContextBuffer.device_id == device_id
            )
        )
        max_seq = result.scalar()
        if max_seq is None:
            return 0
        return max_seq + 1

    async def _sync_durable_flush(
        self, device_id: str, now: datetime, delta: int | None = None
    ) -> None:
        """Sync the durable file-DB MatchBuffer after a flush (RAM mode)."""
        provider = self._durable_stats
        assert provider is not None
        async with provider(device_id) as session:
            stats = await _get_or_create_stats(session, device_id)
            if delta is None:
                stats.current_buffer_size_bytes = 0
            else:
                stats.current_buffer_size_bytes = max(
                    0, (stats.current_buffer_size_bytes or 0) - delta
                )
            stats.last_flush_at = now
            stats.buffer_flushes += 1

    async def _sync_durable_delete(self, device_id: str, size: int) -> None:
        """Sync the durable file-DB MatchBufferStats after a delete (RAM mode)."""
        provider = self._durable_stats
        assert provider is not None
        async with provider(device_id) as session:
            stats = await _get_or_create_stats(session, device_id)
            stats.current_buffer_size_bytes = max(0, (stats.current_buffer_size_bytes or 0) - size)

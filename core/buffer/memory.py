"""Process-wide shared in-memory SQLite engine backing the RAM buffer.

A plain ``sqlite+aiosqlite:///:memory:`` engine gives each connection its own
private empty database. To make every session (BufferManager and ContextBuffer
alike) see the *same* in-process buffer, we keep a single engine with a
``StaticPool`` (one shared connection) and create only the buffer tables on it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, cast

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from core.database import Base, LLMContextBuffer, MatchStats, SessionCache

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy import Table

logger = logging.getLogger(__name__)

# Only the transient buffer tables live in RAM; durable models (patterns,
# registry, …) stay on the on-disk databases.
_BUFFER_TABLES: list[Table] = [
    cast("Table", LLMContextBuffer.__table__),
    cast("Table", SessionCache.__table__),
    cast("Table", MatchStats.__table__),
]

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None
_initialized = False
_init_lock = asyncio.Lock()


class _SessionGate:
    """Lets one session at a time use the single shared in-memory connection.

    ``StaticPool`` hands every session the *same* connection, so two sessions
    that overlap in time share one transaction: one session's COMMIT commits the
    other's half-done work, and the pool's reset-on-return ROLLBACK throws away
    the other's flushed writes. Per-store and per-device locks cannot prevent
    that, because every store and every device shares this connection. The gate
    is re-entrant per task, so a nested session in the same task can never
    deadlock.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._depth = 0

    async def acquire(self) -> None:
        task = asyncio.current_task()
        if task is not None and self._owner is task:
            self._depth += 1
            return
        await self._lock.acquire()
        self._owner = task
        self._depth = 1

    def release(self) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()


_gate: _SessionGate | None = None
_gate_loop: asyncio.AbstractEventLoop | None = None


def _session_gate() -> _SessionGate:
    """The gate for the running event loop (an asyncio lock is loop-bound)."""
    global _gate, _gate_loop  # noqa: PLW0603
    loop = asyncio.get_running_loop()
    if _gate is None or _gate_loop is not loop:
        _gate = _SessionGate()
        _gate_loop = loop
    return _gate


async def _ensure_initialized() -> AsyncEngine:
    """Create the shared in-memory engine and buffer tables once per process."""
    global _engine, _factory, _initialized  # noqa: PLW0603
    if _initialized and _engine is not None:
        return _engine
    async with _init_lock:
        if _initialized and _engine is not None:
            return _engine
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=StaticPool,
        )
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_BUFFER_TABLES)
            )
        _engine = engine
        _factory = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        _initialized = True
        logger.info("In-memory buffer engine initialized (RAM mode)")
        return engine


@contextlib.asynccontextmanager
async def memory_session(_device_id: str) -> AsyncGenerator[AsyncSession, None]:
    """Async context manager yielding a session into the shared in-memory DB.

    ``device_id`` is accepted for call-signature parity with
    ``DatabaseManager.device_session``; it is not used because the RAM buffer
    is a single shared database.

    Sessions are serialized through :class:`_SessionGate`, so each one owns
    the shared connection (and its transaction) for its whole lifetime.
    """
    await _ensure_initialized()
    if _factory is None:
        # _ensure_initialized guarantees the factory is set on success; this
        # explicit guard (instead of an assert) survives ``python -O`` and
        # gives a useful message if the invariant is ever violated.
        raise RuntimeError("in-memory buffer not initialized (call _ensure_initialized first)")
    gate = _session_gate()
    await gate.acquire()
    try:
        session = _factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
    finally:
        gate.release()


async def dispose_memory_db() -> None:
    """Drop the shared in-memory engine (used at shutdown and in tests)."""
    global _engine, _factory, _initialized, _gate, _gate_loop  # noqa: PLW0603
    async with _init_lock:
        if _engine is not None:
            await _engine.dispose()
        _engine = None
        _factory = None
        _initialized = False
        _gate = None
        _gate_loop = None

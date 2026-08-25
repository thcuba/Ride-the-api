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
    """
    await _ensure_initialized()
    assert _factory is not None
    session = _factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def dispose_memory_db() -> None:
    """Drop the shared in-memory engine (used at shutdown and in tests)."""
    global _engine, _factory, _initialized  # noqa: PLW0603
    async with _init_lock:
        if _engine is not None:
            await _engine.dispose()
        _engine = None
        _factory = None
        _initialized = False

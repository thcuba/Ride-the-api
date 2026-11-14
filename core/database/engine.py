"""Database engine configuration, SQLite pragmas, and helper functions."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)

# Sqlite durability settings applied to every engine connection. WAL journaling
# plus synchronous=NORMAL keeps reads concurrent and commits crash-safe without
# the write-lock stalls of the default rollback journal. busy_timeout avoids
# "database is locked" under concurrent writers, and foreign_keys enforce
# referential integrity the default SQLite doesn't.
_SQLITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
)


def _apply_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:  # noqa: ANN401
    """Apply durability PRAGMAs to a SQLite connection (no-op otherwise)."""
    try:
        cursor = dbapi_connection.cursor()
        for pragma in _SQLITE_PRAGMAS:
            cursor.execute(pragma)
        cursor.close()
    except Exception:  # pragma: no cover - defensive for non-sqlite drivers
        return


def create_configured_engine(db_url: str, *, echo: bool = False) -> AsyncEngine:
    """Create an async engine with durability PRAGMAs for SQLite backends.

    Uses NullPool (the de-facto setting for aiosqlite here) so that every
    connection re-runs the PRAGMAs via the connect listener. PostgreSQL accepts
    no SQLite PRAGMAs; the listener returns without touching them.
    """
    engine = create_async_engine(db_url, echo=echo, poolclass=NullPool)
    if db_url.startswith("sqlite"):
        event.listen(engine.sync_engine, "connect", _apply_sqlite_pragmas)
    return engine


def _iso_utc(value: datetime | None) -> str | None:
    """Render a stored timestamp as ISO-8601 UTC.

    SQLite hands the rows back naive (it has no timezone type), so an unqualified
    ``isoformat()`` would leave the caller guessing -- and comparing a naive string
    with the aware one of a not-yet-flushed counter is not the same comparison.
    """
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _collapse_dns_observations(observations: list[dict]) -> list[dict]:
    """Merge observations sharing (client_ip, hostname, qtype) into one row each.

    A batch must not carry the same unique key twice: SQLite refuses a single
    ``ON CONFLICT DO UPDATE`` statement that would touch one row two times, so
    collapsing here keeps a duplicated pair a hit counter, not a failed flush.
    """
    merged: dict[tuple, dict] = {}
    for obs in observations:
        key = (obs["client_ip"], obs["hostname"], obs["qtype"])
        current = merged.get(key)
        if current is None:
            merged[key] = dict(obs)
            continue
        current["hit_count"] = int(current.get("hit_count") or 1) + int(obs.get("hit_count") or 1)
        current["first_seen"] = min(current["first_seen"], obs["first_seen"])
        current["last_seen"] = max(current["last_seen"], obs["last_seen"])
        current["status"] = obs["status"]
    return list(merged.values())


__all__ = [
    "_SQLITE_PRAGMAS",
    "_apply_sqlite_pragmas",
    "_collapse_dns_observations",
    "_iso_utc",
    "create_configured_engine",
]

"""Versioned, idempotent schema migrations.

``Base.metadata.create_all`` creates missing tables but never applies ALTERs, so
adding a column to a model silently no-ops on an existing database and the code
drifts from the deployed schema. This module provides a small, deterministic
migration runner that records applied migration IDs in a ``_schema_migrations``
table and applies pending migrations durably (each in its own transaction), so
schema changes are structured and never lost.

Migrations are plain async callables receiving a SQLAlchemy async connection.
New migrations are appended to ``MIGRATIONS`` — IDs are monotonically
increasing integers and must never be renumbered or deleted once shipped.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

_CREATE_MIGRATION_TABLE = sa.text(
    """
    CREATE TABLE IF NOT EXISTS _schema_migrations (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL UNIQUE,
        applied_at  TEXT NOT NULL
    )
    """
)


class Migration:
    """A single, ordered schema migration."""

    def __init__(self, id: int, name: str, apply_fn) -> None:
        self.id = id
        self.name = name
        self.apply_fn = apply_fn

    async def apply(self, conn) -> None:
        """Apply this migration's DDL/logic on the given connection."""
        await self.apply_fn(conn)


# ---------------------------------------------------------------------------
# Migration implementations
# ---------------------------------------------------------------------------


async def _m0001_record_baseline(conn) -> None:
    """Baseline migration (no DDL).

    Databases created before this runner existed already have their tables via
    ``Base.metadata.create_all``. Recording a baseline here simply reserves the
    migration slot so later migrations always apply *on top of* the pre-existing
    schema. Any actual DDL change must be added as its own newer migration — do
    not rely on create_all to alter existing tables.
    """
    await conn.execute(sa.text("SELECT 1"))


MIGRATIONS: Sequence[Migration] = (
    Migration(1, "baseline", _m0001_record_baseline),
)


class SchemaMigrator:
    """Applies pending migrations against an engine, idempotently."""

    def __init__(self, engine) -> None:
        self.engine = engine

    async def _applied_ids(self, conn) -> set[int]:
        result = await conn.execute(sa.text("SELECT id FROM _schema_migrations"))
        return {row[0] for row in result.fetchall()}

    async def run(self) -> list[str]:
        """Apply all pending migrations, returning the names applied.

        The migration table is created and all pending migrations run inside a
        single transaction: a failure rolls back atomically (no partial schema).
        """
        async with self.engine.begin() as conn:
            await conn.execute(_CREATE_MIGRATION_TABLE)
            applied_ids = await self._applied_ids(conn)
            applied_now: list[str] = []
            for migration in MIGRATIONS:
                if migration.id in applied_ids:
                    continue
                await migration.apply(conn)
                await conn.execute(
                    sa.text(
                        "INSERT INTO _schema_migrations (id, name, applied_at) "
                                        "VALUES (:id, :name, :applied_at)"
                    ),
                                    {
                                        "id": migration.id,
                                        "name": migration.name,
                                        "applied_at": datetime.now(UTC).isoformat(),
                                    },
                                )
                applied_ids.add(migration.id)
                applied_now.append(migration.name)
                logger.info("Applied migration %s: %s", migration.id, migration.name)
        return applied_now
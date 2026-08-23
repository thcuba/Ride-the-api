"""
Tests for the Decipher Ingest (LLM output → pattern DB records).
"""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy import select

from core.database import DatabaseManager, MatchStats, RequestPattern
from core.pattern_db.decipher_ingest import DecipherIngest


@pytest_asyncio.fixture
async def db_manager(tmp_path):
    core_db_path = tmp_path / "core.db"
    dm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{core_db_path}",
        device_db_dir=tmp_path / "device_dbs",
    )
    await dm.initialize()
    yield dm
    await dm.close()


SAMPLE_OUTPUT = {
    "patterns": [
        {
            "pattern_id": "pat_repeat",
            "method": "POST",
            "path_pattern": "/rpc/Status",
            "protocol": "http",
            "intent": "status",
            "field_mappings": [],
        }
    ]
}


async def _count_patterns(db_manager: DatabaseManager, device_id: str) -> int:
    async with db_manager.device_session(device_id) as session:
        result = await session.execute(select(RequestPattern))
        return len(result.scalars().all())


async def test_ingest_is_idempotent(db_manager: DatabaseManager):
    """Re-ingesting the same LLM output must not raise or duplicate patterns."""
    ingester = DecipherIngest(db_manager)
    device_id = "device-idem"

    first = await ingester.ingest(device_id, SAMPLE_OUTPUT)
    assert first == 1
    assert await _count_patterns(db_manager, device_id) == 1

    # Second identical import must be a no-op (skip the existing pattern),
    # not raise IntegrityError and wipe the whole batch.
    second = await ingester.ingest(device_id, SAMPLE_OUTPUT)
    assert second == 0
    assert await _count_patterns(db_manager, device_id) == 1


async def test_ingest_creates_match_stats_row_on_first_import(db_manager: DatabaseManager):
    """The first ingest must materialize a MatchStats row (not skip it)."""
    ingester = DecipherIngest(db_manager)
    device_id = "device-stats"

    await ingester.ingest(device_id, SAMPLE_OUTPUT)

    async with db_manager.device_session(device_id) as session:
        result = await session.execute(select(MatchStats).where(MatchStats.device_id == device_id))
        stats = result.scalar_one_or_none()
        assert stats is not None
        assert (stats.patterns_learned or 0) == 1
        assert (stats.templates_created or 0) == 1
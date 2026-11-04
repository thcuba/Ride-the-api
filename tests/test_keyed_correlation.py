"""Response/request correlation by key (review finding C-3).

A response must be paired with the request it answers. Callers that own both
sides of an exchange pass the correlation key; without one, the FIFO fallback
must never reach a request older than the correlation window.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from core.database import DatabaseManager, SessionCache
from core.pipeline import ContextBuffer, LearningPipeline, MatchRateTracker

DEVICE = "device-keyed"


@pytest_asyncio.fixture
async def db_manager(tmp_path):
    dm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "device_dbs",
    )
    await dm.initialize()
    yield dm
    await dm.close()


def _pipeline(db_manager) -> LearningPipeline:
    return LearningPipeline(
        db_manager, MagicMock(), ContextBuffer(db_manager), MatchRateTracker(db_manager)
    )


async def _register(pipeline, path):
    return await pipeline.register_request(DEVICE, "acme", "https", "GET", path, {}, {}, {})


@pytest.mark.asyncio
async def test_concurrent_responses_pair_with_their_own_request(db_manager):
    """B's response, arriving first, must pair with B — not with the older A."""
    pipeline = _pipeline(db_manager)
    key_a = await _register(pipeline, "/api/config")
    key_b = await _register(pipeline, "/api/telemetry")

    pair_b = await pipeline.match_response(
        DEVICE, "acme", "https", 200, {}, {"ack": True}, correlation_key=key_b
    )
    assert pair_b is not None
    assert pair_b.path == "/api/telemetry"

    pair_a = await pipeline.match_response(
        DEVICE, "acme", "https", 200, {}, {"config": 1}, correlation_key=key_a
    )
    assert pair_a is not None
    assert pair_a.path == "/api/config"


@pytest.mark.asyncio
async def test_keyed_match_never_falls_back_to_another_request(db_manager):
    """A key that is no longer pending yields no pair instead of a wrong one."""
    pipeline = _pipeline(db_manager)
    await _register(pipeline, "/api/other")

    pair = await pipeline.match_response(
        DEVICE, "acme", "https", 200, {}, {}, correlation_key=f"{DEVICE}:GET:/gone:deadbeef"
    )
    assert pair is None
    # The unrelated pending request is still pending, untouched.
    assert len(pipeline._correlation_cache[DEVICE]) == 1


@pytest.mark.asyncio
async def test_keyed_match_uses_db_after_restart(db_manager):
    """A keyed match finds the persisted row when the memory cache is gone."""
    key = await _register(_pipeline(db_manager), "/api/persisted")
    fresh = _pipeline(db_manager)  # new process: empty in-memory cache

    pair = await fresh.match_response(
        DEVICE, "acme", "https", 200, {}, {"ok": True}, correlation_key=key
    )
    assert pair is not None
    assert pair.path == "/api/persisted"
    async with db_manager.device_session(DEVICE) as session:
        row = (
            await session.execute(select(SessionCache).where(SessionCache.correlation_key == key))
        ).scalar_one()
    assert row.correlated is True


@pytest.mark.asyncio
async def test_fifo_fallback_skips_requests_older_than_the_window(db_manager):
    """Without a key, a stale orphan in memory is never paired."""
    pipeline = _pipeline(db_manager)
    await _register(pipeline, "/api/stale")
    stale = pipeline._correlation_cache[DEVICE][0]
    stale["timestamp"] = datetime.now(UTC) - pipeline._correlation_max_age() - timedelta(seconds=5)
    await _register(pipeline, "/api/fresh")

    pair = await pipeline.match_response(DEVICE, "acme", "https", 200, {}, {})
    assert pair is not None
    assert pair.path == "/api/fresh"


@pytest.mark.asyncio
async def test_discard_without_error_does_not_count_an_error(db_manager):
    """A handed-off request (signal mode) is dropped without an error result."""
    pipeline = _pipeline(db_manager)
    key = await _register(pipeline, "/api/handoff")

    assert await pipeline.discard_request(DEVICE, key, count_as_error=False)
    cache = pipeline.tracker._stats_cache.get(DEVICE, {})
    assert cache.get("errors", 0) == 0

"""
Tests for the Learning/Production Pipeline.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from core.database import DatabaseManager, LLMContextBuffer, RequestPattern, SessionCache
from core.pipeline import (
    ContextBuffer,
    CorrelatedPair,
    LearningPipeline,
    MatchRateTracker,
    MatchResult,
    PatternMatcher,
    PipelineMode,
)


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


def make_pair(**overrides) -> CorrelatedPair:
    defaults = dict(
        pair_id="pair-001",
        device_id="device-001",
        vendor="shelly",
        protocol="http",
        method="POST",
        path="/rpc/Switch.GetStatus",
        request_headers={"content-type": "application/json"},
        request_body={"id": 1},
        request_query={},
        response_status=200,
        response_headers={"content-type": "application/json"},
        response_body={"state": True},
        latency_ms=12.5,
        correlation_confidence=0.95,
        timestamp=datetime.now(UTC),
    )
    defaults.update(overrides)
    return CorrelatedPair(**defaults)


class TestPipelineMode:
    def test_enum_values(self):
        assert PipelineMode.LEARNING.value == "learning"
        assert PipelineMode.PRODUCTION.value == "production"
        assert PipelineMode.HYBRID.value == "hybrid"

    def test_enum_len(self):
        assert len(PipelineMode) == 3  # noqa: PLR2004


class TestCorrelatedPair:
    def test_pair_creation(self):
        pair = make_pair()
        assert pair.pair_id == "pair-001"
        assert pair.device_id == "device-001"
        assert pair.vendor == "shelly"
        assert pair.method == "POST"
        assert pair.path == "/rpc/Switch.GetStatus"
        assert pair.latency_ms == 12.5  # noqa: PLR2004


class TestContextBuffer:
    @pytest.mark.asyncio
    async def test_init(self, db_manager):
        buffer = ContextBuffer(db_manager, max_size_bytes=1024)
        assert buffer.max_size_bytes == 1024  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_add_pair_not_full(self, db_manager):
        buffer = ContextBuffer(db_manager, max_size_bytes=1048576)
        pair = make_pair()
        is_full = await buffer.add_pair("device-001", pair)
        assert is_full is False
        size = await buffer.get_current_size("device-001")
        assert size > 0

    @pytest.mark.asyncio
    async def test_get_buffer_pairs(self, db_manager):
        buffer = ContextBuffer(db_manager, max_size_bytes=1048576)
        pair = make_pair()
        await buffer.add_pair("device-001", pair)
        pairs = await buffer.get_buffer_pairs("device-001")
        assert len(pairs) == 1
        assert pairs[0]["pair"]["pair_id"] == "pair-001"

    @pytest.mark.asyncio
    async def test_per_device_isolation(self, db_manager):
        buffer = ContextBuffer(db_manager, max_size_bytes=1048576)
        await buffer.add_pair("device-a", make_pair(device_id="device-a", pair_id="a1"))
        await buffer.add_pair("device-b", make_pair(device_id="device-b", pair_id="b1"))
        pairs_a = await buffer.get_buffer_pairs("device-a")
        pair_ids_a = [p["pair"]["pair_id"] for p in pairs_a]
        assert "a1" in pair_ids_a
        assert "b1" not in pair_ids_a

    @pytest.mark.asyncio
    async def test_flush_clears_buffer(self, db_manager):
        buffer = ContextBuffer(db_manager, max_size_bytes=1048576)
        pair = make_pair(request_body="x" * 1000)
        await buffer.add_pair("device-001", pair)
        count = await buffer.flush("device-001")
        assert count == 1
        remaining = await buffer.get_buffer_pairs("device-001")
        assert len(remaining) == 0

    @pytest.mark.asyncio
    async def test_buffer_becomes_full(self, db_manager):
        buffer = ContextBuffer(db_manager, max_size_bytes=100)
        pair = make_pair(request_body="x" * 5000)
        is_full = await buffer.add_pair("device-001", pair)
        assert is_full is True

    @pytest.mark.asyncio
    async def test_get_current_size_zero_on_new_device(self, db_manager):
        buffer = ContextBuffer(db_manager)
        size = await buffer.get_current_size("nonexistent")
        assert size == 0

    @pytest.mark.asyncio
    async def test_sequence_continues_across_restart(self, db_manager):
        """Sequence numbers must not reset when a new ContextBuffer starts.

        Simulates a process restart: a second buffer instance over the same
        device DB must continue numbering from the persisted max, never
        re-using a sequence number (which would break ordering/dedup).
        """
        first = ContextBuffer(db_manager, max_size_bytes=1048576)
        await first.add_pair("device-001", make_pair(pair_id="a1"))
        await first.add_pair("device-001", make_pair(pair_id="a2"))

        second = ContextBuffer(db_manager, max_size_bytes=1048576)  # "restart"
        await second.add_pair("device-001", make_pair(pair_id="a3"))

        async with db_manager.device_session("device-001") as session:
            result = await session.execute(
                select(LLMContextBuffer.sequence)
                .where(LLMContextBuffer.device_id == "device-001")
                .order_by(LLMContextBuffer.sequence)
            )
            rows = list(result.scalars().all())
        assert rows == sorted(set(rows))
        assert len(rows) == 3  # noqa: PLR2004
        assert rows == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_prune_enforces_ttl_and_cap(self, db_manager):
        """prune() must drop flushed rows older than the TTL and cap retained entries."""
        buffer = ContextBuffer(db_manager, max_size_bytes=1048576)
        await buffer.add_pair("device-001", make_pair(pair_id="old"))  # seq 0
        await buffer.flush("device-001")
        # Backdate the flushed row past the TTL (pair_ttl_hours=1 -> cutoff 1h ago).
        async with db_manager.device_session("device-001") as session:
            from sqlalchemy import update as _update

            now = datetime.now(UTC)
            await session.execute(
                _update(LLMContextBuffer)
                .where(
                    (LLMContextBuffer.device_id == "device-001")
                    & (LLMContextBuffer.sequence == 0)
                )
                .values(flushed=True, flushed_at=now - timedelta(hours=5))
            )
        # Add fresh unflushed rows; prune must not touch these.
        await buffer.add_pair("device-001", make_pair(pair_id="fresh1"))
        deleted = await buffer.prune("device-001", max_pairs_per_device=10, pair_ttl_hours=1)
        assert deleted >= 1
        async with db_manager.device_session("device-001") as session:
            result = await session.execute(
                select(LLMContextBuffer).where(LLMContextBuffer.device_id == "device-001")
            )
            rows = list(result.scalars().all())
        assert all(r.sequence != 0 for r in rows)  # stale row gone

    @pytest.mark.asyncio
    async def test_prune_cap_keeps_newest(self, db_manager):
        """When entries exceed the cap, prune keeps only the newest sequences."""
        buffer = ContextBuffer(db_manager, max_size_bytes=1048576)
        for i in range(5):
            await buffer.add_pair("device-001", make_pair(pair_id=f"p{i}"))
        # Flush so the rows are eligible for cap pruning; only consumed
        # (flushed) rows may be dropped, unflushed training rows are never lost.
        await buffer.flush("device-001")
        deleted = await buffer.prune("device-001", max_pairs_per_device=2, pair_ttl_hours=168)
        async with db_manager.device_session("device-001") as session:
            result = await session.execute(
                select(LLMContextBuffer)
                .where(LLMContextBuffer.device_id == "device-001")
                .order_by(LLMContextBuffer.sequence)
            )
            seqs = list(result.scalars().all())
        assert [s.sequence for s in seqs] == [3, 4]  # newest retained; oldest dropped


class TestPatternMatcher:
    @pytest.mark.asyncio
    async def test_init(self, db_manager):
        matcher = PatternMatcher(db_manager)
        assert matcher.db_manager is db_manager

    @pytest.mark.asyncio
    async def test_find_best_match_no_patterns(self, db_manager):
        matcher = PatternMatcher(db_manager)
        pattern, template, score = await matcher.find_best_match(
            "device-001", "POST", "/test", {}, {}, {}
        )
        assert pattern is None
        assert template is None
        assert score == 0.0

    @pytest.mark.asyncio
    async def test_calculate_similarity_with_method_match(self, db_manager):
        matcher = PatternMatcher(db_manager)

        pattern = RequestPattern(
            pattern_id="p1",
            method="POST",
            path_pattern="/test",
            required_headers=[],
            query_param_keys=[],
        )
        score = matcher._calculate_similarity(pattern, "POST", "/other", {}, {}, {})
        assert 0.2 < score < 0.8  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_calculate_similarity_full_match(self, db_manager):
        matcher = PatternMatcher(db_manager)

        pattern = RequestPattern(
            pattern_id="p2",
            method="GET",
            path_pattern="/status",
            required_headers=[],
            query_param_keys=[],
        )
        score = matcher._calculate_similarity(pattern, "GET", "/status", {}, {}, {})
        assert score >= 0.75  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_calculate_similarity_method_mismatch(self, db_manager):
        matcher = PatternMatcher(db_manager)

        pattern = RequestPattern(
            pattern_id="p3",
            method="POST",
            path_pattern="/status",
            required_headers=[],
            query_param_keys=[],
        )
        score = matcher._calculate_similarity(pattern, "GET", "/other", {}, {}, {})
        assert score < 0.30  # noqa: PLR2004


class TestMatchRateTracker:
    @pytest.mark.asyncio
    async def test_init(self, db_manager):
        tracker = MatchRateTracker(db_manager)
        assert tracker._rolling_window == 1000  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_record_result_hit(self, db_manager):
        tracker = MatchRateTracker(db_manager)
        await tracker.record_result("device-001", MatchResult.LOCAL_HIT)

    @pytest.mark.asyncio
    async def test_record_result_miss(self, db_manager):
        tracker = MatchRateTracker(db_manager)
        await tracker.record_result("device-002", MatchResult.CLOUD_MISS)

    @pytest.mark.asyncio
    async def test_record_result_error(self, db_manager):
        tracker = MatchRateTracker(db_manager)
        await tracker.record_result("device-003", MatchResult.ERROR)


class TestLearningPipeline:
    @pytest.mark.asyncio
    async def test_init(self, db_manager):
        llm = MagicMock()
        buffer = ContextBuffer(db_manager)
        matcher = PatternMatcher(db_manager)
        tracker = MatchRateTracker(db_manager)
        pipeline = LearningPipeline(db_manager, llm, buffer, matcher, tracker)
        assert pipeline.db_manager is db_manager
        assert pipeline.buffer is buffer
        assert pipeline.matcher is matcher

    @pytest.mark.asyncio
    async def test_register_request(self, db_manager):
        llm = MagicMock()
        buffer = ContextBuffer(db_manager)
        matcher = PatternMatcher(db_manager)
        tracker = MatchRateTracker(db_manager)
        pipeline = LearningPipeline(db_manager, llm, buffer, matcher, tracker)
        corr_key = await pipeline.register_request(
            "device-001",
            "shelly",
            "http",
            "POST",
            "/rpc/Switch.GetStatus",
            {},
            {},
            {},
        )
        assert corr_key.startswith("device-001")

    @pytest.mark.asyncio
    async def test_match_response_no_match(self, db_manager):
        llm = MagicMock()
        buffer = ContextBuffer(db_manager)
        matcher = PatternMatcher(db_manager)
        tracker = MatchRateTracker(db_manager)
        pipeline = LearningPipeline(db_manager, llm, buffer, matcher, tracker)
        result = await pipeline.match_response(
            "device-nonexistent",
            "shelly",
            "http",
            200,
            {},
            {},
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_match_response_db_fallback_after_restart(self, db_manager):
        """After the in-memory cache is empty (restart), match_response falls
        back to the persisted SessionCache rows.

        Regression for B1: the DB-fallback loop previously read
        ``cache_entry.protocol`` which did not exist on SessionCache, raising
        ``AttributeError`` on any post-restart correlation. Also verifies the
        row is marked correlated (B3), so stale rows don't phantom-match later.
        """
        llm = MagicMock()
        buffer = ContextBuffer(db_manager)
        matcher = PatternMatcher(db_manager)
        tracker = MatchRateTracker(db_manager)
        pipeline = LearningPipeline(db_manager, llm, buffer, matcher, tracker)

        await pipeline.register_request(
            "device-001",
            "shelly",
            "http",
            "POST",
            "/rpc/x",
            {},
            {},
            {},
        )
        # Simulate a process restart: clear the in-memory correlation cache.
        pipeline._correlation_cache.clear()

        # This previously crashed with AttributeError (SessionCache.protocol).
        result = await pipeline.match_response(
            "device-001",
            "shelly",
            "http",
            200,
            {"content-type": "application/json"},
            {"status": "ok"},
        )
        assert result is not None
        assert result.protocol == "http"
        assert result.request_body == {}
        assert result.response_body == {"status": "ok"}

        # B3: the consumed row must now be marked correlated so it can't be
        # re-selected after another restart (which would phantom-match).
        async with db_manager.device_session("device-001") as session:
            rows = (await session.execute(select(SessionCache))).scalars().all()
        # At least the request row is now correlated (B3 fix).
        assert any(r.correlated for r in rows)

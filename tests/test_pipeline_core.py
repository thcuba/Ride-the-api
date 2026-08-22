"""
Tests for the Learning/Production Pipeline.
"""
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from core.pipeline import (
    ContextBuffer,
    CorrelatedPair,
    LearningPipeline,
    MatchRateTracker,
    MatchResult,
    PatternMatcher,
    PipelineMode,
)
from core.database import DatabaseManager


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
        assert len(PipelineMode) == 3


class TestCorrelatedPair:
    def test_pair_creation(self):
        pair = make_pair()
        assert pair.pair_id == "pair-001"
        assert pair.device_id == "device-001"
        assert pair.vendor == "shelly"
        assert pair.method == "POST"
        assert pair.path == "/rpc/Switch.GetStatus"
        assert pair.latency_ms == 12.5


class TestContextBuffer:
    @pytest.mark.asyncio
    async def test_init(self, db_manager):
        buffer = ContextBuffer(db_manager, max_size_bytes=1024)
        assert buffer.max_size_bytes == 1024

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
        await buffer.add_pair(
            "device-a", make_pair(device_id="device-a", pair_id="a1")
        )
        await buffer.add_pair(
            "device-b", make_pair(device_id="device-b", pair_id="b1")
        )
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
        from core.database import RequestPattern
        pattern = RequestPattern(
            pattern_id="p1",
            method="POST",
            path_pattern="/test",
            required_headers=[],
            query_param_keys=[],
        )
        score = matcher._calculate_similarity(
            pattern, "POST", "/other", {}, {}, {}
        )
        assert 0.2 < score < 0.8

    @pytest.mark.asyncio
    async def test_calculate_similarity_full_match(self, db_manager):
        matcher = PatternMatcher(db_manager)
        from core.database import RequestPattern
        pattern = RequestPattern(
            pattern_id="p2",
            method="GET",
            path_pattern="/status",
            required_headers=[],
            query_param_keys=[],
        )
        score = matcher._calculate_similarity(
            pattern, "GET", "/status", {}, {}, {}
        )
        assert score >= 0.75

    @pytest.mark.asyncio
    async def test_calculate_similarity_method_mismatch(self, db_manager):
        matcher = PatternMatcher(db_manager)
        from core.database import RequestPattern
        pattern = RequestPattern(
            pattern_id="p3",
            method="POST",
            path_pattern="/status",
            required_headers=[],
            query_param_keys=[],
        )
        score = matcher._calculate_similarity(
            pattern, "GET", "/other", {}, {}, {}
        )
        assert score < 0.30


class TestMatchRateTracker:
    @pytest.mark.asyncio
    async def test_init(self, db_manager):
        tracker = MatchRateTracker(db_manager)
        assert tracker._rolling_window == 1000

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
        pipeline = LearningPipeline(
            db_manager, llm, buffer, matcher, tracker
        )
        assert pipeline.db_manager is db_manager
        assert pipeline.buffer is buffer
        assert pipeline.matcher is matcher

    @pytest.mark.asyncio
    async def test_register_request(self, db_manager):
        llm = MagicMock()
        buffer = ContextBuffer(db_manager)
        matcher = PatternMatcher(db_manager)
        tracker = MatchRateTracker(db_manager)
        pipeline = LearningPipeline(
            db_manager, llm, buffer, matcher, tracker
        )
        corr_key = await pipeline.register_request(
            "device-001", "shelly", "http",
            "POST", "/rpc/Switch.GetStatus",
            {}, {}, {},
        )
        assert corr_key.startswith("device-001")

    @pytest.mark.asyncio
    async def test_match_response_no_match(self, db_manager):
        llm = MagicMock()
        buffer = ContextBuffer(db_manager)
        matcher = PatternMatcher(db_manager)
        tracker = MatchRateTracker(db_manager)
        pipeline = LearningPipeline(
            db_manager, llm, buffer, matcher, tracker
        )
        result = await pipeline.match_response(
            "device-nonexistent", "shelly", "http",
            200, {}, {},
        )
        assert result is None
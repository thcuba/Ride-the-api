"""
Unit tests for core/pipeline.py covering ContextBuffer, PatternMatcher,
MatchRateTracker, LearningPipeline, and LearningOrchestrator.
"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select

from core.database import (
    DatabaseManager,
    DeviceRegistry,
    FieldMapping,
    RequestPattern,
    ResponseTemplate,
)
from core.pipeline import (
    ContextBuffer,
    CorrelatedPair,
    LearningOrchestrator,
    LearningPipeline,
    MatchRateTracker,
    MatchResult,
    PatternMatcher,
    _redact_pairs_for_prompt,
    get_orchestrator,
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


def make_correlated_pair(**overrides) -> CorrelatedPair:
    defaults = dict(
        pair_id="pair-123",
        device_id="dev-001",
        vendor="shelly",
        protocol="http",
        method="POST",
        path="/rpc/Switch.Set",
        request_headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
        request_body={"id": 1, "params": {"on": True}},
        request_query={"token": "secret123"},
        response_status=200,
        response_headers={"Content-Type": "application/json"},
        response_body={"was_on": False},
        latency_ms=15.0,
        correlation_confidence=0.9,
        timestamp=datetime.now(UTC),
    )
    defaults.update(overrides)
    return CorrelatedPair(**defaults)


# ============================================================================
# 1. Tests for _redact_pairs_for_prompt
# ============================================================================


class TestRedactPairsForPrompt:
    def test_non_list_input(self):
        assert _redact_pairs_for_prompt("not a list") == "not a list"
        assert _redact_pairs_for_prompt(None) is None

    def test_list_with_non_dict_elements(self):
        pairs = ["string", 123, None]
        assert _redact_pairs_for_prompt(pairs) == ["string", 123, None]

    def test_redaction_of_sensitive_fields(self):
        pairs = [
            {
                "request_headers": {"authorization": "Bearer token123"},
                "response_headers": {"set-cookie": "session=abc"},
                "request_query": {"api_key": "secret_key"},
                "request_body": {"password": "my_password"},
                "response_body": {"access_token": "secret_access"},
                "other_field": "unaffected",
            }
        ]
        redacted = _redact_pairs_for_prompt(pairs)
        assert len(redacted) == 1  # noqa: PLR2004
        r = redacted[0]
        assert r["other_field"] == "unaffected"
        assert r["request_headers"]["authorization"] == "[REDACTED]"
        assert r["request_query"]["api_key"] == "[REDACTED]"
        assert r["request_body"]["password"] == "[REDACTED]"


# ============================================================================
# 2. Tests for ContextBuffer
# ============================================================================


class TestContextBufferMethods:
    @pytest.mark.asyncio
    async def test_flush_selected(self, db_manager):
        buffer = ContextBuffer(db_manager, max_size_bytes=1024 * 1024)
        pair1 = make_correlated_pair(pair_id="p1")
        pair2 = make_correlated_pair(pair_id="p2")
        await buffer.add_pair("dev-001", pair1)
        await buffer.add_pair("dev-001", pair2)

        pairs = await buffer.get_buffer_pairs("dev-001")
        assert len(pairs) == 2  # noqa: PLR2004
        p1_id = pairs[0]["id"]

        flushed_count = await buffer.flush_selected("dev-001", [p1_id])
        assert flushed_count == 1

        remaining = await buffer.get_buffer_pairs("dev-001")
        assert len(remaining) == 1  # noqa: PLR2004
        assert remaining[0]["pair"]["pair_id"] == "p2"

    @pytest.mark.asyncio
    async def test_delete_entry(self, db_manager):
        buffer = ContextBuffer(db_manager)
        pair = make_correlated_pair(pair_id="p-del")
        await buffer.add_pair("dev-001", pair)

        pairs = await buffer.get_buffer_pairs("dev-001")
        entry_id = pairs[0]["id"]

        deleted = await buffer.delete_entry("dev-001", entry_id)
        assert deleted is True

        remaining = await buffer.get_buffer_pairs("dev-001")
        assert len(remaining) == 0  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_clear_cache(self, db_manager):
        buffer = ContextBuffer(db_manager)
        await buffer.clear_cache("dev-001")


# ============================================================================
# 3. Tests for PatternMatcher
# ============================================================================


class TestPatternMatcherExtended:
    @pytest.mark.asyncio
    async def test_resolve_field_and_nested_helpers(self, db_manager):
        matcher = PatternMatcher(db_manager)

        data = {"user": {"profile": {"name": "Alice", "tags": ["admin", "user"]}}}
        assert matcher._get_nested(data, "user.profile.name") == "Alice"
        assert matcher._get_nested(data, "user.profile.tags.0") == "admin"
        assert matcher._get_nested(data, "nonexistent.path") is None

        d = {}
        matcher._set_nested(d, "config.setting.value", 42)  # noqa: PLR2004
        assert d["config"]["setting"]["value"] == 42  # noqa: PLR2004

        body = {"temp": 22.5}
        headers = {"X-Trace-ID": "abc123"}
        query = {"lang": "en"}

        assert matcher._resolve_field("body.temp", body, headers, query) == 22.5  # noqa: PLR2004
        assert matcher._resolve_field("headers.X-Trace-ID", body, headers, query) == "abc123"
        assert matcher._resolve_field("query.lang", body, headers, query) == "en"
        assert matcher._resolve_field("unknown.key", body, headers, query) is None

    @pytest.mark.asyncio
    async def test_build_local_response_with_field_mappings(self, db_manager):
        dev_id = "dev-matcher"
        await db_manager.get_or_create_device(dev_id, "shelly")
        matcher = PatternMatcher(db_manager)

        async with db_manager.device_session(dev_id) as session:
            pattern = RequestPattern(
                pattern_id="p1",
                method="POST",
                path_pattern="/status",
                protocol="http",
                intent="get_status",
            )
            mapping1 = FieldMapping(
                mapping_id="m1",
                request_field="body.power",
                request_type="boolean",
                response_field="state.active",
                response_type="boolean",
                transform="direct",
                intent="get_status",
            )
            mapping2 = FieldMapping(
                mapping_id="m2",
                request_field="body.mode",
                request_type="integer",
                response_field="state.mode_label",
                response_type="string",
                transform="enum_map",
                enum_values={"1": "LOW", "2": "HIGH"},
                intent="get_status",
            )
            session.add_all([pattern, mapping1, mapping2])

        template = ResponseTemplate(
            template_id="tpl_p1",
            pattern_id="p1",
            status_code=200,
            headers_template={"Content-Type": "application/json"},
            body_template={"state": {}},
            expected_variables=["state.missing_var"],
        )

        orig_req = {"body": {"power": True, "mode": 2}}
        res = await matcher.build_local_response(dev_id, template, orig_req)

        assert res["status_code"] == 200  # noqa: PLR2004
        assert res["headers"] == {"Content-Type": "application/json"}
        assert res["body"]["state"]["active"] is True
        assert res["body"]["state"]["mode_label"] == "HIGH"
        assert res["body"]["state"]["missing_var"] == 0

    @pytest.mark.asyncio
    async def test_find_best_match_returns_pattern_and_template(self, db_manager):
        dev_id = "dev-match-search"
        await db_manager.get_or_create_device(dev_id, "shelly")
        matcher = PatternMatcher(db_manager)

        async with db_manager.device_session(dev_id) as session:
            pattern = RequestPattern(
                pattern_id="pat-1",
                method="POST",
                path_pattern="/api/v1/status",
                protocol="http",
                intent="check_status",
                required_headers=["content-type"],
                query_param_keys=["verbose"],
                body_schema={"type": "object"},
            )
            template = ResponseTemplate(
                template_id="tpl_pat-1",
                pattern_id="pat-1",
                status_code=200,
                headers_template={"content-type": "application/json"},
                body_template={"status": "ok"},
            )
            session.add_all([pattern, template])

        best_pattern, best_template, score = await matcher.find_best_match(
            dev_id,
            "POST",
            "/api/v1/status",
            {"content-type": "application/json"},
            {"type": "check"},
            {"verbose": "true"},
        )

        assert best_pattern is not None
        assert best_pattern.pattern_id == "pat-1"
        assert best_template is not None
        assert best_template.template_id == "tpl_pat-1"
        assert score >= 0.5  # noqa: PLR2004


# ============================================================================
# 4. Tests for MatchRateTracker
# ============================================================================


class TestMatchRateTrackerExtended:
    @pytest.mark.asyncio
    async def test_get_stats_empty_device(self, db_manager):
        tracker = MatchRateTracker(db_manager)
        stats = await tracker.get_stats("nonexistent-dev")
        assert stats["total_requests"] == 0  # noqa: PLR2004
        assert stats["match_rate_pct"] == 0.0  # noqa: PLR2004
        assert stats["recent_results"] == []  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_record_result_and_calculate_match_rate(self, db_manager):
        tracker = MatchRateTracker(db_manager)
        dev_id = "dev-tracker-calc"

        await tracker.record_result(dev_id, MatchResult.LOCAL_HIT)
        await tracker.record_result(dev_id, MatchResult.LOCAL_HIT)
        await tracker.record_result(dev_id, MatchResult.CLOUD_MISS)
        await tracker.record_result(dev_id, MatchResult.ERROR)

        stats = await tracker.get_stats(dev_id)
        assert stats["total_requests"] == 4  # noqa: PLR2004
        assert stats["local_hits"] == 2  # noqa: PLR2004
        assert stats["cloud_misses"] == 1  # noqa: PLR2004
        assert stats["errors"] == 1  # noqa: PLR2004
        assert stats["match_rate_pct"] == 66.67  # noqa: PLR2004
        assert len(stats["recent_results"]) == 4  # noqa: PLR2004


# ============================================================================
# 5. Tests for LearningPipeline
# ============================================================================


class TestLearningPipelineExtended:
    @pytest.mark.asyncio
    async def test_flush_and_learn_empty_buffer(self, db_manager):
        llm = MagicMock()
        buffer = ContextBuffer(db_manager)
        matcher = PatternMatcher(db_manager)
        tracker = MatchRateTracker(db_manager)
        pipeline = LearningPipeline(db_manager, llm, buffer, matcher, tracker)

        res = await pipeline.flush_and_learn("dev-empty")
        assert res["success"] is False
        assert res["error"] == "No buffer entries to flush"

    @pytest.mark.asyncio
    async def test_flush_and_learn_device_not_found(self, db_manager):
        llm = MagicMock()
        buffer = ContextBuffer(db_manager)
        matcher = PatternMatcher(db_manager)
        tracker = MatchRateTracker(db_manager)
        pipeline = LearningPipeline(db_manager, llm, buffer, matcher, tracker)

        await buffer.add_pair("dev-unknown", make_correlated_pair(device_id="dev-unknown"))
        res = await pipeline.flush_and_learn("dev-unknown")
        assert res["success"] is False
        assert res["error"] == "Device not found"

    @pytest.mark.asyncio
    async def test_preview_analysis(self, db_manager):
        dev_id = "dev-preview"
        await db_manager.get_or_create_device(dev_id, "shelly")

        llm = MagicMock()
        profile = MagicMock()
        profile.prompt_template = "Analyze pairs: {pairs}"
        profile.base_url = "http://llm.test"
        profile.model_id = "gpt-4"
        llm.get_profile.return_value = profile
        llm.call_profile_chain = AsyncMock(
            return_value={"success": True, "content": '{"patterns": [{"intent": "get_status"}]}'}
        )

        buffer = ContextBuffer(db_manager)
        matcher = PatternMatcher(db_manager)
        tracker = MatchRateTracker(db_manager)
        pipeline = LearningPipeline(db_manager, llm, buffer, matcher, tracker)

        await buffer.add_pair(dev_id, make_correlated_pair(device_id=dev_id))

        res = await pipeline.preview_analysis(dev_id)
        assert res["success"] is True
        assert res["pairs_count"] == 1
        assert "analysis" in res

    @pytest.mark.asyncio
    async def test_save_patterns_v2_delta(self, db_manager):
        dev_id = "dev-v2-delta"
        await db_manager.get_or_create_device(dev_id, "shelly")

        llm = MagicMock()
        buffer = ContextBuffer(db_manager)
        matcher = PatternMatcher(db_manager)
        tracker = MatchRateTracker(db_manager)
        pipeline = LearningPipeline(db_manager, llm, buffer, matcher, tracker)

        analysis_v2 = {
            "commands": [
                {
                    "id": "cmd-1",
                    "kind": "query",
                    "protocol": "http",
                    "method": "POST",
                    "path": "/rpc/Sys.GetStatus",
                    "confidence": 0.9,
                }
            ],
            "responses": [
                {
                    "id": "resp-1",
                    "triggers": ["query"],
                    "status_code": 200,
                    "headers_template": {"content-type": "application/json"},
                    "body_template": {"sys": {"uptime": 100}},
                }
            ],
        }

        await pipeline._save_patterns(dev_id, analysis_v2)

        async with db_manager.device_session(dev_id) as session:
            patterns = (await session.execute(select(RequestPattern))).scalars().all()
            templates = (await session.execute(select(ResponseTemplate))).scalars().all()
            assert len(patterns) >= 1  # noqa: PLR2004
            assert len(templates) >= 1  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_persist_device_meta(self, db_manager):
        dev_id = "dev-meta-test"
        await db_manager.get_or_create_device(dev_id, "shelly")

        async with db_manager.core_session() as session:
            device = (
                await session.execute(
                    select(DeviceRegistry).where(DeviceRegistry.device_id == dev_id)
                )
            ).scalar_one()

        llm = MagicMock()
        buffer = ContextBuffer(db_manager)
        matcher = PatternMatcher(db_manager)
        tracker = MatchRateTracker(db_manager)
        pipeline = LearningPipeline(db_manager, llm, buffer, matcher, tracker)

        analysis = {
            "protocol_info": {
                "transport": "tcp",
                "protocol": "http",
                "proprietary": False,
                "security": "none",
                "handler": "http",
                "identity": "Shelly Plug S",
                "ports": [80],
                "confidence": 0.95,
            },
            "protocols": ["http"],
            "connection_mode": "http",
        }

        await pipeline._persist_device_meta(dev_id, device, analysis)
        stored_meta = await db_manager.read_device_meta(dev_id)
        assert stored_meta is not None
        assert stored_meta["connection_mode"] == "http"
        assert stored_meta["protocols"] == ["http"]


# ============================================================================
# 6. Tests for LearningOrchestrator
# ============================================================================


class TestLearningOrchestratorExtended:
    def test_device_id_from_pattern_file(self, tmp_path):
        p1 = tmp_path / "dev123-patterns.ride-pattern.json"
        p1.write_text(json.dumps({"meta": {"pattern_id": "dev123-patterns"}}))
        assert LearningOrchestrator._device_id_from_pattern_file(p1) == "dev123"

        p2 = tmp_path / "custom_name.ride-pattern.json"
        p2.write_text(json.dumps({"meta": {"pattern_id": "custom_name"}}))
        assert LearningOrchestrator._device_id_from_pattern_file(p2) == "custom_name"

    @pytest.mark.asyncio
    async def test_handle_request_hybrid_mode_miss(self, db_manager):
        dev_id = "dev-hybrid-miss"
        await db_manager.get_or_create_device(dev_id, "shelly")
        await db_manager.update_device_mode(dev_id, "hybrid")

        orch = LearningOrchestrator(db_manager)
        orch.initialize(MagicMock())

        res = await orch.handle_request(
            dev_id,
            "shelly",
            "http",
            "GET",
            "/unmatched/path",
            {},
            {},
            {},
        )
        assert res["action"] == "forward"
        assert res["mode"] == "hybrid"
        assert "correlation_key" in res

    @pytest.mark.asyncio
    async def test_handle_request_production_no_fallback(self, db_manager):
        dev_id = "dev-prod-nofallback"
        await db_manager.get_or_create_device(dev_id, "shelly")
        await db_manager.update_device_mode(dev_id, "production")

        async with db_manager.core_session() as session:
            row = (
                await session.execute(
                    select(DeviceRegistry).where(DeviceRegistry.device_id == dev_id)
                )
            ).scalar_one()
            row.config = {"production_no_fallback": True}
            await session.commit()

        orch = LearningOrchestrator(db_manager)
        orch.initialize(MagicMock())

        res = await orch.handle_request(
            dev_id,
            "shelly",
            "http",
            "GET",
            "/unmatched/path",
            {},
            {},
            {},
        )
        assert res["action"] == "no_fallback"
        assert res["reason"] == "no_pattern"

    @pytest.mark.asyncio
    async def test_handle_response_learning_mode(self, db_manager):
        dev_id = "dev-response-learn"
        await db_manager.get_or_create_device(dev_id, "shelly")
        await db_manager.update_device_mode(dev_id, "learning")

        orch = LearningOrchestrator(db_manager)
        orch.initialize(MagicMock())

        async with db_manager.core_session() as session:
            device = (
                await session.execute(
                    select(DeviceRegistry).where(DeviceRegistry.device_id == dev_id)
                )
            ).scalar_one()

        corr_key = await orch._register_for_learning(
            device,
            "http",
            "GET",
            "/status",
            {},
            {},
            {},
        )
        assert corr_key is not None

        res = await orch.handle_response(
            dev_id,
            "shelly",
            "http",
            200,
            {"content-type": "application/json"},
            {"wifi": "connected"},
        )
        assert res["action"] == "buffered_for_learning"
        assert "pair_id" in res

    @pytest.mark.asyncio
    async def test_get_device_stats(self, db_manager):
        dev_id = "dev-stats-test"
        await db_manager.get_or_create_device(dev_id, "shelly", name="Shelly Plug")

        orch = LearningOrchestrator(db_manager)
        orch.initialize(MagicMock())

        stats = await orch.get_device_stats(dev_id)
        assert stats["name"] == "Shelly Plug"  # noqa: PLR2004
        assert stats["mode"] == "learning"  # noqa: PLR2004
        assert "total_requests" in stats

    def test_get_orchestrator_singleton(self, db_manager):
        with patch("core.pipeline.get_db_manager", return_value=db_manager):
            orch1 = get_orchestrator()
            orch2 = get_orchestrator()
            assert orch1 is orch2

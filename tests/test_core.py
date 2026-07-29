"""
Tests for the Local Cloud Replacement Proxy architecture.
"""

import pytest
import pytest_asyncio
from pathlib import Path
import tempfile
import json
from datetime import datetime, timezone

from core.database import (
    DatabaseManager, Base, init_db_manager, get_db_manager,
    DeviceRegistry, RequestPattern, ResponseTemplate, FieldMapping,
    MatchStats, SessionCache, LLMContextBuffer,
)
from core.pipeline import (
    ContextBuffer, PatternMatcher, MatchRateTracker,
    LearningPipeline, LearningOrchestrator, CorrelatedPair,
    MatchResult, PipelineMode,
)
from core.llm_decipher import LLMDecipherService, LLMProfile
from sqlalchemy import select


# ═══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def db_manager():
    """Create a test database manager with temporary SQLite databases."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        core_db = f"sqlite+aiosqlite:///{tmpdir}/core.db"
        device_db_dir = tmpdir / "devices"

        manager = DatabaseManager(
            core_db_url=core_db,
            device_db_dir=device_db_dir,
            echo=False,
        )
        await manager.initialize()

        yield manager

        await manager.close()


@pytest_asyncio.fixture
async def device_db(db_manager):
    """Get a device database session with a registered device."""
    await db_manager.get_or_create_device(
        "test_device_001", "example", "ac", "Test AC"
    )
    return db_manager


@pytest_asyncio.fixture
def sample_correlated_pair():
    """Create a sample correlated pair for testing."""
    return CorrelatedPair(
        pair_id="test_pair_001",
        device_id="test_device_001",
        vendor="example",
        protocol="http",
        method="POST",
        path="/v1.0/device/test_device_001/commands",
        request_headers={"Content-Type": "application/json"},
        request_body={"commands": [{"code": "temp_set", "value": 240}]},
        request_query={},
        response_status=200,
        response_headers={"Content-Type": "application/json"},
        response_body={"result": {"data": {"1": False, "3": 240}}},
        latency_ms=150.0,
        correlation_confidence=0.9,
        timestamp=datetime.now(timezone.utc),
    )


@pytest_asyncio.fixture
async def sample_patterns(device_db):
    """Create sample patterns in the device database."""
    device_id = "test_device_001"
    async with device_db.device_session(device_id) as session:
        pattern = RequestPattern(
            pattern_id="pat_001",
            method="POST",
            path_pattern="/v1.0/device/{id}/commands",
            protocol="http",
            required_headers=["Content-Type"],
            body_schema={"commands": [{"code": "temp_set", "value": 240}]},
            query_param_keys=[],
            intent="set_temperature",
            confidence=0.9,
        )
        session.add(pattern)

        template = ResponseTemplate(
            template_id="tpl_001",
            pattern_id="pat_001",
            status_code=200,
            headers_template={"Content-Type": "application/json"},
            body_template={"result": {"data": {"1": False, "3": 240}}},
            field_mappings={"body.commands.0.value": "result.data.3"},
            expected_variables=["result.data.3"],
            confidence=0.85,
        )
        session.add(template)

        mapping = FieldMapping(
            mapping_id="map_001",
            request_field="body.commands.0.value",
            request_type="integer",
            response_field="result.data.3",
            response_type="integer",
            transform="direct",
            intent="set_temperature",
            confidence=0.9,
        )
        session.add(mapping)

        stats = MatchStats(
            device_id=device_id,
            total_requests=10,
            local_hits=7,
            cloud_misses=3,
            match_rate_pct=70.0,
            patterns_learned=1,
            templates_created=1,
        )
        session.add(stats)
    return device_id


# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestCoreDatabase:
    """Test core database operations."""

    async def test_core_db_initialization(self, db_manager):
        """Test core database tables are created."""
        async with db_manager.core_session() as session:
            result = await session.execute(select(DeviceRegistry))
            devices = result.scalars().all()
            assert isinstance(devices, list)

    async def test_device_registry_crud(self, db_manager):
        """Test device registry CRUD operations."""
        async with db_manager.core_session() as session:
            device = DeviceRegistry(
                device_id="test_device_001",
                vendor="example",
                device_type="ac",
                name="Test AC",
                mode="learning",
                context_buffer_size=524288,
            )
            session.add(device)
            await session.commit()
            await session.refresh(device)

            assert device.id is not None
            assert device.device_id == "test_device_001"
            assert device.mode == "learning"

            # Read
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == "test_device_001")
            )
            found = result.scalar_one_or_none()
            assert found is not None
            assert found.name == "Test AC"

            # Update mode
            found.mode = "production"
            await session.commit()
            await session.refresh(found)
            assert found.mode == "production"

    async def test_get_or_create_device(self, db_manager):
        """Test device auto-registration."""
        await db_manager.get_or_create_device("new_device", "example", "heat_pump", "New HP")
        async with db_manager.core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == "new_device")
            )
            device = result.scalar_one_or_none()
            assert device is not None
            assert device.vendor == "example"
            assert device.device_type == "heat_pump"
            assert device.mode == "learning"  # default mode

    async def test_update_device_mode(self, db_manager):
        """Test device mode switching."""
        await db_manager.get_or_create_device("mode_test", "example", "ac", "Mode Test")
        success = await db_manager.update_device_mode("mode_test", "production")
        assert success is True

        async with db_manager.core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == "mode_test")
            )
            device = result.scalar_one_or_none()
            assert device.mode == "production"

    async def test_device_databases_created(self, db_manager):
        """Test that device-specific databases are created."""
        await db_manager.get_or_create_device("db_test_dev", "example", "ac", "DB Test")
        engine = await db_manager.get_device_engine("db_test_dev")
        assert engine is not None

        # Verify tables exist in device DB
        async with db_manager.device_session("db_test_dev") as session:
            result = await session.execute(select(RequestPattern))
            assert result.scalars().all() == []

    async def test_list_devices(self, db_manager):
        """Test listing devices."""
        await db_manager.get_or_create_device("list_dev_1", "example", "ac", "Device 1")
        await db_manager.get_or_create_device("list_dev_2", "example", "heat_pump", "Device 2")
        devices = await db_manager.list_devices()
        assert len(devices) == 2
        assert any(d["device_id"] == "list_dev_1" for d in devices)
        assert any(d["device_id"] == "list_dev_2" for d in devices)

    async def test_update_llm_config(self, db_manager):
        """Test updating LLM config per device."""
        await db_manager.get_or_create_device("llm_test", "example", "ac", "LLM Test")
        success = await db_manager.update_device_llm_config(
            "llm_test",
            base_url="http://localhost:11434/v1",
            model_id="llama3.1:8b",
        )
        assert success is True

        async with db_manager.core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == "llm_test")
            )
            device = result.scalar_one_or_none()
            assert device.llm_base_url == "http://localhost:11434/v1"
            assert device.llm_model_id == "llama3.1:8b"


# ═══════════════════════════════════════════════════════════════════════════════
# PIPELINE TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestContextBuffer:
    """Test context buffer operations."""

    async def test_buffer_add_and_check_size(self, db_manager, sample_correlated_pair):
        """Test adding pairs to buffer and checking size."""
        await db_manager.get_or_create_device("test_device_001", "example", "ac", "Test AC")
        buffer = ContextBuffer(db_manager, max_size_bytes=1048576)  # 1MB buffer

        needs_flush = await buffer.add_pair("test_device_001", sample_correlated_pair)
        assert needs_flush is False  # 1MB buffer shouldn't fill from one pair

        size = await buffer.get_current_size("test_device_001")
        assert size > 0

    async def test_buffer_flush(self, db_manager, sample_correlated_pair):
        """Test buffer flush."""
        await db_manager.get_or_create_device("test_device_001", "example", "ac", "Test AC")
        buffer = ContextBuffer(db_manager, max_size_bytes=1)  # Tiny buffer forces flush

        needs_flush = await buffer.add_pair("test_device_001", sample_correlated_pair)
        assert needs_flush is True

        count = await buffer.flush("test_device_001")
        assert count > 0

        await buffer.clear_cache("test_device_001")

        # Verify buffer is empty
        pairs = await buffer.get_buffer_pairs("test_device_001")
        assert len(pairs) == 0


class TestPatternMatcher:
    """Test pattern matching engine."""

    async def test_find_best_match(self, db_manager, sample_patterns):
        """Test finding best matching pattern."""
        matcher = PatternMatcher(db_manager)
        pattern, template, score = await matcher.find_best_match(
            "test_device_001",
            method="POST",
            path="/v1.0/device/test_device_001/commands",
            headers={"Content-Type": "application/json"},
            body={"commands": [{"code": "temp_set", "value": 240}]},
            query_params={},
        )

        assert pattern is not None
        assert template is not None
        assert score >= 0.8

    async def test_no_match(self, db_manager, sample_patterns):
        """Test no match for different request."""
        matcher = PatternMatcher(db_manager)
        pattern, template, score = await matcher.find_best_match(
            "test_device_001",
            method="GET",
            path="/v1.0/device/test_device_001/status",
            headers={},
            body=None,
            query_params={},
        )

        # Should find some match but with lower score
        assert pattern is not None
        assert score < 0.8

    async def test_build_local_response(self, db_manager, sample_patterns):
        """Test building local response from template."""
        matcher = PatternMatcher(db_manager)
        pattern, template, _ = await matcher.find_best_match(
            "test_device_001",
            method="POST",
            path="/v1.0/device/test_device_001/commands",
            headers={"Content-Type": "application/json"},
            body={"commands": [{"code": "temp_set", "value": 240}]},
            query_params={},
        )

        response = await matcher.build_local_response(
            "test_device_001",
            template,
            {"body": {"commands": [{"code": "temp_set", "value": 240}], "headers": {}}, "query_params": {}},
        )

        assert response["status_code"] == 200
        assert "body" in response


class TestMatchRateTracker:
    """Test match rate tracking."""

    async def test_record_and_get_stats(self, db_manager):
        """Test recording match results and getting stats."""
        await db_manager.get_or_create_device("tracker_test", "example", "ac", "Tracker Test")
        tracker = MatchRateTracker(db_manager)

        # Record some hits and misses
        for _ in range(7):
            await tracker.record_result("tracker_test", MatchResult.LOCAL_HIT)
        for _ in range(3):
            await tracker.record_result("tracker_test", MatchResult.CLOUD_MISS)

        stats = await tracker.get_stats("tracker_test")
        assert stats["total_requests"] == 10
        assert stats["local_hits"] == 7
        assert stats["cloud_misses"] == 3
        assert stats["match_rate_pct"] == 70.0

    async def test_empty_stats(self, db_manager):
        """Test stats for device with no requests."""
        await db_manager.get_or_create_device("empty_test", "example", "ac", "Empty Test")
        tracker = MatchRateTracker(db_manager)
        stats = await tracker.get_stats("empty_test")
        assert stats["total_requests"] == 0
        assert stats["match_rate_pct"] == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# PROTOCOL ADAPTER TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestProtocolAdapter:
    """Test protocol adapter functionality (from adapters/example)."""

    @pytest.fixture
    def example_adapter(self):
        from adapters.example import ExampleProtocolAdapter
        return ExampleProtocolAdapter("example", {"region": "eu", "api_version": "v1.0"})

    def test_supported_protocols(self, example_adapter):
        """Test supported protocols."""
        protocols = example_adapter.supported_protocols
        from adapters.base import ProtocolType
        assert ProtocolType.MQTT in protocols
        assert ProtocolType.HTTPS in protocols

    def test_vendor_hostnames(self, example_adapter):
        """Test vendor hostnames."""
        hostnames = example_adapter.vendor_hostnames
        assert "mqtt.example.com" in hostnames
        assert "api.example.com" in hostnames

    def test_mode_mapping(self, example_adapter):
        """Test mode mapping."""
        assert example_adapter.MODE_VENDOR_TO_STD["cold"] == "cool"
        assert example_adapter.MODE_VENDOR_TO_STD["hot"] == "heat"
        assert example_adapter.MODE_STD_TO_VENDOR["cool"] == "cold"
        assert example_adapter.MODE_STD_TO_VENDOR["heat"] == "hot"

    @pytest.mark.asyncio
    async def test_parse_mqtt_request(self, example_adapter):
        """Test parsing MQTT request."""
        from adapters.base import InterceptedRequest, ProtocolType
        from datetime import datetime, timezone
        request = InterceptedRequest(
            device_id="",
            timestamp=datetime.now(timezone.utc),
            protocol=ProtocolType.MQTT,
            topic="thing/command/device_123",
            body={"data": {"1": True, "3": 240}},
        )
        parsed = await example_adapter.parse_request(request)
        assert parsed.device_id == "device_123"
        assert parsed.parsed_params.get("power") is True
        assert parsed.parsed_params.get("temp_set") == 240

    @pytest.mark.asyncio
    async def test_parse_http_request(self, example_adapter):
        """Test parsing HTTP request."""
        from adapters.base import InterceptedRequest, ProtocolType
        from datetime import datetime, timezone
        request = InterceptedRequest(
            device_id="",
            timestamp=datetime.now(timezone.utc),
            protocol=ProtocolType.HTTPS,
            method="POST",
            path="/v1.0/devices/device_456/commands",
            body={"commands": [{"code": "mode", "value": "cold"}]},
        )
        parsed = await example_adapter.parse_request(request)
        assert parsed.device_id == "device_456"
        assert parsed.parsed_params.get("mode") == "cold"


# ═══════════════════════════════════════════════════════════════════════════════
# RUN TESTS
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
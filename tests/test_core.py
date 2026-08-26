"""
Tests for the Local Cloud Replacement Proxy architecture.
"""

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select

from adapters.base import InterceptedRequest, ProtocolType, device_id_from_ip
from adapters.example import ExampleProtocolAdapter
from core.database import (
    DatabaseManager,
    DeviceRegistry,
    FieldMapping,
    MatchStats,
    RequestPattern,
    ResponseTemplate,
)
from core.pipeline import (
    ContextBuffer,
    CorrelatedPair,
    MatchRateTracker,
    MatchResult,
    PatternMatcher,
)
from core.resilience import (
    AUTO_SWITCH_MATCH_RATE,
    MIN_PATTERNS_FOR_SWITCH,
    MIN_TOTAL_REQUESTS,
    ROLLBACK_MATCH_RATE,
    CloudIndependenceVerifier,
)

# ═══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def db_manager():
    """Create a test database manager with temporary SQLite databases."""
    with tempfile.TemporaryDirectory() as raw_tmpdir:
        tmpdir = Path(raw_tmpdir)
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
    await db_manager.get_or_create_device("test_device_001", "example", "ac", "Test AC")
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
        timestamp=datetime.now(UTC),
    )


def test_device_id_from_ip():
    """Dots in an IPv4 are replaced so the id is a safe single component."""
    assert device_id_from_ip("raw", "192.168.1.10") == "raw-192-168-1-10"
    assert device_id_from_ip("h2", "10.0.0.1") == "h2-10-0-0-1"
    assert device_id_from_ip("ip", "172.16.5.2") == "ip-172-16-5-2"


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
        assert len(devices) == 2  # noqa: PLR2004
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


_MATCH_FLOOR = 0.8


class TestPatternMatcher:
    """Test pattern matching engine."""

    async def test_find_best_match(self, db_manager, sample_patterns):  # noqa: ARG002
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
        assert score >= _MATCH_FLOOR

    async def test_no_match(self, db_manager, sample_patterns):  # noqa: ARG002
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
        assert score < _MATCH_FLOOR

    async def test_build_local_response(self, db_manager, sample_patterns):  # noqa: ARG002
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
            {
                "body": {"commands": [{"code": "temp_set", "value": 240}], "headers": {}},
                "query_params": {},
            },
        )

        assert response["status_code"] == 200  # noqa: PLR2004
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
        assert stats["total_requests"] == 10  # noqa: PLR2004
        assert stats["local_hits"] == 7  # noqa: PLR2004
        assert stats["cloud_misses"] == 3  # noqa: PLR2004
        assert stats["match_rate_pct"] == 70.0  # noqa: PLR2004

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
        return ExampleProtocolAdapter("example", {"region": "eu", "api_version": "v1.0"})

    def test_supported_protocols(self, example_adapter):
        """Test supported protocols."""
        protocols = example_adapter.supported_protocols

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
        request = InterceptedRequest(
            device_id="",
            timestamp=datetime.now(UTC),
            protocol=ProtocolType.MQTT,
            topic="thing/command/device_123",
            body={"data": {"1": True, "3": 240}},
        )
        parsed = await example_adapter.parse_request(request)
        assert parsed.device_id == "device_123"
        assert parsed.parsed_params.get("power") is True
        assert parsed.parsed_params.get("temp_set") == 240  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_parse_http_request(self, example_adapter):
        """Test parsing HTTP request."""
        request = InterceptedRequest(
            device_id="",
            timestamp=datetime.now(UTC),
            protocol=ProtocolType.HTTPS,
            method="POST",
            path="/v1.0/devices/device_456/commands",
            body={"commands": [{"code": "mode", "value": "cold"}]},
        )
        parsed = await example_adapter.parse_request(request)
        assert parsed.device_id == "device_456"
        assert parsed.parsed_params.get("mode") == "cold"

    @pytest.mark.asyncio
    async def test_forward_to_cloud_uses_host_header(self, example_adapter):
        """Host header wins over the default vendor host."""
        req = InterceptedRequest(
            device_id="",
            timestamp=datetime.now(UTC),
            protocol=ProtocolType.HTTPS,
            method="GET",
            path="/status",
            headers={"Host": "openapi.example.com:443"},
        )
        with patch(
            "core.cloud_forward.resolve_upstream",
            AsyncMock(return_value=[]),
        ):
            result = await example_adapter.forward_to_cloud(req)
        assert result.success is False
        assert result.forwarded is True
        assert "no address found" in (result.error or "")

    @pytest.mark.asyncio
    async def test_forward_to_cloud_default_host(self):
        """No config hostname / Host header -> vendor default host is used."""
        adapter = ExampleProtocolAdapter("example", {})
        req = InterceptedRequest(
            device_id="",
            timestamp=datetime.now(UTC),
            protocol=ProtocolType.HTTPS,
            method="GET",
            path="/v1.0/devices/device_456/status",
        )
        with patch(
            "core.cloud_forward.resolve_upstream",
            AsyncMock(return_value=[]),
        ):
            result = await adapter.forward_to_cloud(req)
        # Falls back to the default api.example.com; empty upstream -> graceful fail.
        assert result.success is False
        assert result.forwarded is True
        assert "no address found" in (result.error or "")

    @pytest.mark.asyncio
    async def test_forward_to_cloud_uses_config_endpoint(self):
        """Configured api_endpoint host is used when no Host header is present."""
        adapter = ExampleProtocolAdapter(
            "example",
            {"cloud": {"api_endpoint": "https://openapi.example.com"}},
        )
        req = InterceptedRequest(
            device_id="",
            timestamp=datetime.now(UTC),
            protocol=ProtocolType.HTTPS,
            method="GET",
            path="/status",
        )
        with patch(
            "core.cloud_forward.resolve_upstream",
            AsyncMock(return_value=[]),
        ):
            result = await adapter.forward_to_cloud(req)
        assert result.success is False
        assert result.forwarded is True
        assert "no address found" in (result.error or "")


# RESILIENCE / AUTO-SWITCH TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_auto_switch_disabled_by_default(db_manager):
    """Auto-switch should be disabled by default for new devices."""
    await db_manager.get_or_create_device("test_auto_001", "example", "ac", "Test Auto")
    devices = await db_manager.list_devices()
    for d in devices:
        if d["device_id"] == "test_auto_001":
            assert d.get("auto_switch_enabled") is False
            return
    pytest.fail("Device not found")


@pytest.mark.asyncio
async def test_update_device_auto_switch(db_manager):
    """Test toggling auto-switch for a device."""
    await db_manager.get_or_create_device("test_auto_002", "example", "ac", "Test Auto")
    result = await db_manager.update_device_auto_switch("test_auto_002", True)
    assert result is True
    devices = await db_manager.list_devices()
    for d in devices:
        if d["device_id"] == "test_auto_002":
            assert d.get("auto_switch_enabled") is True
            return
    pytest.fail("Device not found")


@pytest.mark.asyncio
async def test_update_device_auto_switch_not_found(db_manager):
    """Test toggling auto-switch for a non-existent device returns False."""
    result = await db_manager.update_device_auto_switch("nonexistent", True)
    assert result is False


@pytest.mark.asyncio
async def test_auto_switch_threshold_constants():
    """Verify the auto-switch thresholds are set correctly."""
    assert AUTO_SWITCH_MATCH_RATE == 99.0  # noqa: PLR2004
    assert ROLLBACK_MATCH_RATE == 90.0  # noqa: PLR2004
    assert MIN_TOTAL_REQUESTS >= 50  # noqa: PLR2004
    assert MIN_PATTERNS_FOR_SWITCH >= 10  # noqa: PLR2004


@pytest.mark.asyncio
async def test_auto_switch_to_production_disabled(device_db, sample_patterns):
    """Auto-switch should not happen when auto_switch_enabled is False."""

    verifier = CloudIndependenceVerifier(device_db)
    result = await verifier.auto_switch_to_production(sample_patterns)
    assert result is False  # auto_switch_enabled is False by default


@pytest.mark.asyncio
async def test_auto_switch_to_production_enabled_but_low_match(device_db, sample_patterns):
    """Auto-switch should not happen when match rate is below 99%."""

    await device_db.update_device_auto_switch(sample_patterns, True)
    verifier = CloudIndependenceVerifier(device_db)
    result = await verifier.auto_switch_to_production(sample_patterns)
    assert result is False  # match rate is 70%


@pytest.mark.asyncio
async def test_cloud_independence_verifier_returns_auto_switch_field(device_db, sample_patterns):
    """Check_cloud_independence should return auto_switch_enabled field."""

    verifier = CloudIndependenceVerifier(device_db)
    status = await verifier.check_cloud_independence(sample_patterns)
    assert "auto_switch_enabled" in status
    assert status["auto_switch_enabled"] is False


@pytest.mark.asyncio
async def test_should_rollback_not_production(device_db, sample_patterns):
    """Rollback should not trigger for devices not in production mode."""

    verifier = CloudIndependenceVerifier(device_db)
    result = await verifier.should_rollback_to_learning(sample_patterns)
    assert result is False  # device is in learning mode


# ═══════════════════════════════════════════════════════════════════════════════
# RUN TESTS
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v"])

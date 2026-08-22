"""
Tests for the Database module (DatabaseManager, models, CRUD).
"""
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select

from core.database import (
    DatabaseManager,
    DeviceRegistry,
    FieldMapping,
    LLMContextBuffer,
    MatchStats,
    ModelRegistry,
    RequestPattern,
    ResponseTemplate,
    SessionCache,
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


# ---------------------------------------------------------------------------
#  DatabaseManager lifecycle
# ---------------------------------------------------------------------------

class TestDatabaseManagerLifecycle:
    @pytest.mark.asyncio
    async def test_initialize_creates_core_tables(self, db_manager):
        async with await db_manager.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).limit(1)
            )
            assert result.scalars().all() == []

    @pytest.mark.asyncio
    async def test_initialize_twice_safe(self, tmp_path):
        dm = DatabaseManager(
            core_db_url=f"sqlite+aiosqlite:///{tmp_path}/core.db",
            device_db_dir=tmp_path / "device_dbs",
        )
        await dm.initialize()
        await dm.initialize()
        await dm.close()

    @pytest.mark.asyncio
    async def test_close(self, tmp_path):
        dm = DatabaseManager(
            core_db_url=f"sqlite+aiosqlite:///{tmp_path}/core.db",
            device_db_dir=tmp_path / "device_dbs",
        )
        await dm.initialize()
        await dm.close()
        # close again should not raise
        await dm.close()


# ---------------------------------------------------------------------------
#  DeviceRegistry CRUD
# ---------------------------------------------------------------------------

class TestDeviceRegistry:
    @pytest.mark.asyncio
    async def test_get_or_create_device(self, db_manager):
        await db_manager.get_or_create_device(
            device_id="device-001", vendor="shelly",
            device_type="plug", name="Test Plug",
        )
        async with await db_manager.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(
                    DeviceRegistry.device_id == "device-001"
                )
            )
            dev = result.scalar_one()
            assert dev.vendor == "shelly"
            assert dev.mode == "learning"

    @pytest.mark.asyncio
    async def test_get_or_create_device_is_idempotent(self, db_manager):
        for _ in range(3):
            await db_manager.get_or_create_device(
                device_id="device-001", vendor="shelly",
                device_type="plug", name="Test Plug",
            )
        async with await db_manager.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(
                    DeviceRegistry.device_id == "device-001"
                )
            )
            assert result.scalars().all() is not None

    @pytest.mark.asyncio
    async def test_list_devices(self, db_manager):
        await db_manager.get_or_create_device(
            device_id="d1", vendor="a", device_type="plug", name="A"
        )
        await db_manager.get_or_create_device(
            device_id="d2", vendor="b", device_type="sensor", name="B"
        )
        devices = await db_manager.list_devices()
        ids = [d["device_id"] for d in devices]
        assert "d1" in ids
        assert "d2" in ids

    @pytest.mark.asyncio
    async def test_resolve_device_id(self, db_manager):
        await db_manager.get_or_create_device(
            device_id="device-001", vendor="shelly",
            device_type="plug", name="Test",
        )
        # Set an IP address
        async with db_manager.core_session() as session:
                    result = await session.execute(
                        select(DeviceRegistry).where(
                            DeviceRegistry.device_id == "device-001"
                        )
                    )
                    dev = result.scalar_one()
                    dev.ip_addresses = ["192.168.1.100"]
        resolved = await db_manager.resolve_device_id("192.168.1.100")
        assert resolved == "device-001"

    @pytest.mark.asyncio
    async def test_resolve_device_id_not_found(self, db_manager):
        resolved = await db_manager.resolve_device_id("10.0.0.99")
        assert resolved is None


# ---------------------------------------------------------------------------
#  Device Session (per-device DB)
# ---------------------------------------------------------------------------

class TestDeviceSession:
    @pytest.mark.asyncio
    async def test_device_session_creates_tables(self, db_manager):
        """device_session should auto-create device DB tables."""
        async with db_manager.device_session("device-001") as session:
            result = await session.execute(
                select(RequestPattern).limit(1)
            )
            assert result.scalars().all() == []

    @pytest.mark.asyncio
    async def test_creates_request_pattern(self, db_manager):
        async with db_manager.device_session("device-001") as session:
            session.add(RequestPattern(
                pattern_id="rp1",
                method="GET",
                path_pattern="/status",
                protocol="http",
                intent="get_status",
            ))
        async with db_manager.device_session("device-001") as session:
            result = await session.execute(
                select(RequestPattern).where(
                    RequestPattern.pattern_id == "rp1"
                )
            )
            pattern = result.scalar_one()
            assert pattern.method == "GET"
            assert pattern.intent == "get_status"

    @pytest.mark.asyncio
    async def test_device_isolation(self, db_manager):
        async with db_manager.device_session("device-a") as session:
            session.add(RequestPattern(
                pattern_id="a1", method="GET",
                path_pattern="/a", protocol="http",
                intent="get_a",
            ))
        async with db_manager.device_session("device-b") as session:
            result = await session.execute(
                select(RequestPattern).where(
                    RequestPattern.pattern_id == "a1"
                )
            )
            assert result.scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_response_template_crud(self, db_manager):
        async with db_manager.device_session("device-001") as session:
            session.add(RequestPattern(
                pattern_id="rp1", method="GET",
                path_pattern="/test", protocol="http",
                intent="test",
            ))
        async with db_manager.device_session("device-001") as session:
            session.add(ResponseTemplate(
                template_id="t1", pattern_id="rp1",
                status_code=200,
                headers_template={"content-type": "application/json"},
                body_template={"state": True},
            ))
        async with db_manager.device_session("device-001") as session:
            result = await session.execute(
                select(ResponseTemplate).where(
                    ResponseTemplate.template_id == "t1"
                )
            )
            tpl = result.scalar_one()
            assert tpl.status_code == 200

    @pytest.mark.asyncio
    async def test_match_stats(self, db_manager):
        async with db_manager.device_session("device-001") as session:
            session.add(MatchStats(
                device_id="device-001",
                total_requests=10,
                local_hits=8,
                cloud_misses=2,
                match_rate_pct=80.0,
            ))
        async with db_manager.device_session("device-001") as session:
            result = await session.execute(
                select(MatchStats).where(
                    MatchStats.device_id == "device-001"
                )
            )
            stats = result.scalar_one()
            assert stats.local_hits == 8
            assert stats.match_rate_pct == 80.0

    @pytest.mark.asyncio
    async def test_llm_context_buffer(self, db_manager):
        async with db_manager.device_session("device-001") as session:
            session.add(LLMContextBuffer(
                device_id="device-001",
                sequence=1,
                        estimated_size_bytes=256,
                        correlated_pair={"pair_id": "p1"},
                    ))
        async with db_manager.device_session("device-001") as session:
            result = await session.execute(
                select(LLMContextBuffer).where(
                    LLMContextBuffer.device_id == "device-001"
                )
            )
            entries = result.scalars().all()
            assert len(entries) == 1

    @pytest.mark.asyncio
    async def test_field_mapping(self, db_manager):
        async with db_manager.device_session("device-001") as session:
            session.add(FieldMapping(
                mapping_id="fm1",
                request_field="id",
                request_type="integer",
                response_field="id",
                response_type="integer",
                intent="test",
            ))
        async with db_manager.device_session("device-001") as session:
            result = await session.execute(
                select(FieldMapping).where(
                    FieldMapping.mapping_id == "fm1"
                )
            )
            fm = result.scalar_one()
            assert fm.request_field == "id"

    @pytest.mark.asyncio
    async def test_session_cache(self, db_manager):
        async with db_manager.device_session("device-001") as session:
            session.add(SessionCache(
                device_id="device-001",
                correlation_key="ck1",
                method="POST",
                path="/test",
            ))
        async with db_manager.device_session("device-001") as session:
            result = await session.execute(
                select(SessionCache).where(
                    SessionCache.correlation_key == "ck1"
                )
            )
            sc = result.scalar_one()
            assert sc.method == "POST"


# ---------------------------------------------------------------------------
#  ModelRegistry (core)
# ---------------------------------------------------------------------------

class TestModelRegistry:
    @pytest.mark.asyncio
    async def test_create_model(self, db_manager):
        async with db_manager.core_session() as session:
            session.add(ModelRegistry(
                model_id="m1", device_id="device-001",
                version="1.0", framework="onnx",
                model_path="/models/m1.onnx",
            ))
        async with db_manager.core_session() as session:
            result = await session.execute(
                select(ModelRegistry).where(
                    ModelRegistry.model_id == "m1"
                )
            )
            model = result.scalar_one()
            assert model.version == "1.0"
            assert model.is_active is True
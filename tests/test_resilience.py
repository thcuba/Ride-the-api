"""
Tests for the Resilience module (cloud independence, auto-switch).
"""

import pytest
import pytest_asyncio
from sqlalchemy import select

from core.database import DatabaseManager, DeviceRegistry, RequestPattern
from core.resilience import (
    AUTO_SWITCH_MATCH_RATE,
    CHECK_INTERVAL_SECONDS,
    MIN_PATTERNS_FOR_SWITCH,
    MIN_TOTAL_REQUESTS,
    ROLLBACK_MATCH_RATE,
    AutoSwitchScheduler,
    CloudIndependenceVerifier,
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


@pytest_asyncio.fixture
async def registered_device(db_manager):
    await db_manager.get_or_create_device(
        device_id="device-001",
        vendor="shelly",
        device_type="plug",
        name="Test Plug",
    )
    return "device-001"


class TestConstants:
    def test_auto_switch_match_rate(self):
        assert AUTO_SWITCH_MATCH_RATE == 99.0  # noqa: PLR2004

    def test_rollback_match_rate(self):
        assert ROLLBACK_MATCH_RATE == 90.0  # noqa: PLR2004

    def test_min_patterns_for_switch(self):
        assert MIN_PATTERNS_FOR_SWITCH == 10  # noqa: PLR2004

    def test_min_total_requests(self):
        assert MIN_TOTAL_REQUESTS == 50  # noqa: PLR2004

    def test_check_interval_seconds(self):
        assert CHECK_INTERVAL_SECONDS == 60  # noqa: PLR2004


class TestCloudIndependenceVerifier:
    @pytest.mark.asyncio
    async def test_init(self, db_manager):
        verifier = CloudIndependenceVerifier(db_manager)
        assert verifier.db_manager is db_manager

    @pytest.mark.asyncio
    async def test_check_device_not_found(self, db_manager):
        verifier = CloudIndependenceVerifier(db_manager)
        result = await verifier.check_cloud_independence("nonexistent")
        assert result.get("independent") is False
        assert result.get("reason") == "device_not_found"

    @pytest.mark.asyncio
    async def test_check_learning_no_data(self, db_manager, registered_device):
        verifier = CloudIndependenceVerifier(db_manager)
        result = await verifier.check_cloud_independence(registered_device)
        assert result.get("independent") is False
        assert result.get("patterns_learned") == 0

    @pytest.mark.asyncio
    async def test_production_with_pattern(self, db_manager):
        device_id = "prod-device"
        await db_manager.get_or_create_device(
            device_id=device_id,
            vendor="generic",
            device_type="sensor",
            name="Prod Sensor",
        )
        async with db_manager.core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            dev = result.scalar_one()
            dev.mode = "production"
            await session.flush()

        async with db_manager.device_session(device_id) as session:
            session.add(
                RequestPattern(
                    pattern_id="rp1",
                    method="GET",
                    path_pattern="/status",
                    protocol="http",
                    intent="get_status",
                )
            )

        verifier = CloudIndependenceVerifier(db_manager)
        result = await verifier.check_cloud_independence(device_id)
        assert result.get("independent") is False
        assert result.get("patterns_learned") >= 1


class TestAutoSwitchScheduler:
    @pytest.mark.asyncio
    async def test_init(self, db_manager):
        scheduler = AutoSwitchScheduler(db_manager)
        assert scheduler.db_manager is db_manager
        assert scheduler._running is False

    @pytest.mark.asyncio
    async def test_start_and_stop(self, db_manager):
        scheduler = AutoSwitchScheduler(db_manager)
        await scheduler.start()
        assert scheduler._running is True
        assert scheduler._task is not None

        await scheduler.stop()
        assert scheduler._running is False
        assert scheduler._task is None

    @pytest.mark.asyncio
    async def test_start_twice_does_nothing(self, db_manager):
        scheduler = AutoSwitchScheduler(db_manager)
        await scheduler.start()
        task = scheduler._task
        await scheduler.start()
        assert scheduler._task is task

        await scheduler.stop()

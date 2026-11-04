"""
Tests for the Resilience module (cloud independence, auto-switch).
"""

import pytest
import pytest_asyncio
from sqlalchemy import select

from core.database import (
    DatabaseManager,
    DeviceRegistry,
    MatchStats,
    RequestPattern,
    ResponseTemplate,
)
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

    @pytest.mark.asyncio
    async def test_production_without_match_rate_is_not_independent(self, db_manager):
        """Learned shapes alone must not earn the "cloud independent" label.

        A device in production mode with patterns and a confident template but no
        recorded matches would otherwise be advertised as independent while every
        request still goes upstream.
        """
        device_id = "prod-no-rate"
        await db_manager.get_or_create_device(
            device_id=device_id,
            vendor="generic",
            device_type="sensor",
            name="High Confidence, No Matches",
        )
        async with db_manager.core_session() as session:
            row = (
                await session.execute(
                    select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
                )
            ).scalar_one()
            row.mode = "production"
            await session.flush()

        async with db_manager.device_session(device_id) as session:
            session.add(
                RequestPattern(
                    pattern_id="rp-no-rate",
                    method="GET",
                    path_pattern="/status",
                    protocol="http",
                    intent="get_status",
                )
            )
            session.add(
                ResponseTemplate(
                    template_id="rt-no-rate",
                    pattern_id="rp-no-rate",
                    status_code=200,
                    headers_template={},
                    body_template={},
                    field_mappings={},
                    confidence=0.95,
                )
            )

        verifier = CloudIndependenceVerifier(db_manager)
        result = await verifier.check_cloud_independence(device_id)

        # Patterns + a confident template are present, but nothing ever matched.
        assert result.get("independent") is False
        assert result.get("patterns_learned") >= 1
        assert result.get("templates_created") >= 1
        assert result.get("match_rate") == 0.0
        assert "match rate" in result.get("reason", "")


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


@pytest.mark.asyncio
async def test_import_device_patterns(db_manager, registered_device):
    """Test importing device patterns with batch logic and duplicate filtering."""
    verifier = CloudIndependenceVerifier(db_manager)
    device_id = registered_device

    data = {
        "patterns": [
            {
                "pattern_id": "p1",
                "method": "GET",
                "path_pattern": "/status",
                "protocol": "http",
                "intent": "status",
            },
            {
                "pattern_id": "p2",
                "method": "POST",
                "path_pattern": "/control",
                "protocol": "http",
                "intent": "control",
            },
        ],
        "templates": [
            {
                "template_id": "t1",
                "pattern_id": "p1",
                "status_code": 200,
            }
        ],
        "field_mappings": [
            {
                "mapping_id": "m1",
                "request_field": "state",
                "request_type": "string",
                "response_field": "ison",
                "response_type": "boolean",
                "intent": "control",
            }
        ],
    }

    # Initial import
    count = await verifier.import_device_patterns(device_id, data)
    assert count == 2  # noqa: PLR2004

    # Re-import same data - should import 0 new items
    count2 = await verifier.import_device_patterns(device_id, data)
    assert count2 == 0

    # Import empty data dict - should handle gracefully
    count3 = await verifier.import_device_patterns(device_id, {})
    assert count3 == 0


class TestWindowMatchRate:
    """Decisions use the rolling window, not the lifetime rate (C-2)."""

    async def _set_stats(self, db_manager, device_id, results):
        """Seed a MatchStats row with a rolling window of results."""

        async with db_manager.device_session(device_id) as session:
            recent = [{"result": r, "timestamp": "t"} for r in results]
            hits = sum(1 for r in results if r == "local_hit")
            total = len(results)
            session.add(
                MatchStats(
                    device_id=device_id,
                    total_requests=total,
                    local_hits=hits,
                    cloud_misses=total - hits,
                    match_rate_pct=0.0,  # stale lifetime value: must be ignored
                    recent_results=recent,
                )
            )
            await session.commit()

    @pytest.mark.asyncio
    async def test_recent_window_drives_match_rate(self, db_manager, registered_device):
        # Lifetime match_rate_pct is stored as 0.0, but the recent window is all hits.
        await self._set_stats(db_manager, registered_device, ["local_hit"] * 60)
        verifier = CloudIndependenceVerifier(db_manager)
        status = await verifier.check_cloud_independence(registered_device)
        assert status["match_rate"] == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_rollback_skipped_when_auto_switch_disabled(self, db_manager):
        """A manually promoted device (auto-switch off) is never rolled back."""
        device_id = "manual-prod"
        await db_manager.get_or_create_device(device_id, "generic", device_type="sensor")
        await db_manager.update_device_mode(device_id, "production")
        await db_manager.update_device_auto_switch(device_id, False)
        # A window full of misses would trip the rollback threshold if it ran.
        await self._set_stats(db_manager, device_id, ["cloud_miss"] * 60)
        verifier = CloudIndependenceVerifier(db_manager)
        assert await verifier.should_rollback_to_learning(device_id) is False
        dev = await db_manager.get_device(device_id)
        assert dev.mode == "production"

    @pytest.mark.asyncio
    async def test_mode_change_resets_match_counters(self, db_manager):
        device_id = "reset-dev"
        await db_manager.get_or_create_device(device_id, "generic", device_type="sensor")
        await self._set_stats(db_manager, device_id, ["cloud_miss"] * 40)
        await db_manager.update_device_mode(device_id, "production")

        async with db_manager.device_session(device_id) as session:
            stats = (
                await session.execute(select(MatchStats).where(MatchStats.device_id == device_id))
            ).scalar_one()
        assert stats.total_requests == 0
        assert stats.cloud_misses == 0
        assert list(stats.recent_results) == []

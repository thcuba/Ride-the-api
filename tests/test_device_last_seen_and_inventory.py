"""The registry has to remember two things the dashboard shows: when a device was
last seen, and which databases exist.

Both were visible only in memory: ``last_seen`` was tracked by the TLS layer in a
dict that a restart emptied, and the database inventory was built from the open
engines, so a database on disk was listed only after its device had used it in
this process.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

import core.server as server_mod
from core.database import DatabaseManager, DeviceRegistry
from core.server import _last_seen_is_stale, handle_device_seen

_HTTP_OK = 200


@pytest_asyncio.fixture
async def dm(tmp_path: Path) -> DatabaseManager:
    manager = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "devices",
    )
    await manager.initialize()
    yield manager
    await manager.close()


async def _device(dm: DatabaseManager, device_id: str, **kwargs) -> None:
    await dm.get_or_create_device(device_id, "unknown")
    async with dm.core_session() as session:
        result = await session.execute(select_device(device_id))
        device = result.scalar_one()
        for key, value in kwargs.items():
            setattr(device, key, value)
        await session.commit()


def select_device(device_id: str):
    return select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)


async def _stored_last_seen(dm: DatabaseManager, device_id: str) -> datetime | None:
    async with dm.core_session() as session:
        result = await session.execute(select_device(device_id))
        return result.scalar_one().last_seen


class TestLastSeenIsPersisted:
    @pytest.mark.asyncio
    async def test_a_seen_device_gets_a_stored_timestamp(self, dm):
        await _device(dm, "ip-192-168-1-10")
        assert await _stored_last_seen(dm, "ip-192-168-1-10") is None

        monkeypatch_target = server_mod
        original = monkeypatch_target.db_manager
        monkeypatch_target.db_manager = dm
        try:
            await handle_device_seen("192.168.1.10", "api.example.com")
        finally:
            monkeypatch_target.db_manager = original

        stored = await _stored_last_seen(dm, "ip-192-168-1-10")
        assert stored is not None

    @pytest.mark.asyncio
    async def test_a_fresh_timestamp_is_not_rewritten(self, dm):
        """This runs on every connection: rewriting each time would wear the disk."""
        recent = datetime.now(UTC) - timedelta(seconds=5)
        await _device(dm, "ip-192-168-1-11", last_seen=recent)

        server_mod.db_manager = dm
        try:
            await handle_device_seen("192.168.1.11", None)
        finally:
            server_mod.db_manager = None

        stored = await _stored_last_seen(dm, "ip-192-168-1-11")
        assert stored is not None
        if stored.tzinfo is None:
            stored = stored.replace(tzinfo=UTC)
        assert abs((stored - recent).total_seconds()) < 1

    @pytest.mark.asyncio
    async def test_an_old_timestamp_is_refreshed(self, dm):
        old = datetime.now(UTC) - timedelta(minutes=10)
        await _device(dm, "ip-192-168-1-12", last_seen=old)

        server_mod.db_manager = dm
        try:
            await handle_device_seen("192.168.1.12", None)
        finally:
            server_mod.db_manager = None

        stored = await _stored_last_seen(dm, "ip-192-168-1-12")
        if stored.tzinfo is None:
            stored = stored.replace(tzinfo=UTC)
        assert stored > old

    def test_a_missing_timestamp_is_always_stale(self):
        assert _last_seen_is_stale(None, datetime.now(UTC)) is True

    def test_a_naive_stored_timestamp_does_not_raise(self):
        """SQLite hands back naive datetimes; comparing them to an aware one raises."""
        now = datetime.now(UTC)
        naive_old = (now - timedelta(minutes=5)).replace(tzinfo=None)
        naive_now = now.replace(tzinfo=None)
        assert _last_seen_is_stale(naive_old, now) is True
        assert _last_seen_is_stale(naive_now, now) is False


class TestDatabaseInventory:
    @pytest.mark.asyncio
    async def test_a_device_database_is_listed_without_an_open_engine(self, dm):
        """The device is known and its file is on disk: it belongs in the list.

        Registering a device also opens its engine, so the case the old
        implementation got wrong — a restart, where nothing is open yet — is
        reproduced by dropping the open engines.
        """
        await _device(dm, "ip-192-168-1-20")
        dm._device_engines.clear()

        listed = await dm.list_databases()

        assert [d["device_id"] for d in listed] == ["ip-192-168-1-20"]
        assert listed[0]["is_active"] is False

    @pytest.mark.asyncio
    async def test_an_open_engine_is_reported_as_active(self, dm):
        await _device(dm, "ip-192-168-1-21")
        await dm.get_device_engine("ip-192-168-1-21")

        listed = await dm.list_databases()

        assert listed[0]["is_active"] is True

    @pytest.mark.asyncio
    async def test_every_device_is_listed_not_only_the_active_one(self, dm):
        await _device(dm, "ip-192-168-1-22")
        await _device(dm, "ip-192-168-1-23")
        await dm.get_device_engine("ip-192-168-1-23")

        listed = await dm.list_databases()

        assert [d["device_id"] for d in listed] == ["ip-192-168-1-22", "ip-192-168-1-23"]

    @pytest.mark.asyncio
    async def test_a_renamed_database_keeps_its_stored_url(self, dm):
        await _device(dm, "ip-192-168-1-24")
        await dm.rename_device_database("ip-192-168-1-24", "Living Room Sensor")

        listed = await dm.list_databases()

        assert listed[0]["database_url"].endswith("Living-Room-Sensor.db")

    @pytest.mark.asyncio
    async def test_a_device_without_a_stored_url_falls_back_to_its_file(self, dm):
        await _device(dm, "ip-192-168-1-25")

        listed = await dm.list_databases()

        assert listed[0]["database_url"].endswith("ip-192-168-1-25.db")

    @pytest.mark.asyncio
    async def test_the_inventory_survives_a_restart(self, tmp_path):
        """A fresh manager over the same files still knows what is there."""
        url = f"sqlite+aiosqlite:///{tmp_path / 'core.db'}"
        first = DatabaseManager(core_db_url=url, device_db_dir=tmp_path / "devices")
        await first.initialize()
        await first.get_or_create_device("ip-192-168-1-26", "unknown")
        await first.close()

        second = DatabaseManager(core_db_url=url, device_db_dir=tmp_path / "devices")
        await second.initialize()
        try:
            listed = await second.list_databases()
            assert [d["device_id"] for d in listed] == ["ip-192-168-1-26"]
        finally:
            await second.close()

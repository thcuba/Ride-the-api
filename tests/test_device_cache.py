"""A device registered after a cached miss must be visible immediately."""

from __future__ import annotations

import pytest
import pytest_asyncio

from core.database import DatabaseManager


@pytest_asyncio.fixture
async def db_manager(tmp_path):
    dm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "device_dbs",
    )
    await dm.initialize()
    yield dm
    await dm.close()


@pytest.mark.asyncio
async def test_new_device_is_not_hidden_by_a_cached_miss(db_manager):
    # A lookup before registration caches "not found"...
    assert await db_manager.get_device("late-device") is None
    await db_manager.get_or_create_device("late-device", "acme")
    # ...which must not hide the device once it exists.
    device = await db_manager.get_device("late-device")
    assert device is not None
    assert device.device_id == "late-device"

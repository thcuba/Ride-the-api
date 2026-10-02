"""
Tests for device deletion (DatabaseManager.delete_device).

Covers removing a device from the registry along with its on-disk per-device
SQLite database and portable ``.ride-pattern.json`` export.
"""

import hashlib

import pytest
import pytest_asyncio

from core.database import DatabaseManager


@pytest_asyncio.fixture
async def dm(tmp_path):
    core_db_path = tmp_path / "core.db"
    manager = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{core_db_path}",
        device_db_dir=tmp_path / "device_dbs",
    )
    await manager.initialize()
    yield manager
    await manager.close()


def pattern_file_path(dm: DatabaseManager, device_id: str):
    """Path of the portable export for a device, mirroring the runtime naming."""
    patterns_dir = dm.device_db_dir.parent / "patterns"
    name = f"{hashlib.sha256(device_id.encode('utf-8')).hexdigest()}.ride-pattern.json"
    return patterns_dir / name


class TestDeleteDevice:
    @pytest.mark.asyncio
    async def test_delete_removes_registry_row_and_device_db(self, dm):
        await dm.get_or_create_device("dev-001", "shelly")
        assert any(d["device_id"] == "dev-001" for d in await dm.list_devices())

        db_path = dm.device_db_dir / dm._safe_db_name("dev-001")  # noqa: SLF001
        assert db_path.exists()

        assert await dm.delete_device("dev-001") is True
        assert not any(d["device_id"] == "dev-001" for d in await dm.list_devices())
        assert not db_path.exists()

    @pytest.mark.asyncio
    async def test_delete_removes_pattern_file(self, dm):
        await dm.get_or_create_device("dev-prt", "shelly")
        pf = pattern_file_path(dm, "dev-prt")
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text('{"version": "v2"}')
        assert pf.exists()

        assert await dm.delete_device("dev-prt") is True
        assert not pf.exists()

    @pytest.mark.asyncio
    async def test_delete_missing_device_returns_false(self, dm):
        assert await dm.delete_device("ghost-device") is False

    @pytest.mark.asyncio
    async def test_recreate_after_delete_is_fresh(self, dm):
        # After deletion the same id can be re-created from scratch.
        await dm.get_or_create_device("dev-re", "mqtt")
        assert await dm.delete_device("dev-re") is True
        await dm.get_or_create_device("dev-re", "coap")
        assert any(d["device_id"] == "dev-re" for d in await dm.list_devices())

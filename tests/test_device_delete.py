"""
Tests for device deletion (DatabaseManager.delete_device).

Covers removing a device from the registry along with its on-disk per-device
SQLite database and portable ``.ride-pattern.json`` export.
"""

import hashlib
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select

import core.server as server_mod
from core.database import DatabaseManager, DeviceRegistry
from core.models.telemetry import RelayedConnection
from core.server import app


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
    async def test_delete_api_rejects_path_traversal(self, dm, monkeypatch):
        monkeypatch.setattr(server_mod, "db_manager", dm)
        cfg = server_mod.config_manager.config
        saved_auth = cfg.security.auth_enabled
        cfg.security.auth_enabled = False
        try:
            client = TestClient(app)
            resp = client.delete("/api/devices/invalid%20id")
            assert resp.status_code == 400  # noqa: PLR2004
            assert "Invalid device_id" in resp.json()["error"]
        finally:
            cfg.security.auth_enabled = saved_auth

    @pytest.mark.asyncio
    async def test_recreate_after_delete_is_fresh(self, dm):
        # After deletion the same id can be re-created from scratch.
        await dm.get_or_create_device("dev-re", "mqtt")
        assert await dm.delete_device("dev-re") is True
        await dm.get_or_create_device("dev-re", "coap")
        assert any(d["device_id"] == "dev-re" for d in await dm.list_devices())

    @pytest.mark.asyncio
    async def test_delete_handles_db_unlink_oserror(self, dm, monkeypatch, caplog):
        await dm.get_or_create_device("dev-err-db", "shelly")
        db_path = dm.device_db_dir / dm._safe_db_name("dev-err-db")  # noqa: SLF001
        assert db_path.exists()

        original_unlink = Path.unlink

        def mock_unlink(path_obj, *args, **kwargs):
            if path_obj == db_path:
                raise OSError("Permission denied / disk error")
            return original_unlink(path_obj, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", mock_unlink)

        with caplog.at_level("WARNING"):
            assert await dm.delete_device("dev-err-db") is True

        assert f"Could not remove device DB {db_path}" in caplog.text
        assert not any(d["device_id"] == "dev-err-db" for d in await dm.list_devices())

    @pytest.mark.asyncio
    async def test_delete_handles_pattern_unlink_oserror(self, dm, monkeypatch, caplog):
        await dm.get_or_create_device("dev-err-pat", "shelly")
        pf = pattern_file_path(dm, "dev-err-pat")
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text('{"version": "v2"}')
        assert pf.exists()

        original_unlink = Path.unlink

        def mock_unlink(path_obj, *args, **kwargs):
            if path_obj == pf:
                raise OSError("Permission denied / disk error")
            return original_unlink(path_obj, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", mock_unlink)

        with caplog.at_level("WARNING"):
            assert await dm.delete_device("dev-err-pat") is True

        assert f"Could not remove pattern file {pf}" in caplog.text
        assert not any(d["device_id"] == "dev-err-pat" for d in await dm.list_devices())

    @pytest.mark.asyncio
    async def test_the_engine_is_closed_before_the_files_are_removed(self, dm, monkeypatch):
        """Il database non deve essere cancellato con l'engine ancora aperto.

        SQLite tiene il file aperto per tutta la vita dell'engine e su Linux l'unlink
        riesce anche con l'handle aperto: una scrittura in volo finisce su un inode
        cancellato e i dati spariscono senza un errore.
        """
        await dm.get_or_create_device("dev-order", "shelly")
        async with await dm.get_device_session("dev-order") as _session:
            pass
        assert "dev-order" in dm._device_engines  # noqa: SLF001

        seen: dict[str, bool] = {}
        original = dm.device_pool.delete_storage_files

        def spy(db_path):
            seen["engine_alive"] = "dev-order" in dm._device_engines  # noqa: SLF001
            return original(db_path)

        monkeypatch.setattr(dm.device_pool, "delete_storage_files", spy)

        assert await dm.delete_device("dev-order") is True

        assert seen["engine_alive"] is False, "file cancellato con l'engine ancora aperto"

    @pytest.mark.asyncio
    async def test_delete_removes_the_database_after_a_rename(self, dm):
        """Dopo un rename il file ha un altro nome: derivarlo dall'id lo mancherebbe."""
        await dm.get_or_create_device("dev-ren", "shelly")
        await dm.rename_device_database("dev-ren", "purificatore-salotto")

        on_disk = list(dm.device_db_dir.glob("*.db"))
        assert len(on_disk) == 1  # noqa: PLR2004
        renamed = on_disk[0]
        assert renamed.name != dm._safe_db_name("dev-ren")  # noqa: SLF001

        assert await dm.delete_device("dev-ren") is True

        assert not renamed.exists(), "il database rinominato è rimasto su disco"

    @pytest.mark.asyncio
    async def test_delete_drops_relayed_connection_rows(self, dm):
        """Le connessioni relayate non hanno cascade e sopravvivrebbero al device."""
        await dm.get_or_create_device("dev-rel", "shelly")
        async with await dm.get_core_session() as session:
            session.add(
                RelayedConnection(
                    device_id="dev-rel",
                    client_ip="192.168.1.77",
                    destination_port=8883,
                    outcome="relayed",
                )
            )
            await session.commit()

        assert await dm.delete_device("dev-rel") is True

        async with await dm.get_core_session() as session:
            result = await session.execute(
                select(RelayedConnection).where(RelayedConnection.device_id == "dev-rel")
            )
            assert result.scalars().all() == []

    @pytest.mark.asyncio
    async def test_delete_invalidates_the_ip_lookup_cache(self, dm):
        """Un device cancellato non deve continuare a risolversi dai suoi vecchi IP."""
        await dm.get_or_create_device("dev-ip", "shelly")
        async with dm.core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == "dev-ip")
            )
            result.scalar_one().ip_addresses = ["192.168.1.90"]

        assert await dm.resolve_device_id("192.168.1.90") == "dev-ip"
        assert dm._ip_lookup_cache is not None  # noqa: SLF001

        assert await dm.delete_device("dev-ip") is True

        assert dm._ip_lookup_cache is None  # noqa: SLF001
        assert await dm.resolve_device_id("192.168.1.90") is None

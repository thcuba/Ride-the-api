"""
Tests for renaming a device's database (DatabaseManager.rename_device_database).

The database file follows the device name: a device that is named after the
fact must not keep a file titled with a bare address. These tests cover the file
move (including the SQLite sidecars), the registry update and the failure modes.
"""

from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import core.server as server_mod
from core.database import DatabaseManager
from core.server import app

_HTTP_OK = 200
_HTTP_BAD_REQUEST = 400
_HTTP_NOT_FOUND = 404
_HTTP_CONFLICT = 409


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


async def _device_with_db(dm: DatabaseManager, device_id: str = "ip-192-168-1-42"):
    """Register a device and make sure its database file exists on disk."""
    await dm.get_or_create_device(device_id, "unknown")
    await dm.get_device_engine(device_id)
    return dm.device_db_dir / dm._safe_db_name(device_id)  # noqa: SLF001


class TestRenameDeviceDatabase:
    @pytest.mark.asyncio
    async def test_rename_moves_the_file_and_reports_the_new_url(self, dm):
        old_path = await _device_with_db(dm)
        assert old_path.exists()

        url = await dm.rename_device_database("ip-192-168-1-42", "purifier-soggiorno")

        new_path = dm.device_db_dir / "purifier-soggiorno.db"
        assert url == f"sqlite+aiosqlite:///{new_path}"
        assert new_path.exists()
        assert not old_path.exists()

    @pytest.mark.asyncio
    async def test_registry_records_the_new_name_and_url(self, dm):
        await _device_with_db(dm)

        await dm.rename_device_database("ip-192-168-1-42", "purifier-soggiorno")

        device = await dm.get_device("ip-192-168-1-42")
        assert device is not None
        assert device.database_name == "purifier-soggiorno"
        assert device.database_url.endswith("purifier-soggiorno.db")

    @pytest.mark.asyncio
    async def test_device_keeps_working_on_the_new_file(self, dm):
        """After the rename the device must serve its database from the new path."""
        await _device_with_db(dm)

        await dm.rename_device_database("ip-192-168-1-42", "purifier-soggiorno")

        # A new engine is built from the updated URL and the schema is present.
        engine = await dm.get_device_engine("ip-192-168-1-42")
        assert "purifier-soggiorno.db" in str(engine.url)

    @pytest.mark.asyncio
    async def test_missing_suffix_is_added(self, dm):
        await _device_with_db(dm)

        url = await dm.rename_device_database("ip-192-168-1-42", "cantina")

        assert url.endswith("cantina.db")
        assert (dm.device_db_dir / "cantina.db").exists()

    @pytest.mark.asyncio
    async def test_name_is_sanitized_into_a_filename(self, dm):
        """A human name must not escape the data directory."""
        await _device_with_db(dm)

        url = await dm.rename_device_database("ip-192-168-1-42", "Purifier Soggiorno // 2")

        assert url.endswith("Purifier-Soggiorno-2.db")
        moved = list(dm.device_db_dir.glob("Purifier*"))
        assert len(moved) == 1
        assert moved[0].parent == dm.device_db_dir

    @pytest.mark.asyncio
    async def test_traversal_attempt_stays_inside_the_directory(self, dm):
        """A name that tries to walk up the tree must be flattened, not followed."""
        await _device_with_db(dm)

        url = await dm.rename_device_database("ip-192-168-1-42", "../../etc/passwd")

        assert url is not None
        written = Path(url.replace("sqlite+aiosqlite:///", ""))
        assert written.parent == dm.device_db_dir
        assert ".." not in written.name

    @pytest.mark.asyncio
    async def test_sqlite_sidecars_travel_with_the_file(self, dm):
        """The -wal and -shm files hold recent writes: they must move too."""
        old_path = await _device_with_db(dm)
        for suffix in ("-wal", "-shm"):
            (old_path.parent / f"{old_path.name}{suffix}").write_bytes(b"x")

        await dm.rename_device_database("ip-192-168-1-42", "con-sidercars")

        new_path = dm.device_db_dir / "con-sidercars.db"
        for suffix in ("-wal", "-shm"):
            assert (new_path.parent / f"{new_path.name}{suffix}").exists()
            assert not (old_path.parent / f"{old_path.name}{suffix}").exists()

    @pytest.mark.asyncio
    async def test_refuses_a_name_already_in_use(self, dm):
        """Two devices must not end up sharing one file."""
        await _device_with_db(dm, "dev-a")
        await _device_with_db(dm, "dev-b")

        with pytest.raises(ValueError, match="already exists"):
            await dm.rename_device_database("dev-a", "dev-b")

        # The clashing rename changed nothing.
        assert (dm.device_db_dir / "dev-a.db").exists()
        assert (dm.device_db_dir / "dev-b.db").exists()

    @pytest.mark.asyncio
    async def test_unknown_device_returns_none(self, dm):
        assert await dm.rename_device_database("does-not-exist", "whatever") is None

    @pytest.mark.asyncio
    async def test_renaming_to_the_same_name_is_a_noop(self, dm):
        old_path = await _device_with_db(dm)

        url = await dm.rename_device_database("ip-192-168-1-42", "ip-192-168-1-42")

        assert url is not None
        assert old_path.exists()


class TestRenameDatabaseEndpoint:
    """The HTTP surface: a bad request must be rejected before any file moves."""

    @staticmethod
    def _client(monkeypatch, dm: DatabaseManager) -> TestClient:
        """Same wiring as the other server tests: fake db_manager + auth off."""
        server_mod.config_manager.config.security.auth_enabled = False
        monkeypatch.setattr(server_mod, "db_manager", dm)
        monkeypatch.setattr(server_mod, "orchestrator", object())
        return TestClient(app)

    @pytest.mark.asyncio
    async def test_missing_name_is_rejected(self, dm, monkeypatch):
        client = self._client(monkeypatch, dm)
        resp = client.post("/api/devices/ip-192-168-1-42/database/rename", json={})
        assert resp.status_code == _HTTP_BAD_REQUEST

    @pytest.mark.asyncio
    async def test_unknown_device_returns_404(self, dm, monkeypatch):
        client = self._client(monkeypatch, dm)
        resp = client.post("/api/devices/nope/database/rename", json={"name": "x"})
        assert resp.status_code == _HTTP_NOT_FOUND

    @pytest.mark.asyncio
    async def test_name_already_used_returns_409(self, dm, monkeypatch):
        await _device_with_db(dm, "dev-a")
        await _device_with_db(dm, "dev-b")
        client = self._client(monkeypatch, dm)

        resp = client.post("/api/devices/dev-a/database/rename", json={"name": "dev-b"})

        assert resp.status_code == _HTTP_CONFLICT
        assert "already exists" in resp.json()["error"]

    @pytest.mark.asyncio
    async def test_rename_returns_the_new_url(self, dm, monkeypatch):
        await _device_with_db(dm)
        client = self._client(monkeypatch, dm)

        resp = client.post("/api/devices/ip-192-168-1-42/database/rename", json={"name": "cantina"})

        assert resp.status_code == _HTTP_OK
        assert resp.json()["database_url"].endswith("cantina.db")
        assert (dm.device_db_dir / "cantina.db").exists()


class TestDeviceRenameMovesItsDatabase:
    """Naming a device is what renames its database: the two must not drift."""

    @pytest.mark.asyncio
    async def test_setting_a_device_name_moves_its_database(self, dm, monkeypatch):
        await _device_with_db(dm)
        client = TestRenameDatabaseEndpoint._client(monkeypatch, dm)

        resp = client.put(
            "/api/devices/ip-192-168-1-42/tls-config",
            json={"name": "Purifier Soggiorno"},
        )

        assert resp.status_code == _HTTP_OK
        assert not (dm.device_db_dir / "ip-192-168-1-42.db").exists()
        assert (dm.device_db_dir / "Purifier-Soggiorno.db").exists()
        device = await dm.get_device("ip-192-168-1-42")
        assert device.database_name == "Purifier Soggiorno"

    @pytest.mark.asyncio
    async def test_a_name_clash_does_not_lose_the_device(self, dm, monkeypatch):
        """If another device holds that file name, the device keeps working."""
        await _device_with_db(dm, "dev-a")
        await _device_with_db(dm, "dev-b")
        client = TestRenameDatabaseEndpoint._client(monkeypatch, dm)

        resp = client.put("/api/devices/dev-a/tls-config", json={"name": "dev-b"})

        assert resp.status_code == _HTTP_OK
        assert (dm.device_db_dir / "dev-a.db").exists()
        device = await dm.get_device("dev-a")
        assert device.name == "dev-b"

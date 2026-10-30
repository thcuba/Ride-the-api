"""Switching the buffer backend must not lose — or duplicate — the pairs in flight.

``disk`` **is** the SQLite backend: buffered pairs live in the device database, so they
survive a restart. ``memory`` keeps them in a process-shared in-memory engine: faster, and
gone the moment the process ends. Switching used to flip the flag and drop the live buffers,
so whatever had not been flushed yet was silently lost in one direction, and left unflushed —
ready to be counted twice — in the other.

These tests are behavioural: they add real pairs through a ``ContextBuffer``, switch, and then
read what the *new* backend sees.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import core.server as server_mod
from core.buffer import (
    create_buffer_store,
    dispose_memory_db,
    get_buffer_backend,
    normalise_backend,
    set_buffer_backend,
)
from core.buffer import switch_backend as switch_buffer_backend
from core.database import DatabaseManager
from core.pipeline import ContextBuffer, CorrelatedPair

DEVICE = "ip-192-168-1-77"


@pytest_asyncio.fixture
async def db_manager(tmp_path):
    dm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "device_dbs",
    )
    await dm.initialize()
    yield dm
    await dm.close()


@pytest_asyncio.fixture(autouse=True)
async def _disk_backend():
    """Start from the SQLite backend and leave no RAM state behind."""
    set_buffer_backend("disk")
    yield
    set_buffer_backend("disk")
    await dispose_memory_db()


def make_pair(pair_id: str = "pair-001", **overrides) -> CorrelatedPair:
    defaults = dict(
        pair_id=pair_id,
        device_id=DEVICE,
        vendor="unknown",
        protocol="https",
        method="POST",
        path="/v1/device/status",
        request_headers={"content-type": "application/json"},
        request_body={"state": "on"},
        request_query={},
        response_status=200,
        response_headers={"content-type": "application/json"},
        response_body={"ok": True},
        latency_ms=12.5,
        correlation_confidence=0.95,
        timestamp=datetime.now(UTC),
    )
    defaults.update(overrides)
    return CorrelatedPair(**defaults)


async def _pending(store, device_id: str = DEVICE) -> list[dict]:
    return await store.get_buffer_pairs(device_id)


async def _buffer_with_a_pair(db_manager, pair_id: str) -> ContextBuffer:
    """Register the device (the capture path always does) and buffer one pair."""
    await db_manager.get_or_create_device(DEVICE, "unknown")
    buffer = ContextBuffer(db_manager)
    await buffer.add_pair(DEVICE, make_pair(pair_id))
    return buffer


class TestBackendNames:
    def test_sqlite_is_the_disk_backend(self):
        """The operator calls it "SQLite"; the API calls it "disk". Both must work."""
        assert normalise_backend("sqlite") == "disk"
        assert normalise_backend("disk") == "disk"

    def test_ram_is_the_memory_backend(self):
        assert normalise_backend("ram") == "memory"
        assert normalise_backend("memory") == "memory"

    def test_names_are_case_insensitive_and_trimmed(self):
        assert normalise_backend("  SQLite ") == "disk"

    def test_an_unknown_backend_is_rejected(self):
        assert normalise_backend("postgres") is None


class TestSwitchKeepsThePairsInFlight:
    @pytest.mark.asyncio
    async def test_memory_to_sqlite_carries_the_pending_pairs(self, db_manager):
        set_buffer_backend("memory")
        await _buffer_with_a_pair(db_manager, "in-flight")

        summary = await switch_buffer_backend(db_manager, "sqlite")

        assert summary["backend"] == "disk"
        assert summary["from"] == "memory"
        assert summary["pairs"] == 1
        assert summary["devices"] == 1
        carried = await _pending(create_buffer_store(db_manager))
        assert [entry["pair"]["pair_id"] for entry in carried] == ["in-flight"]

    @pytest.mark.asyncio
    async def test_sqlite_to_memory_carries_them_back_and_leaves_nothing_behind(self, db_manager):
        await _buffer_with_a_pair(db_manager, "on-disk")

        summary = await switch_buffer_backend(db_manager, "memory")

        assert summary["pairs"] == 1
        in_ram = await _pending(create_buffer_store(db_manager))
        assert [entry["pair"]["pair_id"] for entry in in_ram] == ["on-disk"]
        # The durable rows must be marked flushed, or switching back reads them again.
        duplicate = await _pending(create_buffer_store(db_manager, backend="disk"))
        assert duplicate == []

    @pytest.mark.asyncio
    async def test_switching_back_and_forth_does_not_duplicate(self, db_manager):
        await _buffer_with_a_pair(db_manager, "once")

        await switch_buffer_backend(db_manager, "memory")
        await switch_buffer_backend(db_manager, "sqlite")

        final = await _pending(create_buffer_store(db_manager))
        assert [entry["pair"]["pair_id"] for entry in final] == ["once"]

    @pytest.mark.asyncio
    async def test_switching_to_the_same_backend_moves_nothing(self, db_manager):
        await _buffer_with_a_pair(db_manager, "untouched")

        summary = await switch_buffer_backend(db_manager, "sqlite")

        assert summary["pairs"] == 0
        assert summary["devices"] == 0
        assert len(await _pending(create_buffer_store(db_manager))) == 1

    @pytest.mark.asyncio
    async def test_a_live_context_buffer_is_rebound_to_the_new_store(self, db_manager):
        """Keeping the old store would leave the switch half-applied until a restart."""
        buffer = await _buffer_with_a_pair(db_manager, "before-switch")

        await switch_buffer_backend(db_manager, "memory", rebind=buffer.rebind_store)
        await buffer.add_pair(DEVICE, make_pair("after-switch"))

        entries = await _pending(create_buffer_store(db_manager))
        assert {entry["pair"]["pair_id"] for entry in entries} == {"before-switch", "after-switch"}


class TestSqliteBackendSurvivesARestart:
    @pytest.mark.asyncio
    async def test_a_capture_survives_a_restart(self, db_manager, tmp_path):
        """The whole point of the SQLite backend: the pairs are still there tomorrow."""
        await _buffer_with_a_pair(db_manager, "durable")

        # A restart: a brand-new DatabaseManager on the same device database directory.
        restarted = DatabaseManager(
            core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
            device_db_dir=tmp_path / "device_dbs",
        )
        await restarted.initialize()
        try:
            entries = await _pending(create_buffer_store(restarted))
        finally:
            await restarted.close()

        assert [entry["pair"]["pair_id"] for entry in entries] == ["durable"]

    @pytest.mark.asyncio
    async def test_the_memory_backend_does_not(self, db_manager):
        """Control: the volatile backend is exactly the one that loses them, as documented."""
        set_buffer_backend("memory")
        await _buffer_with_a_pair(db_manager, "volatile")
        await dispose_memory_db()  # what a restart does to the in-process engine

        assert await _pending(create_buffer_store(db_manager)) == []


class TestBackendIsReported:
    @pytest.mark.asyncio
    async def test_the_switch_reports_the_new_backend(self, db_manager):
        summary = await switch_buffer_backend(db_manager, "memory")

        assert summary["backend"] == "memory"
        assert get_buffer_backend() == "memory"


class TestSwitchThroughTheApi:
    """The endpoint is what the dashboard and an operator actually call."""

    def _client(self, monkeypatch, db_manager, orchestrator=None):
        monkeypatch.setattr(server_mod, "db_manager", db_manager)
        monkeypatch.setattr(server_mod, "orchestrator", orchestrator)
        monkeypatch.setattr(server_mod.config_manager.config.security, "password", "testpass")
        return TestClient(server_mod.app)

    @pytest.mark.asyncio
    async def test_sqlite_is_accepted_and_the_pairs_travel(self, db_manager, monkeypatch):
        set_buffer_backend("memory")
        await _buffer_with_a_pair(db_manager, "via-api")
        client = self._client(monkeypatch, db_manager)

        response = client.put(
            "/api/settings/buffer-backend",
            json={"backend": "sqlite"},
            headers={"X-API-Key": "testpass"},
        )

        assert response.status_code == 200  # noqa: PLR2004
        body = response.json()
        assert body["backend"] == "disk"
        assert body["pairs"] == 1
        assert get_buffer_backend() == "disk"

    @pytest.mark.asyncio
    async def test_an_unknown_backend_is_refused_by_name(self, db_manager, monkeypatch):
        client = self._client(monkeypatch, db_manager)

        response = client.put(
            "/api/settings/buffer-backend",
            json={"backend": "postgres"},
            headers={"X-API-Key": "testpass"},
        )

        assert response.status_code == 400  # noqa: PLR2004
        assert "sqlite" in response.json()["error"]
        assert get_buffer_backend() == "disk"

    @pytest.mark.asyncio
    async def test_a_non_object_body_is_refused(self, db_manager, monkeypatch):
        client = self._client(monkeypatch, db_manager)

        response = client.put(
            "/api/settings/buffer-backend",
            json=["disk"],
            headers={"X-API-Key": "testpass"},
        )

        assert response.status_code == 400  # noqa: PLR2004

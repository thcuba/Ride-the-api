"""
Tests for the RAM (SQLite :memory:) buffer backend and the disk <-> memory
runtime switch.

These tests exercise the same BufferStore used by ContextBuffer (learning
pipeline) and BufferManager (export/import) in both backends and check that
durable MatchStats stay coherent while the hot path lives in RAM.
"""

from dataclasses import asdict
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select

import core.buffer as buffer_pkg
from core.buffer import (
    create_buffer_store,
    dispose_memory_db,
    get_buffer_backend,
    initialize_buffer_backend,
    load_persisted_backend,
    persist_backend,
    set_buffer_backend,
)
from core.database import DatabaseManager, LLMContextBuffer, MatchStats
from core.pattern_db.buffer_manager import BufferManager
from core.pipeline import ContextBuffer, CorrelatedPair


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
async def _reset_buffer_backend():
    """Ensure tests start from the disk backend and leave no RAM state behind."""
    set_buffer_backend("disk")
    yield
    set_buffer_backend("disk")
    await dispose_memory_db()


def make_pair(**overrides) -> CorrelatedPair:
    defaults = dict(
        pair_id="pair-001",
        device_id="device-001",
        vendor="shelly",
        protocol="http",
        method="POST",
        path="/rpc/Switch.GetStatus",
        request_headers={"content-type": "application/json"},
        request_body={"id": 1},
        request_query={},
        response_status=200,
        response_headers={"content-type": "application/json"},
        response_body={"state": True},
        latency_ms=12.5,
        correlation_confidence=0.95,
        timestamp=datetime.now(UTC),
    )
    defaults.update(overrides)
    return CorrelatedPair(**defaults)


async def _on_disk_stats(db_manager: DatabaseManager, device_id: str) -> MatchStats | None:
    async with db_manager.device_session(device_id) as session:
        result = await session.execute(select(MatchStats).where(MatchStats.device_id == device_id))
        return result.scalar_one_or_none()


class TestRamMode:
    @pytest.mark.asyncio
    async def test_ram_add_flush_delete(self, db_manager):
        set_buffer_backend("memory")
        assert get_buffer_backend() == "memory"

        buffer = ContextBuffer(db_manager, max_size_bytes=1048576)
        await buffer.add_pair("device-001", make_pair(request_body="x" * 500))
        await buffer.add_pair("device-001", make_pair(pair_id="pair-002", request_body="y" * 500))

        pairs = await buffer.get_buffer_pairs("device-001")
        assert {p["pair"]["pair_id"] for p in pairs} == {"pair-001", "pair-002"}
        assert (await buffer.get_current_size("device-001")) > 0

        # delete one entry
        deleted = await buffer.delete_entry("device-001", pairs[0]["id"])
        assert deleted is True
        remaining = await buffer.get_buffer_pairs("device-001")
        assert len(remaining) == 1

        # flush the rest
        count = await buffer.flush("device-001")
        assert count == 1
        assert await buffer.get_buffer_pairs("device-001") == []
        assert (await buffer.get_current_size("device-001")) == 0

    @pytest.mark.asyncio
    async def test_ram_enrichment_serialized_and_omitted(self, db_manager):
        """D2: enrichment flows into the buffer pair_json; absent when not set."""
        set_buffer_backend("memory")
        assert get_buffer_backend() == "memory"

        buffer = ContextBuffer(db_manager, max_size_bytes=1048576)
        enriched = make_pair(
            enrichment={
                "transport": {"port": 1883, "tls": False, "topic": "shelly/1/status"},
                "security": "none",
                "identity": "shelly-1",
                "kind": "publish",
            }
        )
        await buffer.add_pair("device-001", enriched)

        pairs = await buffer.get_buffer_pairs("device-001")
        buffered = pairs[0]["pair"]
        assert buffered["enrichment"]["kind"] == "publish"
        assert buffered["enrichment"]["transport"]["port"] == 1883  # noqa: PLR2004

        # A pair without enrichment must not gain an empty key (backward compat).
        await buffer.add_pair("device-001", make_pair(pair_id="pair-plain"))
        plain = [
            p["pair"]
            for p in await buffer.get_buffer_pairs("device-001")
            if p["pair"]["pair_id"] == "pair-plain"
        ]
        assert "enrichment" not in plain[0]

    @pytest.mark.asyncio
    async def test_ram_store_shared_between_managers(self, db_manager):
        set_buffer_backend("memory")

        bm = BufferManager(db_manager)
        cb = ContextBuffer(db_manager, max_size_bytes=1048576)

        # Pair added through the export/import manager is visible to the
        # learning pipeline's buffer and vice-versa.
        bm_pair = asdict(make_pair(pair_id="via-bm"))
        bm_pair["timestamp"] = bm_pair["timestamp"].isoformat()
        await bm.add_pair("device-001", bm_pair)
        cb_pairs = await cb.get_buffer_pairs("device-001")
        assert {p["pair"]["pair_id"] for p in cb_pairs} == {"via-bm"}

        await cb.add_pair("device-001", make_pair(pair_id="via-cb"))
        bm_pairs = await bm.get_buffer_pairs("device-001")
        assert {p["pair"]["pair_id"] for p in bm_pairs} == {"via-bm", "via-cb"}

        # flush through the manager resets the size both see
        await bm.flush("device-001")
        assert await cb.get_buffer_pairs("device-001") == []
        assert (await bm.get_current_size("device-001")) == 0

    @pytest.mark.asyncio
    async def test_durable_stats_synced_on_flush(self, db_manager):
        set_buffer_backend("memory")

        buffer = ContextBuffer(db_manager, max_size_bytes=1048576)
        await buffer.add_pair("device-001", make_pair(request_body="x" * 500))
        await buffer.flush("device-001")

        durable = await _on_disk_stats(db_manager, "device-001")
        assert durable is not None
        assert durable.buffer_flushes >= 1
        assert durable.current_buffer_size_bytes == 0

    @pytest.mark.asyncio
    async def test_durable_stats_never_negative_on_delete(self, db_manager):
        set_buffer_backend("memory")

        buffer = ContextBuffer(db_manager, max_size_bytes=1048576)
        await buffer.add_pair("device-001", make_pair(request_body="x" * 500))
        pairs = await buffer.get_buffer_pairs("device-001")
        await buffer.delete_entry("device-001", pairs[0]["id"])

        durable = await _on_disk_stats(db_manager, "device-001")
        assert durable is not None
        assert durable.current_buffer_size_bytes >= 0

    @pytest.mark.asyncio
    async def test_disk_mode_writes_durable_db(self, db_manager):
        """Default (disk) mode still persists entries in the device SQLite DB."""
        assert get_buffer_backend() == "disk"

        buffer = ContextBuffer(db_manager, max_size_bytes=1048576)
        await buffer.add_pair("device-001", make_pair(request_body="z" * 300))

        async with db_manager.device_session("device-001") as session:
            result = await session.execute(
                select(LLMContextBuffer).where(LLMContextBuffer.device_id == "device-001")
            )
            rows = result.scalars().all()
        assert len(rows) == 1
        assert rows[0].correlated_pair["pair_id"] == "pair-001"

    @pytest.mark.asyncio
    async def test_store_factory_honors_current_backend(self, db_manager):
        assert get_buffer_backend() == "disk"
        disk_store = create_buffer_store(db_manager)
        assert disk_store._durable_stats is None

        set_buffer_backend("memory")
        mem_store = create_buffer_store(db_manager)
        assert mem_store._durable_stats is not None


class TestBackendPersistence:
    def test_persist_and_load_roundtrip(self, tmp_path, monkeypatch):
        settings_file = tmp_path / "runtime_settings.json"
        monkeypatch.setattr(buffer_pkg, "_settings_path", lambda: settings_file)

        set_buffer_backend("memory")
        persist_backend("memory")
        assert settings_file.exists()
        assert load_persisted_backend() == "memory"

        persist_backend("disk")
        assert load_persisted_backend() == "disk"

    def test_persist_rejects_unknown_backend(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            buffer_pkg, "_settings_path", lambda: tmp_path / "runtime_settings.json"
        )
        with pytest.raises(ValueError, match="Unknown buffer backend"):
            persist_backend("redis")

    def test_initialize_loads_persisted_backend(self, tmp_path, monkeypatch):
        settings_file = tmp_path / "runtime_settings.json"
        monkeypatch.setattr(buffer_pkg, "_settings_path", lambda: settings_file)
        persist_backend("memory")

        loaded = initialize_buffer_backend()
        assert loaded == "memory"
        assert get_buffer_backend() == "memory"

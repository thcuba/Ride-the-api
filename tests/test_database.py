"""
Tests for the Database module (DatabaseManager, models, CRUD).
"""

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import select

import core.database
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
    get_db_manager,
    init_db_manager,
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
#  Global get_db_manager / init_db_manager
# ---------------------------------------------------------------------------


class TestGetDbManagerGlobal:
    def test_get_db_manager_uninitialized(self, monkeypatch):
        """get_db_manager raises RuntimeError when _db_manager is None."""
        monkeypatch.setattr(core.database, "_db_manager", None)
        with pytest.raises(
            RuntimeError, match="DatabaseManager not initialized. Call init_db_manager first."
        ):
            get_db_manager()

    def test_get_db_manager_initialized(self, tmp_path, monkeypatch):
        """init_db_manager sets global _db_manager and get_db_manager returns it."""
        monkeypatch.setattr(core.database, "_db_manager", None)
        core_db_path = tmp_path / "core.db"
        device_db_dir = tmp_path / "device_dbs"

        manager = init_db_manager(
            core_db_url=f"sqlite+aiosqlite:///{core_db_path}",
            device_db_dir=device_db_dir,
        )

        assert manager is not None
        assert get_db_manager() is manager


# ---------------------------------------------------------------------------
#  DatabaseManager lifecycle
# ---------------------------------------------------------------------------


class TestDatabaseManagerLifecycle:
    @pytest.mark.asyncio
    async def test_initialize_creates_core_tables(self, db_manager):
        async with await db_manager.get_core_session() as session:
            result = await session.execute(select(DeviceRegistry).limit(1))
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
    async def test_get_or_create_device_missing_config_fallback(self, db_manager, monkeypatch):
        """Verify get_or_create_device falls back to empty dict when get_config fails."""

        def mock_get_config():
            raise RuntimeError("Config error")

        monkeypatch.setattr("core.database.get_config", mock_get_config)
        await db_manager.get_or_create_device(
            device_id="device-no-config",
            vendor="shelly",
            device_type="plug",
            name="No Config Plug",
        )
        async with await db_manager.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == "device-no-config")
            )
            dev = result.scalar_one()
            assert dev.config == {}

    @pytest.mark.asyncio
    async def test_get_or_create_device(self, db_manager):
        await db_manager.get_or_create_device(
            device_id="device-001",
            vendor="shelly",
            device_type="plug",
            name="Test Plug",
        )
        async with await db_manager.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == "device-001")
            )
            dev = result.scalar_one()
            assert dev.vendor == "shelly"
            assert dev.mode == "learning"

    @pytest.mark.asyncio
    async def test_get_or_create_device_is_idempotent(self, db_manager):
        for _ in range(3):
            await db_manager.get_or_create_device(
                device_id="device-001",
                vendor="shelly",
                device_type="plug",
                name="Test Plug",
            )
        async with await db_manager.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == "device-001")
            )
            assert result.scalars().all() is not None

    @pytest.mark.asyncio
    async def test_get_or_create_device_concurrent_no_duplicates(self, db_manager):
        """F-12: concurrent creation must not duplicate registry rows."""

        await asyncio.gather(
            *[
                db_manager.get_or_create_device(
                    device_id="device-race",
                    vendor="shelly",
                    device_type="plug",
                    name="Race Plug",
                )
                for _ in range(10)
            ]
        )
        async with await db_manager.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == "device-race")
            )
            rows = result.scalars().all()
            assert len(rows) == 1

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
            device_id="device-001",
            vendor="shelly",
            device_type="plug",
            name="Test",
        )
        # Set an IP address
        async with db_manager.core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == "device-001")
            )
            dev = result.scalar_one()
            dev.ip_addresses = ["192.168.1.100"]
        resolved = await db_manager.resolve_device_id("192.168.1.100")
        assert resolved == "device-001"

    @pytest.mark.asyncio
    async def test_resolve_device_id_not_found(self, db_manager):
        resolved = await db_manager.resolve_device_id("10.0.0.99")
        assert resolved is None

    @pytest.mark.asyncio
    async def test_list_devices_exposes_ip_addresses(self, db_manager):
        """``list_devices`` must return the IPs it stores on the registry.

        The dashboard renders its cards from ``/api/devices``, and the addresses live on
        ``DeviceRegistry.ip_addresses`` (populated by ``POST /api/devices/{id}/ip``), but they
        were missing from the listing, so the UI had nothing to display even though
        ``docs/api.md`` already documented the field.
        """
        await db_manager.get_or_create_device(
            device_id="device-003", vendor="example", device_type="ac", name="Room AC"
        )
        await db_manager.get_or_create_device(
            device_id="device-004", vendor="example", device_type="ac", name="No IP yet"
        )
        async with db_manager.core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == "device-003")
            )
            dev = result.scalar_one()
            dev.ip_addresses = ["192.168.1.55", "192.168.1.56"]
            await session.commit()

        by_id = {d["device_id"]: d for d in await db_manager.list_devices()}
        assert by_id["device-003"]["ip_addresses"] == ["192.168.1.55", "192.168.1.56"]
        # A device without addresses must expose an empty list, not a missing key.
        assert by_id["device-004"]["ip_addresses"] == []

    @pytest.mark.asyncio
    async def test_resolve_device_id_partial_ip_does_not_match(self, db_manager):
        """A partial IPv4 prefix must NOT resolve to a device storing a longer IP.

        Regression for the old ``JSON.contains`` substring match, where
        resolving ``192.168.1.1`` matched a device storing ``192.168.1.100`` and
        routed that traffic to the wrong device DB.
        """
        await db_manager.get_or_create_device(
            device_id="device-002",
            vendor="shelly",
            device_type="plug",
        )
        async with db_manager.core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == "device-002")
            )
            dev = result.scalar_one()
            dev.ip_addresses = ["192.168.1.100"]

        assert await db_manager.resolve_device_id("192.168.1.1") is None
        assert await db_manager.resolve_device_id("192.168.1.10") is None
        # And the exact IP still resolves
        assert await db_manager.resolve_device_id("192.168.1.100") == "device-002"

    @pytest.mark.asyncio
    async def test_resolve_device_id_cache_invalidates(self, db_manager):
        """The reverse index must be rebuilt after IP registration.

        ``resolve_device_id`` is cached so repeated lookups are O(1); when a
        device's ``ip_addresses`` change, ``invalidate_ip_lookup_cache`` drops
        the index so the next call sees the update.
        """
        await db_manager.get_or_create_device(
            device_id="device-cache",
            vendor="shelly",
            device_type="plug",
        )
        # Miss populates the empty index first.
        assert await db_manager.resolve_device_id("10.99.0.1") is None

        async with db_manager.core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == "device-cache")
            )
            dev = result.scalar_one()
            dev.ip_addresses = ["10.99.0.1"]

        # Still cached (stale) until explicitly invalidated.
        assert await db_manager.resolve_device_id("10.99.0.1") is None

        db_manager.invalidate_ip_lookup_cache()
        assert await db_manager.resolve_device_id("10.99.0.1") == "device-cache"


# ---------------------------------------------------------------------------
#  Device cache (get_device / invalidate_device_cache)
# ---------------------------------------------------------------------------


class TestDeviceCache:
    @pytest.mark.asyncio
    async def test_get_device_caches_lookup(self, db_manager):
        """get_device returns the device and serves it from the TTL cache."""
        await db_manager.get_or_create_device(
            device_id="device-cache-1", vendor="shelly", device_type="plug"
        )

        d1 = await db_manager.get_device("device-cache-1")
        assert d1 is not None
        assert d1.mode == "learning"

        # Second call is served from cache — same (expunged) object instance.
        d2 = await db_manager.get_device("device-cache-1")
        assert d2 is not None
        assert d2 is d1

    @pytest.mark.asyncio
    async def test_get_device_caches_missing(self, db_manager):
        """A missing device is cached as None to avoid repeated lookups."""
        assert await db_manager.get_device("no-such-device") is None
        # Cached None so the next call does not hit the DB again.
        assert await db_manager.get_device("no-such-device") is None

    @pytest.mark.asyncio
    async def test_get_device_reflected_after_mode_switch(self, db_manager):
        """Changing device mode invalidates the cache so get_device sees it."""
        await db_manager.get_or_create_device(
            device_id="device-cache-2", vendor="tapo", device_type="plug"
        )
        assert (await db_manager.get_device("device-cache-2")).mode == "learning"

        assert await db_manager.update_device_mode("device-cache-2", "production")
        assert (await db_manager.get_device("device-cache-2")).mode == "production"

    @pytest.mark.asyncio
    async def test_get_device_matches_db_reference(self, db_manager):
        """get_device row equals the registry row (all fields preserved)."""
        await db_manager.get_or_create_device(
            device_id="device-cache-3",
            vendor="hue",
            device_type="light",
            name="Liv 1",
        )
        d = await db_manager.get_device("device-cache-3")
        assert d is not None
        assert d.vendor == "hue"
        assert d.device_type == "light"
        assert d.name == "Liv 1"

    @pytest.mark.asyncio
    async def test_invalidate_device_cache_to_targeted(self, db_manager):
        """invalidate_device_cache(device_id) drops only that device's entry."""
        for i in (1, 2):
            await db_manager.get_or_create_device(
                device_id=f"inv-{i}", vendor="tapo", device_type="plug"
            )
        d1a = await db_manager.get_device("inv-1")
        d2a = await db_manager.get_device("inv-2")
        assert d1a is not None
        assert d2a is not None

        db_manager.invalidate_device_cache("inv-1")
        assert await db_manager.get_device("inv-2") is d2a  # still cached
        d1b = await db_manager.get_device("inv-1")
        assert d1b is not None
        assert d1b is not d1a  # re-fetched

    @pytest.mark.asyncio
    async def test_invalidate_device_cache_all(self, db_manager):
        """invalidate_device_cache() with no arg clears every device entry."""
        for i in (1, 2):
            await db_manager.get_or_create_device(
                device_id=f"invall-{i}", vendor="tapo", device_type="plug"
            )
        d1a = await db_manager.get_device("invall-1")
        d2a = await db_manager.get_device("invall-2")
        assert d1a is not None
        assert d2a is not None

        db_manager.invalidate_device_cache()
        d1b = await db_manager.get_device("invall-1")
        d2b = await db_manager.get_device("invall-2")
        assert d1b is not d1a
        assert d2b is not d2a


# ---------------------------------------------------------------------------
#  Device Session (per-device DB)
# ---------------------------------------------------------------------------


class TestDeviceSession:
    @pytest.mark.asyncio
    async def test_device_session_creates_tables(self, db_manager):
        """device_session should auto-create device DB tables."""
        async with db_manager.device_session("device-001") as session:
            result = await session.execute(select(RequestPattern).limit(1))
            assert result.scalars().all() == []

    @pytest.mark.asyncio
    async def test_creates_request_pattern(self, db_manager):
        async with db_manager.device_session("device-001") as session:
            session.add(
                RequestPattern(
                    pattern_id="rp1",
                    method="GET",
                    path_pattern="/status",
                    protocol="http",
                    intent="get_status",
                )
            )
        async with db_manager.device_session("device-001") as session:
            result = await session.execute(
                select(RequestPattern).where(RequestPattern.pattern_id == "rp1")
            )
            pattern = result.scalar_one()
            assert pattern.method == "GET"
            assert pattern.intent == "get_status"

    @pytest.mark.asyncio
    async def test_device_isolation(self, db_manager):
        async with db_manager.device_session("device-a") as session:
            session.add(
                RequestPattern(
                    pattern_id="a1",
                    method="GET",
                    path_pattern="/a",
                    protocol="http",
                    intent="get_a",
                )
            )
        async with db_manager.device_session("device-b") as session:
            result = await session.execute(
                select(RequestPattern).where(RequestPattern.pattern_id == "a1")
            )
            assert result.scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_response_template_crud(self, db_manager):
        async with db_manager.device_session("device-001") as session:
            session.add(
                RequestPattern(
                    pattern_id="rp1",
                    method="GET",
                    path_pattern="/test",
                    protocol="http",
                    intent="test",
                )
            )
        async with db_manager.device_session("device-001") as session:
            session.add(
                ResponseTemplate(
                    template_id="t1",
                    pattern_id="rp1",
                    status_code=200,
                    headers_template={"content-type": "application/json"},
                    body_template={"state": True},
                )
            )
        async with db_manager.device_session("device-001") as session:
            result = await session.execute(
                select(ResponseTemplate).where(ResponseTemplate.template_id == "t1")
            )
            tpl = result.scalar_one()
            assert tpl.status_code == 200  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_match_stats(self, db_manager):
        async with db_manager.device_session("device-001") as session:
            session.add(
                MatchStats(
                    device_id="device-001",
                    total_requests=10,
                    local_hits=8,
                    cloud_misses=2,
                    match_rate_pct=80.0,
                )
            )
        async with db_manager.device_session("device-001") as session:
            result = await session.execute(
                select(MatchStats).where(MatchStats.device_id == "device-001")
            )
            stats = result.scalar_one()
            assert stats.local_hits == 8  # noqa: PLR2004
            assert stats.match_rate_pct == 80.0  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_llm_context_buffer(self, db_manager):
        async with db_manager.device_session("device-001") as session:
            session.add(
                LLMContextBuffer(
                    device_id="device-001",
                    sequence=1,
                    estimated_size_bytes=256,
                    correlated_pair={"pair_id": "p1"},
                )
            )
        async with db_manager.device_session("device-001") as session:
            result = await session.execute(
                select(LLMContextBuffer).where(LLMContextBuffer.device_id == "device-001")
            )
            entries = result.scalars().all()
            assert len(entries) == 1

    @pytest.mark.asyncio
    async def test_field_mapping(self, db_manager):
        async with db_manager.device_session("device-001") as session:
            session.add(
                FieldMapping(
                    mapping_id="fm1",
                    request_field="id",
                    request_type="integer",
                    response_field="id",
                    response_type="integer",
                    intent="test",
                )
            )
        async with db_manager.device_session("device-001") as session:
            result = await session.execute(
                select(FieldMapping).where(FieldMapping.mapping_id == "fm1")
            )
            fm = result.scalar_one()
            assert fm.request_field == "id"

    @pytest.mark.asyncio
    async def test_session_cache(self, db_manager):
        async with db_manager.device_session("device-001") as session:
            session.add(
                SessionCache(
                    device_id="device-001",
                    correlation_key="ck1",
                    method="POST",
                    path="/test",
                )
            )
        async with db_manager.device_session("device-001") as session:
            result = await session.execute(
                select(SessionCache).where(SessionCache.correlation_key == "ck1")
            )
            sc = result.scalar_one()
            assert sc.method == "POST"


# ---------------------------------------------------------------------------
# ModelRegistry section
# ---------------------------------------------------------------------------


class TestModelRegistry:
    @pytest.mark.asyncio
    async def test_create_model(self, db_manager):
        async with db_manager.core_session() as session:
            session.add(
                ModelRegistry(
                    model_id="m1",
                    device_id="device-001",
                    version="1.0",
                    framework="onnx",
                    model_path="/models/m1.onnx",
                )
            )
        async with db_manager.core_session() as session:
            result = await session.execute(
                select(ModelRegistry).where(ModelRegistry.model_id == "m1")
            )
            model = result.scalar_one()
            assert model.version == "1.0"
            assert model.is_active is True

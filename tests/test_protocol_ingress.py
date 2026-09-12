"""
Tests for ingress protocol resolution and adapter selection.

Covers the PR that consumes `connection_mode`/`ProtocolInfo` at ingress:
- :meth:`core.database.DatabaseManager.resolve_device_protocol` (the
  ``ip_profiles > device_meta > default`` hierarchy).
- :meth:`core.server._select_handler_adapter` (vendor-specific adapter wins,
  else the adapter that supports the resolved protocol).
"""

import pytest
import pytest_asyncio
from sqlalchemy import select

from adapters import get_registered_registry
from core.database import DatabaseManager, DeviceRegistry
from core.pattern_db.schemas import DeviceMeta
from core.server import _select_handler_adapter


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


async def _set_connection(dm: DatabaseManager, device_id: str, connection: str):
    """Set extra_attributes["connection"] (as apply_ip_profile/API do)."""
    async with dm.core_session() as session:
        result = await session.execute(
            select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
        )
        device = result.scalar_one_or_none()
        extra = dict(device.extra_attributes or {})
        extra["connection"] = connection
        device.extra_attributes = extra


# ---------------------------------------------------------------------------
#  resolve_device_protocol — hierarchy ip_profiles > device_meta > default
# ---------------------------------------------------------------------------


class TestResolveDeviceProtocol:
    @pytest.mark.asyncio
    async def test_default_when_no_override_and_no_meta(self, db_manager):
        device_id = "ip-1-2-3-4"
        await db_manager.get_or_create_device(device_id, "unknown")
        assert (
            await db_manager.resolve_device_protocol(device_id, ingress_default="https") == "https"
        )

    @pytest.mark.asyncio
    async def test_auto_yields_to_default(self, db_manager):
        device_id = "ip-1-2-3-4"
        await db_manager.get_or_create_device(device_id, "unknown")
        await _set_connection(db_manager, device_id, "auto")
        assert await db_manager.resolve_device_protocol(device_id, ingress_default="http") == "http"

    @pytest.mark.asyncio
    async def test_ip_profile_connection_wins(self, db_manager):
        device_id = "ip-1-2-3-4"
        await db_manager.get_or_create_device(device_id, "unknown")
        await _set_connection(db_manager, device_id, "coap")
        await db_manager.write_device_meta(
            device_id, DeviceMeta(connection_mode="mqtt").model_dump()
        )
        # ip_profiles (extra_attributes) beats device_meta.
        assert await db_manager.resolve_device_protocol(device_id, ingress_default="http") == "coap"

    @pytest.mark.asyncio
    async def test_device_meta_wins_over_default(self, db_manager):
        device_id = "ip-1-2-3-4"
        await db_manager.get_or_create_device(device_id, "unknown")
        await db_manager.write_device_meta(
            device_id, DeviceMeta(connection_mode="modbus").model_dump()
        )
        assert (
            await db_manager.resolve_device_protocol(device_id, ingress_default="https") == "modbus"
        )

    @pytest.mark.asyncio
    async def test_auto_in_meta_yields_to_default(self, db_manager):
        device_id = "ip-1-2-3-4"
        await db_manager.get_or_create_device(device_id, "unknown")
        await db_manager.write_device_meta(
            device_id, DeviceMeta(connection_mode="auto").model_dump()
        )
        assert (
            await db_manager.resolve_device_protocol(device_id, ingress_default="https") == "https"
        )


# ---------------------------------------------------------------------------
#  _select_handler_adapter — vendor-specific first, then resolved protocol
# ---------------------------------------------------------------------------


class TestSelectHandlerAdapter:
    def test_vendor_specific_adapter_wins(self):
        registry = get_registered_registry()
        adapter = _select_handler_adapter(registry, "shelly", "mqtt")
        assert adapter is not None
        assert adapter.vendor == "shelly"

    def test_unknown_vendor_uses_resolved_protocol_adapter(self):
        registry = get_registered_registry()
        adapter = _select_handler_adapter(registry, "unknown", "mqtt")
        assert adapter is not None
        # The adapter must actually support mqtt.
        protocols = {p.value for p in adapter.supported_protocols}
        assert "mqtt" in protocols

    def test_unknown_vendor_unknown_protocol_returns_none(self):
        registry = get_registered_registry()
        assert _select_handler_adapter(registry, "unknown", "not-a-protocol") is None

    def test_none_registry_returns_none(self):
        assert _select_handler_adapter(None, "unknown", "mqtt") is None

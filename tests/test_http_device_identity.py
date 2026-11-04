"""On the HTTP data path a bound source IP decides the device (S-7)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from core import server as server_mod
from core.database import DatabaseManager, DeviceRegistry

ASGI_CLIENT_IP = "127.0.0.1"  # what httpx.ASGITransport reports


class _Adapter:
    async def parse_request(self, intercepted):
        return intercepted  # no vendor-specific identity


@pytest_asyncio.fixture
async def proxy(tmp_path, monkeypatch):
    dm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "devices",
    )
    await dm.initialize()
    registry = MagicMock()
    registry.get_adapter.return_value = _Adapter()
    orch = MagicMock()
    orch.handle_request = AsyncMock(
        return_value={
            "action": "local_response",
            "response": {"status_code": 200, "headers": {}, "body": {}},
        }
    )
    monkeypatch.setattr(server_mod.config_manager.config.security, "auth_enabled", False)
    monkeypatch.setattr(server_mod, "db_manager", dm)
    monkeypatch.setattr(server_mod, "orchestrator", orch)
    monkeypatch.setattr(server_mod, "adapter_registry", registry)
    monkeypatch.setattr(server_mod, "get_traffic_selector", lambda: None)
    transport = httpx.ASGITransport(app=server_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

        async def device_for(path: str = "/acme/api/status", **headers: str) -> str:
            orch.handle_request.reset_mock()
            response = await client.get(path, headers=headers)
            assert response.status_code == 200, response.text  # noqa: PLR2004
            return orch.handle_request.call_args.kwargs["device_id"]

        yield dm, device_for
    await dm.close()


async def _bind(dm: DatabaseManager, device_id: str, ip: str) -> None:
    await dm.get_or_create_device(device_id, "acme", "plug")
    async with dm.core_session() as session:
        device = (
            await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
        ).scalar_one()
        device.ip_addresses = [ip]
        await session.commit()
    dm.invalidate_ip_lookup_cache()


@pytest.mark.asyncio
async def test_a_bound_ip_wins_over_the_claimed_id(proxy):
    dm, device_for = proxy
    await _bind(dm, "living-room-plug", ASGI_CLIENT_IP)
    assert await device_for(**{"x-device-id": "someone-else"}) == "living-room-plug"
    assert await device_for("/acme/devices/ABCDEF123456/state") == "living-room-plug"


@pytest.mark.asyncio
async def test_an_unbound_client_keeps_its_claimed_id(proxy):
    _dm, device_for = proxy
    assert await device_for(**{"x-device-id": "plug-42"}) == "plug-42"
    assert await device_for("/acme/devices/ABCDEF123456/state") == "ABCDEF123456"


@pytest.mark.asyncio
async def test_empty_or_oversized_claims_are_ignored(proxy):
    _dm, device_for = proxy
    assert await device_for(**{"x-device-id": ""}) == "unknown"
    assert await device_for(**{"x-device-id": "x" * 1000}) == "unknown"

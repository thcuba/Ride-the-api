"""Traffic-selector rules take effect for real device traffic (C-20)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from fastapi.responses import JSONResponse

from core import server as server_mod
from core.database import DatabaseManager
from core.traffic_selector import (
    TrafficAction,
    TrafficRequestInfo,
    TrafficSelector,
    normalize_hostname,
)

# The rule shipped in config/config.yaml.
FIRMWARE_PASSTHROUGH = {
    "name": "firmware_passthrough",
    "scope": "external",
    "match_type": "hostname",
    "match_value": "firmware.example.com",
    "action": "passthrough",
    "priority": 20,
}


class _Config:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class _ConfigManager:
    def __init__(self, rules: list[dict]) -> None:
        self.config = _Config(traffic_selection=_Config(default_action="intercept", rules=rules))

    def register_callback(self, _callback) -> None:
        pass


def _selector() -> TrafficSelector:
    return TrafficSelector(config_manager=_ConfigManager([FIRMWARE_PASSTHROUGH]))


@pytest.mark.parametrize(
    "host",
    [
        "firmware.example.com",
        "firmware.example.com:443",
        "FIRMWARE.example.com.",
        "firmware.example.com:",
    ],
)
def test_external_rule_applies_to_lan_devices(host):
    info = TrafficRequestInfo(client_ip="192.168.1.50", hostname=host, is_local=True)
    assert _selector().evaluate(info) is TrafficAction.PASSTHROUGH


def test_other_hosts_keep_the_default():
    info = TrafficRequestInfo(client_ip="192.168.1.50", hostname="api.example.com", is_local=True)
    assert _selector().evaluate(info) is TrafficAction.INTERCEPT


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("[2001:db8::1]:8443", "2001:db8::1"),
        ("[2001:db8::1]", "2001:db8::1"),
        ("2001:db8::1", "2001:db8::1"),
        ("Host.Example.COM.:80", "host.example.com"),
        (None, None),
    ],
)
def test_normalize_hostname(raw, expected):
    assert normalize_hostname(raw) == expected


class _Registry:
    def get_adapter(self, _vendor):
        return MagicMock()

    def list_vendors(self):
        return ["acme"]


@pytest_asyncio.fixture
async def proxy(tmp_path, monkeypatch):
    dm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "devices",
    )
    await dm.initialize()
    orch = MagicMock()
    orch.handle_request = AsyncMock(return_value={"action": "forward"})
    forwarded = AsyncMock(return_value=JSONResponse({"from": "cloud"}))
    monkeypatch.setattr(server_mod.config_manager.config.security, "auth_enabled", False)
    monkeypatch.setattr(server_mod, "db_manager", dm)
    monkeypatch.setattr(server_mod, "orchestrator", orch)
    monkeypatch.setattr(server_mod, "adapter_registry", _Registry())
    monkeypatch.setattr(server_mod, "get_traffic_selector", _selector)
    monkeypatch.setattr(server_mod, "_forward_bypassed_request", forwarded)
    transport = httpx.ASGITransport(app=server_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, orch, forwarded
    await dm.close()


@pytest.mark.asyncio
async def test_passthrough_rule_forwards_without_the_pipeline(proxy):
    client, orch, forwarded = proxy
    response = await client.get("/acme/fw/latest.bin", headers={"host": "firmware.example.com:443"})
    assert response.json() == {"from": "cloud"}
    forwarded.assert_awaited_once()
    orch.handle_request.assert_not_called()

"""Tests for the common protocol-server request handler (handle_protocol_request)."""

from __future__ import annotations

import pytest

import core.server as server_mod
from adapters.base import InterceptedRequest, ProtocolType


class _FakeDB:
    async def get_or_create_device(self, device_id: str, vendor: str) -> None:
        pass


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def handle_request(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return {"action": "local_response", "response": {"ok": True}}


@pytest.mark.asyncio
async def test_protocol_request_routes_to_orchestrator(monkeypatch):
    db = _FakeDB()
    orch = _FakeOrchestrator()
    monkeypatch.setattr(server_mod, "db_manager", db)
    monkeypatch.setattr(server_mod, "orchestrator", orch)

    req = InterceptedRequest(
        device_id="coap-192-168-1-5",
        timestamp=0,
        protocol=ProtocolType.COAP,
        method="GET",
        path="/sensors/temp",
        body=None,
    )
    result = await server_mod.handle_protocol_request(req)

    assert result == {"action": "local_response", "response": {"ok": True}}
    assert orch.calls
    call = orch.calls[0]
    assert call["device_id"] == "coap-192-168-1-5"
    assert call["protocol"] == "coap"
    assert call["method"] == "GET"
    assert call["path"] == "/sensors/temp"


@pytest.mark.asyncio
async def test_mqtt_topic_maps_to_path_and_publish(monkeypatch):
    db = _FakeDB()
    orch = _FakeOrchestrator()
    monkeypatch.setattr(server_mod, "db_manager", db)
    monkeypatch.setattr(server_mod, "orchestrator", orch)

    req = InterceptedRequest(
        device_id="some-mqtt-device",
        timestamp=0,
        protocol=ProtocolType.MQTT,
        topic="home/sensors/temp",
        body={"t": 22.5},
    )
    await server_mod.handle_protocol_request(req)

    call = orch.calls[0]
    assert call["protocol"] == "mqtt"
    assert call["method"] == "publish"
    assert call["path"] == "/home/sensors/temp"


@pytest.mark.asyncio
async def test_protocol_request_service_not_ready(monkeypatch):
    monkeypatch.setattr(server_mod, "db_manager", None)
    monkeypatch.setattr(server_mod, "orchestrator", None)
    req = InterceptedRequest(device_id="d1", timestamp=0, protocol=ProtocolType.HTTP)
    result = await server_mod.handle_protocol_request(req)
    assert result is None

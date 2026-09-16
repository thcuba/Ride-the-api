"""Integration tests for the per-device threshold and context-buffer endpoints."""

import json

import pytest
from fastapi.testclient import TestClient

import core.server as server_mod
from core.server import app

_HTTP_OK = 200
_HTTP_BAD_REQUEST = 400
_HTTP_NOT_FOUND = 404
_HTTP_SERVICE_UNAVAILABLE = 503
_1MB = 1048576


class _FakeDB:
    """Minimal db_manager stub supporting the two device-config writers."""

    def __init__(self) -> None:
        self.threshold_calls = []
        self.buffer_calls = []

    async def update_device_match_threshold(self, device_id, threshold):
        self.threshold_calls.append((device_id, threshold))
        return True

    async def update_device_context_buffer_size(self, device_id, size):
        self.buffer_calls.append((device_id, size))
        return True


@pytest.fixture
def client(monkeypatch):
    """TestClient with a fake db_manager and control-plane auth disabled."""
    server_mod.config_manager.config.security.auth_enabled = False
    fake = _FakeDB()
    monkeypatch.setattr(server_mod, "db_manager", fake)
    return TestClient(app), fake


def test_set_threshold_valid(client):
    tc, fake = client
    resp = tc.put(
        "/api/devices/d1/threshold",
        content=json.dumps({"match_threshold": 0.9}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == _HTTP_OK
    assert resp.json() == {"device_id": "d1", "match_threshold": 0.9}
    assert fake.threshold_calls == [("d1", 0.9)]


def test_set_threshold_out_of_range(client):
    tc, _ = client
    resp = tc.put(
        "/api/devices/d1/threshold",
        content=json.dumps({"match_threshold": 1.5}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == _HTTP_BAD_REQUEST
    assert "between 0.0 and 1.0" in resp.json()["error"]


def test_set_threshold_missing_key(client):
    tc, _ = client
    resp = tc.put(
        "/api/devices/d1/threshold",
        content=json.dumps({"other": 1.0}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == _HTTP_BAD_REQUEST
    assert "match_threshold" in resp.json()["error"]


def test_set_context_buffer_valid(client):
    tc, fake = client
    resp = tc.put(
        "/api/devices/d1/context-buffer",
        content=json.dumps({"context_buffer_size": _1MB}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == _HTTP_OK
    assert resp.json() == {"device_id": "d1", "context_buffer_size": _1MB}
    assert fake.buffer_calls == [("d1", _1MB)]


def test_set_context_buffer_non_positive(client):
    tc, _ = client
    resp = tc.put(
        "/api/devices/d1/context-buffer",
        content=json.dumps({"context_buffer_size": -5}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == _HTTP_BAD_REQUEST
    assert "must be positive" in resp.json()["error"]


def test_config_endpoints_service_unavailable(monkeypatch):
    """503 is returned when db_manager is None."""
    server_mod.config_manager.config.security.auth_enabled = False
    monkeypatch.setattr(server_mod, "db_manager", None)
    tc = TestClient(app)
    resp = tc.put("/api/devices/d1/threshold", content=json.dumps({"match_threshold": 0.9}))
    assert resp.status_code == _HTTP_SERVICE_UNAVAILABLE

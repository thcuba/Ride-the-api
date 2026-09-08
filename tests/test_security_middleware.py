"""Tests for the control-plane authentication middleware (F-01)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import core.server as server_mod
from core.server import app

_ADMIN_KEY = "test-admin-key-123"
_READONLY_KEY = "test-readonly-key-456"


class _FakeDB:
    def __init__(self) -> None:
        self.updated = []

    async def list_devices(self):
        return []

    async def update_device_mode(self, device_id: str, mode: str) -> bool:
        self.updated.append((device_id, mode))
        return True


class _FakeCertManager:
    def ca_cert_pem(self) -> str:
        return "-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n"


@pytest.fixture
def client(monkeypatch):
    """TestClient with a fake db_manager and deterministic API keys."""
    cfg = server_mod.config_manager.config
    saved = (cfg.security.auth_enabled, cfg.security.admin_api_key, cfg.security.readonly_api_key)
    cfg.security.auth_enabled = True
    cfg.security.admin_api_key = _ADMIN_KEY
    cfg.security.readonly_api_key = _READONLY_KEY
    monkeypatch.setattr(server_mod, "db_manager", _FakeDB())
    monkeypatch.setattr(server_mod, "orchestrator", object())
    monkeypatch.setattr(server_mod, "cert_manager", _FakeCertManager())
    yield TestClient(app)
    cfg.security.auth_enabled, cfg.security.admin_api_key, cfg.security.readonly_api_key = saved


def _get(client, path, key=None):
    headers = {"X-API-Key": key} if key else {}
    return client.get(path, headers=headers)


def _post(client, path, key=None, json=None):
    headers = {"X-API-Key": key} if key else {}
    return client.post(path, headers=headers, json=json or {})


def test_read_without_key_returns_401(client):
    assert _get(client, "/api/devices").status_code == 401


def test_read_with_readonly_key_ok(client):
    resp = _get(client, "/api/devices", key=_READONLY_KEY)
    assert resp.status_code == 200


def test_read_with_admin_key_ok(client):
    resp = _get(client, "/api/devices", key=_ADMIN_KEY)
    assert resp.status_code == 200


def test_read_with_wrong_key_returns_401(client):
    assert _get(client, "/api/devices", key="wrong-key").status_code == 401


def test_write_without_key_returns_401(client):
    assert _post(client, "/api/devices/some-device/mode", json={"mode": "production"}).status_code == 401


def test_write_with_readonly_key_returns_401(client):
    resp = _post(client, "/api/devices/some-device/mode", key=_READONLY_KEY, json={"mode": "production"})
    assert resp.status_code == 401


def test_write_with_admin_key_ok(client):
    resp = _post(client, "/api/devices/some-device/mode", key=_ADMIN_KEY, json={"mode": "production"})
    assert resp.status_code == 200


def test_write_with_bearer_admin_key_ok(client):
    resp = client.post(
        "/api/devices/some-device/mode",
        headers={"Authorization": f"Bearer {_ADMIN_KEY}"},
        json={"mode": "production"},
    )
    assert resp.status_code == 200


def test_health_endpoint_public(client):
    assert client.get("/health").status_code == 200


def test_ca_cert_endpoint_public(client):
    resp = client.get("/api/tls/ca-cert")
    assert resp.status_code == 200
    assert "BEGIN CERTIFICATE" in resp.text


def test_unauthorized_returns_www_authenticate(client):
    resp = _get(client, "/api/devices")
    assert resp.headers.get("www-authenticate") == "Bearer"

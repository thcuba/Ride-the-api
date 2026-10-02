"""Tests for server exception-handling fixes.

Covers:
- malformed JSON bodies return a client-friendly 400 (global JSONDecodeError
  handler) instead of an unhandled 500 from unguarded ``request.json()``;
- non-object JSON bodies are rejected cleanly (no AttributeError);
- ``_get_request_body`` treats non-JSON payloads as absent without raising.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import core.server as server_mod
from core.server import _get_request_body, app

_HTTP_BAD_REQUEST = 400
_HTTP_OK = 200


class _FakeDB:
    def __init__(self) -> None:
        self.updated = []
        self.device_db_dir = Path("/tmp/test_db_dir")

    @staticmethod
    def _is_ip(value: str) -> bool:
        import ipaddress
        try:
            ipaddress.ip_address(value)
            return True
        except ValueError:
            return False

    async def update_device_mode(self, device_id: str, mode: str) -> bool:
        self.updated.append((device_id, mode))
        return True

    async def assign_device_database(
        self, device_id: str, database_url: str | None = None, database_name: str | None = None
    ) -> bool:
        _ = (device_id, database_url, database_name)  # params consumed (fake DB contract)
        return True

    async def resolve_device_id(self, ip_address: str) -> str | None:
        return "dev-1" if ip_address == "192.168.1.100" else None

    async def is_ips_bypassed(self, ip_address: str) -> bool:
        return False


@pytest.fixture
def client(monkeypatch):
    """TestClient with a fake db_manager so routes get past the 503 guard.

    Control-plane auth is disabled for these exception-handling tests (they
    exercise request parsing, not authentication); dedicated auth tests live
    in test_security_middleware.py.
    """
    server_mod.config_manager.config.security.auth_enabled = False
    monkeypatch.setattr(server_mod, "db_manager", _FakeDB())
    monkeypatch.setattr(server_mod, "orchestrator", object())
    return TestClient(app)


def test_malformed_json_body_returns_400(client):
    """Unguarded request.json() must yield a clean 400, not an unhandled 500."""
    resp = client.post(
        "/api/devices/some-device/mode",
        content="not-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == _HTTP_BAD_REQUEST
    assert resp.json() == {"error": "Invalid JSON body"}


def test_non_object_json_body_returns_400(client):
    """A JSON list/scalar body must not crash body.get() with an AttributeError."""
    resp = client.post(
        "/api/devices/some-device/mode",
        content=json.dumps(["learning"]),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == _HTTP_BAD_REQUEST
    assert resp.json() == {"error": "Invalid JSON body: expected an object"}


def test_valid_json_object_still_processes(client):
    """Valid object bodies continue to reach the handler logic."""
    resp = client.post(
        "/api/devices/some-device/mode",
        content=json.dumps({"mode": "production"}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == _HTTP_OK
    assert resp.json()["mode"] == "production"


@pytest.mark.asyncio
async def test_get_request_body_returns_none_on_non_json():
    """Non-JSON payloads must produce None (no crash, no swallowed exception)."""

    class _Req:
        async def body(self) -> bytes:
            return b"<xml><a>1</a></xml>"

    assert await _get_request_body(_Req()) is None


@pytest.mark.asyncio
async def test_get_request_body_parses_json():
    class _Req:
        async def body(self) -> bytes:
            return b'{"key": "value"}'

    assert await _get_request_body(_Req()) == {"key": "value"}


@pytest.mark.asyncio
async def test_get_request_body_empty_is_none():
    class _Req:
        async def body(self) -> bytes:
            return b""

    assert await _get_request_body(_Req()) is None


def test_assign_device_database_path_traversal_rejected(client):
    """Path traversal in database_name parameter must be rejected with 400."""
    resp = client.post(
        "/api/devices/some-device/database",
        json={"database_name": "../../evil_db"},
    )
    assert resp.status_code == _HTTP_BAD_REQUEST
    assert "Invalid database_name" in resp.json()["error"]


def test_assign_device_database_valid_name_accepted(client):
    """Valid database_name parameter must be accepted."""
    resp = client.post(
        "/api/devices/some-device/database",
        json={"database_name": "valid_db_name"},
    )
    assert resp.status_code == _HTTP_OK
    assert resp.json()["database_name"] == "valid_db_name"


def test_register_device_ip_invalid_format_rejected(client):
    """Invalid IP format in register_device_ip must be rejected with 400."""
    resp = client.post(
        "/api/devices/some-device/ip",
        json={"ip_address": "invalid_ip_format"},
    )
    assert resp.status_code == _HTTP_BAD_REQUEST
    assert resp.json() == {"error": "Invalid IP address format"}


def test_get_device_by_ip_invalid_format_rejected(client):
    """Invalid IP format in get_device_by_ip must be rejected with 400."""
    resp = client.get("/api/devices/by-ip/not-an-ip")
    assert resp.status_code == _HTTP_BAD_REQUEST
    assert resp.json() == {"error": "Invalid IP address format"}


def test_get_ip_bypass_invalid_format_rejected(client):
    """Invalid IP format in get_ip_bypass must be rejected with 400."""
    resp = client.get("/api/ip-profiles/not-an-ip/bypass")
    assert resp.status_code == _HTTP_BAD_REQUEST
    assert resp.json() == {"error": "Invalid IP address format"}


def test_set_ip_bypass_invalid_format_rejected(client):
    """Invalid IP format in set_ip_bypass must be rejected with 400."""
    resp = client.put("/api/ip-profiles/not-an-ip/bypass", json={"bypass": True})
    assert resp.status_code == _HTTP_BAD_REQUEST
    assert resp.json() == {"error": "Invalid IP address format"}


def test_get_device_by_ip_valid_format_accepted(client):
    """Valid IP format in get_device_by_ip is accepted."""
    resp = client.get("/api/devices/by-ip/192.168.1.100")
    assert resp.status_code == _HTTP_OK
    assert resp.json() == {"device_id": "dev-1", "ip_address": "192.168.1.100"}


def test_system_update_starts_detached_updater(client, monkeypatch):
    """POST /api/system/update launches the updater detached and reports started."""
    launched: dict = {}
    devnull = object()

    class _SP:
        DEVNULL = devnull

        @staticmethod
        def Popen(*args, **kwargs) -> object:  # noqa: ANN001
            launched["args"] = args
            launched["kwargs"] = kwargs
            return object()

    monkeypatch.setattr(server_mod, "subprocess", _SP)
    resp = client.post("/api/system/update")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["status"] == "started"
    kwargs = launched["kwargs"]
    assert kwargs["start_new_session"] is True
    assert launched["args"][0][1].endswith("update.sh")
    assert kwargs["cwd"].endswith(("ride-the-api", "Ride-the-api", "app", "APP"))


def test_system_update_finds_script_in_frozen_bundle(client, monkeypatch, tmp_path):
    """In a PyInstaller bundle the updater lives under _internal/deploy/."""
    bundle = tmp_path
    deploy = bundle / "deploy"
    deploy.mkdir()
    (deploy / "update.sh").touch()
    launched: dict = {}

    class _SP:
        DEVNULL = object()

        @staticmethod
        def Popen(*args, **kwargs) -> object:  # noqa: ANN001
            launched["args"] = args
            launched["kwargs"] = kwargs
            return object()

    monkeypatch.setattr(server_mod, "subprocess", _SP)
    monkeypatch.setattr(server_mod, "is_frozen", lambda: True)
    monkeypatch.setattr(server_mod, "bundle_root", lambda: bundle)
    resp = client.post("/api/system/update")
    assert resp.status_code == _HTTP_OK
    assert launched["args"][0][1] == str(deploy / "update.sh")
    assert launched["args"][0][5] == "systemd"

"""Tests for server exception-handling fixes.

Covers:
- malformed JSON bodies return a client-friendly 400 (global JSONDecodeError
  handler) instead of an unhandled 500 from unguarded ``request.json()``;
- non-object JSON bodies are rejected cleanly (no AttributeError);
- ``_get_request_body`` treats non-JSON payloads as absent without raising.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import core.server as server_mod
from core.server import _get_request_body, app


from pathlib import Path


class _FakeDB:
    def __init__(self) -> None:
        self.updated = []
        self.device_db_dir = Path("/tmp/test_db_dir")

    async def update_device_mode(self, device_id: str, mode: str) -> bool:
        self.updated.append((device_id, mode))
        return True

    async def assign_device_database(
        self, device_id: str, database_url: str | None = None, database_name: str | None = None
    ) -> bool:
        return True


@pytest.fixture
def client(monkeypatch):
    """TestClient with a fake db_manager so routes get past the 503 guard."""
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
    assert resp.status_code == 400
    assert resp.json() == {"error": "Invalid JSON body"}


def test_non_object_json_body_returns_400(client):
    """A JSON list/scalar body must not crash body.get() with an AttributeError."""
    resp = client.post(
        "/api/devices/some-device/mode",
        content=json.dumps(["learning"]),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "Invalid JSON body: expected an object"}


def test_valid_json_object_still_processes(client):
    """Valid object bodies continue to reach the handler logic."""
    resp = client.post(
        "/api/devices/some-device/mode",
        content=json.dumps({"mode": "production"}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200
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
    assert resp.status_code == 400
    assert "Invalid database_name" in resp.json()["error"]


def test_assign_device_database_valid_name_accepted(client):
    """Valid database_name parameter must be accepted."""
    resp = client.post(
        "/api/devices/some-device/database",
        json={"database_name": "valid_db_name"},
    )
    assert resp.status_code == 200
    assert resp.json()["database_name"] == "valid_db_name"
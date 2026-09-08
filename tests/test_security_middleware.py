"""Tests for the control-plane authentication middleware (F-01)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

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


# ── F-09: MaxBodySizeMiddleware ────────────────────────────────────────────────


async def _echo_body(request):
    body = await request.body()
    return JSONResponse({"len": len(body)})


def _make_size_app(max_size):
    from starlette.applications import Starlette
    from starlette.routing import Route

    from core.security import MaxBodySizeMiddleware

    inner = Starlette(
        routes=[
            Route("/api/echo", _echo_body, methods=["POST"]),
            Route("/data/echo", _echo_body, methods=["POST"]),
        ]
    )
    return MaxBodySizeMiddleware(inner, max_size=max_size)


def test_body_size_content_length_rejected():
    from starlette.testclient import TestClient

    client = TestClient(_make_size_app(100))
    resp = client.post("/api/echo", content=b"x" * 200)
    assert resp.status_code == 413


def test_body_size_under_limit_ok():
    from starlette.testclient import TestClient

    client = TestClient(_make_size_app(100))
    resp = client.post("/api/echo", content=b"x" * 50)
    assert resp.status_code == 200
    assert resp.json()["len"] == 50


def test_body_size_data_plane_exempt():
    from starlette.testclient import TestClient

    client = TestClient(_make_size_app(100))
    resp = client.post("/data/echo", content=b"x" * 5000)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_body_size_streaming_over_limit_413():
    """Chunked bodies (no Content-Length) are guarded via the receive channel."""
    from core.security import MaxBodySizeMiddleware

    async def inner(scope, receive, send):
        total = 0
        while True:
            msg = await receive()
            if msg["type"] == "http.request":
                total += len(msg.get("body") or b"")
                if not msg.get("more_body"):
                    break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": str(total).encode()})

    class _ChunkedReceive:
        def __init__(self, chunks):
            self._chunks = list(chunks) + [b""]
            self._i = 0

        async def __call__(self):
            msg = {
                "type": "http.request",
                "body": self._chunks[self._i],
                "more_body": self._i < len(self._chunks) - 1,
            }
            self._i += 1
            return msg

    class _Recorder:
        def __init__(self):
            self.messages = []

        async def __call__(self, message):
            self.messages.append(message)

    app = MaxBodySizeMiddleware(inner, max_size=10)
    recorder = _Recorder()
    await app(
        {"type": "http", "path": "/api/echo", "headers": []},
        _ChunkedReceive([b"aaaa", b"bbbb", b"cccc"]),
        recorder,
    )
    assert recorder.messages[0]["status"] == 413

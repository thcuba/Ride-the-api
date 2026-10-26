"""Tests for the control-plane authentication middleware (F-01)."""

from __future__ import annotations

import stat

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

import core.server as server_mod
from core import security as security_mod
from core.security import MaxBodySizeMiddleware
from core.server import app

_PASSWORD = "test-password-123"


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
    """TestClient with a fake db_manager and a deterministic password."""
    cfg = server_mod.config_manager.config
    saved = (cfg.security.auth_enabled, cfg.security.password)
    cfg.security.auth_enabled = True
    cfg.security.password = _PASSWORD
    monkeypatch.setattr(server_mod, "db_manager", _FakeDB())
    monkeypatch.setattr(server_mod, "orchestrator", object())
    monkeypatch.setattr(server_mod, "cert_manager", _FakeCertManager())
    yield TestClient(app)
    cfg.security.auth_enabled, cfg.security.password = saved


def _get(client, path, key=None):
    headers = {"X-API-Key": key} if key else {}
    return client.get(path, headers=headers)


def _post(client, path, key=None, json=None):
    headers = {"X-API-Key": key} if key else {}
    return client.post(path, headers=headers, json=json or {})


def test_read_without_password_returns_401(client):
    assert _get(client, "/api/devices").status_code == 401  # noqa: PLR2004


def test_read_with_password_ok(client):
    resp = _get(client, "/api/devices", key=_PASSWORD)
    assert resp.status_code == 200  # noqa: PLR2004


def test_read_with_wrong_password_returns_401(client):
    assert _get(client, "/api/devices", key="wrong-password").status_code == 401  # noqa: PLR2004


def test_write_without_password_returns_401(client):
    assert (
        _post(client, "/api/devices/some-device/mode", json={"mode": "production"}).status_code
        == 401  # noqa: PLR2004
    )


def test_write_with_password_ok(client):
    resp = _post(
        client,
        "/api/devices/some-device/mode",
        key=_PASSWORD,
        json={"mode": "production"},
    )
    assert resp.status_code == 200  # noqa: PLR2004


def test_write_with_bearer_password_ok(client):
    resp = client.post(
        "/api/devices/some-device/mode",
        headers={"Authorization": f"Bearer {_PASSWORD}"},
        json={"mode": "production"},
    )
    assert resp.status_code == 200  # noqa: PLR2004


def test_single_level_access_same_password_for_read_and_write(client):
    """The one password grants both reads and writes (no read-only distinction)."""
    assert _get(client, "/api/devices", key=_PASSWORD).status_code == 200  # noqa: PLR2004
    write = _post(
        client,
        "/api/devices/some-device/mode",
        key=_PASSWORD,
        json={"mode": "production"},
    )
    assert write.status_code == 200  # noqa: PLR2004


def test_health_endpoint_public(client):
    assert client.get("/health").status_code == 200  # noqa: PLR2004


def test_ca_cert_endpoint_public(client):
    resp = client.get("/api/tls/ca-cert")
    assert resp.status_code == 200  # noqa: PLR2004
    assert "BEGIN CERTIFICATE" in resp.text


def test_unauthorized_returns_www_authenticate(client):
    resp = _get(client, "/api/devices")
    assert resp.headers.get("www-authenticate") == "Bearer"


# ── Empty password (auth enabled) locks the control plane ───────────────────


def test_empty_password_locks_control_plane_with_instruction(monkeypatch):
    """With auth on and an empty password, the API tells the operator to set it."""
    cfg = server_mod.config_manager.config
    saved = (cfg.security.auth_enabled, cfg.security.password)
    cfg.security.auth_enabled = True
    cfg.security.password = ""
    monkeypatch.setattr(server_mod, "db_manager", _FakeDB())
    monkeypatch.setattr(server_mod, "orchestrator", object())
    monkeypatch.setattr(server_mod, "cert_manager", _FakeCertManager())
    try:
        client = TestClient(app)
        resp = client.get("/api/devices")
        assert resp.status_code == 503  # noqa: PLR2004
        assert "security.password" in resp.json()["error"]
        # Even a non-empty header does not bypass the lock.
        assert (
            client.get("/api/devices", headers={"X-API-Key": "whatever"}).status_code == 503  # noqa: PLR2004
        )
    finally:
        cfg.security.auth_enabled, cfg.security.password = saved


# ── First-run password setup (/api/auth/password) ───────────────────────────


def test_empty_password_503_carries_x_auth_setup_header(monkeypatch):
    """The locked 503 tells the frontend a password must be created."""
    cfg = server_mod.config_manager.config
    saved = (cfg.security.auth_enabled, cfg.security.password)
    cfg.security.auth_enabled = True
    cfg.security.password = ""
    monkeypatch.setattr(server_mod, "db_manager", _FakeDB())
    monkeypatch.setattr(server_mod, "orchestrator", object())
    monkeypatch.setattr(server_mod, "cert_manager", _FakeCertManager())
    try:
        client = TestClient(app)
        resp = client.get("/api/devices")
        assert resp.status_code == 503  # noqa: PLR2004
        assert resp.headers.get("X-Auth-Setup") == "required"
    finally:
        cfg.security.auth_enabled, cfg.security.password = saved


def test_setup_password_allows_first_run_set(monkeypatch):
    """With an empty password, /api/auth/password is reachable and sets it."""
    cfg = server_mod.config_manager.config
    saved = (cfg.security.auth_enabled, cfg.security.password)
    cfg.security.auth_enabled = True
    cfg.security.password = ""
    monkeypatch.setattr(server_mod, "db_manager", _FakeDB())
    monkeypatch.setattr(server_mod, "orchestrator", object())
    monkeypatch.setattr(server_mod, "cert_manager", _FakeCertManager())
    calls = []
    monkeypatch.setattr(
        server_mod.config_manager,
        "set_security_password",
        lambda pw: calls.append(pw) or True,
    )
    monkeypatch.setattr(security_mod, "_SETUP_TOKEN", "test-setup-token")
    try:
        client = TestClient(app)
        resp = client.post(
            "/api/auth/password",
            json={"password": "nuova-password-123"},
            headers={"X-Setup-Token": "test-setup-token"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        assert calls == ["nuova-password-123"]
    finally:
        cfg.security.auth_enabled, cfg.security.password = saved


def test_setup_password_rejects_short_or_mismatched(monkeypatch):
    """Empty / too-short / non-dict bodies are rejected without touching config."""
    cfg = server_mod.config_manager.config
    saved = (cfg.security.auth_enabled, cfg.security.password)
    cfg.security.auth_enabled = True
    cfg.security.password = ""
    monkeypatch.setattr(server_mod, "db_manager", _FakeDB())
    monkeypatch.setattr(server_mod, "orchestrator", object())
    monkeypatch.setattr(server_mod, "cert_manager", _FakeCertManager())
    try:
        monkeypatch.setattr(security_mod, "_SETUP_TOKEN", "test-setup-token")
        auth = {"X-Setup-Token": "test-setup-token"}
        client = TestClient(app)
        assert (
            client.post("/api/auth/password", json={"password": ""}, headers=auth).status_code
            == 400  # noqa: PLR2004
        )
        assert (
            client.post("/api/auth/password", json={"password": "short"}, headers=auth).status_code
            == 400  # noqa: PLR2004
        )
        assert (
            client.post(
                "/api/auth/password",
                content=b"not-json",
                headers={**auth, "Content-Type": "application/json"},
            ).status_code
            == 400  # noqa: PLR2004
        )
    finally:
        cfg.security.auth_enabled, cfg.security.password = saved


def test_setup_password_forbidden_once_already_set(client):
    """Once a password exists, the setup endpoint returns 403 (even with key)."""
    resp = client.post(
        "/api/auth/password",
        json={"password": "overrides-123"},
        headers={"X-API-Key": _PASSWORD},
    )
    assert resp.status_code == 403  # noqa: PLR2004


def test_first_install_end_to_end_flow(monkeypatch):
    """Reproduce the real Windows first-boot UX against a live app instance.

    Starts in the pristine state (auth enabled, empty password), asserts the
    UI sees the 503 + X-Auth-Setup signal, creates the password through the
    API, and confirms the very next request is authorized with it — exactly
    the flow a fresh install goes through. The set_security_password mock
    mutates the in-memory config (the real method also persists to disk,
    which we avoid here), so the middleware, which reads live config per
    request, genuinely picks up the new password.
    """
    cfg = server_mod.config_manager.config
    saved = (cfg.security.auth_enabled, cfg.security.password)
    cfg.security.auth_enabled = True
    cfg.security.password = ""

    def _set_password(pw: str) -> bool:
        """Mirror set_security_password but persist only to in-memory state."""
        cfg.security.password = pw
        return True

    monkeypatch.setattr(server_mod.config_manager, "set_security_password", _set_password)
    monkeypatch.setattr(security_mod, "_SETUP_TOKEN", "test-setup-token")
    monkeypatch.setattr(server_mod, "db_manager", _FakeDB())
    monkeypatch.setattr(server_mod, "orchestrator", object())
    monkeypatch.setattr(server_mod, "cert_manager", _FakeCertManager())
    try:
        client = TestClient(app)

        # 1) First boot: every protected call is locked and signals setup.
        locked = client.get("/api/devices")
        assert locked.status_code == 503  # noqa: PLR2004
        assert locked.headers.get("X-Auth-Setup") == "required"

        # 2) The UI creates the control-plane password via the API.
        new_password = "prima-password-123"
        created = client.post(
            "/api/auth/password",
            json={"password": new_password},
            headers={"X-Setup-Token": "test-setup-token"},
        )
        assert created.status_code == 200  # noqa: PLR2004

        # 3) The next request, using the fresh password, is authorized.
        authorized = client.get(
            "/api/devices",
            headers={"X-API-Key": new_password},
        )
        assert authorized.status_code == 200  # noqa: PLR2004

        # 4) And the setup endpoint is no longer a bypass.
        replay = client.post(
            "/api/auth/password",
            json={"password": "overrides-123"},
            headers={"X-API-Key": new_password},
        )
        assert replay.status_code == 403  # noqa: PLR2004
    finally:
        cfg.security.auth_enabled, cfg.security.password = saved


def test_setup_token_required_while_no_password(monkeypatch):
    """The first-run window must not be claimable by whoever reaches the port."""
    cfg = server_mod.config_manager.config
    saved = (cfg.security.auth_enabled, cfg.security.password)
    cfg.security.auth_enabled = True
    cfg.security.password = ""
    monkeypatch.setattr(security_mod, "_SETUP_TOKEN", "test-setup-token")
    monkeypatch.setattr(server_mod, "db_manager", _FakeDB())
    monkeypatch.setattr(server_mod, "orchestrator", object())
    monkeypatch.setattr(server_mod, "cert_manager", _FakeCertManager())
    try:
        client = TestClient(app)
        resp = client.post("/api/auth/password", json={"password": "nuova-password-123"})
        assert resp.status_code == 403  # noqa: PLR2004
        assert resp.headers.get("X-Auth-Setup") == "token-required"
        assert "setup token" in resp.json()["error"].lower()

        wrong = client.post(
            "/api/auth/password",
            json={"password": "nuova-password-123"},
            headers={"X-Setup-Token": "not-the-token"},
        )
        assert wrong.status_code == 403  # noqa: PLR2004
    finally:
        cfg.security.auth_enabled, cfg.security.password = saved


def test_setup_token_unlocks_first_run_set(monkeypatch):
    """The correct token completes the bootstrap."""
    cfg = server_mod.config_manager.config
    saved = (cfg.security.auth_enabled, cfg.security.password)
    cfg.security.auth_enabled = True
    cfg.security.password = ""
    monkeypatch.setattr(security_mod, "_SETUP_TOKEN", "test-setup-token")
    monkeypatch.setattr(server_mod, "db_manager", _FakeDB())
    monkeypatch.setattr(server_mod, "orchestrator", object())
    monkeypatch.setattr(server_mod, "cert_manager", _FakeCertManager())
    calls = []
    monkeypatch.setattr(
        server_mod.config_manager, "set_security_password", lambda pw: calls.append(pw) or True
    )
    try:
        client = TestClient(app)
        resp = client.post(
            "/api/auth/password",
            json={"password": "nuova-password-123"},
            headers={"X-Setup-Token": "test-setup-token"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        assert calls == ["nuova-password-123"]
    finally:
        cfg.security.auth_enabled, cfg.security.password = saved


def test_setup_token_is_persisted_owner_only(monkeypatch, tmp_path):
    """The operator can read the token from the filesystem, and nobody else."""
    monkeypatch.setattr(security_mod, "_SETUP_TOKEN", None)
    target = tmp_path / "ridebase" / "setup-token"
    token = security_mod.setup_token(target)

    assert token
    assert target.read_text().strip() == token
    assert stat.S_IMODE(target.stat().st_mode) == 0o600  # noqa: PLR2004
    # Idempotent: a second call returns the same token (no re-issue).
    assert security_mod.setup_token() == token


# ── F-09: MaxBodySizeMiddleware ────────────────────────────────────────────────


async def _echo_body(request):
    body = await request.body()
    return JSONResponse({"len": len(body)})


def _make_size_app(max_size):

    inner = Starlette(
        routes=[
            Route("/api/echo", _echo_body, methods=["POST"]),
            Route("/data/echo", _echo_body, methods=["POST"]),
        ]
    )
    return MaxBodySizeMiddleware(inner, max_size=max_size)


def test_body_size_content_length_rejected():

    client = TestClient(_make_size_app(100))
    resp = client.post("/api/echo", content=b"x" * 200)
    assert resp.status_code == 413  # noqa: PLR2004


@pytest.mark.asyncio
async def test_body_size_limit_covers_paths_that_normalize_into_api():
    """Path normalization must not bypass the control-plane body cap."""
    sent = []

    async def receive():
        raise AssertionError("Content-Length rejection should happen before reading the body")

    async def send(message):
        sent.append(message)

    app = _make_size_app(10)
    await app(
        {
            "type": "http",
            "path": "/health/../api/echo",
            "headers": [(b"content-length", b"11")],
        },
        receive,
        send,
    )

    assert sent[0]["status"] == 413  # noqa: PLR2004


def test_body_size_under_limit_ok():

    client = TestClient(_make_size_app(100))
    resp = client.post("/api/echo", content=b"x" * 50)
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["len"] == 50  # noqa: PLR2004


def test_body_size_data_plane_exempt():

    client = TestClient(_make_size_app(100))
    resp = client.post("/data/echo", content=b"x" * 5000)
    assert resp.status_code == 200  # noqa: PLR2004


@pytest.mark.asyncio
async def test_body_size_streaming_over_limit_413():
    """Chunked bodies (no Content-Length) are guarded via the receive channel."""

    async def inner(scope, receive, send):  # noqa: ARG001
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
        def __init__(self, chunks) -> None:
            self._chunks = list(chunks) + [b""]
            self._i = 0

        async def __call__(self) -> dict:
            msg = {
                "type": "http.request",
                "body": self._chunks[self._i],
                "more_body": self._i < len(self._chunks) - 1,
            }
            self._i += 1
            return msg

    class _Recorder:
        def __init__(self) -> None:
            self.messages = []

        async def __call__(self, message) -> None:
            self.messages.append(message)

    app = MaxBodySizeMiddleware(inner, max_size=10)
    recorder = _Recorder()
    await app(
        {"type": "http", "path": "/api/echo", "headers": []},
        _ChunkedReceive([b"aaaa", b"bbbb", b"cccc"]),
        recorder,
    )
    assert recorder.messages[0]["status"] == 413  # noqa: PLR2004


def test_security_headers_present(client):
    """Verify standard security response headers are included on HTTP responses."""
    response = client.get("/health")
    assert response.status_code == 200  # noqa: PLR2004
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("x-frame-options") == "DENY"
    assert response.headers.get("x-xss-protection") == "1; mode=block"
    assert response.headers.get("referrer-policy") == "strict-origin-when-cross-origin"

    # Verify on an authenticated /api route as well
    api_resp = _get(client, "/api/devices", key=_PASSWORD)
    assert api_resp.status_code == 200  # noqa: PLR2004
    assert api_resp.headers.get("x-content-type-options") == "nosniff"
    assert api_resp.headers.get("x-frame-options") == "DENY"
    assert api_resp.headers.get("x-xss-protection") == "1; mode=block"
    assert api_resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"

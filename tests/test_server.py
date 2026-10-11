"""Comprehensive tests for core/server.py endpoints, helpers, and guard conditions."""

from __future__ import annotations

from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import core.server as server_mod
from adapters.base import ProtocolAdapterRegistry, ProtocolType
from core.server import (
    _apply_response_modifications,
    _resolve_device_for_ip,
    _select_handler_adapter,
    app,
)

# ── Helper Function Tests ───────────────────────────────────────────────────


def test_select_handler_adapter_none_registry():
    """Returns None when adapter_registry is None."""
    assert _select_handler_adapter(None, "shelly", "http") is None


def test_select_handler_adapter_vendor_match():
    """Returns vendor-specific adapter when registered."""
    mock_registry = MagicMock(spec=ProtocolAdapterRegistry)
    mock_adapter = MagicMock()
    mock_registry.get_adapter.return_value = mock_adapter

    result = _select_handler_adapter(mock_registry, "shelly", "http")
    assert result == mock_adapter
    mock_registry.get_adapter.assert_called_once_with("shelly")


def test_select_handler_adapter_protocol_fallback():
    """Falls back to protocol adapter when vendor adapter is not found."""
    mock_registry = MagicMock(spec=ProtocolAdapterRegistry)
    mock_registry.get_adapter.return_value = None
    mock_proto_adapter = MagicMock()
    mock_registry.get_adapter_by_protocol.return_value = [mock_proto_adapter]

    result = _select_handler_adapter(mock_registry, "unknown_vendor", "http")
    assert result == mock_proto_adapter
    mock_registry.get_adapter_by_protocol.assert_called_once_with(ProtocolType.HTTP)


def test_select_handler_adapter_invalid_protocol():
    """Returns None when protocol string is invalid or not registered."""
    mock_registry = MagicMock(spec=ProtocolAdapterRegistry)
    mock_registry.get_adapter.return_value = None

    result = _select_handler_adapter(mock_registry, "unknown_vendor", "invalid_proto_type")
    assert result is None


@pytest.mark.asyncio
async def test_resolve_device_for_ip_unbound_or_invalid():
    """Returns None when IP is None or 'unknown'."""
    assert await _resolve_device_for_ip(MagicMock(), None) is None
    assert await _resolve_device_for_ip(MagicMock(), "unknown") is None


@pytest.mark.asyncio
async def test_resolve_device_for_ip_calls_db():
    """Calls db.resolve_device_id for valid IP."""
    mock_db = AsyncMock()
    mock_db.resolve_device_id.return_value = "dev-123"

    result = await _resolve_device_for_ip(mock_db, "192.168.1.50")
    assert result == "dev-123"
    mock_db.resolve_device_id.assert_called_once_with("192.168.1.50")


def test_apply_response_modifications_wrapped():
    """Returns wrapped dict directly if process_response returns status_code."""
    mock_engine = MagicMock()
    mock_engine.process_response.return_value = (
        {"status_code": HTTPStatus.OK, "headers": {}, "body": {"ok": True}},
        True,
    )
    with patch("core.server.get_modification_engine", return_value=mock_engine):
        res = _apply_response_modifications(MagicMock(), {"body": {}})
        assert res["status_code"] == HTTPStatus.OK
        assert res["body"] == {"ok": True}


def test_apply_response_modifications_body_override():
    """Normalises body override and removes 'modifications' metadata key."""
    mock_engine = MagicMock()
    mock_engine.process_response.return_value = (
        {"data": 123, "modifications": ["rule1"]},
        True,
    )
    with patch("core.server.get_modification_engine", return_value=mock_engine):
        res = _apply_response_modifications(
            MagicMock(), {"status_code": HTTPStatus.OK, "headers": {}}
        )
        assert res["body"] == {"data": 123}
        assert "modifications" not in res["body"]


# ── HTML Web UI Route Tests ────────────────────────────────────────────────


def test_web_ui_dashboard_route(monkeypatch, tmp_path):
    """GET / returns HTML content."""
    html_file = tmp_path / "dashboard.html"
    html_file.write_text("<html>Dashboard</html>", encoding="utf-8")
    monkeypatch.setattr(server_mod, "DASHBOARD_HTML", html_file)

    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == HTTPStatus.OK
    assert "Dashboard" in resp.text


def test_web_ui_dashboard_route_missing_file(monkeypatch, tmp_path):
    """GET / returns fallback HTML when DASHBOARD_HTML does not exist."""
    html_file = tmp_path / "non_existent.html"
    monkeypatch.setattr(server_mod, "DASHBOARD_HTML", html_file)

    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == HTTPStatus.OK
    assert "Dashboard not found" in resp.text


def test_web_ui_patterns_route(monkeypatch, tmp_path):
    """GET /patterns/{device_id} returns HTML content."""
    html_file = tmp_path / "patterns.html"
    html_file.write_text("<html>Patterns</html>", encoding="utf-8")
    monkeypatch.setattr(server_mod, "PATTERNS_HTML", html_file)

    client = TestClient(app)
    resp = client.get("/patterns/dev-123")
    assert resp.status_code == HTTPStatus.OK
    assert "Patterns" in resp.text


def test_web_ui_config_route(monkeypatch, tmp_path):
    """GET /config returns HTML content."""
    html_file = tmp_path / "config.html"
    html_file.write_text("<html>Config UI</html>", encoding="utf-8")
    monkeypatch.setattr(server_mod, "CONFIG_HTML", html_file)

    client = TestClient(app)
    resp = client.get("/config")
    assert resp.status_code == HTTPStatus.OK
    assert "Config UI" in resp.text


def test_web_ui_options_route(monkeypatch, tmp_path):
    """GET /options returns HTML content."""
    html_file = tmp_path / "options.html"
    html_file.write_text("<html>Options UI</html>", encoding="utf-8")
    monkeypatch.setattr(server_mod, "OPTIONS_HTML", html_file)

    client = TestClient(app)
    resp = client.get("/options")
    assert resp.status_code == HTTPStatus.OK
    assert "Options UI" in resp.text


# ── Unready Service Guards Tests ───────────────────────────────────────────


def test_unready_db_manager_returns_503(monkeypatch):
    """Endpoints requiring db_manager return 503 when service is not ready."""
    server_mod.config_manager.config.security.auth_enabled = False
    monkeypatch.setattr(server_mod, "db_manager", None)
    monkeypatch.setattr(server_mod, "orchestrator", None)

    client = TestClient(app)

    endpoints = [
        ("GET", "/api/devices", {}),
        ("GET", "/api/devices/dev-1", {}),
        ("DELETE", "/api/devices/dev-1", {}),
        ("GET", "/api/tls/unidentified", {}),
        ("PUT", "/api/devices/dev-1/tls-config", {}),
        ("POST", "/api/devices/dev-1/mode", {}),
        ("PUT", "/api/devices/dev-1/threshold", {}),
        ("PUT", "/api/devices/dev-1/context-buffer", {}),
        ("GET", "/api/devices/dev-1/auto-switch", {}),
        ("PUT", "/api/devices/dev-1/auto-switch", {}),
        ("PUT", "/api/devices/dev-1/llm", {}),
        ("POST", "/api/devices/dev-1/database", {}),
        ("GET", "/api/databases", {}),
        ("GET", "/api/devices/dev-1/database", {}),
        ("GET", "/api/devices/by-ip/192.168.1.1", {}),
        ("GET", "/api/devices/dev-1/connection", {}),
        ("PUT", "/api/devices/dev-1/connection", {}),
        ("GET", "/api/ip-profiles/192.168.1.1/bypass", {}),
        ("PUT", "/api/ip-profiles/192.168.1.1/bypass", {}),
        ("POST", "/api/devices/dev-1/ip", {}),
        ("GET", "/api/devices/dev-1/patterns", {}),
        ("GET", "/api/devices/dev-1/patterns/export", {}),
        ("GET", "/api/devices/dev-1/patterns/p1", {}),
        ("PUT", "/api/devices/dev-1/patterns/p1", {}),
        ("PATCH", "/api/devices/dev-1/patterns/p1", {}),
        ("DELETE", "/api/devices/dev-1/patterns/p1", {}),
        ("GET", "/api/devices/dev-1/context", {}),
        ("PUT", "/api/devices/dev-1/context", {}),
        ("GET", "/api/llm/user-profiles", {}),
        ("POST", "/api/llm/user-profiles", {}),
        ("GET", "/api/llm/user-profiles/p1", {}),
        ("PUT", "/api/llm/user-profiles/p1", {}),
        ("DELETE", "/api/llm/user-profiles/p1", {}),
        ("POST", "/api/devices/dev-1/patterns/import", {}),
        ("GET", "/api/devices/dev-1/capture/export", {}),
        ("POST", "/api/devices/dev-1/capture/import", {}),
        ("POST", "/api/system/update", {}),
        ("GET", "/shelly/status", {}),
    ]

    for method, path, kwargs in endpoints:
        resp = client.request(method, path, **kwargs)
        assert resp.status_code == HTTPStatus.SERVICE_UNAVAILABLE, f"Failed for {method} {path}"
        assert resp.json() == {"error": "Service not ready"}


def test_unready_cert_manager_returns_503(monkeypatch):
    """Endpoints requiring cert_manager return 503 when cert_manager is None."""
    server_mod.config_manager.config.security.auth_enabled = False
    monkeypatch.setattr(server_mod, "cert_manager", None)

    client = TestClient(app)

    endpoints = [
        ("GET", "/api/tls/ca-cert", {}),
        ("GET", "/api/tls/certs", {}),
        ("GET", "/api/tls/certs/example.com", {}),
        (
            "POST",
            "/api/tls/certs/upload",
            {"data": {"hostname": "ex"}, "files": {"cert": b"c", "key": b"k"}},
        ),
        ("POST", "/api/tls/certs/upload-json", {}),
        ("DELETE", "/api/tls/certs/example.com", {}),
        ("POST", "/api/tls/certs/example.com/rotate", {}),
        ("POST", "/api/tls/root-ca/download", {}),
    ]

    for method, path, kwargs in endpoints:
        resp = client.request(method, path, **kwargs)
        assert resp.status_code == HTTPStatus.SERVICE_UNAVAILABLE, f"Failed for {method} {path}"
        assert "Cert manager not ready" in resp.json().get("error", "")


def test_unready_llm_decipher_returns_503(monkeypatch):
    """Endpoints requiring llm_decipher_service return 503 when it is None."""
    server_mod.config_manager.config.security.auth_enabled = False
    monkeypatch.setattr(server_mod, "llm_decipher_service", None)

    client = TestClient(app)

    endpoints = [
        ("GET", "/api/llm/profiles"),
        ("GET", "/api/llm/settings"),
        ("PUT", "/api/llm/settings"),
        ("POST", "/api/llm/profiles/test"),
        ("POST", "/api/llm/settings/profiles"),
        ("DELETE", "/api/llm/settings/profiles/p1"),
    ]

    for method, path in endpoints:
        resp = client.request(method, path)
        assert resp.status_code == HTTPStatus.SERVICE_UNAVAILABLE, f"Failed for {method} {path}"
        assert resp.json() == {"error": "Service not ready"}


# ── TLS API & Frida Script Tests ───────────────────────────────────────────


def test_tls_frida_script():
    """GET /api/tls/frida/script.js returns javascript content."""
    client = TestClient(app)
    resp = client.get("/api/tls/frida/script.js")
    assert resp.status_code == HTTPStatus.OK
    assert "Ride the API" in resp.text
    assert resp.headers["content-type"].startswith("application/javascript")


def test_tls_ports_endpoint(monkeypatch):
    """GET /api/tls/ports returns active ports or disabled when server is None."""
    server_mod.config_manager.config.security.auth_enabled = False

    client = TestClient(app)

    # When tls_mitm_server is None
    monkeypatch.setattr(server_mod, "tls_mitm_server", None)
    resp = client.get("/api/tls/ports")
    assert resp.status_code == HTTPStatus.OK
    assert resp.json() == {"ports": [], "enabled": False}

    # When tls_mitm_server exists
    mock_server = MagicMock()
    mock_server.listen_ports = [443, 8443]
    monkeypatch.setattr(server_mod, "tls_mitm_server", mock_server)
    resp = client.get("/api/tls/ports")
    assert resp.status_code == HTTPStatus.OK
    assert resp.json() == {"ports": [443, 8443], "enabled": True}


def test_tls_stats_endpoint(monkeypatch):
    """GET /api/tls/stats returns stats structure."""
    server_mod.config_manager.config.security.auth_enabled = False

    mock_cert = MagicMock()
    mock_cert.stats.return_value = {"active": True}
    monkeypatch.setattr(server_mod, "cert_manager", mock_cert)

    mock_mitm = MagicMock()
    mock_mitm.listen_ports = [443]
    mock_mitm.device_ports = {}
    monkeypatch.setattr(server_mod, "tls_mitm_server", mock_mitm)

    client = TestClient(app)
    resp = client.get("/api/tls/stats")
    assert resp.status_code == HTTPStatus.OK
    data = resp.json()
    assert data["cert_manager"] == {"active": True}
    assert data["mitm_server"] is True
    assert data["listen_ports"] == [443]


# ── Device Settings Input Validation Tests ─────────────────────────────────


class _FakeDBForValidation:
    """Fake DB Manager to support testing validation logic on device routes."""

    async def update_device_match_threshold(self, device_id: str, threshold: float) -> bool:
        _ = threshold
        return device_id == "valid-dev"

    async def update_device_context_buffer_size(self, device_id: str, size: int) -> bool:
        _ = size
        return device_id == "valid-dev"

    async def update_device_auto_switch(self, device_id: str, enabled: bool) -> bool:
        _ = enabled
        return device_id == "valid-dev"

    async def update_device_mode(self, device_id: str, mode: str) -> bool:
        _ = mode
        return device_id == "valid-dev"


@pytest.fixture
def validation_client(monkeypatch):
    server_mod.config_manager.config.security.auth_enabled = False
    monkeypatch.setattr(server_mod, "db_manager", _FakeDBForValidation())
    return TestClient(app)


def test_set_device_threshold_validation(validation_client):
    """Test validation on PUT /api/devices/{device_id}/threshold."""
    # Invalid non-object body
    resp = validation_client.put("/api/devices/valid-dev/threshold", json=["not", "object"])
    assert resp.status_code == HTTPStatus.BAD_REQUEST

    # Missing field
    resp = validation_client.put("/api/devices/valid-dev/threshold", json={})
    assert resp.status_code == HTTPStatus.BAD_REQUEST

    # Non-numeric
    resp = validation_client.put(
        "/api/devices/valid-dev/threshold", json={"match_threshold": "abc"}
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST

    # Out of range (< 0)
    resp = validation_client.put(
        "/api/devices/valid-dev/threshold", json={"match_threshold": -0.1}
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST

    # Out of range (> 1.0)
    resp = validation_client.put(
        "/api/devices/valid-dev/threshold", json={"match_threshold": 1.5}
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST

    # Device not found
    resp = validation_client.put(
        "/api/devices/unknown-dev/threshold", json={"match_threshold": 0.8}
    )
    assert resp.status_code == HTTPStatus.NOT_FOUND

    # Valid
    resp = validation_client.put(
        "/api/devices/valid-dev/threshold", json={"match_threshold": 0.85}
    )
    assert resp.status_code == HTTPStatus.OK
    assert resp.json() == {"device_id": "valid-dev", "match_threshold": 0.85}


def test_set_device_context_buffer_validation(validation_client):
    """Test validation on PUT /api/devices/{device_id}/context-buffer."""
    # Invalid non-object body
    resp = validation_client.put("/api/devices/valid-dev/context-buffer", json="invalid")
    assert resp.status_code == HTTPStatus.BAD_REQUEST

    # Missing field
    resp = validation_client.put("/api/devices/valid-dev/context-buffer", json={})
    assert resp.status_code == HTTPStatus.BAD_REQUEST

    # Non-integer
    resp = validation_client.put(
        "/api/devices/valid-dev/context-buffer", json={"context_buffer_size": "abc"}
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST

    # Non-positive integer (<= 0)
    resp = validation_client.put(
        "/api/devices/valid-dev/context-buffer", json={"context_buffer_size": 0}
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST

    # Device not found
    resp = validation_client.put(
        "/api/devices/unknown-dev/context-buffer", json={"context_buffer_size": 1024}
    )
    assert resp.status_code == HTTPStatus.NOT_FOUND

    # Valid
    resp = validation_client.put(
        "/api/devices/valid-dev/context-buffer", json={"context_buffer_size": 2048}
    )
    assert resp.status_code == HTTPStatus.OK
    assert resp.json() == {"device_id": "valid-dev", "context_buffer_size": 2048}


def test_set_device_auto_switch_validation(validation_client):
    """Test validation on PUT /api/devices/{device_id}/auto-switch."""
    # Invalid non-object body
    resp = validation_client.put("/api/devices/valid-dev/auto-switch", json=123)
    assert resp.status_code == HTTPStatus.BAD_REQUEST

    # Non-boolean enabled field
    resp = validation_client.put("/api/devices/valid-dev/auto-switch", json={"enabled": "yes"})
    assert resp.status_code == HTTPStatus.BAD_REQUEST

    # Device not found
    resp = validation_client.put("/api/devices/unknown-dev/auto-switch", json={"enabled": True})
    assert resp.status_code == HTTPStatus.NOT_FOUND

    # Valid
    resp = validation_client.put("/api/devices/valid-dev/auto-switch", json={"enabled": True})
    assert resp.status_code == HTTPStatus.OK
    assert resp.json() == {"device_id": "valid-dev", "auto_switch_enabled": True}


def test_set_device_mode_validation(validation_client):
    """Test validation on POST /api/devices/{device_id}/mode."""
    # Invalid non-object body
    resp = validation_client.post("/api/devices/valid-dev/mode", json="learning")
    assert resp.status_code == HTTPStatus.BAD_REQUEST

    # Invalid mode string
    resp = validation_client.post("/api/devices/valid-dev/mode", json={"mode": "invalid_mode"})
    assert resp.status_code == HTTPStatus.BAD_REQUEST

    # Device not found
    resp = validation_client.post("/api/devices/unknown-dev/mode", json={"mode": "production"})
    assert resp.status_code == HTTPStatus.NOT_FOUND

    # Valid mode
    resp = validation_client.post("/api/devices/valid-dev/mode", json={"mode": "production"})
    assert resp.status_code == HTTPStatus.OK
    assert resp.json() == {"device_id": "valid-dev", "mode": "production"}

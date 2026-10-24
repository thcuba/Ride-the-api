"""Control-plane errors must never echo internal details to the client.

Regression tests for CodeQL ``py/stack-trace-exposure``: raw exception text can
carry filesystem paths, library internals or configuration details. The client
gets a stable message, the detail goes to the server log.
"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

import core.server as server_mod

SECRET = "/etc/ride-secret/private.key"
PASSWORD = "testpass"
AUTH = {"X-API-Key": PASSWORD}


class _LeakyCertManager:
    """Double whose failures carry a path — exactly what must not leak."""

    def list_imported_certs(self):
        return []

    def get_cert_info(self, _hostname):
        raise ValueError(f"cannot read {SECRET}")

    def import_cert(self, *_args, **_kwargs):
        raise ValueError(f"cannot write {SECRET}")

    def delete_cert(self, _hostname):
        raise ValueError(f"cannot remove {SECRET}")

    def has_external_cert(self, _hostname):
        raise ValueError(f"cannot stat {SECRET}")


def _client(monkeypatch, cert_manager=None) -> TestClient:
    monkeypatch.setattr(server_mod, "cert_manager", cert_manager or _LeakyCertManager())
    monkeypatch.setattr(server_mod.config_manager.config.security, "password", PASSWORD)
    monkeypatch.setattr(server_mod.config_manager.config.security, "auth_enabled", True)
    return TestClient(server_mod.app)


def test_cert_lookup_failure_does_not_leak_the_exception(monkeypatch):
    client = _client(monkeypatch)
    resp = client.get("/api/tls/certs/some.example.com", headers=AUTH)

    assert resp.status_code == 400  # noqa: PLR2004
    assert resp.json() == {"error": "Invalid certificate lookup request"}
    assert SECRET not in resp.text
    assert "/etc" not in resp.text


def test_cert_delete_failure_does_not_leak_the_exception(monkeypatch):
    client = _client(monkeypatch)
    resp = client.delete("/api/tls/certs/some.example.com", headers=AUTH)

    assert resp.status_code == 400  # noqa: PLR2004
    assert resp.json() == {"error": "Invalid certificate deletion request"}
    assert SECRET not in resp.text


def test_cert_upload_failure_does_not_leak_the_exception(monkeypatch):
    client = _client(monkeypatch)
    resp = client.post(
        "/api/tls/certs/upload-json",
        json={"hostname": "h.example.com", "cert_base64": "Y2VydA==", "key_base64": "a2V5"},
        headers=AUTH,
    )

    assert resp.status_code == 400  # noqa: PLR2004
    assert (
        resp.json()["error"]
        == "Invalid certificate payload. Provide a valid PEM or DER certificate."
    )
    assert SECRET not in resp.text


def test_unexpected_failure_is_sanitised_as_well(monkeypatch):
    """Non-input failures return a generic 500, never the exception text."""

    class _Broken(_LeakyCertManager):
        def get_cert_info(self, _hostname):
            raise RuntimeError(f"boom while reading {SECRET}")

    client = _client(monkeypatch, _Broken())
    resp = client.get("/api/tls/certs/some.example.com", headers=AUTH)

    assert resp.status_code == 500  # noqa: PLR2004
    assert resp.json() == {"error": "An internal error occurred"}
    assert SECRET not in resp.text


def test_rejected_request_is_logged_server_side(monkeypatch, caplog):
    """The detail is not lost — it moves to the log, where it belongs."""
    client = _client(monkeypatch)
    with caplog.at_level(logging.INFO):
        client.get("/api/tls/certs/some.example.com", headers=AUTH)

    messages = [record.getMessage() for record in caplog.records]
    assert any("Rejected request" in message for message in messages)
    assert any(SECRET in message for message in messages)


def test_client_error_helper_never_returns_the_exception_text():
    """The helper itself is the single choke point for error responses."""
    resp = server_mod._client_error(ValueError(f"secret at {SECRET}"), "Nope")

    assert resp.status_code == 400  # noqa: PLR2004
    assert resp.body == b'{"error":"Nope"}'

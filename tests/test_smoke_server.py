"""Minimal smoke tests: the server imports and its /health is alive.

These are deliberately tiny and fast. They do NOT spin up a real socket or
touch a database; they just prove that the production ``app`` imports cleanly
(top-level module graph loads: FastAPI, adapters registry, config, TLS, etc.)
and that the liveness endpoint responds. Marked ``smoke`` so they can be run
in isolation or skipped with ``-m \"not smoke\"``.

Connection: ``test_server_cors.py`` already exercises ``core.server.app`` via
``TestClient``; this file is the explicitly-tagged, minimal smoke set.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient

import core.server as server_mod
from core.server import app

pytestmark = pytest.mark.smoke


def test_server_app_imports() -> None:
    """The production app object is importable (top-level deps load)."""
    assert app is not None
    # A couple of expected routes that must be registered by import time.
    paths = {
        str(getattr(route, "path", ""))
        for route in app.routes
        if getattr(route, "path", None) is not None
    }
    assert "/health" in paths
    assert "/api/devices" in paths
    assert "/api/config" in paths
    assert "/config" in paths
    assert "/options" in paths


def test_config_endpoint_returns_config() -> None:
    """GET /api/config returns the (validated) full config object."""
    server_mod.config_manager.config.security.auth_enabled = False
    client = TestClient(app)
    resp = client.get("/api/config")
    assert resp.status_code == HTTPStatus.OK
    body = resp.json()
    assert "config" in body
    assert isinstance(body["config"], dict)
    # A few well-known top-level sections must be present.
    for section in (
        "core",
        "security",
        "learning",
        "tls_decrypt",
        "protocol_servers",
        "llm_decipher",
    ):
        assert section in body["config"]


def test_health_endpoint_returns_healthy() -> None:
    """GET /health responds 200 with status='healthy'."""
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == HTTPStatus.OK
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["service"] == "local-cloud-replacement-proxy"


def test_runtime_version_falls_back_without_version_file(monkeypatch, tmp_path) -> None:
    """Without a VERSION file, runtime_version() returns the hardcoded fallback."""
    monkeypatch.chdir(tmp_path)
    assert tmp_path.joinpath("VERSION").exists() is False
    assert server_mod.runtime_version() == server_mod.APP_VERSION_FALLBACK


def test_runtime_version_reads_version_file(monkeypatch, tmp_path) -> None:
    """runtime_version() reads the tag from <cwd>/VERSION when present."""
    tmp_path.joinpath("VERSION").write_text("1.2.6\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert server_mod.runtime_version() == "1.2.6"


def test_runtime_version_ignores_blank_file(monkeypatch, tmp_path) -> None:
    """A VERSION file with only whitespace falls back to the hardcoded version."""
    tmp_path.joinpath("VERSION").write_text("   \n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert server_mod.runtime_version() == server_mod.APP_VERSION_FALLBACK


def test_updater_env_strips_loader_paths(monkeypatch) -> None:
    """updater_env() drops PyInstaller loader-path vars but keeps the rest."""
    monkeypatch.setenv("LD_LIBRARY_PATH", "/home/pi4ride/ride-the-api/ride-the-api/_internal")
    monkeypatch.setenv("LD_PRELOAD", "evil.so")
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/bundle")
    monkeypatch.setenv("KEEP_ME", "value")
    env = server_mod.updater_env()
    # The bundle loader-path vars must never reach a spawned updater/curl.
    for var in server_mod._UPDATER_STRIP_ENV:
        assert var not in env, f"updater_env() must strip {var}"
    # Unrelated env is preserved.
    assert env["KEEP_ME"] == "value"
    # Sanity: PATH (needed by curl) survives.
    assert "PATH" in env

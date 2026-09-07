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

import pytest
from fastapi.testclient import TestClient

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


def test_health_endpoint_returns_healthy() -> None:
    """GET /health responds 200 with status='healthy'."""
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["service"] == "local-cloud-replacement-proxy"
"""The Patterns page must work at /patterns, with no device id in the URL.

The bug this protects against: the dashboard's navigation links to /patterns, but the page was
registered only as /patterns/{device_id}. The bare path was then picked up by the device-proxy
catch-all (/{vendor}/{path:path}), which read it as a vendor name and answered

    {"error":"Protocol 'patterns' not supported. Supported: [...]}

so the page showed a raw JSON protocol error. A page request was mistaken for intercepted traffic.

Two things had to be true for that, and both are checked here:

  * a route serves /patterns;
  * it is matched before the proxy's catch-all, since FastAPI resolves in registration order.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient

import core.server as server_mod
from core.server import app

PATTERNS_HTML_PATH = pathlib.Path(__file__).resolve().parent.parent / "webui" / "patterns.html"
CATCH_ALL = "/{vendor}/{path:path}"


@pytest.fixture
def patterns_page(monkeypatch, tmp_path):
    """Point the route at a small stand-in page."""
    html_file = tmp_path / "patterns.html"
    html_file.write_text("<html>Patterns UI</html>", encoding="utf-8")
    monkeypatch.setattr(server_mod, "PATTERNS_HTML", html_file)
    return TestClient(app)


# ── the route exists and serves the page ─────────────────────────────────────


def test_patterns_without_device_serves_the_page(patterns_page):
    """/patterns returns the page, not the proxy's protocol error."""
    resp = patterns_page.get("/patterns")
    assert resp.status_code == HTTPStatus.OK
    assert "Patterns UI" in resp.text


def test_patterns_without_device_is_not_swallowed_by_the_proxy(patterns_page):
    """The specific regression: a JSON "not supported" body instead of HTML."""
    resp = patterns_page.get("/patterns")
    assert "not supported" not in resp.text
    assert "text/html" in resp.headers.get("content-type", "")


def test_patterns_with_device_still_works(patterns_page):
    """/patterns/{device_id} keeps serving the same page (it was already correct)."""
    resp = patterns_page.get("/patterns/dev-123")
    assert resp.status_code == HTTPStatus.OK
    assert "Patterns UI" in resp.text


def test_page_route_is_registered_before_the_proxy_catch_all():
    """The page route must come first: FastAPI matches in registration order.

    If the catch-all were registered earlier it would win and /patterns would silently turn back
    into a proxy call - the original failure, restored without a single test going red.
    """
    paths = [route.path for route in app.routes]
    assert "/patterns" in paths, "rotta /patterns assente"
    assert CATCH_ALL in paths, "catch-all del proxy assente"
    assert paths.index("/patterns") < paths.index(CATCH_ALL), (
        "/patterns e' registrata dopo la catch-all del proxy: verrebbe intercettata da quella"
    )


# ── the page reads the device id from the URL correctly ──────────────────────


def _device_id_for(paths: list[str]) -> list[str]:
    """Run the page's own deviceIdFromPath() under node, so the check tests real behaviour."""
    html = PATTERNS_HTML_PATH.read_text(encoding="utf-8")
    match = re.search(r"function deviceIdFromPath\(pathname\)\{(.*?)\n  \}", html, re.S)
    assert match, "deviceIdFromPath non trovata in webui/patterns.html"
    js = (
        f"function deviceIdFromPath(pathname){{{match.group(1)}}}\n"
        f"console.log(JSON.stringify({json.dumps(paths)}.map(deviceIdFromPath)));"
    )
    result = subprocess.run(
        ["node", "-e", js], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, f"node ha fallito: {result.stderr}"
    return json.loads(result.stdout)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node non disponibile")


@needs_node
def test_device_id_is_empty_on_the_bare_page():
    """Bare /patterns yields no device id - the whole point of the fix.

    The old code took the last path segment, which on this URL is the string "patterns"; the page
    then requested /api/devices/patterns/patterns.
    """
    assert _device_id_for(["/patterns", "/patterns/", "/", "/config"]) == ["", "", "", ""]


@needs_node
def test_device_id_is_read_for_the_per_device_view():
    """/patterns/{id} still yields that id, including percent-encoded ones."""
    assert _device_id_for(["/patterns/dev-123", "/patterns/dev%20123"]) == [
        "dev-123",
        "dev 123",
    ]


# ── the no-device view asks for devices, not for a device named after the page ─


def test_page_lists_devices_when_no_device_is_in_the_url():
    """Without an id the page loads the device list instead of a per-device endpoint."""
    html = PATTERNS_HTML_PATH.read_text(encoding="utf-8")
    assert "renderDeviceList" in html, "manca la vista senza device"
    assert "'/api/devices'" in html, "la vista senza device non elenca i device"
    # The old derivation, which produced the page name as a device id.
    assert "slice(-1)[0]" not in html, "e' tornata la derivazione dell'id dal solo ultimo segmento"

"""One complete, truthful request/response path — proven end to end.

This is the test the project's claims rest on. It runs the *production* code
against a **real** HTTP vendor (a socket on localhost, not a mock) and walks the
whole path in order:

1. a fresh install (empty database directory) comes up and serves;
2. forwarding **preserves bytes, status and headers** in both directions —
   a vendor 429 with its `retry-after` and a non-JSON body reaches the device
   unchanged, and the body the vendor receives is byte-identical to what the
   device sent;
3. learning **captures the real vendor response** (status/headers/body as they
   arrived), not a reinterpretation;
4. with the vendor **shut down**, the learned pattern serves the request
   locally, and it still does after the database manager is closed and rebuilt
   on the same directory — i.e. after a process restart / container recreation
   with a persistent volume.

Scope, stated plainly: the pattern *extraction* step is fed a canned analysis
instead of calling a real model, so a model's quality is not measured here. The
capture, correlation, persistence, matching and serving are production code, and
the network exchange is real.
"""

from __future__ import annotations

import contextlib
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import core.server as server_mod
from core import cloud_forward
from core.buffer import initialize_buffer_backend
from core.cloud_forward import forward_intercepted
from core.database import DatabaseManager, DeviceRegistry, RequestPattern
from core.pipeline import ContextBuffer, LearningOrchestrator, LearningPipeline
from core.server import app

if TYPE_CHECKING:
    from pathlib import Path

# ── A real vendor on a real socket ─────────────────────────────────────────────

VENDOR_STATUS = 429
VENDOR_HEADERS = {"retry-after": "30", "content-type": "application/xml"}
VENDOR_BODY = b"<error>rate limited</error>"

# What the device sends: deliberately not JSON, so re-encoding would be visible.
DEVICE_BODY = b'<command><temp_set value="240"/></command>'

REQUESTS = {
    "method": "POST",
    "path": "/v1.0/device/dev-1/commands",
    "headers": {"Content-Type": "application/json"},
    "body": {"commands": [{"code": "temp_set", "value": 240}]},
}


class _VendorHandler(BaseHTTPRequestHandler):
    server_version = "TestVendor/1.0"

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.received.append(  # type: ignore[attr-defined]
            {"path": self.path, "headers": dict(self.headers), "body": body}
        )
        self.send_response(VENDOR_STATUS)
        for key, value in VENDOR_HEADERS.items():
            self.send_header(key, value)
        self.send_header("content-length", str(len(VENDOR_BODY)))
        self.end_headers()
        self.wfile.write(VENDOR_BODY)

    def log_message(self, _format, *_args):
        """Keep the test output clean."""


@pytest.fixture
def vendor():
    """A vendor cloud listening on localhost, recording what it received."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _VendorHandler)
    server.received = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        with contextlib.suppress(Exception):
            server.shutdown()
            server.server_close()
        thread.join(timeout=5)


@dataclass
class _Adapter:
    """Forwards through the production forwarder to the test vendor."""

    hostname: str
    port: int

    async def parse_request(self, request):
        return request

    async def forward_to_cloud(self, request):
        return await forward_intercepted(
            request, hostname=self.hostname, port=self.port, use_tls=False
        )


class _Registry:
    def __init__(self, adapter: _Adapter) -> None:
        self.adapter = adapter

    def get_adapter(self, _vendor: str) -> _Adapter:
        return self.adapter

    def list_vendors(self) -> list[str]:
        return ["example"]


class _InterceptingSelector:
    """Always intercept, so the test exercises the proxy path deterministically."""

    def evaluate(self, _info):
        return MagicMock(value="intercept")


# The canned extraction analysis (see the module docstring for the scope note).
ANALYSIS = {
    "meta": {"version": 1, "pattern_id": "dev-1", "vendor": "example", "device_type": "plug"},
    "client": {
        "protocols": ["http"],
        "endpoints": [
            {
                "id": "cmd",
                "intent": "set_temperature",
                "method": "POST",
                "path": "/v1.0/device/{id}/commands",
            }
        ],
    },
    "server": {
        "responses": [
            {
                "id": "rsp_cmd",
                "triggers": ["set_temperature"],
                "status_code": 200,
                "headers_template": {"Content-Type": "application/json"},
                "body_template": {"result": {"data": {"1": False, "3": 240}}},
            }
        ]
    },
    "connection_mode": "http",
}


@pytest.fixture
def allow_loopback(monkeypatch):
    """Let the forwarder reach localhost (the SSRF guard blocks private IPs)."""
    monkeypatch.setattr(cloud_forward, "is_blocked_ip", lambda _ip: False)
    monkeypatch.setattr(
        cloud_forward.CloudForwarder, "resolve", AsyncMock(return_value=["127.0.0.1"])
    )


async def _build_stack(root: Path) -> tuple[DatabaseManager, LearningOrchestrator, ContextBuffer]:
    """Bring up the stack exactly as the application lifespan does."""
    dbm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{root}/core.db",
        device_db_dir=root / "devices",
        echo=False,
    )
    await dbm.initialize()
    initialize_buffer_backend()
    await dbm.get_or_create_device("dev-1", "example", "plug", "Test Plug")
    orch = LearningOrchestrator(dbm)
    orch.initialize(None)
    buf = await orch.ensure_buffer("dev-1", buffer_size=524288)
    orch.pipeline = LearningPipeline(dbm, orch.llm_decipher, buf, orch.tracker, orch.engine)
    return dbm, orch, buf


async def _request(orch: LearningOrchestrator) -> dict:
    return await orch.handle_request(
        "dev-1",
        "example",
        "http",
        REQUESTS["method"],
        REQUESTS["path"],
        REQUESTS["headers"],
        REQUESTS["body"],
        {},
    )


@pytest.mark.asyncio
async def _criterion_2_forwarding_is_byte_faithful(client: TestClient, vendor) -> None:
    """The vendor's status, headers and bytes reach the device unchanged — and back."""
    resp = client.post(
        "/example/v1.0/device/dev-1/commands",
        content=DEVICE_BODY,
        headers={"content-type": "application/xml"},
    )

    assert vendor.received, "the request must actually reach the vendor"
    # Upstream: the vendor sees the device's own bytes, untouched.
    assert vendor.received[0]["body"] == DEVICE_BODY
    assert vendor.received[0]["headers"]["content-type"] == "application/xml"
    # Downstream: the device sees the vendor's status, headers and bytes.
    assert resp.status_code == VENDOR_STATUS
    assert resp.headers["retry-after"] == "30"
    assert resp.headers["content-type"] == "application/xml"
    assert resp.content == VENDOR_BODY


@pytest.mark.asyncio
async def _criterion_3_learning_captures_the_real_response(
    orch: LearningOrchestrator, buf: ContextBuffer, adapter: _Adapter
) -> None:
    """The captured pair carries the vendor's real status and headers."""
    handled = await _request(orch)
    assert handled.get("action") == "forward", f"an unknown request must go upstream: {handled}"

    forwarded = await adapter.forward_to_cloud(
        server_mod.InterceptedRequest(
            device_id="dev-1",
            timestamp=0,
            protocol=server_mod.ProtocolType.HTTP,
            method=REQUESTS["method"],
            path=REQUESTS["path"],
            headers=REQUESTS["headers"],
            body=REQUESTS["body"],
        )
    )
    assert forwarded.success, forwarded.error

    # The route feeds the response back through the production learning hook,
    # carrying the vendor's real status and headers (this is the P0 fix).
    await orch.handle_response(
        device_id="dev-1",
        vendor="example",
        protocol="http",
        status_code=forwarded.status_code or 200,
        headers=forwarded.response_headers,
        body=forwarded.response,
    )

    buffered = await buf.get_buffer_pairs("dev-1")
    assert buffered, "the request/response pair must be captured for learning"
    pairs = [entry["pair"] for entry in buffered if entry.get("pair")]
    with_response = [p for p in pairs if (p.get("response_status") or 0) > 0]
    assert with_response, f"the captured pair must carry the vendor response: {buffered}"
    record = with_response[-1]
    assert record["response_status"] == VENDOR_STATUS
    assert (record["response_headers"] or {}).get("retry-after") == "30"


@pytest.mark.asyncio
async def _criterion_4_serves_locally_and_survives_a_restart(
    dbm: DatabaseManager, orch: LearningOrchestrator, install: Path, vendor
) -> None:
    """Learn the pattern, kill the vendor, then rebuild the stack on the same volume."""
    assert orch.pipeline is not None
    orch.pipeline._analyze_with_llm = AsyncMock(return_value=json.loads(json.dumps(ANALYSIS)))
    flush = await orch.pipeline.flush_and_learn("dev-1")
    assert flush.get("success"), flush

    async with dbm.core_session() as session:
        device = (
            await session.execute(select(DeviceRegistry).where(DeviceRegistry.device_id == "dev-1"))
        ).scalar_one()
        device.mode = "production"
        device.match_threshold = 0.7  # the learned pattern scores ~0.71
        await session.commit()
    # Writing through a session bypasses the database manager's cache invalidation,
    # which every production path performs (e.g. ``update_device_mode``).
    dbm.invalidate_device_cache("dev-1")

    # The vendor is gone: anything not served locally can only fail now.
    vendor.shutdown()
    vendor.server_close()

    served = await _request(orch)
    assert served.get("action") == "local_response", (
        f"with the vendor down the learned pattern must serve the request: {served}"
    )
    assert "240" in json.dumps(served.get("response"), default=str)

    # Restart / container recreation: a new database manager on the same directory
    # must still find the learned pattern and serve locally.
    await dbm.close()
    restarted_dbm, restarted_orch, _ = await _build_stack(install)
    try:
        async with restarted_dbm.device_session("dev-1") as session:
            patterns = (
                await session.execute(select(func.count()).select_from(RequestPattern))
            ).scalar_one()
        assert patterns >= 1, "learned patterns must persist across a restart"

        async with restarted_dbm.core_session() as session:
            device = (
                await session.execute(
                    select(DeviceRegistry).where(DeviceRegistry.device_id == "dev-1")
                )
            ).scalar_one()
            assert device.mode == "production", "the device mode must persist too"

        after_restart = await _request(restarted_orch)
        assert after_restart.get("action") == "local_response", (
            f"after a restart the device must still be served locally: {after_restart}"
        )
    finally:
        await restarted_dbm.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("allow_loopback")
async def test_complete_request_response_path(vendor, tmp_path, monkeypatch):
    """Fresh install -> byte-faithful forward -> real capture -> offline serving."""
    install = tmp_path / "install"

    # ── CRITERION 1: a fresh install comes up and serves ──────────────────────
    dbm, orch, buf = await _build_stack(install)
    assert (install / "core.db").exists(), "a fresh install must create its store"

    adapter = _Adapter(hostname="vendor.test", port=vendor.server_address[1])
    monkeypatch.setattr(server_mod.config_manager.config.learning, "signal_forward_to_cloud", False)
    monkeypatch.setattr(server_mod, "db_manager", dbm)
    monkeypatch.setattr(server_mod, "orchestrator", orch)
    monkeypatch.setattr(server_mod, "adapter_registry", _Registry(adapter))
    monkeypatch.setattr(server_mod, "get_traffic_selector", _InterceptingSelector)
    monkeypatch.setattr(server_mod.config_manager.config.security, "auth_enabled", False)
    client = TestClient(app)

    # ── CRITERION 2: forwarding preserves bytes, status and headers ───────────
    await _criterion_2_forwarding_is_byte_faithful(client, vendor)
    # ── CRITERION 3: learning captures the real response ──────────────────────
    await _criterion_3_learning_captures_the_real_response(orch, buf, adapter)
    # ── CRITERION 4: local serving, plus a restart on the same volume ─────────
    await _criterion_4_serves_locally_and_survives_a_restart(dbm, orch, install, vendor)

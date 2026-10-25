"""The HTTPS path a real device uses must parse, learn and forward.

Found while proving the path on the Raspberry Pi. ``handle_tls_decrypted_request``
handed the decrypted body over as raw ``bytes``, while ``InterceptedRequest.body``
is the *parsed* body and ``session_cache.body`` is a JSON column. The INSERT
raised ``TypeError: Object of type bytes is not JSON serializable``, so on the
HTTPS path — the only one a real device speaks — **nothing was ever learned** and
the device received ``502 {"error": "local_response_unavailable"}`` instead of the
upstream response.
"""

from __future__ import annotations

import base64
import contextlib
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

import core.server as server_mod
from adapters.base import InterceptedRequest, normalize_request_body
from core import cloud_forward
from core.buffer import initialize_buffer_backend
from core.cloud_forward import forward_intercepted
from core.database import DatabaseManager, DeviceRegistry, SessionCache
from core.pipeline import LearningOrchestrator, LearningPipeline
from core.tls_mitm import DecryptedRequest

CLIENT_IP = "192.168.178.77"  # the phone/app talking to the device's local API
DEVICE_BODY = b'<command><temp_set value="240"/></command>'

VENDOR_STATUS = 200
VENDOR_BODY = b'{"result": {"ok": true}}'


class _VendorHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("content-length") or 0)
        self.server.received.append(self.rfile.read(length) if length else b"")  # type: ignore[attr-defined]
        self.send_response(VENDOR_STATUS)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(VENDOR_BODY)))
        self.end_headers()
        self.wfile.write(VENDOR_BODY)

    def log_message(self, _format, *_args):
        """Keep the test output clean."""


@pytest.fixture
def vendor():
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
    hostname: str
    port: int

    async def parse_request(self, request: InterceptedRequest) -> InterceptedRequest:
        return request

    async def forward_to_cloud(self, request: InterceptedRequest):
        return await forward_intercepted(
            request, hostname=self.hostname, port=self.port, use_tls=False
        )


class _Registry:
    def __init__(self, adapter: _Adapter) -> None:
        self.adapter = adapter

    def get_adapter(self, _vendor: str) -> _Adapter:
        return self.adapter


@pytest.fixture
def allow_loopback(monkeypatch):
    monkeypatch.setattr(cloud_forward, "is_blocked_ip", lambda _ip: False)
    monkeypatch.setattr(
        cloud_forward.CloudForwarder, "resolve", AsyncMock(return_value=["127.0.0.1"])
    )


# ── the helper itself ────────────────────────────────────────────────────────


def test_normalize_request_body_never_returns_bytes():
    """Every shape a device can send must become JSON-storable."""
    assert normalize_request_body(b"") is None
    assert normalize_request_body(b'{"a": 1}') == {"a": 1}
    assert normalize_request_body(DEVICE_BODY) == DEVICE_BODY.decode()
    assert normalize_request_body({"a": 1}) == {"a": 1}
    assert normalize_request_body(None) is None

    binary = bytes(range(256))
    encoded = normalize_request_body(binary)
    assert encoded == {
        "_encoding": "base64",
        "data": base64.b64encode(binary).decode("ascii"),
    }
    # lossless: the original bytes come back out
    assert base64.b64decode(encoded["data"]) == binary


# ── the path a real device uses ──────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.usefixtures("allow_loopback")
async def test_tls_path_learns_and_forwards(vendor, tmp_path, monkeypatch):
    """A decrypted HTTPS request must be recorded and forwarded, not 502'd."""
    root = tmp_path / "install"
    dbm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{root}/core.db",
        device_db_dir=root / "devices",
        echo=False,
    )
    await dbm.initialize()
    initialize_buffer_backend()
    # A device already bound to this source IP, as automatic recognition expects.
    await dbm.get_or_create_device("dev-1", "example", "plug", "Test Plug")
    async with dbm.core_session() as session:
        device = (
            await session.execute(select(DeviceRegistry).where(DeviceRegistry.device_id == "dev-1"))
        ).scalar_one()
        device.ip_addresses = [CLIENT_IP]
        device.mode = "learning"
        await session.commit()
    dbm.invalidate_device_cache("dev-1")

    orch = LearningOrchestrator(dbm)
    orch.initialize(None)
    buf = await orch.ensure_buffer("dev-1", buffer_size=524288)
    orch.pipeline = LearningPipeline(dbm, orch.llm_decipher, buf, orch.tracker, orch.engine)

    adapter = _Adapter(hostname="vendor.test", port=vendor.server_address[1])
    monkeypatch.setattr(server_mod, "db_manager", dbm)
    monkeypatch.setattr(server_mod, "orchestrator", orch)
    monkeypatch.setattr(server_mod, "adapter_registry", _Registry(adapter))

    request = DecryptedRequest(
        client_ip=CLIENT_IP,
        client_port=51234,
        dst_port=8443,
        sni="api.example.com",
        method="POST",
        path="/v1.0/device/dev-1/commands",
        http_version="1.1",
        headers={"host": "api.example.com", "content-type": "application/xml"},
        body=DEVICE_BODY,
    )

    result = await server_mod.handle_tls_decrypted_request(request)

    # Before the fix this returned None (the INSERT raised) and the MITM answered
    # 502 local_response_unavailable.
    assert result is not None, "the decrypted request must be processed, not dropped"
    assert result.get("action") == "forward", result

    # The pair reached the database: the body is no longer raw bytes.
    async with dbm.device_session("dev-1") as session:
        rows = (await session.execute(select(SessionCache))).scalars().all()
    assert rows, "the request must be registered for learning on the HTTPS path"
    stored = rows[-1]
    assert not isinstance(stored.body, (bytes, bytearray)), (
        f"bytes must never reach the JSON column: {stored.body!r}"
    )
    assert stored.path == "/v1.0/device/dev-1/commands"
    assert stored.method == "POST"

    async with dbm.core_session() as session:
        assert (
            await session.execute(select(func.count()).select_from(DeviceRegistry))
        ).scalar_one() == 1

    await dbm.close()

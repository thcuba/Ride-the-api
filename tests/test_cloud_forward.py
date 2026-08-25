"""
Tests for the DNS-loop-free cloud forwarding module.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from adapters.base import CommandResult, InterceptedRequest, ProtocolType
from core.cloud_forward import (
    CloudForwarder,
    CloudForwardError,
    _parse_status_line,
    forward_intercepted,
)


def _request(**kw) -> InterceptedRequest:
    defaults = dict(
        device_id="dev-1",
        timestamp=datetime.now(UTC),
        protocol=ProtocolType.HTTP,
        method="GET",
        path="/status",
    )
    defaults.update(kw)
    return InterceptedRequest(**defaults)


class FakeEchoServer:
    """Minimal HTTP/1.1 echo/JSON server for driving the forwarder end-to-end."""

    def __init__(self) -> None:
        self.handled: list[dict] = []

    async def _handler(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line_b = await asyncio.wait_for(reader.readline(), timeout=5)
        request_line = request_line_b.decode("latin-1").strip()
        headers: dict[str, str] = {}
        content_length = 0
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            if line in (b"\r\n", b"\n", b""):
                break
            key, _, value = line.decode("latin-1").partition(":")
            headers[key.strip().lower()] = value.strip()
            if key.strip().lower() == "content-length":
                content_length = int(value.strip())

        body = (
            await asyncio.wait_for(reader.readexactly(content_length), 5)
            if content_length
            else b""
        )

        status = 200  # noqa: PLR2004
        body_out = {
            "echo": request_line,
            "host_header": headers.get("host"),
            "length": len(body),
        }
        payload = json.dumps(body_out).encode("utf-8")
        response_head = (
            f"HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(response_head)
        writer.write(payload)
        with contextlib.suppress(Exception):
            await writer.drain()
        writer.close()
        self.handled.append(request_line)

    async def start(self):
        server = await asyncio.start_server(self._handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]  # type: ignore[index]
        self.server = server
        return port

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()


@pytest.fixture
def echo_server():
    srv = FakeEchoServer()
    srv.handled = []
    return srv


# --------------------
# CloudForwarder unit tests
# --------------------


@pytest.mark.asyncio
async def test_forward_basic_json(echo_server):
    port = await echo_server.start()
    forwarder = CloudForwarder()
    with patch(
        "core.cloud_forward.resolve_upstream",
        AsyncMock(return_value=["127.0.0.1"]),
    ):
        response = await forwarder.forward(
            hostname="cloud.example.com",
            ip="127.0.0.1",
            port=port,
            method="GET",
            path="/status",
            headers={"accept": "application/json"},
            use_tls=False,
        )
    assert response.status_code == 200  # noqa: PLR2004
    assert response.headers["content-type"] == "application/json"
    body = json.loads(response.body.decode())
    assert "echo" in body
    assert body["host_header"] == f"cloud.example.com:{port}"


@pytest.mark.asyncio
async def test_forward_connection_refused():
    forwarder = CloudForwarder(connect_timeout=2)
    # Port 1 is closed on most systems -> immediate failure.
    with pytest.raises(CloudForwardError):
        await forwarder.forward(
            hostname="cloud.example.com",
            ip="127.0.0.1",
            port=1,
            method="GET",
            path="/",
            use_tls=False,
        )


@pytest.mark.asyncio
async def test_parse_status_line_malformed():
    with pytest.raises(CloudForwardError):
        _parse_status_line(b"NONSENSE\r\n")


# --------------------
# forward_intercepted integration tests
# --------------------


@pytest.mark.asyncio
async def test_forward_intercepted_success(echo_server):
    port = await echo_server.start()
    req = _request(method="POST", path="/rpc/Switch.Set", body={"on": True})
    with patch(
        "core.cloud_forward.resolve_upstream",
        AsyncMock(return_value=["127.0.0.1", "192.0.2.1"]),
    ):
        result: CommandResult = await forward_intercepted(
            req, hostname="cloud.example.com", port=port, use_tls=False
        )
    assert result.success is True
    assert result.forwarded is True
    assert result.response is not None
    assert "echo" in result.response


@pytest.mark.asyncio
async def test_forward_intercepted_no_host():
    req = _request()
    result = await forward_intercepted(req, hostname="")
    assert result.success is False
    assert result.forwarded is True
    assert "no upstream host" in (result.error or "")


@pytest.mark.asyncio
async def test_forward_intercepted_dns_empty():
    req = _request()
    with patch(
        "core.cloud_forward.resolve_upstream",
        AsyncMock(return_value=[]),
    ):
        result = await forward_intercepted(req, hostname="cloud.example.com")
    assert result.success is False
    assert "no address found" in (result.error or "")


@pytest.mark.asyncio
async def test_forward_intercepted_all_ips_fail():
    req = _request()
    with patch(
        "core.cloud_forward.resolve_upstream",
        AsyncMock(return_value=["192.0.2.1", "198.51.100.1"]),
    ):
        result = await forward_intercepted(
            req, hostname="cloud.example.com", port=1, use_tls=False, connect_timeout=0.1
        )
    assert result.success is False
    assert result.forwarded is True
    assert "Cloud forward failed" in (result.error or "")

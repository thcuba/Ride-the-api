"""Tests for HTTP/2 bidirectional MITM proxy forwarding and learning correlation."""

from __future__ import annotations

import httpx
import pytest

from core.config import HTTP2ServerConfig
from core.protocol_servers.http2_server import HTTP2ServerPlugin


@pytest.mark.asyncio
async def test_http2_mitm_local_response_takes_precedence() -> None:
    """When a local response is available, return it without upstream forwarding."""

    async def handler(_request) -> dict:  # noqa: ANN001
        return {
            "action": "local_response",
            "response": {
                "status_code": 200,
                "headers": {"content-type": "application/json", "x-local": "true"},
                "body": {"mode": "local_cached"},
            },
        }

    config = HTTP2ServerConfig(
        host="127.0.0.1",
        cleartext_port=0,
        enabled=True,
        upstream_url="http://192.0.2.1:8080",
    )
    server = HTTP2ServerPlugin(config, handler=handler)
    await server.start()
    assert server._server is not None  # noqa: SLF001
    port = server._server.sockets[0].getsockname()[1]  # noqa: SLF001

    try:
        async with httpx.AsyncClient(
            http1=False, http2=True, base_url=f"http://127.0.0.1:{port}", timeout=5
        ) as client:
            resp = await client.get("/api/v1/state")
            assert resp.status_code == 200  # noqa: PLR2004
            assert resp.json() == {"mode": "local_cached"}
            assert resp.headers.get("x-local") == "true"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_http2_mitm_forwarding_and_learning() -> None:
    """In learning mode, forward request upstream and correlate response."""
    # 1. Start an upstream mock server
    upstream_received = []

    async def upstream_handler(request) -> dict:  # noqa: ANN001
        upstream_received.append(request)
        return {
            "action": "local_response",
            "response": {
                "status_code": 200,
                "headers": {"content-type": "application/json", "x-upstream": "cloud-v2"},
                "body": {"cloud_state": "online", "temperature": 23.5},
            },
        }

    upstream_config = HTTP2ServerConfig(host="127.0.0.1", cleartext_port=0, enabled=True)
    upstream_server = HTTP2ServerPlugin(upstream_config, handler=upstream_handler)
    await upstream_server.start()
    assert upstream_server._server is not None  # noqa: SLF001
    upstream_port = upstream_server._server.sockets[0].getsockname()[1]  # noqa: SLF001

    # 2. Start MITM proxy server pointing to mock upstream
    correlated = []

    def mock_response_handler(result, resp_body, status_code=200, headers=None) -> None:  # noqa: ANN001
        correlated.append(
            {
                "result": result,
                "body": resp_body,
                "status_code": status_code,
                "headers": headers or {},
            }
        )

    async def mitm_handler(request) -> dict:  # noqa: ANN001
        return {
            "action": "forward",
            "correlation_key": "h2-corr-12345",
            "device_id": request.device_id,
        }

    mitm_config = HTTP2ServerConfig(
        host="127.0.0.1",
        cleartext_port=0,
        enabled=True,
        upstream_url=f"http://127.0.0.1:{upstream_port}",
    )
    mitm_server = HTTP2ServerPlugin(
        mitm_config,
        handler=mitm_handler,
        response_handler=mock_response_handler,
    )
    await mitm_server.start()
    assert mitm_server._server is not None  # noqa: SLF001
    mitm_port = mitm_server._server.sockets[0].getsockname()[1]  # noqa: SLF001

    try:
        async with httpx.AsyncClient(
            http1=False, http2=True, base_url=f"http://127.0.0.1:{mitm_port}", timeout=5
        ) as client:
            resp = await client.post("/telemetry", json={"sensor": "temp", "val": 23.5})
            assert resp.status_code == 200  # noqa: PLR2004
            data = resp.json()
            assert data == {"cloud_state": "online", "temperature": 23.5}
            assert resp.headers.get("x-upstream") == "cloud-v2"

            # Verify upstream received the request
            assert len(upstream_received) == 1
            up_req = upstream_received[0]
            assert up_req.path == "/telemetry"
            assert up_req.body == {"sensor": "temp", "val": 23.5}

            # Verify response was correlated into learning pipeline
            assert len(correlated) == 1
            corr = correlated[0]
            assert corr["result"]["correlation_key"] == "h2-corr-12345"
            assert corr["body"] == {"cloud_state": "online", "temperature": 23.5}
            assert corr["status_code"] == 200  # noqa: PLR2004
    finally:
        await mitm_server.stop()
        await upstream_server.stop()


@pytest.mark.asyncio
async def test_http2_mitm_auto_upstream_loopback_refused() -> None:
    """Auto-upstream refuses loopback or self-address authority to prevent recursion."""

    async def mitm_handler(_request) -> dict:  # noqa: ANN001
        return {"action": "forward", "correlation_key": "loop-check"}

    mitm_config = HTTP2ServerConfig(
        host="127.0.0.1",
        cleartext_port=0,
        enabled=True,
        auto_upstream=True,
    )
    mitm_server = HTTP2ServerPlugin(mitm_config, handler=mitm_handler)
    await mitm_server.start()
    assert mitm_server._server is not None  # noqa: SLF001
    mitm_port = mitm_server._server.sockets[0].getsockname()[1]  # noqa: SLF001

    try:
        async with httpx.AsyncClient(
            http1=False, http2=True, base_url=f"http://127.0.0.1:{mitm_port}", timeout=5
        ) as client:
            # Client connects to loopback; auto-upstream detects loopback and refuses
            resp = await client.get("/ping")
            assert resp.status_code == 502  # noqa: PLR2004
    finally:
        await mitm_server.stop()

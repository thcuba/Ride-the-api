"""The h2c listener serves real HTTP/2 traffic (C-21)."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from core.config import HTTP2ServerConfig
from core.protocol_servers.http2_server import HTTP2ServerPlugin


def _served(status: int, body: object, headers: dict | None = None) -> dict:
    return {
        "action": "local_response",
        "response": {"status_code": status, "headers": headers or {}, "body": body},
    }


@pytest_asyncio.fixture
async def h2():
    state: dict = {"result": _served(200, {"ok": True}), "requests": []}

    async def handler(request) -> dict:  # noqa: ANN001
        state["requests"].append(request)
        return state["result"]

    plugin = HTTP2ServerPlugin(
        HTTP2ServerConfig(host="127.0.0.1", cleartext_port=0, enabled=True), handler=handler
    )
    await plugin.start()
    port = plugin._server.sockets[0].getsockname()[1]  # noqa: SLF001
    try:
        async with httpx.AsyncClient(
            http1=False, http2=True, base_url=f"http://127.0.0.1:{port}", timeout=10
        ) as client:
            yield client, state
    finally:
        await plugin.stop()


@pytest.mark.asyncio
async def test_request_line_body_and_served_response(h2):
    client, state = h2
    state["result"] = _served(201, {"ok": True}, {"X-Vendor": "acme", "Content-Length": "999"})

    response = await client.post("/api/v1/state?x=1", json={"t": 1})

    assert response.http_version == "HTTP/2"
    assert response.status_code == 201  # noqa: PLR2004
    assert response.json() == {"ok": True}
    assert response.headers["x-vendor"] == "acme"
    (request,) = state["requests"]
    assert (request.method, request.path, request.body) == ("POST", "/api/v1/state?x=1", {"t": 1})


@pytest.mark.asyncio
async def test_large_bodies_flow_both_ways(h2):
    client, state = h2
    state["result"] = _served(200, {"blob": "y" * 200_000})

    response = await client.post("/upload", json={"blob": "x" * 300_000})

    assert response.json() == {"blob": "y" * 200_000}  # > 16 KiB frames and the 64 KiB window
    (request,) = state["requests"]
    assert request.body == {"blob": "x" * 300_000}


@pytest.mark.asyncio
async def test_unserved_requests_leak_nothing(h2):
    client, state = h2
    state["result"] = {"action": "forward", "correlation_key": "secret-correlation-key"}

    response = await client.get("/api/status")

    assert response.status_code == 502  # noqa: PLR2004
    assert b"correlation" not in response.content

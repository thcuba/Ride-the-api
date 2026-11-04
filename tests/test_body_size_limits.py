"""Over-limit request bodies are a 413, on both planes (S-15, T-14)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from core import server as server_mod
from core.security import MaxBodySizeMiddleware

if TYPE_CHECKING:
    from starlette.requests import Request

TOO_LARGE = 413
CONTROL_CAP = server_mod.config_manager.config.proxy.max_request_size
DEVICE_CAP = server_mod.config_manager.config.proxy.max_device_request_size


async def _chunks(total: int, size: int = 64 * 1024):
    """A JSON-ish body sent with chunked transfer encoding (no Content-Length)."""
    sent = 0
    while sent < total:
        n = min(size, total - sent)
        sent += n
        yield b" " * n


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server_mod.config_manager.config.security, "auth_enabled", False)
    transport = httpx.ASGITransport(app=server_mod.app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "url"), [("POST", "/api/auth/password"), ("PUT", "/api/config")]
)
async def test_chunked_over_limit_control_plane_body_is_413(client, method, url):
    async with client:
        response = await client.request(
            method,
            url,
            content=_chunks(CONTROL_CAP + 1),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == TOO_LARGE  # it was a 500


@pytest.mark.asyncio
async def test_device_bodies_have_a_cap_too(client):
    async with client:
        response = await client.post("/acme/upload", content=b"x" * (DEVICE_CAP + 1))
    assert response.status_code == TOO_LARGE


def _app(control: int, device: int) -> Starlette:
    async def echo(request: Request) -> JSONResponse:
        return JSONResponse({"received": len(await request.body())})

    class PassThrough(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            return await call_next(request)

    app = Starlette(
        routes=[
            Route("/api/echo", echo, methods=["POST"]),
            Route("/{vendor}/{path:path}", echo, methods=["POST"]),
        ]
    )
    app.add_middleware(PassThrough)  # as in the real stack: wraps errors in groups
    app.add_middleware(MaxBodySizeMiddleware, max_size=control, data_plane_max_size=device)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("chunked", [False, True])
async def test_each_plane_gets_its_own_cap(chunked):
    app = _app(control=1024, device=4096)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    def body(n: int):
        return _chunks(n, 512) if chunked else b" " * n

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.post("/api/echo", content=body(1025))).status_code == TOO_LARGE
        assert (await c.post("/acme/up", content=body(2048))).json() == {"received": 2048}
        assert (await c.post("/acme/up", content=body(4097))).status_code == TOO_LARGE


@pytest.mark.asyncio
async def test_zero_disables_the_device_cap():
    transport = httpx.ASGITransport(app=_app(control=1024, device=0))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.post("/acme/up", content=b" " * 10_000)).json() == {"received": 10_000}

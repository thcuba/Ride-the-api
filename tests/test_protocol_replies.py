"""Protocol listeners answer devices with the served body, nothing internal (C-21)."""

from __future__ import annotations

import asyncio
import json

import aiocoap
import pytest
from aiocoap import GET, Message
from websockets.asyncio.client import connect

from core.config import CoAPServerConfig, WebSocketServerConfig
from core.protocol_servers import device_reply, encode_reply
from core.protocol_servers.coap_server import CoAPServerPlugin, _CoAPRoot
from core.protocol_servers.websocket_server import WebSocketServerPlugin

SERVED = {"action": "local_response", "response": {"status_code": 200, "body": {"ok": True}}}
FORWARDED = {"action": "forward", "correlation_key": "secret-correlation-key"}


def test_only_a_served_response_has_a_reply():
    assert device_reply(SERVED) == {"ok": True}
    assert device_reply(FORWARDED) is None
    assert device_reply(None) is None
    assert encode_reply({"ok": True}) == (b'{"ok": true}', True)
    assert encode_reply("pong") == (b"pong", False)


class _Recorder:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.requests: list = []

    async def __call__(self, request) -> dict:  # noqa: ANN001
        self.requests.append(request)
        return self.result


async def _ws_exchange(result: dict) -> tuple[str | None, _Recorder]:
    recorder = _Recorder(result)
    plugin = WebSocketServerPlugin(
        WebSocketServerConfig(host="127.0.0.1", port=0, enabled=True), handler=recorder
    )
    await plugin.start()
    port = next(iter(plugin._server.sockets)).getsockname()[1]  # noqa: SLF001
    try:
        async with connect(f"ws://127.0.0.1:{port}/device/socket?v=2") as ws:
            await ws.send('{"temp": 21}')
            try:
                reply = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except TimeoutError:
                reply = None
    finally:
        await plugin.stop()
    return reply, recorder


@pytest.mark.asyncio
async def test_websocket_answers_with_the_served_body():
    reply, recorder = await _ws_exchange(SERVED)
    assert json.loads(reply) == {"ok": True}
    (request,) = recorder.requests
    assert (request.path, request.query_params) == ("/device/socket", {"v": "2"})


@pytest.mark.asyncio
async def test_websocket_sends_nothing_internal():
    reply, _ = await _ws_exchange(FORWARDED)
    assert reply is None


class _Remote:
    sockaddr = ("::ffff:192.168.1.5", 5683, 0, 0)  # an IPv4 client on the v6 socket


def _coap_get(*path: str) -> Message:
    message = Message(code=GET, uri_path=path)
    message.remote = _Remote()
    return message


@pytest.mark.asyncio
async def test_coap_routes_every_path_and_knows_the_client():
    recorder = _Recorder(SERVED)
    root = _CoAPRoot(CoAPServerPlugin(CoAPServerConfig(), handler=recorder))

    response = await root.render(_coap_get("api", "v1", "status"))

    assert response.code == aiocoap.CONTENT
    assert json.loads(response.payload) == {"ok": True}
    (request,) = recorder.requests
    assert request.path == "/api/v1/status"
    assert request.device_id == "coap-192.168.1.5"


@pytest.mark.asyncio
async def test_coap_does_not_leak_pipeline_results():
    root = _CoAPRoot(CoAPServerPlugin(CoAPServerConfig(), handler=_Recorder(FORWARDED)))
    response = await root.render(_coap_get("api", "status"))
    assert response.code == aiocoap.NOT_FOUND
    assert b"correlation" not in response.payload


@pytest.mark.asyncio
async def test_coap_discovery_still_answers():
    root = _CoAPRoot(CoAPServerPlugin(CoAPServerConfig(), handler=_Recorder(SERVED)))
    response = await root.render(_coap_get(".well-known", "core"))
    assert response.payload.startswith(b"</>")

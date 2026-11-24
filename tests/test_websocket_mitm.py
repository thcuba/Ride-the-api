"""Tests for WebSocket bidirectional MITM proxy, upstream duplexing, and learning."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest
import websockets
from websockets.asyncio.client import connect

from adapters.base import InterceptedRequest, ProtocolType
from core.config import WebSocketServerConfig
from core.protocol_servers.websocket_server import WebSocketServerPlugin


class _MockUpstreamServer:
    """Mock vendor cloud WebSocket server."""

    def __init__(self) -> None:
        self.received_messages: list[Any] = []
        self.server: Any = None
        self.port: int = 0
        self.auto_reply: dict[str, Any] | bytes | None = None

    async def _handler(self, ws: Any) -> None:
        async for msg in ws:
            self.received_messages.append(msg)
            if self.auto_reply is not None:
                if isinstance(self.auto_reply, bytes):
                    await ws.send(self.auto_reply)
                else:
                    await ws.send(json.dumps(self.auto_reply))

    async def start(self) -> None:
        self.server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self.port = next(iter(self.server.sockets)).getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()


@pytest.mark.asyncio
async def test_websocket_mitm_duplex_forwarding_and_learning() -> None:
    """Test full duplex forwarding: client -> proxy -> cloud -> proxy -> client."""
    upstream = _MockUpstreamServer()
    upstream.auto_reply = {"id": 42, "status": "success", "state": "active"}
    await upstream.start()

    correlated_responses: list[tuple[dict, Any]] = []

    def mock_response_handler(handler_result: dict, body: Any) -> None:
        correlated_responses.append((handler_result, body))

    recorded_requests: list[InterceptedRequest] = []

    async def mock_pipeline_handler(req: InterceptedRequest) -> dict:
        recorded_requests.append(req)
        return {
            "action": "forward",
            "correlation_key": "corr-uuid-42",
            "_learning_context": {
                "device_id": req.device_id,
                "vendor": "tuya",
                "protocol": "websocket",
            },
        }

    cfg = WebSocketServerConfig(
        host="127.0.0.1",
        port=0,
        enabled=True,
        upstream_url=f"ws://127.0.0.1:{upstream.port}/cloud/ws",
    )
    plugin = WebSocketServerPlugin(
        cfg,
        handler=mock_pipeline_handler,
        response_handler=mock_response_handler,
    )
    await plugin.start()
    proxy_port = next(iter(plugin._server.sockets)).getsockname()[1]  # noqa: SLF001

    try:
        async with connect(f"ws://127.0.0.1:{proxy_port}/cloud/ws") as client_ws:
            client_msg = {"id": 42, "method": "power_on", "target": "switch_1"}
            await client_ws.send(json.dumps(client_msg))

            reply = await asyncio.wait_for(client_ws.recv(), timeout=2.0)
            parsed_reply = json.loads(reply)

            assert parsed_reply == {"id": 42, "status": "success", "state": "active"}
            assert len(upstream.received_messages) == 1
            assert json.loads(upstream.received_messages[0]) == client_msg

            # Verify pipeline request interception
            assert len(recorded_requests) == 1
            assert recorded_requests[0].protocol == ProtocolType.WEBSOCKET
            assert recorded_requests[0].body == client_msg

            # Verify response correlation
            assert len(correlated_responses) == 1
            handler_result, resp_body = correlated_responses[0]
            assert handler_result["correlation_key"] == "corr-uuid-42"
            assert resp_body == {"id": 42, "status": "success", "state": "active"}
    finally:
        await plugin.stop()
        await upstream.stop()


@pytest.mark.asyncio
async def test_websocket_mitm_local_response_takes_precedence() -> None:
    """When a learned pattern matches locally, answer client directly without upstream call."""
    upstream = _MockUpstreamServer()
    upstream.auto_reply = {"upstream": True}
    await upstream.start()

    async def mock_handler(req: InterceptedRequest) -> dict:  # noqa: ARG001
        return {
            "action": "local_response",
            "response": {"body": {"local_match": True, "source": "pattern_engine"}},
        }

    cfg = WebSocketServerConfig(
        host="127.0.0.1",
        port=0,
        enabled=True,
        upstream_url=f"ws://127.0.0.1:{upstream.port}/ws",
    )
    plugin = WebSocketServerPlugin(cfg, handler=mock_handler)
    await plugin.start()
    proxy_port = next(iter(plugin._server.sockets)).getsockname()[1]  # noqa: SLF001

    try:
        async with connect(f"ws://127.0.0.1:{proxy_port}/ws") as client_ws:
            await client_ws.send(json.dumps({"ping": 1}))
            reply = await asyncio.wait_for(client_ws.recv(), timeout=2.0)
            assert json.loads(reply) == {"local_match": True, "source": "pattern_engine"}

            # Upstream should NOT receive anything
            assert len(upstream.received_messages) == 0
    finally:
        await plugin.stop()
        await upstream.stop()


@pytest.mark.asyncio
async def test_websocket_mitm_auto_upstream_from_host_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-detect upstream cloud from client's Host header using resolve_upstream."""
    upstream = _MockUpstreamServer()
    upstream.auto_reply = {"cloud_ack": True}
    await upstream.start()

    # Mock DNS upstream resolver so the simulated domain points to our mock upstream server
    async def mock_resolve_upstream(hostname: str) -> list[str]:
        if hostname == "iot.custom-vendor.com":
            return ["127.0.0.1"]
        return []

    monkeypatch.setattr(
        "core.protocol_servers.websocket_server.resolve_upstream",
        mock_resolve_upstream,
    )

    async def mock_handler(req: InterceptedRequest) -> dict:  # noqa: ARG001
        return {"action": "forward", "correlation_key": "auto-key-1"}

    cfg = WebSocketServerConfig(
        host="127.0.0.1",
        port=0,
        enabled=True,
        upstream_url=None,  # No hardcoded upstream URL
        auto_upstream=True,
        upstream_ssl=False,
    )
    plugin = WebSocketServerPlugin(cfg, handler=mock_handler)
    await plugin.start()
    proxy_port = next(iter(plugin._server.sockets)).getsockname()[1]  # noqa: SLF001

    try:
        # Client connects with simulated vendor Host header
        custom_headers = {
            "Host": f"iot.custom-vendor.com:{upstream.port}",
            "X-Vendor-Token": "secret-device-token",
        }
        async with connect(
            f"ws://127.0.0.1:{proxy_port}/device/channel",
            additional_headers=custom_headers,
        ) as client_ws:
            await client_ws.send(json.dumps({"telemetry": "temp", "val": 22.5}))
            reply = await asyncio.wait_for(client_ws.recv(), timeout=2.0)
            assert json.loads(reply) == {"cloud_ack": True}
            assert len(upstream.received_messages) == 1
    finally:
        await plugin.stop()
        await upstream.stop()


@pytest.mark.asyncio
async def test_websocket_mitm_fallback_to_local_when_upstream_fails() -> None:
    """When upstream cloud is unreachable, falls back gracefully to local standalone mode."""
    # Port 1 is closed/unreachable
    cfg = WebSocketServerConfig(
        host="127.0.0.1",
        port=0,
        enabled=True,
        upstream_url="ws://127.0.0.1:1/ws",
        connect_timeout=0.5,
    )

    async def mock_handler(req: InterceptedRequest) -> dict:  # noqa: ARG001
        return {"action": "local_response", "response": {"body": {"fallback_ok": True}}}

    plugin = WebSocketServerPlugin(cfg, handler=mock_handler)
    await plugin.start()
    proxy_port = next(iter(plugin._server.sockets)).getsockname()[1]  # noqa: SLF001

    try:
        async with connect(f"ws://127.0.0.1:{proxy_port}/ws") as client_ws:
            await client_ws.send(json.dumps({"ping": "hello"}))
            reply = await asyncio.wait_for(client_ws.recv(), timeout=2.0)
            assert json.loads(reply) == {"fallback_ok": True}
    finally:
        await plugin.stop()


@pytest.mark.asyncio
async def test_websocket_mitm_binary_frames() -> None:
    """Bidirectional binary frame forwarding."""
    upstream = _MockUpstreamServer()
    upstream.auto_reply = b"\xde\xad\xbe\xef"
    await upstream.start()

    async def mock_handler(req: InterceptedRequest) -> dict:  # noqa: ARG001
        return {"action": "forward", "correlation_key": "bin-corr"}

    cfg = WebSocketServerConfig(
        host="127.0.0.1",
        port=0,
        enabled=True,
        upstream_url=f"ws://127.0.0.1:{upstream.port}/raw",
    )
    plugin = WebSocketServerPlugin(cfg, handler=mock_handler)
    await plugin.start()
    proxy_port = next(iter(plugin._server.sockets)).getsockname()[1]  # noqa: SLF001

    try:
        async with connect(f"ws://127.0.0.1:{proxy_port}/raw") as client_ws:
            raw_payload = b"\xca\xfe\xba\xbe"
            await client_ws.send(raw_payload)
            reply = await asyncio.wait_for(client_ws.recv(), timeout=2.0)
            assert reply == b"\xde\xad\xbe\xef"
            assert upstream.received_messages[0] == raw_payload
    finally:
        await plugin.stop()
        await upstream.stop()


EXPECTED_CORRELATED_COUNT = 2


@pytest.mark.asyncio
async def test_websocket_mitm_out_of_order_responses_with_event() -> None:
    """Out-of-order responses with intermediate push events must correlate by ID correctly."""
    connected_ws: list[Any] = []

    async def upstream_handler(ws: Any) -> None:
        connected_ws.append(ws)
        async for _ in ws:
            pass

    server = await websockets.serve(upstream_handler, "127.0.0.1", 0)
    upstream_port = next(iter(server.sockets)).getsockname()[1]

    correlated: list[tuple[str, Any]] = []

    def mock_resp_handler(res: dict, body: Any) -> None:
        correlated.append((res["correlation_key"], body))

    async def mock_handler(req: InterceptedRequest) -> dict:
        msg_id = (req.body or {}).get("id")
        return {
            "action": "forward",
            "correlation_key": f"key-{msg_id}",
            "_learning_context": {"device_id": "dev-1", "vendor": "v", "protocol": "websocket"},
        }

    cfg = WebSocketServerConfig(
        host="127.0.0.1",
        port=0,
        enabled=True,
        upstream_url=f"ws://127.0.0.1:{upstream_port}/ws",
    )
    plugin = WebSocketServerPlugin(cfg, handler=mock_handler, response_handler=mock_resp_handler)
    await plugin.start()
    proxy_port = next(iter(plugin._server.sockets)).getsockname()[1]  # noqa: SLF001

    try:
        async with connect(f"ws://127.0.0.1:{proxy_port}/ws") as client_ws:
            # Client sends req 1 and req 2
            await client_ws.send(json.dumps({"id": 1, "cmd": "get_temp"}))
            await client_ws.send(json.dumps({"id": 2, "cmd": "get_humidity"}))

            await asyncio.sleep(0.05)
            assert len(connected_ws) == 1
            upstream_conn = connected_ws[0]

            # Cloud sends unsolicited push event (no ID)
            await upstream_conn.send(json.dumps({"event": "motion_detected"}))
            # Cloud sends response 2 first (out of order!)
            await upstream_conn.send(json.dumps({"id": 2, "humidity": 55}))
            # Cloud sends response 1
            await upstream_conn.send(json.dumps({"id": 1, "temp": 21.0}))

            # Client receives all three messages
            m1 = json.loads(await asyncio.wait_for(client_ws.recv(), timeout=2.0))
            m2 = json.loads(await asyncio.wait_for(client_ws.recv(), timeout=2.0))
            m3 = json.loads(await asyncio.wait_for(client_ws.recv(), timeout=2.0))

            assert m1 == {"event": "motion_detected"}
            assert m2 == {"id": 2, "humidity": 55}
            assert m3 == {"id": 1, "temp": 21.0}

            # Correlated responses must be exactly matched to their keys
            assert len(correlated) == EXPECTED_CORRELATED_COUNT
            keys_and_bodies = {item[0]: item[1] for item in correlated}
            assert keys_and_bodies["key-2"] == {"id": 2, "humidity": 55}
            assert keys_and_bodies["key-1"] == {"id": 1, "temp": 21.0}
    finally:
        await plugin.stop()
        server.close()
        await server.wait_closed()

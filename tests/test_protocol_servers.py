"""Tests for the functional protocol servers.

Covers the stdlib-asyncio servers end-to-end (raw TCP, Matter control
endpoint, HTTP/2, WebSocket) and lifecycle assertions for the library-backed
servers (CoAP, Modbus, MQTT, bridges) so that a regression from the old
non-binding shells is caught: every plugin must bind/mark running and stop.
"""

import asyncio
import json

import pytest

from core.config import (
    CoAPServerConfig,
    HTTP2ServerConfig,
    MatterBridgeConfig,
    ModbusServerConfig,
    MQTTServerConfig,
    RawTCPServerConfig,
    WebSocketServerConfig,
    ZigbeeBridgeConfig,
    ZWaveBridgeConfig,
)
from core.protocol_servers.coap_server import CoAPServerPlugin
from core.protocol_servers.http2_server import HTTP2ServerPlugin
from core.protocol_servers.matter_bridge import MatterBridgePlugin
from core.protocol_servers.modbus_server import ModbusServerPlugin
from core.protocol_servers.mqtt_server import MQTTServerPlugin
from core.protocol_servers.raw_tcp_server import RawTCPServerPlugin
from core.protocol_servers.websocket_server import WebSocketServerPlugin
from core.protocol_servers.zigbee_bridge import ZigbeeBridgePlugin
from core.protocol_servers.zwave_bridge import ZWaveBridgePlugin


async def _bind_port(server: asyncio.AbstractServer) -> int:
    sock = server.sockets[0]
    return sock.getsockname()[1]


@pytest.mark.asyncio
async def test_raw_tcp_server_binds_and_routes():
    received = asyncio.get_running_loop().create_future()
    handler = received.set_result
    cfg = RawTCPServerConfig(host="127.0.0.1", port=0, enabled=True)
    plugin = RawTCPServerPlugin(cfg, handler=handler)
    await plugin.start()
    assert plugin.running
    assert plugin._server is not None  # noqa: SLF001
    port = await _bind_port(plugin._server)  # noqa: SLF001

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"hello raw")
    await writer.drain()
    req = await asyncio.wait_for(received, timeout=5)
    assert req is not None
    assert req.body["raw"].startswith("68656c6c6f")  # "hello" hex
    writer.close()
    await writer.wait_closed()

    await plugin.stop()
    assert not plugin.running


@pytest.mark.asyncio
async def test_matter_bridge_binds_and_routes():
    received = asyncio.get_running_loop().create_future()
    handler = received.set_result
    cfg = MatterBridgeConfig(controller_port=0, enabled=True)
    plugin = MatterBridgePlugin(cfg, handler=handler)
    await plugin.start()
    assert plugin.running
    port = await _bind_port(plugin._server)  # noqa: SLF001

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(json.dumps({"on": True}).encode("utf-8"))
    await writer.drain()
    req = await asyncio.wait_for(received, 5)
    assert req is not None
    assert req.body == {"on": True}
    writer.close()
    await writer.wait_closed()

    await plugin.stop()
    assert not plugin.running


@pytest.mark.asyncio
async def test_websocket_server_binds():
    cfg = WebSocketServerConfig(host="127.0.0.1", port=0, enabled=True)
    plugin = WebSocketServerPlugin(cfg)
    await plugin.start()
    assert plugin.running
    assert plugin._serving_task is not None  # noqa: SLF001
    await asyncio.sleep(0.2)
    assert not plugin._serving_task.done()  # noqa: SLF001
    await plugin.stop()
    assert not plugin.running


@pytest.mark.asyncio
async def test_http2_server_binds():
    cfg = HTTP2ServerConfig(host="127.0.0.1", cleartext_port=0, enabled=True)
    plugin = HTTP2ServerPlugin(cfg)
    await plugin.start()
    assert plugin.running
    assert plugin._server is not None  # noqa: SLF001
    await plugin.stop()
    assert not plugin.running


@pytest.mark.asyncio
async def test_coap_server_binds():
    cfg = CoAPServerConfig(host="127.0.0.1", port=0, enabled=True)
    plugin = CoAPServerPlugin(cfg)
    await plugin.start()
    assert plugin.running
    assert plugin._context is not None  # noqa: SLF001
    await plugin.stop()
    assert not plugin.running


@pytest.mark.asyncio
async def test_modbus_server_starts_and_stops():
    cfg = ModbusServerConfig(host="127.0.0.1", port=0, enabled=True)
    plugin = ModbusServerPlugin(cfg)
    await plugin.start()
    assert plugin.running
    await plugin.stop()
    assert not plugin.running


def test_mqtt_requires_amqtt_handler_graceful():
    """MQTT plugin with no handler must still construct cleanly."""
    cfg = MQTTServerConfig(host="127.0.0.1", port=0, enabled=True)
    plugin = MQTTServerPlugin(cfg)
    assert plugin.name == "mqtt"


@pytest.mark.asyncio
async def test_bridge_status_no_crash():
    zb = ZigbeeBridgePlugin(ZigbeeBridgeConfig(enabled=True))
    status = await zb.get_status()
    assert status["name"] == "zigbee_bridge"
    status2 = await ZWaveBridgePlugin(ZWaveBridgeConfig(enabled=True)).get_status()
    assert status2["name"] == "zwave_bridge"
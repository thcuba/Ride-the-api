"""A listener whose port is taken fails loudly and can start again later (R-7)."""

from __future__ import annotations

import socket

import pytest

from core.config import MQTTServerConfig, ProtocolServersConfig, WebSocketServerConfig
from core.protocol_servers import ProtocolServerManager
from core.protocol_servers.mqtt_server import MQTTServerPlugin
from core.protocol_servers.websocket_server import WebSocketServerPlugin


@pytest.fixture
def busy_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    yield sock.getsockname()[1]
    sock.close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _listening(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except OSError:
        return False
    return True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_cls", "config_cls"),
    [(WebSocketServerPlugin, WebSocketServerConfig), (MQTTServerPlugin, MQTTServerConfig)],
)
async def test_failed_bind_leaves_nothing_behind(busy_port, plugin_cls, config_cls):
    plugin = plugin_cls(config_cls(host="127.0.0.1", port=busy_port, enabled=True))
    with pytest.raises(Exception):  # noqa: B017, PT011 - OSError or the broker's wrapper
        await plugin.start()
    assert plugin.running is False

    # Moved to a free port (what a config reconcile does): it must really bind.
    port = _free_port()
    plugin.config = config_cls(host="127.0.0.1", port=port, enabled=True)
    await plugin.start()
    try:
        assert plugin.running is True
        assert _listening(port)
    finally:
        await plugin.stop()
    assert plugin.running is False


@pytest.mark.asyncio
async def test_manager_reports_the_failed_start(busy_port):
    config = ProtocolServersConfig()
    config.websocket = WebSocketServerConfig(host="127.0.0.1", port=busy_port, enabled=True)
    manager = ProtocolServerManager(config)
    manager.register_plugin(WebSocketServerPlugin(config.websocket))

    assert await manager.start_plugin("websocket") is False
    (status,) = [s for s in await manager.get_all_status() if s["name"] == "websocket"]
    assert status["running"] is False
    assert await manager.stop_plugin("websocket") is True  # stopping a failed listener works

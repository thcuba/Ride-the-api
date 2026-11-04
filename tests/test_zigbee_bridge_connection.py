"""The Zigbee bridge logs in, reconnects, and only counts a real connection (C-24)."""

from __future__ import annotations

import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from core.config import ZigbeeBridgeConfig
from core.protocol_servers import zigbee_bridge
from core.protocol_servers.zigbee_bridge import ZigbeeBridgePlugin

RECONNECT_S = 30


class _FakeClient:
    instances: list[_FakeClient] = []

    def __init__(self, *_args, **_kwargs) -> None:
        self.calls: list[tuple] = []
        _FakeClient.instances.append(self)

    def username_pw_set(self, user, password) -> None:  # noqa: ANN001
        self.calls.append(("auth", user, password))

    def reconnect_delay_set(self, min_delay, max_delay) -> None:  # noqa: ANN001
        self.calls.append(("delay", min_delay, max_delay))

    def connect_async(self, host, port) -> None:  # noqa: ANN001
        self.calls.append(("connect", host, port))

    def loop_forever(self, retry_first_connection: bool = False) -> None:
        self.calls.append(("loop", retry_first_connection))

    def subscribe(self, topic) -> None:  # noqa: ANN001
        self.calls.append(("subscribe", topic))

    def disconnect(self) -> None:
        pass


@pytest.mark.asyncio
async def test_bridge_logs_in_and_keeps_retrying(monkeypatch):
    monkeypatch.setattr(zigbee_bridge.mqtt, "Client", _FakeClient)
    plugin = ZigbeeBridgePlugin(
        ZigbeeBridgeConfig(
            enabled=True, mqtt_user="z2m", mqtt_pass="pw", reconnect_interval=RECONNECT_S
        )
    )
    await plugin.start()
    plugin._thread.join(timeout=2)
    calls = _FakeClient.instances[-1].calls
    await plugin.stop()

    assert ("auth", "z2m", "pw") in calls
    assert ("delay", 1, RECONNECT_S) in calls
    assert ("loop", True) in calls, "the first connection must be retried too"


def test_a_refused_connection_is_not_connected():
    plugin = ZigbeeBridgePlugin(ZigbeeBridgeConfig())
    plugin._prefix = "zigbee2mqtt"
    client = _FakeClient()

    plugin._on_connect(client, None, None, ReasonCode(PacketTypes.CONNACK, "Not authorized"), None)
    assert plugin._connected is False
    assert ("subscribe", "zigbee2mqtt/#") not in client.calls

    plugin._on_connect(client, None, None, ReasonCode(PacketTypes.CONNACK, "Success"), None)
    assert plugin._connected is True
    assert ("subscribe", "zigbee2mqtt/#") in client.calls

    plugin._on_disconnect(client, None, None, "connection lost", None)
    assert plugin._connected is False

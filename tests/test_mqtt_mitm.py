"""Tests for MQTT bidirectional MITM proxy forwarding, downlink relay, and learning."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.config import MQTTServerConfig
from core.protocol_servers.mqtt_server import (
    HAS_PAHO,
    MQTTServerPlugin,
    _parse_mqtt_payload,
)


@pytest.mark.skipif(not HAS_PAHO, reason="paho-mqtt not installed")
def test_parse_mqtt_payload() -> None:
    """Test payload parsing for JSON, string, and raw bytes."""
    assert _parse_mqtt_payload(b'{"temp": 21.5}') == {"temp": 21.5}
    assert _parse_mqtt_payload(b"[1, 2, 3]") == [1, 2, 3]
    assert _parse_mqtt_payload(b"\x00\x01\x02") == {"raw": "000102"}
    assert _parse_mqtt_payload(b"") == {"raw": ""}
    assert _parse_mqtt_payload(None) is None


@pytest.mark.asyncio
async def test_mqtt_mitm_forward_to_upstream_client() -> None:
    """When in learning mode (action=forward), publish to upstream client."""
    config = MQTTServerConfig(
        host="127.0.0.1",
        port=1883,
        upstream_host="cloud.iot-vendor.com",
        upstream_port=1883,
    )
    handler = AsyncMock(return_value={"action": "forward", "device_id": "mqtt-sensor"})
    plugin = MQTTServerPlugin(config, handler=handler)

    # Mock upstream client
    mock_upstream = MagicMock()
    plugin._upstream_client = mock_upstream

    result = await plugin.handle_message(
        client_id="mqtt-sensor",
        topic="devices/sensor/telemetry",
        payload=b'{"temperature": 22.4}',
        qos=1,
        retain=False,
    )

    assert result == {"action": "forward", "device_id": "mqtt-sensor"}
    mock_upstream.publish.assert_called_once_with(
        "devices/sensor/telemetry",
        b'{"temperature": 22.4}',
        qos=1,
        retain=False,
    )


@pytest.mark.asyncio
async def test_mqtt_mitm_downlink_relayed_and_correlated() -> None:
    """Cloud downlink message correlates into response handler and relays to local broker."""
    config = MQTTServerConfig(
        host="127.0.0.1",
        port=1883,
        upstream_host="cloud.iot-vendor.com",
    )
    correlated = []

    def mock_response_handler(result, resp_body, status_code=200, headers=None) -> None:  # noqa: ANN001
        correlated.append(
            {
                "result": result,
                "body": resp_body,
                "status_code": status_code,
                "headers": headers,
            }
        )

    plugin = MQTTServerPlugin(config, response_handler=mock_response_handler)
    mock_forward_client = MagicMock()
    plugin._forward_client = mock_forward_client

    # Simulate cloud downlink message
    downlink_payload = b'{"command": "set_target", "temp": 24.0}'
    await plugin._handle_upstream_downlink(
        topic="devices/sensor/commands",
        payload=downlink_payload,
        qos=1,
        retain=False,
    )

    # 1. Correlated into learning pipeline
    assert len(correlated) == 1
    corr = correlated[0]
    assert corr["body"] == {"command": "set_target", "temp": 24.0}
    assert corr["headers"]["topic"] == "devices/sensor/commands"

    # 2. Relayed down to local broker
    mock_forward_client.publish.assert_called_once_with(
        "devices/sensor/commands",
        downlink_payload,
        qos=1,
        retain=False,
    )

    # 3. Echo guard prevents re-publishing
    assert ("devices/sensor/commands", downlink_payload) in plugin._downlink_echo_guard

    # 4. Message arriving on local broker from our relay is suppressed
    handled = await plugin.handle_message(
        client_id="mqtt-devices",
        topic="devices/sensor/commands",
        payload=downlink_payload,
        qos=1,
        retain=False,
    )
    assert handled is None
    # Guard was consumed
    assert ("devices/sensor/commands", downlink_payload) not in plugin._downlink_echo_guard


@pytest.mark.asyncio
async def test_mqtt_mitm_self_loop_refused() -> None:
    """Upstream bridge refuses to connect to self on the same port."""
    config = MQTTServerConfig(
        host="127.0.0.1",
        port=1883,
        upstream_host="127.0.0.1",
        upstream_port=1883,
    )
    plugin = MQTTServerPlugin(config)
    plugin._start_upstream_bridge(config)

    # Bridge refused to connect to self port
    assert plugin._upstream_client is None

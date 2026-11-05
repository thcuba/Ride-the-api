"""Tests for MQTTServerPlugin payload parsing performance and parity."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.protocol_servers.mqtt_server import MQTTServerPlugin


@pytest.mark.asyncio
async def test_mqtt_handle_message_json_parsing():
    received_requests = []

    async def mock_handler(req):
        received_requests.append(req)
        return {"action": "ok"}

    cfg = SimpleNamespace(host="127.0.0.1", port=1883, tls_enabled=False)
    plugin = MQTTServerPlugin(cfg, handler=mock_handler)

    # 1. Valid JSON object
    await plugin.handle_message(
        client_id="mqtt-dev1",
        topic="sensors/temp",
        payload=b'{"temperature": 22.5, "humidity": 60}',
        qos=0,
        retain=False,
    )
    assert received_requests[-1].body == {"temperature": 22.5, "humidity": 60}

    # 2. JSON with leading whitespace
    await plugin.handle_message(
        client_id="mqtt-dev1",
        topic="sensors/status",
        payload=b'  \r\n\t {"state": "active"}',
        qos=1,
        retain=False,
    )
    assert received_requests[-1].body == {"state": "active"}

    # 3. JSON array
    await plugin.handle_message(
        client_id="mqtt-dev1",
        topic="sensors/array",
        payload=b"[1, 2, 3]",
        qos=0,
        retain=False,
    )
    assert received_requests[-1].body == [1, 2, 3]

    # 4. Plain text non-JSON string
    await plugin.handle_message(
        client_id="mqtt-dev1",
        topic="sensors/switch",
        payload=b"ON",
        qos=0,
        retain=False,
    )
    assert received_requests[-1].body == {"raw": b"ON".hex()}

    # 5. Arbitrary binary payload
    bin_payload = b"\x00\x01\xfe\xff"
    await plugin.handle_message(
        client_id="mqtt-dev1",
        topic="sensors/bin",
        payload=bin_payload,
        qos=0,
        retain=False,
    )
    assert received_requests[-1].body == {"raw": bin_payload.hex()}

    # 6. Empty payload
    await plugin.handle_message(
        client_id="mqtt-dev1",
        topic="sensors/empty",
        payload=b"",
        qos=0,
        retain=False,
    )
    assert received_requests[-1].body == {"raw": ""}

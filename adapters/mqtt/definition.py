"""Unified MQTT Protocol Definition combining adapter and network server plugin."""

from __future__ import annotations

from adapters.base import ProtocolDefinition
from adapters.mqtt.adapter import MQTTProtocolAdapter
from adapters.mqtt.server import MQTTServerPlugin

PROTOCOL_DEFINITION = ProtocolDefinition(
    name="mqtt",
    adapter_class=MQTTProtocolAdapter,
    server_plugin_class=MQTTServerPlugin,
    config_attr="mqtt",
    description="Standard MQTT broker server and generic protocol adapter",
)

__all__ = ["PROTOCOL_DEFINITION"]

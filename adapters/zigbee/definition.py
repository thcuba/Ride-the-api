"""Unified Zigbee Protocol Definition combining adapter and network bridge plugin."""

from __future__ import annotations

from adapters.base import ProtocolDefinition
from adapters.zigbee.adapter import ZigbeeProtocolAdapter
from adapters.zigbee.server import ZigbeeBridgePlugin

PROTOCOL_DEFINITION = ProtocolDefinition(
    name="zigbee",
    adapter_class=ZigbeeProtocolAdapter,
    server_plugin_class=ZigbeeBridgePlugin,
    config_attr="zigbee_bridge",
    description="Zigbee2MQTT bridge listener and generic ZCL adapter",
)

__all__ = ["PROTOCOL_DEFINITION"]

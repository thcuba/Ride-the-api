"""Unified CoAP Protocol Definition combining adapter and network server plugin."""

from __future__ import annotations

from adapters.base import ProtocolDefinition
from adapters.coap.adapter import CoAPProtocolAdapter
from adapters.coap.server import CoAPServerPlugin

PROTOCOL_DEFINITION = ProtocolDefinition(
    name="coap",
    adapter_class=CoAPProtocolAdapter,
    server_plugin_class=CoAPServerPlugin,
    config_attr="coap",
    adapter_vendor="coap_example",
    description="CoAP UDP server and protocol adapter",
)

__all__ = ["PROTOCOL_DEFINITION"]

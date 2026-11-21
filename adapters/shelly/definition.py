"""Shelly Protocol Definition."""

from __future__ import annotations

from adapters.base.definition import ProtocolDefinition
from adapters.shelly.adapter import ShellyProtocolAdapter

PROTOCOL_DEFINITION = ProtocolDefinition(
    name="shelly",
    adapter_class=ShellyProtocolAdapter,
    server_plugin_class=None,
    description="Shelly smart home device adapter",
)

__all__ = ["PROTOCOL_DEFINITION"]

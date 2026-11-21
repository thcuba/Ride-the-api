"""Z-Wave Protocol Definition."""

from __future__ import annotations

from adapters.base.definition import ProtocolDefinition
from adapters.zwave.adapter import ZWaveProtocolAdapter
from adapters.zwave.server import ZWaveBridgePlugin

PROTOCOL_DEFINITION = ProtocolDefinition(
    name="zwave",
    adapter_class=ZWaveProtocolAdapter,
    server_plugin_class=ZWaveBridgePlugin,
    config_attr="zwave_bridge",
    description="Z-Wave JS UI bridge listener and command class adapter",
)

__all__ = ["PROTOCOL_DEFINITION"]

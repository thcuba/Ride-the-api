"""Thread / Matter Protocol Definition."""

from __future__ import annotations

from adapters.base.definition import ProtocolDefinition
from adapters.thread_matter.adapter import MatterProtocolAdapter
from adapters.thread_matter.server import MatterBridgePlugin

PROTOCOL_DEFINITION = ProtocolDefinition(
    name="matter",
    adapter_class=MatterProtocolAdapter,
    server_plugin_class=MatterBridgePlugin,
    config_attr="matter_bridge",
    description="Matter controller bridge listener and interaction model adapter",
)

__all__ = ["PROTOCOL_DEFINITION"]

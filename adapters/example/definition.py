"""Example Protocol Definition."""

from __future__ import annotations

from adapters.base.definition import ProtocolDefinition
from adapters.example.adapter import ExampleProtocolAdapter

PROTOCOL_DEFINITION = ProtocolDefinition(
    name="example",
    adapter_class=ExampleProtocolAdapter,
    server_plugin_class=None,
    adapter_config={"region": "eu", "api_version": "v1.0"},
    description="Reference HVAC protocol adapter",
)

__all__ = ["PROTOCOL_DEFINITION"]

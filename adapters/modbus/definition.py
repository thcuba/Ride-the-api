"""Unified Modbus Protocol Definition combining adapter and network server plugin."""

from __future__ import annotations

from adapters.base import ProtocolDefinition
from adapters.modbus.adapter import ModbusProtocolAdapter
from adapters.modbus.server import ModbusServerPlugin

PROTOCOL_DEFINITION = ProtocolDefinition(
    name="modbus",
    adapter_class=ModbusProtocolAdapter,
    server_plugin_class=ModbusServerPlugin,
    config_attr="modbus",
    adapter_vendor="modbus_example",
    description="Modbus TCP industrial server and protocol adapter",
)

__all__ = ["PROTOCOL_DEFINITION"]

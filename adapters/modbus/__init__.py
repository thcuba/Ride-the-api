"""
Modbus Protocol Adapter Example — for Modbus TCP industrial devices (PLCs,
energy meters, inverters).

Modbus is a widely used industrial protocol. This adapter translates Modbus register
read/write operations to standardized CommandTypes.
"""

from __future__ import annotations

from adapters.modbus.adapter import (
    EXAMPLE_REGISTER_MAP,
    ModbusProtocolAdapter,
)
from adapters.modbus.definition import PROTOCOL_DEFINITION
from adapters.modbus.server import ModbusServerPlugin

__all__ = [
    "EXAMPLE_REGISTER_MAP",
    "ModbusProtocolAdapter",
    "ModbusServerPlugin",
    "PROTOCOL_DEFINITION",
]

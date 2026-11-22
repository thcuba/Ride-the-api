"""Modbus network server plugin re-export for the unified Modbus protocol bundle."""

from __future__ import annotations

try:
    from core.protocol_servers.modbus_server import ModbusServerPlugin
except ImportError:  # pragma: no cover
    ModbusServerPlugin = None  # type: ignore[assignment, misc]

__all__ = ["ModbusServerPlugin"]

"""
Modbus Protocol Server — industrial IoT protocol for PLCs, meters, sensors.

Modbus TCP carries register read/write operations over TCP port 502.
This server translates Modbus function codes into device commands:

- Read Holding Registers (0x03)   → get_state / read sensor
- Write Single Register (0x06)    → set command
- Write Multiple Registers (0x10) → set command
- Read Coils (0x01)               → read binary states
- Write Single Coil (0x05)        → turn_on / turn_off
- Read Input Registers (0x04)     → read sensor values
- Read Discrete Inputs (0x02)     → read binary sensor
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from datetime import datetime, timezone
from typing import Any, Callable

try:
    from pymodbus.server import StartAsyncTcpServer
    from pymodbus.datastore import ModbusSlaveContext, ModbusServerContext
    from pymodbus.device import ModbusDeviceIdentification
    HAS_PYMODBUS = True
except ImportError:
    HAS_PYMODBUS = False

from core.protocol_servers import ProtocolServerPlugin
from adapters.base import InterceptedRequest, ProtocolType, CommandType

logger = logging.getLogger(__name__)

# Modbus function codes
READ_COILS = 0x01
READ_DISCRETE_INPUTS = 0x02
READ_HOLDING_REGISTERS = 0x03
READ_INPUT_REGISTERS = 0x04
WRITE_SINGLE_COIL = 0x05
WRITE_SINGLE_REGISTER = 0x06
WRITE_MULTIPLE_COILS = 0x0F
WRITE_MULTIPLE_REGISTERS = 0x10


class ModbusServerPlugin(ProtocolServerPlugin):
    """Modbus TCP server for industrial device interception."""

    name = "modbus"

    def __init__(self, config: Any, handler: Callable | None = None):
        super().__init__(config)
        self.handler = handler
        self._context: ModbusServerContext | None = None
        self._server_task: asyncio.Task | None = None

    async def start(self) -> None:
        if not HAS_PYMODBUS:
            logger.warning("Modbus: pymodbus not installed — pip install pymodbus")
            self._running = False
            return

        cfg = self.config
        self._running = True

        # Initialize default register store
        store = ModbusSlaveContext(
            zero_mode=True,
            di=None, do=None,  # discrete inputs/outputs
            ir=struct.pack(">HHHHHHHH", 0, 0, 0, 0, 0, 0, 0, 0),  # input registers
            hr=struct.pack(">HHHHHHHH", 0, 0, 0, 0, 0, 0, 0, 0),  # holding registers
        )
        self._context = ModbusServerContext(slaves={cfg.unit_id: store}, single=False)

        logger.info("Modbus server enabled on %s:%d (unit_id=%d)", cfg.host, cfg.port, cfg.unit_id)

    async def stop(self) -> None:
        self._context = None
        await super().stop()
        logger.info("Modbus server stopped")

    async def handle_modbus_request(self, device_id: str, function_code: int,
                                     address: int, values: list[int] | None = None) -> dict | None:
        """Convert Modbus request to pipeline command."""
        if not self.handler:
            return None

        cmd_type = CommandType.UNKNOWN
        if function_code in (READ_HOLDING_REGISTERS, READ_INPUT_REGISTERS, READ_COILS, READ_DISCRETE_INPUTS):
            cmd_type = CommandType.GET_STATE
        elif function_code in (WRITE_SINGLE_REGISTER, WRITE_MULTIPLE_REGISTERS):
            cmd_type = CommandType.UNKNOWN  # Will be mapped by adapter
        elif function_code in (WRITE_SINGLE_COIL, WRITE_MULTIPLE_COILS):
            cmd_type = CommandType.TURN_ON if values and values[0] else CommandType.TURN_OFF

        request = InterceptedRequest(
            device_id=device_id,
            timestamp=datetime.now(timezone.utc).timestamp(),
            protocol=ProtocolType.MODBUS,
            method=f"0x{function_code:02X}",
            path=f"/modbus/{function_code}/{address}",
            body={"address": address, "values": values, "function_code": function_code},
            parsed_intent=cmd_type,
            parsed_params={"address": address, "values": values},
        )

        try:
            if asyncio.iscoroutinefunction(self.handler):
                return await self.handler(request)
            return self.handler(request)
        except Exception as e:
            logger.error("Modbus handler error: %s", e)
            return None

    async def get_status(self) -> dict:
        return {
            "name": self.name,
            "running": self._running,
            "host": self.config.host,
            "port": self.config.port,
            "unit_id": self.config.unit_id,
            "tls_enabled": self.config.tls_enabled,
        }
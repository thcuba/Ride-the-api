"""
Modbus Protocol Server ? industrial IoT protocol for PLCs, meters, sensors.

Modbus TCP carries register read/write operations over TCP port 502.
This server translates Modbus function codes into device commands:

- Read Holding Registers (0x03)   ? get_state / read sensor
- Write Single Register (0x06)    ? set command
- Write Multiple Registers (0x10) ? set command
- Read Coils (0x01)               ? read binary states
- Write Single Coil (0x05)        ? turn_on / turn_off
- Read Input Registers (0x04)     ? read sensor values
- Read Discrete Inputs (0x02)     ? read binary sensor
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

try:
    from pymodbus.datastore import (  # noqa: TC002
        ModbusDeviceContext,
        ModbusSequentialDataBlock,
        ModbusServerContext,
    )
    from pymodbus.server import ModbusTcpServer  # noqa: TC002

    HAS_PYMODBUS = True
except ImportError:  # pragma: no cover - exercised at import time only
    HAS_PYMODBUS = False

from adapters.base import CommandType, InterceptedRequest, ProtocolType
from core.protocol_servers import ProtocolServerPlugin

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

    def __init__(self, config: Any, handler: Callable | None = None) -> None:  # noqa: ANN401
        super().__init__(config)
        self.handler = handler
        self._context: ModbusServerContext | None = None
        self._server_task: asyncio.Task | None = None
        self._server: Any = None

    async def start(self) -> None:
        if not HAS_PYMODBUS:
            logger.warning("Modbus: pymodbus not installed")
            self._running = False
            return
        if self._server_task is not None:
            return

        cfg = self.config

        # Default register store (pymodbus 3.x device context).
        zero = [0] * 32
        store = ModbusDeviceContext(
            di=None,
            co=None,
                    ir=ModbusSequentialDataBlock(1, [0] * 8),
                    hr=ModbusSequentialDataBlock(1, zero),
        )
        self._context = ModbusServerContext(devices=store, single=True)

        self._server_task = asyncio.create_task(self._run_server(cfg.host, cfg.port))
        self._running = True
        logger.info("Modbus server listening on %s:%d (unit_id=%d)", cfg.host, cfg.port, cfg.unit_id)

    async def _run_server(self, host: str, port: int) -> None:
        """Run the pymodbus TCP server until it is stopped."""
        with contextlib.suppress(asyncio.CancelledError, OSError):
            server = ModbusTcpServer(self._context, address=(host, port))
            self._server = server
            await server.serve_forever()

    async def stop(self) -> None:
        if self._server_task is not None:
            self._server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._server_task
            self._server_task = None
        self._server = None
        self._context = None
        await super().stop()
        logger.info("Modbus server stopped")

    async def handle_modbus_request(
        self, device_id: str, function_code: int, address: int, values: list[int] | None = None
    ) -> dict | None:
        """Convert Modbus request to pipeline command."""
        if not self.handler:
            return None

        cmd_type = CommandType.UNKNOWN
        if function_code in (
            READ_HOLDING_REGISTERS,
            READ_INPUT_REGISTERS,
            READ_COILS,
            READ_DISCRETE_INPUTS,
        ):
            cmd_type = CommandType.GET_STATE
        elif function_code in (WRITE_SINGLE_REGISTER, WRITE_MULTIPLE_REGISTERS):
            cmd_type = CommandType.UNKNOWN  # Will be mapped by adapter
        elif function_code in (WRITE_SINGLE_COIL, WRITE_MULTIPLE_COILS):
            cmd_type = CommandType.TURN_ON if values and values[0] else CommandType.TURN_OFF

        request = InterceptedRequest(
            device_id=device_id,
            timestamp=datetime.now(UTC).timestamp(),
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
        except Exception:
            logger.exception("Modbus handler error:")
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

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

from adapters.base import InterceptedRequest, ProtocolType
from core.protocol_servers import ProtocolServerPlugin

logger = logging.getLogger(__name__)


if HAS_PYMODBUS:

    class _ForwardingModbusContext(ModbusServerContext):
        """Modbus device store that mirrors every access to the pipeline.

        Wraps the legacy pymodbus ``ModbusServerContext`` so that read/write
        operations on the register store are forwarded to the orchestrator
        handler in addition to being served from the local datastore. This
        connects intercepted Modbus traffic to the learning pipeline instead of
        silently serving a static register file.
        """

        def __init__(
            self,
            store: ModbusServerContext,
            handler: Callable | None,
            loop: asyncio.AbstractEventLoop,
        ) -> None:
            self._store = store
            self._handler = handler
            self._loop = loop

        def _notify(self, device_id: int, func_code: int, address: int, values=None, count: int = 1) -> None:
            """Fire a best-effort InterceptedRequest at the pipeline."""
            handler = self._handler
            if handler is None or self._loop is None or self._loop.is_closed():
                return
            operation = "write" if values is not None else "read"
            body = {
                "device_id": device_id,
                "func_code": func_code,
                "address": address,
                "operation": operation,
            }
            if values is not None:
                body["values"] = list(values) if isinstance(values, (list, tuple)) else values
            else:
                body["count"] = count
            request = InterceptedRequest(
                device_id=f"modbus-{device_id}",
                timestamp=datetime.now(UTC).timestamp(),
                protocol=ProtocolType.MODBUS,
                method="publish" if operation == "write" else "GET",
                path=f"/modbus/{func_code}/{address}",
                query_params={"device_id": str(device_id), "func_code": str(func_code), "address": str(address)},
                body=body,
            )
            try:
                if asyncio.iscoroutinefunction(handler):
                    asyncio.run_coroutine_threadsafe(
                        self._mirror(handler, request), self._loop
                    )
                else:
                    self._loop.call_soon_threadsafe(handler, request)
            except Exception:  # pragma: no cover - defensive
                logger.debug("Modbus forward to pipeline failed", exc_info=True)

        @staticmethod
        async def _mirror(handler, request: InterceptedRequest) -> None:
            try:
                await handler(request)
            except Exception:  # pragma: no cover - defensive
                logger.debug("Modbus pipeline handler error", exc_info=True)

        async def async_getValues(self, device_id: int, func_code: int, address: int, count: int = 1):
            self._notify(device_id, func_code, address, count=count)
            return await self._store.async_getValues(device_id, func_code, address, count)

        async def async_setValues(self, device_id: int, func_code: int, address: int, values):
            self._notify(device_id, func_code, address, values=values)
            return await self._store.async_setValues(device_id, func_code, address, values)


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
        self._context = _ForwardingModbusContext(
            ModbusServerContext(devices=store, single=True),
            handler=self.handler,
            loop=asyncio.get_event_loop(),
        )
        self._server_task = asyncio.create_task(self._run_server(cfg.host, cfg.port))
        self._running = True
        logger.info(
            "Modbus server listening on %s:%d (unit_id=%d)", cfg.host, cfg.port, cfg.unit_id
        )

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

    async def get_status(self) -> dict:
        return {
            "name": self.name,
            "running": self._running,
            "host": self.config.host,
            "port": self.config.port,
            "unit_id": self.config.unit_id,
            "tls_enabled": self.config.tls_enabled,
        }

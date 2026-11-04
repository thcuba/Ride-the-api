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
import time
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
from core.pattern_db.schemas import ObservationKind, TransportMeta
from core.protocol_servers import ProtocolServerPlugin, call_handler

logger = logging.getLogger(__name__)

# Longest wait for the server to shut down.
_STOP_TIMEOUT_S = 5


class _MirroringContext:
    """The server's datastore, with every read and write mirrored to the pipeline.

    pymodbus (3.x) builds its own datastore core from the ModbusServerContext
    and request PDUs reach it through ``server.context``. The former
    ModbusServerContext subclass skipped the base initialiser (so building
    the server raised AttributeError inside a background task, unseen, while
    the listener reported "running"), and pymodbus would have bypassed its
    overrides anyway. Wrapping the object the server really uses works
    whatever it is.
    """

    def __init__(self, inner: Any, notify: Callable) -> None:  # noqa: ANN401
        self._inner = inner
        self._notify = notify

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        return getattr(self._inner, name)

    async def async_getValues(  # noqa: N802 - pymodbus API
        self, device_id: int, func_code: int, address: int, count: int = 1
    ) -> Any:  # noqa: ANN401
        self._notify(device_id, func_code, address, count=count)
        return await self._inner.async_getValues(device_id, func_code, address, count)

    async def async_setValues(  # noqa: N802 - pymodbus API
        self,
        device_id: int,
        func_code: int,
        address: int,
        values: Any,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        self._notify(device_id, func_code, address, values=values)
        return await self._inner.async_setValues(device_id, func_code, address, values)


class ModbusServerPlugin(ProtocolServerPlugin):
    """Modbus TCP server for industrial device interception."""

    name = "modbus"

    def __init__(self, config: Any, handler: Callable | None = None) -> None:  # noqa: ANN401
        super().__init__(config)
        self.handler = handler
        self._server: Any = None
        self._pending: set[asyncio.Task] = set()

    async def start(self) -> None:
        if not HAS_PYMODBUS:
            logger.warning("Modbus: pymodbus not installed")
            self._running = False
            return
        if self._server is not None:
            return
        cfg = self.config
        # Default register store (pymodbus 3.x device context).
        store = ModbusDeviceContext(
            di=None,
            co=None,
            ir=ModbusSequentialDataBlock(1, [0] * 8),
            hr=ModbusSequentialDataBlock(1, [0] * 32),
        )
        server = ModbusTcpServer(
            ModbusServerContext(devices=store, single=True), address=(cfg.host, cfg.port)
        )
        server.context = _MirroringContext(server.context, self._notify)
        # Listen here so a failure (port in use) raises to the caller instead
        # of ending a background task nobody watches.
        await server.serve_forever(background=True)
        self._server = server
        self._running = True
        logger.info(
            "Modbus server listening on %s:%d (unit_id=%d)", cfg.host, cfg.port, cfg.unit_id
        )

    def _notify(
        self,
        device_id: int,
        func_code: int,
        address: int,
        values: Any = None,  # noqa: ANN401
        count: int = 1,
    ) -> None:
        """Fire a best-effort InterceptedRequest at the pipeline for one access."""
        if self.handler is None:
            return
        operation = "write" if values is not None else "read"
        body: dict[str, Any] = {
            "device_id": device_id,
            "func_code": func_code,
            "address": address,
            "operation": operation,
        }
        if values is not None:
            body["values"] = list(values) if isinstance(values, (list, tuple)) else values
        else:
            body["count"] = count
        tls = bool(getattr(self.config, "tls_enabled", False))
        request = InterceptedRequest(
            device_id=f"modbus-{device_id}",
            timestamp=time.time(),
            protocol=ProtocolType.MODBUS,
            method="publish" if operation == "write" else "GET",
            path=f"/modbus/{func_code}/{address}",
            query_params={
                "device_id": str(device_id),
                "func_code": str(func_code),
                "address": str(address),
            },
            body=body,
            transport=TransportMeta(
                port=getattr(self.config, "port", 502),
                tls=tls,
                func_code=func_code,
                reg_address=address,
            ),
            security="tls" if tls else "none",
            identity=f"modbus-{device_id}",
            kind=ObservationKind.FRAME,
        )
        task = asyncio.get_running_loop().create_task(self._mirror(request))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _mirror(self, request: InterceptedRequest) -> None:
        try:
            await call_handler(self.handler, request)
        except Exception:
            logger.debug("Modbus pipeline handler error", exc_info=True)

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.shutdown(), _STOP_TIMEOUT_S)
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

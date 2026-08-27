"""
Matter Bridge ? integrates with a Matter.js controller for Matter devices.

Matter (formerly Project CHIP) is the smart home standard based on IPv6; devices
communicate via the Interaction Model (clusters/attributes/commands). A full
Matter.js controller stack is a heavy dependency. This bridge instead binds a
real TCP control endpoint on ``controller_port`` that a Matter.js controller
can attach to, and routes any JSON payloads it receives into the pipeline.
If nothing connects it reports ``connected: False`` rather than faking it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from adapters.base import InterceptedRequest, ProtocolType, device_id_from_ip
from core.protocol_servers import ProtocolServerPlugin

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


class MatterBridgePlugin(ProtocolServerPlugin):
    """Bridge to a Matter.js controller for Matter device interception."""

    name = "matter_bridge"

    def __init__(self, config: Any, handler=None) -> None:  # noqa: ANN001, ANN401
        super().__init__(config)
        self.handler = handler
        self._server: asyncio.AbstractServer | None = None
        self._connected = False

    async def start(self) -> None:
        if self._server is not None:
            return
        cfg = self.config
        self._server = await asyncio.start_server(
            self._handle_connection, host="0.0.0.0", port=cfg.controller_port
        )
        self._running = True
        logger.info(
            "Matter bridge listening on :%d (fabric=%d)",
            cfg.controller_port, cfg.fabric_id,
        )

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.Writer) -> None:
        peername = writer.get_extra_info("peername", ("unknown", 0))
        remote_ip = peername[0]
        self._connected = True
        device_id = device_id_from_ip("matter", remote_ip)
        try:
            while True:
                try:
                    data = await asyncio.wait_for(reader.read(65535), timeout=30)
                except TimeoutError:
                    logger.debug("Matter bridge idle timeout from %s, closing", remote_ip)
                    break
                if not data:
                    break
                try:
                    body = json.loads(data.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    body = {"raw": data.hex()}
                request = InterceptedRequest(
                    device_id=device_id,
                    timestamp=datetime.now(UTC).timestamp(),
                    protocol=ProtocolType.MATTER,
                    body=body,
                )
                if self.handler:
                    try:
                        if asyncio.iscoroutinefunction(self.handler):
                            await self.handler(request)
                        else:
                            self.handler(request)
                    except Exception:
                        logger.exception("Matter handler error:")
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass
        except Exception:
            logger.exception("Matter bridge connection error:")
        finally:
            self._connected = False
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._connected = False
        await super().stop()
        logger.info("Matter bridge stopped")

    async def get_status(self) -> dict:
        return {
            "name": self.name,
            "running": self._running,
            "connected": self._connected,
            "controller_port": self.config.controller_port,
            "fabric_id": self.config.fabric_id,
        }

"""
Raw TCP Protocol Server — generic TCP listener with heuristic protocol detection.

For devices that use custom binary protocols over raw TCP (no HTTP/MQTT/CoAP framing).
The server buffers incoming data and uses heuristics (magic bytes, port, patterns) to
attempt protocol identification before falling back to a raw handler.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from adapters.base import InterceptedRequest, ProtocolType, device_id_from_ip
from core.protocol_servers import ProtocolServerPlugin

logger = logging.getLogger(__name__)

# Magic byte signatures for protocol detection
PROTOCOL_SIGNATURES: list[tuple[bytes, ProtocolType, str]] = [
    (b"GET ", ProtocolType.HTTP, "http"),
    (b"POST ", ProtocolType.HTTP, "http"),
    (b"PUT ", ProtocolType.HTTP, "http"),
    (b"DELETE ", ProtocolType.HTTP, "http"),
    (b"MQTT", ProtocolType.MQTT, "mqtt"),
    (b"\x10\x00", ProtocolType.MQTT, "mqtt_connect"),  # MQTT CONNECT packet
    (b"\x00\x00\x00\x00\x00\x06\x00", ProtocolType.MODBUS, "modbus"),
]


class RawTCPServerPlugin(ProtocolServerPlugin):
    """Raw TCP listener with protocol detection."""

    name = "raw_tcp"

    def __init__(self, config: Any, handler: Callable | None = None) -> None:  # noqa: ANN401
        super().__init__(config)
        self.handler = handler
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
            if self._server is not None:
                return
            cfg = self.config
            self._server = await asyncio.start_server(
                self._handle_connection, host=cfg.host, port=cfg.port
            )
            self._running = True
            logger.info("Raw TCP server listening on %s:%d", cfg.host, cfg.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await super().stop()
        logger.info("Raw TCP server stopped")

    def _detect_protocol(self, data: bytes, port: int) -> tuple[ProtocolType, str]:
        """Try to detect protocol from raw bytes."""
        for sig, proto, name in PROTOCOL_SIGNATURES:
            if data.startswith(sig):
                return proto, name
        if port == 502:  # noqa: PLR2004
            return ProtocolType.MODBUS, "modbus"
        if port == 1883:  # noqa: PLR2004
            return ProtocolType.MQTT, "mqtt"
        return ProtocolType.TCPIP, "raw"

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.Writer
    ) -> None:
        """Handle an incoming raw TCP connection."""
        peername = writer.get_extra_info("peername", ("unknown", 0))
        remote_ip = peername[0]
        # The remote (source) port is an ephemeral client port - protocol port
        # sniffing must use the local listening port from ``sockname``.
        local_port = int(
            (writer.get_extra_info("sockname") or (None, 0))[1] or 0
        )
        device_id = device_id_from_ip("raw", remote_ip)

        try:
            data = await asyncio.wait_for(reader.read(self.config.buffer_size), timeout=10)
            if not data:
                return

            # Some raw protocols split a single message across TCP segments, so
            # keep draining for a short idle grace period up to the configured
            # buffer cap. This stays bounded and never blocks indefinitely.
            collected = bytearray(data)
            while len(collected) < self.config.buffer_size:
                try:
                    chunk = await asyncio.wait_for(
                        reader.read(self.config.buffer_size), timeout=0.2
                    )
                except TimeoutError:
                    break  # idle: no more data in flight
                if not chunk:
                    break
                collected.extend(chunk)
            data = bytes(collected)

            proto, proto_name = self._detect_protocol(data, local_port)

            request = InterceptedRequest(
                device_id=device_id,
                timestamp=datetime.now(UTC).timestamp(),
                protocol=proto,
                body={"raw": data.hex(), "length": len(data), "port": local_port},
            )

            if self.handler:
                if asyncio.iscoroutinefunction(self.handler):
                    await self.handler(request)
                else:
                    self.handler(request)

        except TimeoutError:
            logger.debug("Raw TCP timeout from %s", remote_ip)
        except Exception:
            logger.exception("Raw TCP handler error from %s", remote_ip)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def get_status(self) -> dict:
        return {
            "name": self.name,
            "running": self._running,
            "host": self.config.host,
            "port": self.config.port,
            "protocol_detect": self.config.protocol_detect,
        }

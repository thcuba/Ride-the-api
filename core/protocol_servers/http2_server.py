"""
HTTP/2 Protocol Server ? h2c (cleartext, prior-knowledge) server.

HTTP/2 is used by some modern IoT clouds and devices for multiplexed
communication. This server implements the h2c "prior knowledge" transport
(and h2c upgrade) using the validated ``h2`` / ``hyperframe`` library: it puts
raw bytes received on the cleartext port through an ``H2Connection`` state
machine, decodes HEADERS/DATA frames into a request, routes to the pipeline
handler, and writes back a minimal HTTP/2 response stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import h2.config
import h2.connection
import h2.events
import h2.exceptions

if TYPE_CHECKING:
    from collections.abc import Callable

from adapters.base import InterceptedRequest, ProtocolType, device_id_from_ip
from core.protocol_servers import ProtocolServerPlugin

logger = logging.getLogger(__name__)


class HTTP2ServerPlugin(ProtocolServerPlugin):
    """HTTP/2 server for h2c prior-knowledge transport."""

    name = "http2"

    def __init__(self, config: Any, handler: Callable | None = None) -> None:  # noqa: ANN401
        super().__init__(config)
        self.handler = handler
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        if self._server is not None:
            return
        cfg = self.config
        self._server = await asyncio.start_server(
            self._handle_connection, host=cfg.host, port=cfg.cleartext_port
        )
        self._running = True
        logger.info("HTTP/2 server listening on %s:%d (cleartext)", cfg.host, cfg.cleartext_port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await super().stop()
        logger.info("HTTP/2 server stopped")

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.Writer) -> None:
        peername = writer.get_extra_info("peername", ("unknown", 0))
        remote_ip = peername[0]
        device_id = device_id_from_ip("h2", remote_ip)

        try:
            conn = h2.connection.H2Connection(
                config=h2.config.H2Configuration(client_side=False)
            )
            conn.initiate_connection()
            writer.write(conn.data_to_send())
            await writer.drain()

            buffered_data: dict[int, bytearray] = {}

            while True:
                data = await reader.read(65535)
                if not data:
                    break
                events = conn.receive_data(data)
                for event in events:
                    if isinstance(event, h2.events.RequestReceived):
                        await self._handle_request(conn, writer, event, device_id, buffered_data)
                    elif isinstance(event, h2.events.DataReceived):
                        buffered_data.setdefault(event.stream_id, bytearray()).extend(event.data)
                if conn.data_to_send():
                    writer.write(conn.data_to_send())
                    await writer.drain()
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass
        except Exception:
            logger.exception("HTTP/2 handler error from %s", remote_ip)
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _handle_request(self, conn, writer, event, device_id, buffered_data) -> None:  # noqa: ANN001
        headers = dict(event.headers)
        method = headers.get(":method", "GET")
        path = headers.get(":path", "/")
        scheme = headers.get(":scheme", "http")
        stream_id = event.stream_id

        body = None
        data = bytes(buffered_data.get(stream_id, bytearray()))
        if data:
            try:
                body = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                body = {"raw": data.hex()}
        buffered_data.pop(stream_id, None)

        request = InterceptedRequest(
            device_id=device_id,
            timestamp=datetime.now(UTC).timestamp(),
            protocol=ProtocolType.HTTP2,
            method=method,
            path=path if path.startswith("/") else f"/{path}",
            scheme=scheme,
            headers=headers,
            body=body,
        )

        result = None
        if self.handler:
            try:
                if asyncio.iscoroutinefunction(self.handler):
                    result = await self.handler(request)
                else:
                    result = self.handler(request)
            except Exception:
                logger.exception("HTTP/2 pipeline handler error:")

        if result and isinstance(result, dict):
            rsp_body = json.dumps(result.get("response", result)).encode("utf-8")
        else:
            rsp_body = b"{}"
        conn.send_headers(
            stream_id=stream_id,
            headers=[
                (":status", "200"),
                ("content-type", "application/json"),
                ("content-length", str(len(rsp_body))),
            ],
        )
        conn.send_data(stream_id=stream_id, data=rsp_body, end_stream=True)
        writer.write(conn.data_to_send())
        await writer.drain()

    async def get_status(self) -> dict:
        return {
            "name": self.name,
            "running": self._running,
            "host": self.config.host,
            "port": self.config.port,
            "cleartext_port": self.config.cleartext_port,
            "tls_enabled": self.config.tls_enabled,
        }

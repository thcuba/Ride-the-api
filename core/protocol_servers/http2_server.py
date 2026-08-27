"""
HTTP/2 Protocol Server — h2c (cleartext, prior-knowledge) server.

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
import h2.errors
import h2.events
import h2.exceptions

if TYPE_CHECKING:
    from collections.abc import Callable

from adapters.base import InterceptedRequest, ProtocolType, device_id_from_ip
from core.protocol_servers import ProtocolServerPlugin

logger = logging.getLogger(__name__)

# Idle read timeout (seconds) before a client that opens a socket but never
# sends is dropped, preventing connection/resource exhaustion.
_IDLE_TIMEOUT = 60

# Maximum buffered request body per stream (bytes). Bodies beyond this abort
# the stream (REFUSED_STREAM) instead of growing without bound in memory.
_MAX_BODY_BYTES = 1024 * 1024

# Maximum number of in-flight request headers held while awaiting their body.
# Guards the per-connection request map against unbounded growth.
_MAX_PENDING_STREAMS = 512


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

    async def _handle_connection(  # noqa: C901, PLR0912, PLR0915
        self, reader: asyncio.StreamReader, writer: asyncio.Writer
    ) -> None:
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

            # Per-stream pending requests: headers arrive in the HEADERS frame
            # (RequestReceived) and the body arrives later in DATA frames, so
            # we must assemble both before dispatching to the handler.
            pending: dict[int, dict[str, Any]] = {}

            while True:
                try:
                    data = await asyncio.wait_for(reader.read(65535), timeout=_IDLE_TIMEOUT)
                except TimeoutError:
                    logger.info("HTTP/2 idle timeout from %s, closing", remote_ip)
                    break
                if not data:
                    break
                events = conn.receive_data(data)
                for event in events:
                    if isinstance(event, h2.events.RequestReceived):
                        if len(pending) >= _MAX_PENDING_STREAMS:
                            conn.reset_stream(
                                event.stream_id,
                                error_code=h2.errors.ErrorCodes.REFUSED_STREAM,
                            )
                        else:
                            pending[event.stream_id] = {
                                "headers": dict(event.headers),
                                "body": bytearray(),
                                "stream_id": event.stream_id,
                            }
                            # A request whose headers carry END_STREAM has no body
                            # and is fully dispatched while here.
                            if event.stream_ended:
                                stream = pending.pop(event.stream_id)
                                await self._drain(conn, writer, stream, device_id)
                    elif isinstance(event, h2.events.DataReceived):
                        stream = pending.get(event.stream_id)
                        if stream is not None:
                            if len(stream["body"]) + len(event.data) > _MAX_BODY_BYTES:
                                conn.reset_stream(
                                    event.stream_id,
                                    error_code=h2.errors.ErrorCodes.REFUSED_STREAM,
                                )
                                # Drop the entry so a later StreamEnded cannot try to
                                # send headers on a locally-closed stream.
                                pending.pop(event.stream_id, None)
                            else:
                                stream["body"].extend(event.data)
                        # Always acknowledge so flow control keeps the stream alive.
                        conn.acknowledge_received_data(len(event.data), event.stream_id)
                    elif isinstance(event, h2.events.StreamEnded):
                        stream = pending.pop(event.stream_id, None)
                        if stream is not None:
                            await self._drain(conn, writer, stream, device_id)
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

    async def _drain(
        self,
        conn: h2.connection.H2Connection,
        writer: asyncio.Writer,
        stream: dict,
        device_id: str,
    ) -> None:
        """Build the request from headers + accumulated body and dispatch."""
        headers = stream["headers"]
        body_raw = bytes(stream["body"])
        method = headers.get(":method", "GET")
        path = headers.get(":path", "/")
        scheme = headers.get(":scheme", "http")
        stream_id = stream["stream_id"]

        body = None
        if body_raw:
            try:
                body = json.loads(body_raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                body = {"raw": body_raw.hex()}

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

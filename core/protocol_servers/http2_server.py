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
import time
from typing import TYPE_CHECKING, Any

import h2.config
import h2.connection
import h2.errors
import h2.events
import h2.exceptions

if TYPE_CHECKING:
    from collections.abc import Callable

from adapters.base import InterceptedRequest, ProtocolType, device_id_from_ip
from core.http_framing import servable_status, without_framing_headers
from core.pattern_db.schemas import ObservationKind, TransportMeta
from core.protocol_servers import (
    ProtocolServerPlugin,
    call_handler,
    device_reply,
    encode_reply,
)

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
            # str header names/values: with h2's default (bytes) every
            # headers.get(":method") / (":path") missed, so all requests were
            # read as "GET /".
            conn = h2.connection.H2Connection(
                config=h2.config.H2Configuration(client_side=False, header_encoding="utf-8")
            )
            conn.initiate_connection()
            await _flush(conn, writer)

            # Per-stream pending requests: headers arrive in the HEADERS frame
            # (RequestReceived) and the body arrives later in DATA frames, so
            # we must assemble both before dispatching to the handler.
            pending: dict[int, dict[str, Any]] = {}
            # Response bodies still waiting for flow-control window.
            outbox: dict[int, bytes] = {}

            while True:
                try:
                    data = await asyncio.wait_for(reader.read(65535), timeout=_IDLE_TIMEOUT)
                except TimeoutError:
                    logger.info("HTTP/2 idle timeout from %s, closing", remote_ip)
                    break
                if not data:
                    break
                for event in conn.receive_data(data):
                    if isinstance(event, h2.events.RequestReceived):
                        if len(pending) >= _MAX_PENDING_STREAMS:
                            conn.reset_stream(
                                event.stream_id,
                                error_code=h2.errors.ErrorCodes.REFUSED_STREAM,
                            )
                            continue
                        pending[event.stream_id] = {
                            "headers": dict(event.headers),
                            "body": bytearray(),
                            "stream_id": event.stream_id,
                        }
                        # A request whose headers carry END_STREAM has no body
                        # and is fully dispatched while here.
                        if event.stream_ended:
                            stream = pending.pop(event.stream_id)
                            await self._respond(conn, outbox, stream, device_id, remote_ip)
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
                        conn.acknowledge_received_data(
                            event.flow_controlled_length, event.stream_id
                        )
                    elif isinstance(event, h2.events.StreamEnded):
                        stream = pending.pop(event.stream_id, None)
                        if stream is not None:
                            await self._respond(conn, outbox, stream, device_id, remote_ip)
                    elif isinstance(event, h2.events.StreamReset):
                        pending.pop(event.stream_id, None)
                        outbox.pop(event.stream_id, None)
                # Window updates (or the response just queued) may let more
                # body frames out.
                _send_queued(conn, outbox)
                await _flush(conn, writer)
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass
        except Exception:
            logger.exception("HTTP/2 handler error from %s", remote_ip)
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _respond(
        self,
        conn: h2.connection.H2Connection,
        outbox: dict[int, bytes],
        stream: dict,
        device_id: str,
        remote_ip: str,
    ) -> None:
        """Dispatch a complete request and queue its response."""
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

        # There is no ProtocolType.HTTP2: building the request raised for
        # every stream. HTTP/2 carries HTTP semantics; the scheme says which.
        request = InterceptedRequest(
            device_id=device_id,
            timestamp=time.time(),
            protocol=ProtocolType.HTTPS if scheme == "https" else ProtocolType.HTTP,
            client_ip=remote_ip,
            method=method,
            path=path if path.startswith("/") else f"/{path}",
            scheme=scheme,
            headers={k: v for k, v in headers.items() if not k.startswith(":")},
            body=body,
            # This listener is the cleartext (h2c) port.
            transport=TransportMeta(port=getattr(self.config, "cleartext_port", 0), tls=False),
            security="none",
            identity=device_id,
            kind=ObservationKind.REQUEST,
        )

        result = None
        if self.handler:
            try:
                result = await call_handler(self.handler, request)
            except Exception:
                logger.exception("HTTP/2 pipeline handler error:")

        # Only a locally served response answers with content; anything else
        # used to come back as a 200 carrying the internal pipeline dict.
        reply = device_reply(result)
        if reply is None:
            conn.send_headers(stream_id, [(":status", "502")], end_stream=True)
            return
        response = result["response"]
        payload, is_json = encode_reply(reply)
        response_headers = [(":status", str(servable_status(response.get("status_code"))))]
        response_headers += [
            (name.lower(), value)
            for name, value in without_framing_headers(response.get("headers")).items()
            if name.lower() != "content-type"
        ]
        content_type = "application/json" if is_json else "application/octet-stream"
        response_headers += [
            ("content-type", (response.get("headers") or {}).get("content-type", content_type)),
            ("content-length", str(len(payload))),
        ]
        conn.send_headers(stream_id, response_headers, end_stream=not payload)
        if payload:
            outbox[stream_id] = payload
            _send_queued(conn, outbox)

    async def get_status(self) -> dict:
        return {
            "name": self.name,
            "running": self._running,
            "host": self.config.host,
            "port": self.config.port,
            "cleartext_port": self.config.cleartext_port,
            "tls_enabled": self.config.tls_enabled,
        }


def _send_queued(conn: h2.connection.H2Connection, outbox: dict[int, bytes]) -> None:
    """Send queued response bodies as far as frame size and flow control allow.

    One send_data call with the whole body raised FrameTooLargeError above
    16 KiB and ignored the peer's window.
    """
    for stream_id in list(outbox):
        data = outbox[stream_id]
        while data:
            room = min(conn.local_flow_control_window(stream_id), conn.max_outbound_frame_size)
            if room <= 0:
                break
            chunk, data = data[:room], data[room:]
            conn.send_data(stream_id, chunk, end_stream=not data)
        if data:
            outbox[stream_id] = data
        else:
            del outbox[stream_id]


async def _flush(conn: h2.connection.H2Connection, writer: asyncio.Writer) -> None:
    # data_to_send() drains the buffer: calling it once to test and again to
    # write sent nothing, so SETTINGS ACKs and WINDOW_UPDATEs were lost.
    data = conn.data_to_send()
    if data:
        writer.write(data)
        await writer.drain()

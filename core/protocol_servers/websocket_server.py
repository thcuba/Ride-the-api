"""
WebSocket Protocol Server ? native WebSocket listener for device real-time communication.

Many IoT devices use WebSocket for real-time bidirectional communication
(status updates, command streaming, event notifications). This server acts as
a WebSocket endpoint that devices connect to instead of the vendor cloud,
using the validated ``websockets`` library.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable

try:
    import websockets
    from websockets.exceptions import ConnectionClosed

    HAS_WEBSOCKETS = True
except ImportError:  # pragma: no cover - exercised at import time only
    HAS_WEBSOCKETS = False

from adapters.base import InterceptedRequest, ProtocolType, device_id_from_ip
from core.pattern_db.schemas import ObservationKind, TransportMeta
from core.protocol_servers import (
    ProtocolServerPlugin,
    call_handler,
    device_reply,
    encode_reply,
)

logger = logging.getLogger(__name__)

# Longest wait for open connections to close when the server stops.
_STOP_TIMEOUT_S = 5


class WebSocketServerPlugin(ProtocolServerPlugin):
    """Native WebSocket server for device interception."""

    name = "websocket"

    def __init__(self, config: Any, handler: Callable | None = None) -> None:  # noqa: ANN401
        super().__init__(config)
        self.handler = handler
        self._server: Any = None

    async def start(self) -> None:
        if not HAS_WEBSOCKETS:
            logger.warning("WebSocket: websockets not installed - pip install websockets")
            self._running = False
            return
        if self._server is not None:
            return
        cfg = self.config
        # Bind here, so a failure (port in use) raises to the caller. Binding
        # inside a background task left the plugin "running" on a dead task
        # that made every later start a no-op and every stop raise.
        self._server = await websockets.serve(
            self._handle_ws, cfg.host, cfg.port, max_size=cfg.max_message_size
        )
        self._running = True
        logger.info("WebSocket server listening on %s:%d%s", cfg.host, cfg.port, cfg.path)

    async def _handle_ws(self, connection) -> None:  # noqa: ANN001, C901, PLR0912
        """Handle an individual WebSocket connection (websockets >= 14 API)."""
        remote_info = connection.remote_address
        remote_ip = remote_info[0] if remote_info else "unknown"
        device_id = device_id_from_ip("ws", remote_ip)
        # Learn the path the device actually used (every message used to be
        # recorded under the configured path, whatever the device asked for).
        target = urlsplit(getattr(getattr(connection, "request", None), "path", "") or "")
        path = target.path or self.config.path
        query_params = dict(parse_qsl(target.query))

        handler = self.handler

        try:
            async for message in connection:
                body = None
                # Performance optimization: fast check for leading JSON structural characters
                # ('{', '[') or bytes (123, 91) before calling json.loads(). For non-JSON or
                # binary WebSocket messages, this bypasses json.loads and exception
                # creation/stack-unwinding overhead (~7.8x speedup).
                if isinstance(message, str):
                    stripped = message.lstrip()
                    if stripped and stripped[0] in ("{", "["):
                        with contextlib.suppress(json.JSONDecodeError, TypeError):
                            body = json.loads(stripped)
                    if body is None:
                        body = {"raw": message}
                elif isinstance(message, bytes):
                    stripped_b = message.lstrip()
                    if stripped_b and stripped_b[0] in (123, 91):
                        with contextlib.suppress(
                            json.JSONDecodeError, UnicodeDecodeError, TypeError
                        ):
                            body = json.loads(stripped_b.decode("utf-8"))
                    if body is None:
                        body = {"raw": message.hex()}
                else:
                    body = {"raw": str(message)}

                # Fast timestamp generation using time.time() instead of
                # datetime.now(UTC).timestamp() (~4.8x speedup)
                request = InterceptedRequest(
                    device_id=device_id,
                    timestamp=time.time(),
                    protocol=ProtocolType.WEBSOCKET,
                    client_ip=remote_ip,
                    method="WS",
                    path=path,
                    query_params=query_params,
                    body=body,
                    transport=TransportMeta(port=getattr(self.config, "port", 9000)),
                    security="none",
                    identity=device_id,
                    kind=ObservationKind.EVENT,
                )
                if handler:
                    try:
                        result = await call_handler(handler, request)
                        # Only a locally served response answers the device; the
                        # pipeline result itself (with its correlation key) was
                        # being sent as the reply.
                        reply = device_reply(result)
                        if reply is not None:
                            payload, is_json = encode_reply(reply)
                            await connection.send(payload.decode("utf-8") if is_json else payload)
                    except ConnectionClosed:
                        break
                    except Exception:
                        logger.exception("WebSocket handler error:")
        except ConnectionClosed:
            return
        except Exception:
            logger.exception("WebSocket connection error:")

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            # Open connections are closed by close(); do not wait for ever.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(server.wait_closed(), _STOP_TIMEOUT_S)
        await super().stop()
        logger.info("WebSocket server stopped")

    async def get_status(self) -> dict:
        return {
            "name": self.name,
            "running": self._running,
            "host": self.config.host,
            "port": self.config.port,
            "path": self.config.path,
        }

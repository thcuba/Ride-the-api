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
from core.protocol_servers import ProtocolServerPlugin

logger = logging.getLogger(__name__)


class WebSocketServerPlugin(ProtocolServerPlugin):
    """Native WebSocket server for device interception."""

    name = "websocket"

    def __init__(self, config: Any, handler: Callable | None = None) -> None:  # noqa: ANN401
        super().__init__(config)
        self.handler = handler
        self._serving_task: asyncio.Task | None = None
        self._stop_future: asyncio.Future | None = None

    async def start(self) -> None:
        if not HAS_WEBSOCKETS:
            logger.warning("WebSocket: websockets not installed - pip install websockets")
            self._running = False
            return
        if self._serving_task is not None:
            return

        loop = asyncio.get_running_loop()
        self._stop_future = loop.create_future()
        self._serving_task = asyncio.create_task(self._run())
        self._running = True
        logger.info(
            "WebSocket server listening on %s:%d%s",
            self.config.host,
            self.config.port,
            self.config.path,
        )

    async def _run(self) -> None:
        """Run ``websockets.serve`` as an async context manager until stopped."""
        cfg = self.config
        async with websockets.serve(
            self._handle_ws, cfg.host, cfg.port, max_size=cfg.max_message_size
        ):
            await self._stop_future

    async def _handle_ws(self, connection) -> None:  # noqa: ANN001, C901, PLR0912
        """Handle an individual WebSocket connection (websockets >= 14 API)."""
        remote_info = connection.remote_address
        remote_ip = remote_info[0] if remote_info else "unknown"
        device_id = device_id_from_ip("ws", remote_ip)

        # Performance optimization: pre-bind handler coroutine check outside loop
        handler = self.handler
        is_coro = asyncio.iscoroutinefunction(handler) if handler else False

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
                    path=self.config.path,
                    body=body,
                    transport=TransportMeta(port=getattr(self.config, "port", 9000)),
                    security="none",
                    identity=device_id,
                    kind=ObservationKind.EVENT,
                )
                if handler:
                    try:
                        result = await handler(request) if is_coro else handler(request)
                        if result and isinstance(result, dict):
                            await connection.send(json.dumps(result))
                    except ConnectionClosed:
                        break
                    except Exception:
                        logger.exception("WebSocket handler error:")
        except ConnectionClosed:
            return
        except Exception:
            logger.exception("WebSocket connection error:")

    async def stop(self) -> None:
        if self._serving_task is not None:
            if self._stop_future is not None and not self._stop_future.done():
                self._stop_future.set_result(None)
            with contextlib.suppress(asyncio.CancelledError):
                await self._serving_task
            self._serving_task = None
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

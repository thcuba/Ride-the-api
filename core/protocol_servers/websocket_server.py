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
from datetime import UTC, datetime
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
            self.config.host, self.config.port, self.config.path,
        )

    async def _run(self) -> None:
        """Run ``websockets.serve`` as an async context manager until stopped."""
        cfg = self.config
        async with websockets.serve(
            self._handle_ws, cfg.host, cfg.port, max_size=cfg.max_message_size
        ):
            await self._stop_future

    async def _handle_ws(self, connection) -> None:  # noqa: ANN001
        """Handle an individual WebSocket connection (websockets >= 14 API)."""
        remote_info = connection.remote_address
        remote_ip = remote_info[0] if remote_info else "unknown"
        device_id = device_id_from_ip("ws", remote_ip)

        try:
            async for message in connection:
                body = None
                try:
                    body = json.loads(message)
                except (json.JSONDecodeError, TypeError):
                    body = (
                        {"raw": message.hex()}
                        if isinstance(message, bytes)
                        else {"raw": str(message)}
                    )
                request = InterceptedRequest(
                    device_id=device_id,
                    timestamp=datetime.now(UTC).timestamp(),
                    protocol=ProtocolType.WEBSOCKET,
                    method="WS",
                    path=self.config.path,
                    body=body,
                    transport=TransportMeta(port=getattr(self.config, "port", 9000)),
                    security="none",
                    identity=device_id,
                    kind=ObservationKind.EVENT,
                )
                if self.handler:
                    try:
                        if asyncio.iscoroutinefunction(self.handler):
                            result = await self.handler(request)
                        else:
                            result = self.handler(request)
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

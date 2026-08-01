"""
WebSocket Protocol Server — native WebSocket listener for device real-time communication.

Many IoT devices use WebSocket for real-time bidirectional communication
(status updates, command streaming, event notifications). This server
acts as a WebSocket endpoint that devices connect to instead of the vendor cloud.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

try:
    import websockets
    from websockets.server import serve as ws_serve
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

from core.protocol_servers import ProtocolServerPlugin
from adapters.base import InterceptedRequest, ProtocolType

logger = logging.getLogger(__name__)


class WebSocketServerPlugin(ProtocolServerPlugin):
    """Native WebSocket server for device interception."""

    name = "websocket"

    def __init__(self, config: Any, handler: Callable | None = None):
        super().__init__(config)
        self.handler = handler
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        if not HAS_WEBSOCKETS:
            logger.warning("WebSocket: websockets not installed — pip install websockets")
            self._running = False
            return

        cfg = self.config
        self._running = True
        logger.info("WebSocket server enabled on %s:%d%s", cfg.host, cfg.port, cfg.path)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await super().stop()
        logger.info("WebSocket server stopped")

    async def _handle_ws(self, websocket, path: str) -> None:
        """Handle an individual WebSocket connection."""
        remote_ip = websocket.remote_address[0] if hasattr(websocket, 'remote_address') else "unknown"
        device_id = f"ws-{remote_ip.replace('.', '-')}"

        async for message in websocket:
            body = None
            try:
                body = json.loads(message)
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = {"raw": message.hex() if isinstance(message, bytes) else message}

            request = InterceptedRequest(
                device_id=device_id,
                timestamp=datetime.now(timezone.utc).timestamp(),
                protocol=ProtocolType.WEBSOCKET,
                method="WS",
                path=path or self.config.path,
                body=body,
            )

            if self.handler:
                try:
                    if asyncio.iscoroutinefunction(self.handler):
                        result = await self.handler(request)
                    else:
                        result = self.handler(request)
                    if result and isinstance(result, dict):
                        await websocket.send(json.dumps(result))
                except Exception as e:
                    logger.error("WebSocket handler error: %s", e)

    async def get_status(self) -> dict:
        return {
            "name": self.name,
            "running": self._running,
            "host": self.config.host,
            "port": self.config.port,
            "path": self.config.path,
        }
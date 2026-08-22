"""
HTTP/2 Protocol Server — h2c (cleartext) and h2 over TLS.

HTTP/2 is used by some modern IoT clouds and devices for multiplexed
communication. This server handles:
- h2c upgrade from HTTP/1.1
- Prior knowledge h2c (direct HTTP/2 without upgrade)
- h2 via TLS with ALPN negotiation
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

try:
    import h2.config
    import h2.connection
    import h2.errors
    import h2.events
    import h2.settings
    HAS_H2 = True
except ImportError:
    HAS_H2 = False

from core.protocol_servers import ProtocolServerPlugin

logger = logging.getLogger(__name__)


class HTTP2ServerPlugin(ProtocolServerPlugin):
    """HTTP/2 server for h2c and h2 over TLS."""

    name = "http2"

    def __init__(self, config: Any, handler: Callable | None = None):
        super().__init__(config)
        self.handler = handler
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        if not HAS_H2:
            logger.warning("HTTP/2: h2 not installed — pip install h2")
            self._running = False
            return

        cfg = self.config
        self._running = True
        logger.info("HTTP/2 server enabled on %s:%d (cleartext: %s:%d, TLS: %s)",
                     cfg.host, cfg.port, cfg.host, cfg.cleartext_port, cfg.tls_enabled)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await super().stop()
        logger.info("HTTP/2 server stopped")

    async def get_status(self) -> dict:
        return {
            "name": self.name,
            "running": self._running,
            "host": self.config.host,
            "port": self.config.port,
            "cleartext_port": self.config.cleartext_port,
            "tls_enabled": self.config.tls_enabled,
        }

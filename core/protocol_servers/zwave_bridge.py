"""
Z-Wave Bridge — integrates with Z-Wave JS UI.

Z-Wave JS UI provides MQTT and WebSocket interfaces for Z-Wave devices.
This plugin connects and converts Z-Wave events to the pipeline.
"""

from __future__ import annotations

import logging
from typing import Any

from core.protocol_servers import ProtocolServerPlugin

logger = logging.getLogger(__name__)


class ZWaveBridgePlugin(ProtocolServerPlugin):
    """Bridge to Z-Wave JS UI via MQTT or WebSocket."""

    name = "zwave_bridge"

    def __init__(self, config: Any, handler=None) -> None:  # noqa: ANN401
        super().__init__(config)
        self.handler = handler
        self._connected = False

    async def start(self) -> None:
        self._running = True
        logger.info(
            "Z-Wave bridge enabled (type=%s, host=%s)",
            self.config.connection_type,
            self.config.host,
        )

    async def stop(self) -> None:
        self._connected = False
        await super().stop()
        logger.info("Z-Wave bridge stopped")

    async def get_status(self) -> dict:
        return {
            "name": self.name,
            "running": self._running,
            "connected": self._connected,
            "connection_type": self.config.connection_type,
            "host": self.config.host,
        }

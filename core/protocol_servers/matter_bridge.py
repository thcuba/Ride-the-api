"""
Matter Bridge — integrates with Matter.js for Matter protocol devices.

Matter (formerly Project CHIP) is the new smart home standard based on IPv6.
Devices communicate via Interaction Model (clusters/attributes/commands).
This bridge connects to a Matter.js controller to translate Matter traffic.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from core.protocol_servers import ProtocolServerPlugin

logger = logging.getLogger(__name__)


class MatterBridgePlugin(ProtocolServerPlugin):
    """Bridge to Matter.js controller for Matter device interception."""

    name = "matter_bridge"

    def __init__(self, config: Any, handler=None):
        super().__init__(config)
        self.handler = handler
        self._connected = False

    async def start(self) -> None:
        self._running = True
        logger.info("Matter bridge enabled (port=%d, fabric=%d)",
                     self.config.controller_port, self.config.fabric_id)

    async def stop(self) -> None:
        self._connected = False
        await super().stop()
        logger.info("Matter bridge stopped")

    async def get_status(self) -> dict:
        return {
            "name": self.name,
            "running": self._running,
            "connected": self._connected,
            "controller_port": self.config.controller_port,
            "fabric_id": self.config.fabric_id,
        }
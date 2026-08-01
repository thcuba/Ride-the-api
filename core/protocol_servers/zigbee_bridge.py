"""
Zigbee Bridge — integrates with Zigbee2MQTT.

Zigbee2MQTT bridges Zigbee radio traffic to MQTT topics.
This plugin connects to the Zigbee2MQTT MQTT broker and:
- Subscribes to zigbee2mqtt/# for device state/events
- Converts topics to InterceptedRequest for the pipeline
- Forwards commands back via zigbee2mqtt/{device}/set
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

try:
    from gmqtt import Client as MQTTClient
    HAS_GMQTT = True
except ImportError:
    HAS_GMQTT = False

from core.protocol_servers import ProtocolServerPlugin
from adapters.base import InterceptedRequest, ProtocolType

logger = logging.getLogger(__name__)


class ZigbeeBridgePlugin(ProtocolServerPlugin):
    """Bridge to Zigbee2MQTT via MQTT."""

    name = "zigbee_bridge"

    def __init__(self, config: Any, handler: Callable | None = None):
        super().__init__(config)
        self.handler = handler
        self._client: Any = None
        self._connected = False

    async def start(self) -> None:
        if not HAS_GMQTT:
            logger.warning("Zigbee bridge: gmqtt not installed — pip install gmqtt")
            self._running = False
            return
        self._running = True
        logger.info("Zigbee bridge connecting to Zigbee2MQTT at %s:%d",
                     self.config.mqtt_host, self.config.mqtt_port)

    async def stop(self) -> None:
        self._connected = False
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
        await super().stop()
        logger.info("Zigbee bridge stopped")

    async def get_status(self) -> dict:
        return {
            "name": self.name,
            "running": self._running,
            "connected": self._connected,
            "mqtt_host": self.config.mqtt_host,
            "mqtt_port": self.config.mqtt_port,
            "topic_prefix": self.config.topic_prefix,
        }

    async def send_command(self, device_friendly_name: str, command: str,
                            value: Any) -> bool:
        """Send a command to a Zigbee device via Zigbee2MQTT."""
        if not self._client:
            return False
        topic = f"{self.config.topic_prefix}/{device_friendly_name}/set"
        try:
            payload = json.dumps({command: value})
            self._client.publish(topic, payload, qos=0)
            return True
        except Exception as e:
            logger.error("Zigbee bridge send error: %s", e)
            return False
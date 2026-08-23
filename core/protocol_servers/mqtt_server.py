"""
MQTT Protocol Server — local broker that intercepts device MQTT traffic.

Acts as a transparent MQTT broker: devices connect here instead of the vendor cloud.
Messages are converted to InterceptedRequest and passed to the pipeline.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from adapters.base import InterceptedRequest, ProtocolType
from core.protocol_servers import ProtocolServerPlugin

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.config import MQTTServerConfig

try:
    from gmqtt import Client as MQTTClient  # noqa: TC002
    from gmqtt.mqtt.constants import MQTTv5, MQTTv311  # noqa: F401

    HAS_GMQTT = True
except ImportError:
    HAS_GMQTT = False

logger = logging.getLogger(__name__)


class MQTTServerPlugin(ProtocolServerPlugin):
    """Local MQTT broker for device protocol interception."""

    name = "mqtt"

    def __init__(self, config: MQTTServerConfig, handler: Callable | None = None) -> None:
        super().__init__(config)
        self.handler = handler
        self._server: asyncio.AbstractServer | None = None
        self._clients: dict[str, MQTTClient] = {}
        self._client_subscriptions: dict[str, list[str]] = {}

    async def start(self) -> None:
        """Start the MQTT server."""
        if not HAS_GMQTT:
            logger.warning("MQTT: gmqtt not installed — install with: pip install gmqtt")
            return

        cfg = self.config
        self._running = True
        logger.info("MQTT server starting on %s:%d", cfg.host, cfg.port)

    async def stop(self) -> None:
        """Stop the MQTT server and disconnect all clients."""
        for cid, client in self._clients.items():
            with contextlib.suppress(Exception):
                await client.disconnect()
        self._clients.clear()
        self._client_subscriptions.clear()
        await super().stop()
        logger.info("MQTT server stopped")

    async def handle_message(
        self, client_id: str, topic: str, payload: bytes, qos: int, retain: bool
    ) -> dict | None:
        """Convert an intercepted MQTT message to a local response via the pipeline."""
        if not self.handler:
            return None

        body = None
        try:
            body = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {"raw": payload.hex()}

        request = InterceptedRequest(
            device_id=client_id,
            timestamp=datetime.now(UTC).timestamp(),
            protocol=ProtocolType.MQTT,
            topic=topic,
            qos=qos,
            retain=retain,
            body=body,
        )

        try:
            if asyncio.iscoroutinefunction(self.handler):
                result = await self.handler(request)
            else:
                result = self.handler(request)
        except Exception:
            logger.exception("MQTT handler error:")
            return None
        else:
            return result

    async def get_status(self) -> dict:
        return {
            "name": self.name,
            "running": self._running,
            "host": self.config.host,
            "port": self.config.port,
            "tls_enabled": self.config.tls_enabled,
            "clients": len(self._clients),
            "subscriptions": sum(len(s) for s in self._client_subscriptions.values()),
        }


class MQTTBridgeClient(ProtocolServerPlugin):
    """MQTT client that connects to an external broker (for Zigbee2MQTT bridging)."""

    name = "mqtt_bridge"

    def __init__(self, config: MQTTServerConfig, handler: Callable | None = None) -> None:
        super().__init__(config)
        self.handler = handler
        self._client: MQTTClient | None = None

    async def start(self) -> None:
        """Connect to external MQTT broker."""
        if not HAS_GMQTT:
            logger.warning("MQTT bridge: gmqtt not installed")
            return
        cfg = self.config
        self._running = True
        logger.info("MQTT bridge connecting to %s:%d", cfg.mqtt_host, cfg.mqtt_port)

    async def stop(self) -> None:
        if self._client:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
            self._client = None
        await super().stop()

    async def publish(self, topic: str, payload: dict | str | bytes, qos: int = 0) -> bool:
        """Publish a message to the external broker."""
        if not self._client:
            return False
        try:
            self._client.publish(
                topic, json.dumps(payload) if isinstance(payload, dict) else payload, qos
            )
        except Exception:
            logger.exception("MQTT bridge publish error:")
            return False
        else:
            return True

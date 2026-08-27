"""
Zigbee Bridge ? integrates with Zigbee2MQTT.

Zigbee2MQTT bridges Zigbee radio traffic to MQTT topics. This plugin connects
to the Zigbee2MQTT MQTT broker (via the validated ``paho-mqtt`` client) and:
- Subscribes to ``zigbee2mqtt/#`` for device state/events
- Converts topics to ``InterceptedRequest`` for the pipeline
- Forwards commands back via ``zigbee2mqtt/{device}/set``

If the external broker is unreachable it degrades gracefully and reports
``connected: False`` instead of faking a running bridge.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from adapters.base import InterceptedRequest, ProtocolType
from core.protocol_servers import ProtocolServerPlugin

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.config import ZigbeeBridgeConfig

try:
    import paho.mqtt.client as mqtt

    HAS_PAHO = True
except ImportError:  # pragma: no cover - exercised at import time only
    HAS_PAHO = False


logger = logging.getLogger(__name__)


class ZigbeeBridgePlugin(ProtocolServerPlugin):
    """Bridge to Zigbee2MQTT via MQTT."""

    name = "zigbee_bridge"

    def __init__(self, config: ZigbeeBridgeConfig, handler: Callable | None = None) -> None:
        super().__init__(config)
        self.handler = handler
        self._client: Any = None
        self._thread: threading.Thread | None = None
        self._connected = False
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        if not HAS_PAHO:
            logger.warning("Zigbee bridge: paho-mqtt not installed")
            self._running = False
            return
        if self._thread is not None:
            return
        cfg = self.config
        self._loop = asyncio.get_running_loop()
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._prefix = cfg.topic_prefix
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.connect_async(cfg.mqtt_host, cfg.mqtt_port)
        self._thread = threading.Thread(target=client.loop_forever, name="zigbee-bridge", daemon=True)
        self._client = client
        self._thread.start()
        self._running = True
        logger.info(
            "Zigbee bridge connecting to Zigbee2MQTT at %s:%d",
            cfg.mqtt_host, cfg.mqtt_port,
        )

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:  # noqa: ANN001
        self._connected = True
        client.subscribe(f"{self._prefix}/#")
        logger.info("Zigbee bridge connected")

    def _on_message(self, client, userdata, msg) -> None:  # noqa: ANN001
        if self._loop is None or self._loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._route_to_pipeline(msg.topic, msg.payload or b""), self._loop)

    async def _route_to_pipeline(self, topic: str, payload: bytes) -> None:
        try:
            body = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            body = {"raw": payload.hex()}
        device = topic.split('/')[1] if len(topic.split('/')) > 1 else 'dev'
        device = device or 'dev'
        request = InterceptedRequest(
            device_id=f"zigbee-{device}",
            timestamp=datetime.now(UTC).timestamp(),
            protocol=ProtocolType.ZIGBEE,
            topic=topic,
            body=body,
        )
        if self.handler:
            try:
                if asyncio.iscoroutinefunction(self.handler):
                    await self.handler(request)
                else:
                    self.handler(request)
            except Exception:
                logger.exception("Zigbee handler error:")

    async def stop(self) -> None:
        self._connected = False
        if self._client:
            with contextlib.suppress(Exception):
                self._client.disconnect()
            self._client = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
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

    async def send_command(self, device_friendly_name: str, command: str, value: Any) -> bool:
        """Send a command to a Zigbee device via Zigbee2MQTT."""
        if not self._client:
            return False
        topic = f"{self.config.topic_prefix}/{device_friendly_name}/set"
        try:
            self._client.publish(topic, json.dumps({command: value}), qos=0)
        except Exception:
            logger.exception("Zigbee bridge send error:")
            return False
        return True

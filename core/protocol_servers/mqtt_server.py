"""
MQTT Protocol Server ? local broker that intercepts device MQTT traffic.

Acts as a transparent MQTT broker: devices connect here instead of the vendor
cloud. Messages are converted to InterceptedRequest and passed to the pipeline.

The broker itself is `amqtt` (a validated, pure-Python async MQTT broker); a
paho client loop subscribes to the configured topic filters and forwards each
captured PUBLISH to the pipeline handler.
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
from core.pattern_db.schemas import ObservationKind, TransportMeta
from core.protocol_servers import ProtocolServerPlugin

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.config import MQTTServerConfig

try:
    from amqtt.broker import Broker as AMQTTBroker

    HAS_AMQTT = True
except ImportError:  # pragma: no cover - exercised at import time only
    HAS_AMQTT = False

try:
    import paho.mqtt.client as mqtt

    HAS_PAHO = True
except ImportError:  # pragma: no cover - exercised at import time only
    HAS_PAHO = False


logger = logging.getLogger(__name__)


class MQTTServerPlugin(ProtocolServerPlugin):
    """Local MQTT broker for device protocol interception."""

    name = "mqtt"

    def __init__(self, config: MQTTServerConfig, handler: Callable | None = None) -> None:
        super().__init__(config)
        self.handler = handler
        self._broker: AMQTTBroker | None = None
        self._forward_thread: threading.Thread | None = None
        self._forward_client = None
        self._stop_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        """Start the MQTT broker and the capture-forwarding loop."""
        if not HAS_AMQTT:
            logger.warning("MQTT: amqtt not installed ? install with: pip install amqtt")
            return
        if self._broker is not None:
            return

        cfg = self.config
        self._loop = asyncio.get_running_loop()
        broker_config = {
            "listeners": {
                "default": {
                    "type": "tcp",
                    "bind": f"{cfg.host}:{cfg.port}",
                    "max_connections": 512,
                }
            },
            "sys_interval": 0,
            "auth": {"allow-anonymous": True},
            "topic-check": {"enabled": False},
        }
        self._broker = AMQTTBroker(broker_config)
        await self._broker.start()
        self._running = True
        logger.info("MQTT broker listening on %s:%d", cfg.host, cfg.port)

        # Forward captured messages to the pipeline (best-effort; devices work
        # even if no handler is wired).
        if HAS_PAHO and self.handler:
            self._start_forwarder(cfg)

    def _start_forwarder(self, cfg) -> None:  # noqa: ANN001
        filters = list(getattr(cfg, "topic_filters", None) or ["#"])
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        self._forward_client = client
        self._stop_event.clear()
        host = cfg.host if cfg.host not in ("0.0.0.0", "::") else "127.0.0.1"
        self._filters = filters
        self._forward_thread = threading.Thread(
            target=lambda: client.connect_async(host, cfg.port)
            or client.loop_forever(),
            name="mqtt-forwarder",
            daemon=True,
        )
        self._forward_thread.start()

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:  # noqa: ANN001
        for topic in self._filters:
            client.subscribe(topic)

    def _on_message(self, client, userdata, msg) -> None:  # noqa: ANN001
        if self._loop is None or self._loop.is_closed():
            return
        # Single paho forwarder client; the publishing device's client_id is
        # not exposed on subscribe. Derive a stable per-device id from the
        # topic's first segment so per-device learning/matching doesn't
        # collapse to one "mqtt-forwarder" device for every message.
        device_id = self._device_id_from_topic(msg.topic)
        asyncio.run_coroutine_threadsafe(
            self.handle_message(
                client_id=device_id,
                topic=msg.topic,
                payload=msg.payload or b"",
                qos=msg.qos,
                retain=msg.retain,
            ),
            self._loop,
        )

    @staticmethod
    def _device_id_from_topic(topic: str) -> str:
        """Derive a device id from an MQTT topic (first non-empty segment)."""
        parts = [p for p in topic.split("/") if p]
        return ("mqtt-" + parts[0]) if parts else "mqtt-unknown"

    async def stop(self) -> None:
        """Stop the MQTT broker and disconnect all clients."""
        self._stop_event.set()
        if self._forward_client is not None:
            with contextlib.suppress(Exception):
                self._forward_client.disconnect()
            self._forward_client = None
        if self._forward_thread is not None:
            self._forward_thread.join(timeout=2)
            self._forward_thread = None
        if self._broker is not None:
            with contextlib.suppress(Exception):
                await self._broker.shutdown()
            self._broker = None
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
            transport=TransportMeta(
                port=getattr(self.config, "port", 1883),
                tls=getattr(self.config, "tls_enabled", False),
                topic=topic,
                qos=qos,
                retain=retain,
            ),
            security="tls" if getattr(self.config, "tls_enabled", False) else "none",
            identity=client_id,
            kind=ObservationKind.PUBLISH,
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
        }


class MQTTBridgeClient(ProtocolServerPlugin):
    """MQTT client that connects to an external broker (for Zigbee2MQTT bridging)."""

    name = "mqtt_bridge"

    def __init__(self, config: MQTTServerConfig, handler: Callable | None = None) -> None:
        super().__init__(config)
        self.handler = handler
        self._client = None

    async def start(self) -> None:
        """Connect to external MQTT broker."""
        if not HAS_PAHO:
            logger.warning("MQTT bridge: paho-mqtt not installed")
            return
        cfg = self.config
        host = getattr(cfg, "host", "localhost")
        port = int(getattr(cfg, "port", 1883))
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.connect_async(host, port)
        self._loop = asyncio.get_running_loop()
        self._bridge_thread = threading.Thread(
            target=client.loop_forever, name="mqtt-bridge", daemon=True
        )
        self._client = client
        self._bridge_thread.start()
        self._running = True
        logger.info("MQTT bridge connecting to %s:%d", host, port)

    async def stop(self) -> None:
        if self._client:
            with contextlib.suppress(Exception):
                self._client.disconnect()
            self._client = None
        await super().stop()

    async def publish(self, topic: str, payload: dict | str | bytes, qos: int = 0) -> bool:
        """Publish a message to the external broker."""
        if not self._client:
            return False
        try:
            data = json.dumps(payload) if isinstance(payload, dict) else payload
            self._client.publish(topic, data, qos)
        except Exception:
            logger.exception("MQTT bridge publish error:")
            return False
        else:
            return True

"""
Z-Wave Bridge ? integrates with Z-Wave JS UI.

Z-Wave JS UI provides MQTT and WebSocket interfaces for Z-Wave devices. This
plugin connects via the MQTT interface (``paho-mqtt``) or a WebSocket interface
(``websockets``), subscribes to device events, and forwards them to the
pipeline. If the external endpoint is unreachable it reports ``connected:
False`` rather than faking an active bridge.
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

try:
    import paho.mqtt.client as mqtt

    HAS_PAHO = True
except ImportError:  # pragma: no cover - exercised at import time only
    HAS_PAHO = False

try:
    import websockets  # noqa: F401

    HAS_WEBSOCKETS = True
except ImportError:  # pragma: no cover - exercised at import time only
    HAS_WEBSOCKETS = False


logger = logging.getLogger(__name__)


class ZWaveBridgePlugin(ProtocolServerPlugin):
    """Bridge to Z-Wave JS UI via MQTT or WebSocket."""

    name = "zwave_bridge"

    def __init__(self, config: Any, handler=None) -> None:  # noqa: ANN001, ANN401
        super().__init__(config)
        self.handler = handler
        self._client: Any = None
        self._thread: threading.Thread | None = None
        self._ws_task: asyncio.Task | None = None
        self._connected = False
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        conn_type = getattr(self.config, "connection_type", "mqtt")
        if conn_type == "ws":
            await self._start_ws()
        else:
            self._start_mqtt()
        self._running = True
        logger.info(
            "Z-Wave bridge enabled (type=%s, host=%s)",
            self.config.connection_type, self.config.host,
        )

    async def _start_ws(self) -> None:
        if not HAS_WEBSOCKETS:
            logger.warning("Z-Wave ws bridge: websockets not installed")
            return
        host = self.config.host
        port = getattr(self.config, "ws_port", 3000)
        url = f"ws://{host}:{port}"
        self._ws_task = asyncio.create_task(self._ws_loop(url))

    async def _ws_loop(self, url: str) -> None:
        with contextlib.suppress(OSError, asyncio.CancelledError):
            async for ws in websockets.connect(url):
                self._connected = True
                logger.info("Z-Wave ws bridge connected to %s", url)
                try:
                    async for raw in ws:
                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError:
                            payload = {"raw": str(raw)}
                        await self._route_to_pipeline("zwave", payload)
                except websockets.ConnectionClosed:
                    self._connected = False

    def _start_mqtt(self) -> None:
        if not HAS_PAHO:
            logger.warning("Z-Wave mqtt bridge: paho-mqtt not installed")
            return
        usr = getattr(self.config, "mqtt_user", "") or None
        pwd = getattr(self.config, "mqtt_pass", "") or None
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if usr is not None:
            client.username_pw_set(usr, pwd)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.connect_async(self.config.host, self.config.port)
        self._client = client
        self._thread = threading.Thread(target=client.loop_forever, name="zwave-bridge", daemon=True)
        self._thread.start()

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:  # noqa: ANN001
        self._connected = True
        client.subscribe("#")

    def _on_message(self, client, userdata, msg) -> None:  # noqa: ANN001
        if self._loop is None or self._loop.is_closed():
            return
        try:
            body = json.loads((msg.payload or b"").decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            body = {"raw": (msg.payload or b"").hex()}
        asyncio.run_coroutine_threadsafe(
            self._route_to_pipeline(msg.topic, body), self._loop
        )

    async def _route_to_pipeline(self, topic: str, body) -> None:  # noqa: ANN001
        device = topic.split('/')[1] if len(topic.split('/')) > 1 else 'dev'
        device = device or 'dev'
        request = InterceptedRequest(
            device_id=f"zwave-{device}",
            timestamp=datetime.now(UTC).timestamp(),
            protocol=ProtocolType.ZWAVE,
            topic=topic,
            body=body,
            transport=TransportMeta(
                topic=topic,
                port=getattr(self.config, "mqtt_port", 1883),
            ),
            security="none",
            identity=f"zwave-{device}",
            kind=ObservationKind.PUBLISH,
        )
        if self.handler:
            try:
                if asyncio.iscoroutinefunction(self.handler):
                    await self.handler(request)
                else:
                    self.handler(request)
            except Exception:
                logger.exception("Z-Wave handler error:")

    async def stop(self) -> None:
        self._connected = False
        if self._ws_task is not None:
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ws_task
            self._ws_task = None
        if self._client:
            with contextlib.suppress(Exception):
                self._client.disconnect()
            self._client = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
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

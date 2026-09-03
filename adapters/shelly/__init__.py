"""
Shelly Protocol Adapter — for Shelly smart home devices (Gen1, Gen2, Gen3).

Shelly devices communicate via:
- **Gen1** (Shelly 1, 1PM, 2.5, etc.): HTTP REST + CoAP for real-time status
- **Gen2/Gen3** (Shelly Plus, Pro): HTTP REST + MQTT + WebSocket
- All: local HTTP API at shelly-xxx.local, cloud via Shell

This adapter translates Shelly API calls to standardized CommandTypes.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from adapters.base import (
    Command,
    CommandResult,
    CommandType,
    DeviceCapability,
    DeviceInfo,
    DeviceState,
    InterceptedRequest,
    ProtocolAdapter,
    ProtocolType,
)
from core.cloud_forward import forward_intercepted

logger = logging.getLogger(__name__)

# Shelly Gen1 status key mapping
SHELLY_GEN1_KEYS = {
    "power": "power",
    "temperature": "temperature",
    "overtemperature": "overtemp",
    "humidity": "humidity",
    "lux": "illuminance",
}

# Shelly Gen2 RPC methods
SHELLY_GEN2_RPC = {
    "Shelly.GetStatus": CommandType.GET_STATE,
    "Shelly.GetConfig": CommandType.GET_STATE,
    "Switch.Set": CommandType.TURN_ON,
    "Switch.Toggle": CommandType.UNKNOWN,
    "Cover.SetState": CommandType.UNKNOWN,
    "Light.Set": CommandType.TURN_ON,
    "Temperature.GetStatus": CommandType.GET_STATE,
    "Humidity.GetStatus": CommandType.GET_STATE,
}


class ShellyProtocolAdapter(ProtocolAdapter):
    """Adapter for Shelly smart home devices."""

    VENDOR_CODE = "shelly"
    VENDOR_HOSTNAMES = ["shelly-*.local", "shelly-*"]

    def __init__(self, vendor: str, config: dict[str, Any]) -> None:
        super().__init__(vendor, config)
        self._devices: dict[str, dict] = {}

    @property
    def supported_protocols(self) -> list[ProtocolType]:
        return [
            ProtocolType.HTTP,
            ProtocolType.HTTPS,
            ProtocolType.COAP,
            ProtocolType.MQTT,
            ProtocolType.WEBSOCKET,
        ]

    @property
    def vendor_hostnames(self) -> list[str]:
        return self.VENDOR_HOSTNAMES

    async def parse_request(self, request: InterceptedRequest) -> InterceptedRequest:
        """Parse Shelly HTTP/CoAP/MQTT/WS request."""
        if request.protocol in (ProtocolType.HTTP, ProtocolType.HTTPS):
            return await self._parse_http(request)
        if request.protocol == ProtocolType.COAP:
            return await self._parse_coap(request)
        if request.protocol in (ProtocolType.MQTT, ProtocolType.WEBSOCKET):
            return await self._parse_mqtt_ws(request)
        return request

    async def _parse_http(self, request: InterceptedRequest) -> InterceptedRequest:
        """Parse Shelly HTTP REST API call."""
        path = request.path or "/"
        # Gen1 status endpoint
        if "/status" in path:
            request.parsed_intent = CommandType.GET_STATE
            return request

        # Gen2 RPC endpoint
        if "/rpc/" in path or "/rpc" in path:
            rpc_method = path.split("/rpc/")[-1].split("?")[0]
            request.parsed_intent = SHELLY_GEN2_RPC.get(rpc_method, CommandType.UNKNOWN)
            return request

        # Gen1 relay/switch control: /relay/0?turn=on
        path_lower = path.lower()
        if "turn=on" in path_lower:
            request.parsed_intent = CommandType.TURN_ON
            return request
        if "turn=off" in path_lower:
            request.parsed_intent = CommandType.TURN_OFF
            return request

        # Settings endpoints
        if "/settings" in path:
            request.parsed_intent = CommandType.GET_STATE
            return request

        return request

    async def _parse_coap(self, request: InterceptedRequest) -> InterceptedRequest:
        """Parse Shelly Gen1 CoAP status update."""
        # Shelly Gen1 devices broadcast CoAP on /shelly/status
        if request.path and "shelly" in request.path:
            request.parsed_intent = CommandType.GET_STATE
        return request

    async def _parse_mqtt_ws(self, request: InterceptedRequest) -> InterceptedRequest:
        """Parse Shelly Gen2/Gen3 MQTT or WebSocket message."""
        topic = request.topic or request.path or ""
        body = request.body or {}

        # Status report topics: shelly/<device>/status/...
        if "/status/" in topic or topic.endswith("/status"):
            request.parsed_intent = CommandType.GET_STATE
            return request

        # RPC command response
        if "src" in body and body.get("src") == "shelly":
            method = body.get("method", "")
            request.parsed_intent = SHELLY_GEN2_RPC.get(method, CommandType.UNKNOWN)
            return request

        return request

    async def handle_request(self, request: InterceptedRequest) -> CommandResult:
        """Handle Shelly request locally."""
        state = await self.get_device_state(request.device_id)
        if state and request.parsed_intent == CommandType.GET_STATE:
            return CommandResult(
                success=True,
                response={
                    "power": state.power_watts,
                    "temperature": state.temp_actual,
                    "humidity": state.humidity,
                    "mode": state.mode,
                },
            )
        return await self.forward_to_cloud(request)

    async def forward_to_cloud(self, request: InterceptedRequest) -> CommandResult:
        """Forward to the real Shelly cloud, resolving the host loop-free.

        The target hostname is taken (in order) from the adapter config
        (``cloud.hostname``), from the request's ``Host`` header, or falls back
        to the vendor default ``https://shelly-api-eu.shelly.cloud``. The
        actual location is resolved via upstream DNS (bypassing the local DNS
        server) so forwarding never re-enters the proxy.
        """
        hostname = (self.config.get("cloud") or {}).get("hostname") or self._host_from_request(
            request
        )
        port = int((self.config.get("cloud") or {}).get("port", 443))
        use_tls = bool((self.config.get("cloud") or {}).get("tls", True))
        result = await forward_intercepted(
            request,
            hostname=hostname,
            port=port,
            use_tls=use_tls,
        )
        if result.error:
            logger.warning("Shelly cloud forward degraded: %s", result.error)
        return result

    @staticmethod
    def _host_from_request(request: InterceptedRequest) -> str | None:
        host = (request.headers or {}).get("Host")
        if host:
            # Strip an explicit :port suffix if present.
            return host.split(":", 1)[0]
        return None

    async def build_response(self, request: InterceptedRequest, result: CommandResult) -> dict:  # noqa: ARG002
        if result.success and result.response:
            return result.response
        return {"success": False, "error": result.error or "unknown"}

    async def get_device_info(self, device_id: str) -> DeviceInfo | None:
        info = self._devices.get(device_id)
        if info:
            return DeviceInfo(
                device_id=device_id,
                vendor="shelly",
                device_type="smart_home",
                model=info.get("model"),
                firmware_version=info.get("fw"),
                capabilities=[
                    DeviceCapability.POWER_MONITORING,
                    DeviceCapability.INDOOR_TEMP_SENSOR,
                ],
            )
        return None

    async def get_device_state(self, device_id: str) -> DeviceState | None:
        info = self._devices.get(device_id)
        if not info:
            return None
        return DeviceState(
            device_id=device_id,
            timestamp=datetime.now(UTC),
            power_watts=info.get("power"),
            temp_actual=info.get("temperature"),
            humidity=info.get("humidity"),
            source="device",
            quality="good",
        )

    async def send_command(self, device_id: str, command: Command) -> CommandResult:  # noqa: ARG002
        return CommandResult(success=False, error="Send not implemented")

    async def on_device_connect(self, device_id: str, initial_data: dict) -> None:
        self._devices[device_id] = initial_data
        logger.info("Shelly device connected: %s", device_id)

    async def on_device_disconnect(self, device_id: str) -> None:
        self._devices.pop(device_id, None)
        logger.info("Shelly device disconnected: %s", device_id)

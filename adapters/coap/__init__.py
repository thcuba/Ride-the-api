"""
CoAP Protocol Adapter Example — for CoAP-enabled IoT devices (constrained sensors/actuators).

CoAP (Constrained Application Protocol) is a REST-like protocol over UDP for IoT devices.
This adapter translates CoAP resource interactions to standardized CommandTypes.

Typical CoAP resources:
  - /s/temp      → temperature sensor
  - /s/humidity  → humidity sensor
  - /s/light     → light sensor
  - /a/relay/0   → relay actuator
  - /a/led       → LED actuator
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

logger = logging.getLogger(__name__)

# Standard CoAP resource-to-command mapping (read-only sensors, actuators,
# and dimmable devices below).
COAP_RESOURCE_MAP: dict[str, CommandType] = {
    # Sensors (read-only)  # noqa: ERA001
    "temp": CommandType.GET_STATE,
    "temperature": CommandType.GET_STATE,
    "humidity": CommandType.GET_STATE,
    "light": CommandType.GET_STATE,
    "pressure": CommandType.GET_STATE,
    "co2": CommandType.GET_STATE,
    "pir": CommandType.GET_STATE,  # motion sensor
    # Actuators
    "relay": CommandType.TURN_ON,
    "switch": CommandType.TURN_ON,
    "led": CommandType.TURN_ON,
    "valve": CommandType.UNKNOWN,
    # System
    "firmware": CommandType.FIRMWARE_CHECK,
    "config": CommandType.UNKNOWN,
}


class CoAPProtocolAdapter(ProtocolAdapter):
    """Example adapter for CoAP-based IoT devices."""

    VENDOR_CODE = "coap_example"
    VENDOR_HOSTNAMES: list[str] = []

    def __init__(self, vendor: str, config: dict[str, Any]) -> None:
        super().__init__(vendor, config)
        self._devices: dict[str, dict] = {}

    @property
    def supported_protocols(self) -> list[ProtocolType]:
        return [ProtocolType.COAP]

    @property
    def vendor_hostnames(self) -> list[str]:
        return self.VENDOR_HOSTNAMES

    async def parse_request(self, request: InterceptedRequest) -> InterceptedRequest:
        """Parse a CoAP request and extract intent."""
        path = request.path or "/"
        method = (request.method or "GET").upper()

        # Strip leading slashes and split
        parts = [p for p in path.strip("/").split("/") if p]

        # Determine intent from resource path
        for part in parts:
            if part in COAP_RESOURCE_MAP:
                intent = COAP_RESOURCE_MAP[part]
                # For write operations (PUT/POST) on actuator resources
                if (
                    method in ("PUT", "POST")
                    and intent == CommandType.GET_STATE
                    and part in ("relay", "switch", "led")
                ):
                    body = request.body or {}
                    val = body.get("value", body.get("state", ""))
                    if val in ("on", 1, True, "1"):
                        intent = CommandType.TURN_ON
                    elif val in ("off", 0, False, "0"):
                        intent = CommandType.TURN_OFF
                request.parsed_intent = intent
                request.parsed_params = {
                    "resource": part,
                    "path": path,
                    "method": method,
                }
                return request

        return request

    async def handle_request(self, request: InterceptedRequest) -> CommandResult:
        """Handle CoAP request locally."""
        if request.parsed_intent == CommandType.GET_STATE:
            state = await self.get_device_state(request.device_id)
            if state:
                return CommandResult(
                    success=True,
                    response={
                        "temperature": state.temp_actual,
                        "humidity": state.humidity,
                        "power": state.power_watts,
                    },
                )
        return await self.forward_to_cloud(request)

    async def forward_to_cloud(self, _request: InterceptedRequest) -> CommandResult:
        return CommandResult(success=False, error="Cloud forward not implemented", forwarded=True)

    async def build_response(self, _request: InterceptedRequest, result: CommandResult) -> dict:
        if result.success and result.response:
            return result.response
        return {"error": result.error or "unknown"}

    async def get_device_info(self, device_id: str) -> DeviceInfo | None:
        info = self._devices.get(device_id)
        if info:
            return DeviceInfo(
                device_id=device_id,
                vendor="coap_example",
                device_type="sensor",
                model=info.get("model"),
                firmware_version=info.get("fw"),
                capabilities=[
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
            temp_actual=info.get("temperature"),
            humidity=info.get("humidity"),
            source="device",
            quality="good",
        )

    async def send_command(self, _device_id: str, _command: Command) -> CommandResult:
        return CommandResult(success=False, error="Send not implemented")

    async def on_device_connect(self, device_id: str, initial_data: dict) -> None:
        self._devices[device_id] = initial_data
        logger.info("CoAP device connected: %s", device_id)

    async def on_device_disconnect(self, device_id: str) -> None:
        self._devices.pop(device_id, None)
        logger.info("CoAP device disconnected: %s", device_id)

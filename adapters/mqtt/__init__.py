"""
MQTT Protocol Adapter — generic, vendor-neutral MQTT message handling.

MQTT is a lightweight pub/sub messaging protocol widely used in IoT.
This adapter translates standard MQTT topics and payloads into
standardized CommandTypes without vendor-specific logic.

Standard topic patterns recognized:
  - {device_id}/status/{sensor}  →  GET_STATE
  - {device_id}/set/{property}   →  SET_* commands
  - {device_id}/cmd/{action}     →  TURN_* / REBOOT / etc.
  - {device_id}/telemetry        →  GET_STATE
  - {device_id}/will             →  UNKNOWN (last-will)
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

# Topic keyword → CommandType mapping (case-insensitive)
TOPIC_INTENT_MAP: dict[str, CommandType] = {
    "status": CommandType.GET_STATE,
    "state": CommandType.GET_STATE,
    "telemetry": CommandType.GET_STATE,
    "health": CommandType.GET_STATE,
    "set": CommandType.UNKNOWN,  # resolved via payload
    "cmd": CommandType.UNKNOWN,  # resolved via payload
    "command": CommandType.UNKNOWN,  # resolved via payload
    "turn_on": CommandType.TURN_ON,
    "turn_off": CommandType.TURN_OFF,
    "on": CommandType.TURN_ON,
    "off": CommandType.TURN_OFF,
    "reboot": CommandType.REBOOT,
    "reset": CommandType.FACTORY_RESET,
    "firmware": CommandType.FIRMWARE_CHECK,
    "ota": CommandType.FIRMWARE_UPDATE,
    "update": CommandType.FIRMWARE_UPDATE,
    "config": CommandType.GET_STATE,
    "mode": CommandType.SET_MODE,
    "temperature": CommandType.SET_TEMPERATURE,
    "power": CommandType.GET_STATE,
    "energy": CommandType.GET_STATE,
    "will": CommandType.UNKNOWN,
}

# Payload keywords for finer-grained intent resolution
PAYLOAD_INTENT_MAP: dict[str, CommandType] = {
    "on": CommandType.TURN_ON,
    "off": CommandType.TURN_OFF,
    "true": CommandType.TURN_ON,
    "false": CommandType.TURN_OFF,
    "reboot": CommandType.REBOOT,
    "reset": CommandType.FACTORY_RESET,
    "cool": CommandType.SET_MODE,
    "heat": CommandType.SET_MODE,
    "fan": CommandType.SET_MODE,
    "auto": CommandType.SET_MODE,
}


class MQTTProtocolAdapter(ProtocolAdapter):
    """Generic MQTT protocol adapter — no vendor-specific logic."""

    VENDOR_CODE = "mqtt"
    VENDOR_HOSTNAMES: list[str] = []

    def __init__(self, vendor: str, config: dict[str, Any]) -> None:
        super().__init__(vendor, config)
        self._devices: dict[str, dict] = {}

    @property
    def supported_protocols(self) -> list[ProtocolType]:
        return [ProtocolType.MQTT, ProtocolType.MQTTS]

    @property
    def vendor_hostnames(self) -> list[str]:
        return self.VENDOR_HOSTNAMES

    async def parse_request(self, request: InterceptedRequest) -> InterceptedRequest:
        """Parse an MQTT message from its topic + payload."""
        topic = (request.topic or request.path or "").strip("/")
        body = request.body or {}

        intent = self._resolve_intent(topic, body)
        request.parsed_intent = intent
        request.parsed_params = {
            "topic": topic,
            "qos": request.qos,
            "retain": request.retain,
        }
        return request

    def _resolve_intent(self, topic: str, body: dict) -> CommandType:
        """Walk the topic path and payload to determine the intent."""
        parts = [p for p in topic.split("/") if p]
        if not parts:
            return CommandType.UNKNOWN

        # Check each topic part for a known keyword
        for part in reversed(parts):
            key = part.lower()
            if key in TOPIC_INTENT_MAP:
                intent = TOPIC_INTENT_MAP[key]
                if intent != CommandType.UNKNOWN:
                    return intent

        # Check topic-level patterns for set/cmd/command
        for i, part in enumerate(parts):
            key = part.lower()
            if key in ("set", "cmd", "command"):
                # Look at the next segment for the action
                if i + 1 < len(parts):
                    action_key = parts[i + 1].lower()
                    if action_key in TOPIC_INTENT_MAP:
                        resolved = TOPIC_INTENT_MAP[action_key]
                        if resolved != CommandType.UNKNOWN:
                            return resolved
                # Fall back to checking payload
                return self._payload_intent(body)

        # Final fallback: check payload
        return self._payload_intent(body)

    def _payload_intent(self, body: dict) -> CommandType:
        """Determine intent from payload content."""
        if not body:
            return CommandType.UNKNOWN

        # String payloads (coerced via body when parsed as JSON)
        for key in ("action", "command", "cmd", "value", "state"):
            text = body.get(key)
            if isinstance(text, str) and text.lower() in PAYLOAD_INTENT_MAP:
                return PAYLOAD_INTENT_MAP[text.lower()]

        # Boolean payloads
        if body.get("on") is True:
            return CommandType.TURN_ON
        if body.get("on") is False:
            return CommandType.TURN_OFF

        # Numeric temperature in payload
        if "temperature" in body or "temp" in body:
            return CommandType.SET_TEMPERATURE

        return CommandType.UNKNOWN

    async def handle_request(self, request: InterceptedRequest) -> CommandResult:
        """Handle an MQTT request locally."""
        if request.parsed_intent in (CommandType.GET_STATE, CommandType.GET_SCHEDULE):
            state = await self.get_device_state(request.device_id)
            if state:
                return CommandResult(
                    success=True,
                    response={
                        "temperature_actual": state.temp_actual,
                        "temperature_setpoint": state.temp_target,
                        "mode": state.mode,
                        "power_watts": state.power_watts,
                        "humidity": state.humidity,
                        "on_off": state.on_off,
                    },
                )
        return await self.forward_to_cloud(request)

    async def forward_to_cloud(self, request: InterceptedRequest) -> CommandResult:  # noqa: ARG002
        return CommandResult(success=False, error="Cloud forward not implemented", forwarded=False)

    async def build_response(self, request: InterceptedRequest, result: CommandResult) -> dict:  # noqa: ARG002
        if result.success and result.response:
            return result.response
        return {"error": result.error or "unknown"}

    async def get_device_info(self, device_id: str) -> DeviceInfo | None:
        info = self._devices.get(device_id)
        if info:
            return DeviceInfo(
                device_id=device_id,
                vendor="mqtt",
                device_type="generic_mqtt",
                model=info.get("model"),
                firmware_version=info.get("fw"),
                capabilities=[
                    DeviceCapability.POWER_MONITORING,
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
            temp_target=info.get("temperature_setpoint"),
            temp_actual=info.get("temperature_actual"),
            humidity=info.get("humidity"),
            power_watts=info.get("power_consumption"),
            mode=info.get("mode"),
            on_off=info.get("on_off"),
            source="device",
            quality="good",
        )

    async def send_command(self, device_id: str, command: Command) -> CommandResult:  # noqa: ARG002
        return CommandResult(success=False, error="Send not implemented")

    async def on_device_connect(self, device_id: str, initial_data: dict) -> None:
        self._devices[device_id] = initial_data
        logger.info("MQTT device connected: %s", device_id)

    async def on_device_disconnect(self, device_id: str) -> None:
        self._devices.pop(device_id, None)
        logger.info("MQTT device disconnected: %s", device_id)

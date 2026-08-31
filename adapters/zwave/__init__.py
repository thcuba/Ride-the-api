"""
Z-Wave Protocol Adapter — generic, vendor-neutral Z-Wave message handling.

Z-Wave is a low-power wireless mesh protocol (ITU-T G.9959) for smart home
devices. This adapter translates Z-Wave Command Classes into standardized
CommandTypes.

Standard command classes handled:
  - SWITCH_BINARY (0x25): on/off control → TURN_ON / TURN_OFF
  - SWITCH_MULTILEVEL (0x26): dimmer/level control → SET_MODE
  - THERMOSTAT_SETPOINT (0x43): temperature target → SET_TEMPERATURE
  - THERMOSTAT_MODE (0x40): HVAC mode → SET_MODE
  - SENSOR_MULTILEVEL (0x31): temperature/humidity/lumin → GET_STATE
  - METER (0x32): power/energy → GET_STATE
  - BATTERY (0x80): battery status → GET_STATE
  - ASSOCIATION (0x85): group management
  - NOTIFICATION (0x71): sensor alerts (smoke, motion, etc.)
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

# Z-Wave Command Class IDs
CC_SWITCH_BINARY = 0x25
CC_SWITCH_MULTILEVEL = 0x26
CC_SWITCH_ALL = 0x27
CC_THERMOSTAT_SETPOINT = 0x43
CC_THERMOSTAT_MODE = 0x40
CC_THERMOSTAT_FAN_MODE = 0x44
CC_SENSOR_MULTILEVEL = 0x31
CC_METER = 0x32
CC_BATTERY = 0x80
CC_NOTIFICATION = 0x71
CC_ASSOCIATION = 0x85
CC_ASSOCIATION_GRP_INFO = 0x59
CC_CONFIGURATION = 0x70
CC_PROTECTION = 0x75
CC_VERSION = 0x86
CC_MANUFACTURER_SPECIFIC = 0x72
CC_POWERLEVEL = 0x73
CC_WAKE_UP = 0x84
CC_INDICATOR = 0x87
CC_MULTI_CHANNEL = 0x60
CC_MULTI_CHANNEL_ASSOC = 0x8E

# Command IDs (common across classes)
CMD_GET = 0x02
CMD_REPORT = 0x03
CMD_SET = 0x01
CMD_SWITCH_BINARY_GET = 0x02
CMD_SWITCH_BINARY_SET = 0x01
CMD_SWITCH_MULTILEVEL_GET = 0x05
CMD_SWITCH_MULTILEVEL_SET = 0x01
CMD_SWITCH_MULTILEVEL_START = 0x07
CMD_SWITCH_MULTILEVEL_STOP = 0x08

# Z-Wave on/off level values (0xFF = full on, 0x00 = off)
ZWAVE_LEVEL_FULL = 0xFF


class ZWaveProtocolAdapter(ProtocolAdapter):
    """Generic Z-Wave protocol adapter — no vendor-specific logic."""

    VENDOR_CODE = "zwave"
    VENDOR_HOSTNAMES: list[str] = []

    def __init__(self, vendor: str, config: dict[str, Any]) -> None:
        super().__init__(vendor, config)
        self._devices: dict[str, dict] = {}

    @property
    def supported_protocols(self) -> list[ProtocolType]:
        return [ProtocolType.ZWAVE]

    @property
    def vendor_hostnames(self) -> list[str]:
        return self.VENDOR_HOSTNAMES

    async def parse_request(self, request: InterceptedRequest) -> InterceptedRequest:
        """Parse a Z-Wave command class message into a standardized intent."""
        body = request.body or {}
        cc_id = body.get("command_class_id")
        cmd_id = body.get("command_id")
        params = body.get("parameters", {})

        intent = self._resolve_zwave_intent(cc_id, cmd_id, params)
        request.parsed_intent = intent
        request.parsed_params = {
            "command_class_id": cc_id,
            "command_id": cmd_id,
            "parameters": params,
            "node_id": body.get("node_id"),
            "endpoint": body.get("endpoint", 0),
        }
        return request

    def _resolve_zwave_intent(  # noqa: C901, PLR0911, PLR0912
        self, cc: int | None, cmd: int | None, params: dict
    ) -> CommandType:
        """Resolve a Z-Wave command class to a standard CommandType."""
        if cc == CC_SWITCH_BINARY:
            if cmd in (CMD_SET, None):
                val = params.get("target_value", params.get("value"))
                if val is True or val == ZWAVE_LEVEL_FULL:
                    return CommandType.TURN_ON
                if val is False or val == 0x00:
                    return CommandType.TURN_OFF
            return CommandType.GET_STATE

        if cc == CC_SWITCH_MULTILEVEL:
            if cmd in (CMD_SET, CMD_SWITCH_MULTILEVEL_START, None):
                return CommandType.SET_MODE
            return CommandType.GET_STATE

        if cc == CC_SWITCH_ALL:
            if cmd == CMD_SET:
                val = params.get("target_value")
                if val == ZWAVE_LEVEL_FULL:
                    return CommandType.TURN_ON
                if val == 0x00:
                    return CommandType.TURN_OFF
            return CommandType.GET_STATE

        if cc == CC_THERMOSTAT_SETPOINT:
            if cmd in (CMD_SET, None):
                return CommandType.SET_TEMPERATURE
            return CommandType.GET_STATE

        if cc == CC_THERMOSTAT_MODE:
            if cmd in (CMD_SET, None):
                return CommandType.SET_MODE
            return CommandType.GET_STATE

        if cc == CC_THERMOSTAT_FAN_MODE:
            if cmd in (CMD_SET, None):
                return CommandType.SET_FAN_SPEED
            return CommandType.GET_STATE

        if cc in (
            CC_SENSOR_MULTILEVEL,
            CC_METER,
            CC_BATTERY,
            CC_VERSION,
            CC_MANUFACTURER_SPECIFIC,
            CC_INDICATOR,
            CC_POWERLEVEL,
            CC_PROTECTION,
            CC_WAKE_UP,
            CC_CONFIGURATION,
            CC_NOTIFICATION,
        ):
            return CommandType.GET_STATE

        return CommandType.UNKNOWN

    async def handle_request(self, request: InterceptedRequest) -> CommandResult:
        """Handle a Z-Wave request locally."""
        if request.parsed_intent in (CommandType.GET_STATE,):
            state = await self.get_device_state(request.device_id)
            if state:
                return CommandResult(
                    success=True,
                    response={
                        "on_off": state.on_off,
                        "mode": state.mode,
                        "temperature_actual": state.temp_actual,
                        "temperature_setpoint": state.temp_target,
                        "humidity": state.humidity,
                        "power_watts": state.power_watts,
                    },
                )
        return await self.forward_to_cloud(request)

    async def forward_to_cloud(self, request: InterceptedRequest) -> CommandResult:  # noqa: ARG002
        return CommandResult(success=False, error="Cloud forward not implemented", forwarded=False)

    async def build_response(self, request: InterceptedRequest, result: CommandResult) -> dict:
        if result.success and result.response:
            return result.response
        return {
            "error": result.error or "unknown",
            "command_class_id": request.parsed_params.get("command_class_id"),
        }

    async def get_device_info(self, device_id: str) -> DeviceInfo | None:
        info = self._devices.get(device_id)
        if info:
            return DeviceInfo(
                device_id=device_id,
                vendor="zwave",
                device_type="zwave_node",
                model=info.get("model"),
                firmware_version=info.get("fw"),
                capabilities=[
                    DeviceCapability.POWER_MONITORING,
                    DeviceCapability.INDOOR_TEMP_SENSOR,
                    DeviceCapability.MODE_CONTROL,
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
            temp_target=info.get("temperature_setpoint"),
            humidity=info.get("humidity"),
            power_watts=info.get("power"),
            mode=info.get("mode"),
            on_off=info.get("on_off"),
            source="device",
            quality="good",
        )

    async def send_command(self, device_id: str, command: Command) -> CommandResult:  # noqa: ARG002
        return CommandResult(success=False, error="Send not implemented")

    async def on_device_connect(self, device_id: str, initial_data: dict) -> None:
        self._devices[device_id] = initial_data
        logger.info("Z-Wave device connected: %s (node=%s)", device_id, initial_data.get("node_id"))

    async def on_device_disconnect(self, device_id: str) -> None:
        self._devices.pop(device_id, None)
        logger.info("Z-Wave device disconnected: %s", device_id)

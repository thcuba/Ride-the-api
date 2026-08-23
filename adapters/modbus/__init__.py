"""
Modbus Protocol Adapter Example — for Modbus TCP industrial devices (PLCs,
energy meters, inverters).

Modbus is a widely used industrial protocol. This adapter translates Modbus register
read/write operations to standardized CommandTypes.

Common Modbus operations:
  - Read Holding Registers (0x03) → read device parameters
  - Write Single Register (0x06) → set device parameters
  - Read Input Registers (0x04) → read sensor values
  - Read Coils (0x01) → read digital outputs
  - Write Single Coil (0x05) → set digital outputs

Register mapping is device-specific and should be configured per vendor.
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

# Example register mapping for a thermostat/AC device
# In practice, this would come from a device profile database or config
EXAMPLE_REGISTER_MAP: dict[int, tuple[str, CommandType]] = {
    0x0000: ("temperature_setpoint", CommandType.SET_TEMPERATURE),
    0x0001: ("temperature_actual", CommandType.GET_STATE),
    0x0002: ("mode", CommandType.SET_MODE),  # 0=cool, 1=heat, 2=fan, 3=auto, 4=dry
    0x0003: ("fan_speed", CommandType.SET_FAN_SPEED),  # 0=low, 1=medium, 2=high, 3=auto
    0x0004: ("on_off", CommandType.TURN_ON),  # 0=off, 1=on
    0x0005: ("swing", CommandType.SET_SWING),
    0x0006: ("humidity", CommandType.GET_STATE),
    0x0010: ("power_consumption", CommandType.GET_STATE),
}


# Write intent types that must be coerced to GET_STATE when a register is
# read (a read of a writable register reports state, not a write action).
_WRITE_INTENTS: set[CommandType] = {
    CommandType.SET_TEMPERATURE,
    CommandType.SET_MODE,
    CommandType.SET_FAN_SPEED,
    CommandType.SET_SWING,
    CommandType.SET_SCHEDULE,
    CommandType.TURN_ON,
    CommandType.TURN_OFF,
    CommandType.FIRMWARE_UPDATE,
}


class ModbusProtocolAdapter(ProtocolAdapter):
    """Example adapter for Modbus TCP industrial devices."""

    VENDOR_CODE = "modbus_example"
    VENDOR_HOSTNAMES: list[str] = []

    def __init__(self, vendor: str, config: dict[str, Any]) -> None:
        super().__init__(vendor, config)
        self._devices: dict[str, dict] = {}
        self._register_map: dict[int, tuple[str, CommandType]] = EXAMPLE_REGISTER_MAP

    @property
    def supported_protocols(self) -> list[ProtocolType]:
        return [ProtocolType.MODBUS, ProtocolType.MODBUSS]

    @property
    def vendor_hostnames(self) -> list[str]:
        return self.VENDOR_HOSTNAMES

    async def parse_request(self, request: InterceptedRequest) -> InterceptedRequest:
        """Parse a Modbus request and extract intent."""
        body = request.body or {}

        function_code = body.get("function_code")
        register = body.get("register")
        values = body.get("values", body.get("value"))

        if function_code is not None and register is not None:
            # Read operations (0x01, 0x02, 0x03, 0x04)
            if function_code in (0x01, 0x02, 0x03, 0x04):
                if register in self._register_map:
                    field_name, intent = self._register_map[register]
                    # A read of any writable register reports state, so a
                    # write intent (SET_MODE, TURN_ON, …) must not leak through
                    # on a read (it would mis-route the request to forward).
                    if intent in _WRITE_INTENTS:
                        intent = CommandType.GET_STATE
                    request.parsed_intent = intent
                    request.parsed_params = {
                        "function_code": function_code,
                        "register": register,
                        "field": field_name,
                        "count": body.get("count", 1),
                    }
                else:
                    request.parsed_intent = CommandType.GET_STATE
                    request.parsed_params = {"register": register}
                return request

            # Write operations (0x05, 0x06, 0x0F, 0x10)
            if function_code in (0x05, 0x06, 0x0F, 0x10):
                if register in self._register_map:
                    field_name, intent = self._register_map[register]
                    # Decode on_off from value
                    if (
                        intent == CommandType.TURN_ON
                        and values is not None
                        and values
                        in (
                            0,
                            False,
                            "0",
                            "off",
                        )
                    ):
                        intent = CommandType.TURN_OFF
                    request.parsed_intent = intent
                    request.parsed_params = {
                        "function_code": function_code,
                        "register": register,
                        "field": field_name,
                        "value": values,
                    }
                else:
                    request.parsed_intent = CommandType.UNKNOWN
                    request.parsed_params = {"register": register, "value": values}
                return request

        return request

    async def handle_request(self, request: InterceptedRequest) -> CommandResult:
        """Handle Modbus request locally."""
        if request.parsed_intent == CommandType.GET_STATE:
            state = await self.get_device_state(request.device_id)
            if state:
                return CommandResult(
                    success=True,
                    response={
                        "temperature_actual": state.temp_actual,
                        "temperature_setpoint": state.temp_target,
                        "mode": state.mode,
                        "fan_speed": state.fan_speed,
                        "on_off": state.on_off,
                        "power_consumption": state.power_watts,
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
                vendor="modbus_example",
                device_type="industrial",
                model=info.get("model"),
                firmware_version=info.get("fw"),
                capabilities=[
                    DeviceCapability.TEMPERATURE_CONTROL,
                    DeviceCapability.MODE_CONTROL,
                    DeviceCapability.FAN_SPEED_CONTROL,
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
            mode=info.get("mode"),
            fan_speed=info.get("fan_speed"),
            on_off=info.get("on_off"),
            power_watts=info.get("power_consumption"),
            source="device",
            quality="good",
        )

    async def send_command(self, _device_id: str, _command: Command) -> CommandResult:
        return CommandResult(success=False, error="Send not implemented")

    async def on_device_connect(self, device_id: str, initial_data: dict) -> None:
        self._devices[device_id] = initial_data
        logger.info("Modbus device connected: %s", device_id)

    async def on_device_disconnect(self, device_id: str) -> None:
        self._devices.pop(device_id, None)
        logger.info("Modbus device disconnected: %s", device_id)

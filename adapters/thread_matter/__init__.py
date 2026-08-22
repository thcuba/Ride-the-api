"""
Thread / Matter Protocol Adapter — generic, vendor-neutral handling.

Matter (formerly Project CHIP) is an IP-based smart home standard built on
Thread (IEEE 802.15.4 mesh) and Wi-Fi. Devices communicate via Interaction
Model protocol over TCP/UDP (IPv6). This adapter translates Matter cluster
messages into standardized CommandTypes.

Matter clusters handled:
  - OnOff (0x0006): on/off → TURN_ON / TURN_OFF
  - LevelControl (0x0008): dimming → SET_MODE
  - TemperatureControl (0x0202): setpoint → SET_TEMPERATURE
  - Thermostat (0x0201): HVAC → mode/temperature
  - FanControl (0x0203): fan speed → SET_FAN_SPEED
  - Descriptor (0x001D): device info
  - BridgedDeviceBasic (0x0039): bridged device info
  - Groups (0x0004): group management
  - Scenes (0x0005): scene management
  - PowerSource (0x002F): battery/power info
  - ElectricalMeasurement (0x0905): power monitoring
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

# Matter Cluster IDs (standard)
MATTER_CLUSTER_ON_OFF = 0x0006
MATTER_CLUSTER_LEVEL_CONTROL = 0x0008
MATTER_CLUSTER_THERMOSTAT = 0x0201
MATTER_CLUSTER_TEMP_CONTROL = 0x0202
MATTER_CLUSTER_FAN_CONTROL = 0x0203
MATTER_CLUSTER_DESCRIPTOR = 0x001D
MATTER_CLUSTER_BRIDGED_DEVICE_BASIC = 0x0039
MATTER_CLUSTER_GROUPS = 0x0004
MATTER_CLUSTER_SCENES = 0x0005
MATTER_CLUSTER_POWER_SOURCE = 0x002F
MATTER_CLUSTER_ELEC_MEASUREMENT = 0x0905
MATTER_CLUSTER_IDENTIFY = 0x0003
MATTER_CLUSTER_BASIC_INFO = 0x0028

# Matter Interaction Model command types
IM_INVOKE_COMMAND = "invoke"
IM_READ_ATTRIBUTE = "read"
IM_WRITE_ATTRIBUTE = "write"
IM_SUBSCRIBE = "subscribe"

# On/Off cluster commands
MATTER_CMD_ON = 0x01
MATTER_CMD_OFF = 0x00
MATTER_CMD_TOGGLE = 0x02


class MatterProtocolAdapter(ProtocolAdapter):
    """Generic Thread/Matter protocol adapter — no vendor-specific logic."""

    VENDOR_CODE = "matter"
    VENDOR_HOSTNAMES: list[str] = []

    def __init__(self, vendor: str, config: dict[str, Any]):
        super().__init__(vendor, config)
        self._devices: dict[str, dict] = {}

    @property
    def supported_protocols(self) -> list[ProtocolType]:
        return [ProtocolType.MATTER]

    @property
    def vendor_hostnames(self) -> list[str]:
        return self.VENDOR_HOSTNAMES

    async def parse_request(self, request: InterceptedRequest) -> InterceptedRequest:
        """Parse a Matter Interaction Model message into a standardized intent."""
        body = request.body or {}
        interaction_type = body.get("interaction_type", IM_READ_ATTRIBUTE)
        cluster = body.get("cluster_id")
        command_id = body.get("command_id")
        attributes = body.get("attributes", {})

        intent = self._resolve_matter_intent(interaction_type, cluster, command_id, attributes)
        request.parsed_intent = intent
        request.parsed_params = {
            "interaction_type": interaction_type,
            "cluster_id": cluster,
            "command_id": command_id,
            "attributes": attributes,
            "endpoint": body.get("endpoint", 0),
            "fabric_id": body.get("fabric_id"),
        }
        return request

    def _resolve_matter_intent(
        self, interaction: str, cluster: int | None, cmd: int | None, attrs: dict
    ) -> CommandType:
        """Resolve Matter cluster + interaction to a standard CommandType."""

        # Write attributes (commands to set values)
        if interaction == IM_WRITE_ATTRIBUTE:
            return self._resolve_write_intent(cluster, attrs)

        # Invoke commands (action commands)
        if interaction == IM_INVOKE_COMMAND:
            return self._resolve_invoke_intent(cluster, cmd)

        # Read / subscribe = state query
        if interaction in (IM_READ_ATTRIBUTE, IM_SUBSCRIBE):
            return CommandType.GET_STATE

        return CommandType.UNKNOWN

    def _resolve_write_intent(self, cluster: int | None, attrs: dict) -> CommandType:
        """Resolve a write-attribute interaction to a command."""
        if cluster == MATTER_CLUSTER_ON_OFF:
            val = attrs.get(0x0000)  # OnOff attribute
            if val is True or val == 1:
                return CommandType.TURN_ON
            if val is False or val == 0:
                return CommandType.TURN_OFF
            return CommandType.UNKNOWN

        if cluster == MATTER_CLUSTER_LEVEL_CONTROL:
            return CommandType.SET_MODE

        if cluster in (MATTER_CLUSTER_THERMOSTAT, MATTER_CLUSTER_TEMP_CONTROL):
            # Check if setting temperature or mode
            if any(k in attrs for k in (0x0012, 0x0013, 0x0014, 0x0015)):  # setpoints
                return CommandType.SET_TEMPERATURE
            if 0x001C in attrs:  # ThermostatMode
                return CommandType.SET_MODE
            if 0x0000 in attrs:  # FanMode (FanControl)
                return CommandType.SET_FAN_SPEED
            return CommandType.UNKNOWN

        if cluster == MATTER_CLUSTER_FAN_CONTROL:
            return CommandType.SET_FAN_SPEED

        return CommandType.UNKNOWN

    def _resolve_invoke_intent(self, cluster: int | None, cmd: int | None) -> CommandType:
        """Resolve an invoke-command interaction to a command."""
        if cluster == MATTER_CLUSTER_ON_OFF:
            if cmd == MATTER_CMD_ON:
                return CommandType.TURN_ON
            if cmd == MATTER_CMD_OFF:
                return CommandType.TURN_OFF
            if cmd == MATTER_CMD_TOGGLE:
                return CommandType.UNKNOWN

        if cluster == MATTER_CLUSTER_LEVEL_CONTROL:
            return CommandType.SET_MODE

        if cluster == MATTER_CLUSTER_IDENTIFY:
            return CommandType.GET_STATE

        return CommandType.UNKNOWN

    async def handle_request(self, request: InterceptedRequest) -> CommandResult:
        """Handle a Matter request locally."""
        if request.parsed_intent in (CommandType.GET_STATE,):
            state = await self.get_device_state(request.device_id)
            if state:
                return CommandResult(success=True, response={
                    "on_off": state.on_off,
                    "mode": state.mode,
                    "temperature_actual": state.temp_actual,
                    "temperature_setpoint": state.temp_target,
                    "fan_speed": state.fan_speed,
                    "humidity": state.humidity,
                    "power_watts": state.power_watts,
                })
        return await self.forward_to_cloud(request)

    async def forward_to_cloud(self, request: InterceptedRequest) -> CommandResult:
        return CommandResult(success=False, error="Cloud forward not implemented", forwarded=True)

    async def build_response(self, request: InterceptedRequest, result: CommandResult) -> dict:
        if result.success and result.response:
            return result.response
        return {
            "error": result.error or "unknown",
            "cluster_id": request.parsed_params.get("cluster_id"),
        }

    async def get_device_info(self, device_id: str) -> DeviceInfo | None:
        info = self._devices.get(device_id)
        if info:
            return DeviceInfo(
                device_id=device_id,
                vendor="matter",
                device_type="matter_node",
                model=info.get("model"),
                firmware_version=info.get("fw"),
                capabilities=[
                    DeviceCapability.POWER_MONITORING,
                    DeviceCapability.INDOOR_TEMP_SENSOR,
                    DeviceCapability.MODE_CONTROL,
                    DeviceCapability.FAN_SPEED_CONTROL,
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
            fan_speed=info.get("fan_speed"),
            on_off=info.get("on_off"),
            source="device",
            quality="good",
        )

    async def send_command(self, device_id: str, command: Command) -> CommandResult:
        return CommandResult(success=False, error="Send not implemented")

    async def on_device_connect(self, device_id: str, initial_data: dict) -> None:
        self._devices[device_id] = initial_data
        logger.info("Matter device connected: %s (fabric=%s)",
                     device_id, initial_data.get("fabric_id"))

    async def on_device_disconnect(self, device_id: str) -> None:
        self._devices.pop(device_id, None)
        logger.info("Matter device disconnected: %s", device_id)
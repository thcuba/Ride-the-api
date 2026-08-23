"""
Zigbee Protocol Adapter — generic, vendor-neutral Zigbee message handling.

Zigbee is a low-power wireless mesh protocol (IEEE 802.15.4) widely used
in smart home devices. This adapter translates standard Zigbee Cluster
Library (ZCL) messages into standardized CommandTypes.

Standard topic/attribute patterns:
  - ZCL on/off cluster (0x0006): on/off attribute → TURN_ON / TURN_OFF
  - ZCL level control (0x0008): level attribute → SET_MODE / fan speed
  - ZCL temperature (0x0402): measured value → GET_STATE / SET_TEMPERATURE
  - ZCL groups (0x0004): group commands → group add/remove
  - ZCL basic cluster (0x0000): device info queries
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

# ZCL Cluster IDs (standard Zigbee clusters)
ZCL_CLUSTER_ON_OFF = 0x0006
ZCL_CLUSTER_LEVEL = 0x0008
ZCL_CLUSTER_TEMPERATURE = 0x0402
ZCL_CLUSTER_HUMIDITY = 0x0405
ZCL_CLUSTER_POWER_CONFIG = 0x0001
ZCL_CLUSTER_BASIC = 0x0000
ZCL_CLUSTER_GROUPS = 0x0004
ZCL_CLUSTER_SCENES = 0x0005
ZCL_CLUSTER_IAS_ZONE = 0x0500
ZCL_CLUSTER_METERING = 0x0702

# ZCL Attribute IDs for common clusters
ATTR_ON_OFF = 0x0000
ATTR_LEVEL = 0x0000
ATTR_TEMP_MEASURED = 0x0000
ATTR_HUMIDITY_MEASURED = 0x0000
ATTR_BATTERY_VOLTAGE = 0x0020
ATTR_BATTERY_PERCENT = 0x0021
ATTR_INSTANT_POWER = 0x0504
ATTR_TOTAL_ENERGY = 0x0000

# ZCL Command IDs for on/off cluster
CMD_OFF = 0x00
CMD_ON = 0x01
CMD_TOGGLE = 0x02


class ZigbeeProtocolAdapter(ProtocolAdapter):
    """Generic Zigbee protocol adapter — no vendor-specific logic."""

    VENDOR_CODE = "zigbee"
    VENDOR_HOSTNAMES: list[str] = []

    def __init__(self, vendor: str, config: dict[str, Any]) -> None:
        super().__init__(vendor, config)
        self._devices: dict[str, dict] = {}

    @property
    def supported_protocols(self) -> list[ProtocolType]:
        return [ProtocolType.ZIGBEE]

    @property
    def vendor_hostnames(self) -> list[str]:
        return self.VENDOR_HOSTNAMES

    async def parse_request(self, request: InterceptedRequest) -> InterceptedRequest:
        """Parse a Zigbee ZCL message into a standardized command intent."""
        body = request.body or {}
        cluster = body.get("cluster_id")
        command_id = body.get("command_id")
        attributes = body.get("attributes", {})

        intent = self._resolve_zcl_intent(cluster, command_id, attributes)
        request.parsed_intent = intent
        request.parsed_params = {
            "cluster_id": cluster,
            "command_id": command_id,
            "attributes": attributes,
            "source_endpoint": body.get("source_endpoint"),
            "dest_endpoint": body.get("dest_endpoint"),
        }
        return request

    def _resolve_zcl_intent(  # noqa: C901, PLR0911
        self, cluster: int | None, cmd: int | None, attrs: dict
    ) -> CommandType:
        """Resolve ZCL cluster/command/attributes to a standard CommandType."""
        if cluster == ZCL_CLUSTER_ON_OFF:
            if cmd == CMD_ON:
                return CommandType.TURN_ON
            if cmd == CMD_OFF:
                return CommandType.TURN_OFF
            if cmd == CMD_TOGGLE:
                return CommandType.UNKNOWN
            # Attribute reporting
            if attrs.get(ATTR_ON_OFF) == 1:
                return CommandType.TURN_ON
            if attrs.get(ATTR_ON_OFF) == 0:
                return CommandType.TURN_OFF
            return CommandType.GET_STATE

        if cluster == ZCL_CLUSTER_LEVEL:
            if cmd is not None:
                return CommandType.SET_MODE
            return CommandType.GET_STATE

        if cluster == ZCL_CLUSTER_TEMPERATURE:
            if cmd is not None:
                return CommandType.SET_TEMPERATURE
            return CommandType.GET_STATE

        if cluster in (
            ZCL_CLUSTER_POWER_CONFIG,
            ZCL_CLUSTER_IAS_ZONE,
            ZCL_CLUSTER_METERING,
            ZCL_CLUSTER_BASIC,
        ):
            return CommandType.GET_STATE

        return CommandType.UNKNOWN

    async def handle_request(self, request: InterceptedRequest) -> CommandResult:
        """Handle a Zigbee ZCL request locally."""
        if request.parsed_intent in (CommandType.GET_STATE,):
            state = await self.get_device_state(request.device_id)
            if state:
                return CommandResult(
                    success=True,
                    response={
                        "on_off": state.on_off,
                        "temperature_actual": state.temp_actual,
                        "humidity": state.humidity,
                        "power_watts": state.power_watts,
                        "battery": state.vendor_data.get("battery_percent"),
                    },
                )
        return await self.forward_to_cloud(request)

    async def forward_to_cloud(self, request: InterceptedRequest) -> CommandResult:  # noqa: ARG002
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
                vendor="zigbee",
                device_type="zigbee_end_device",
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
            temp_actual=info.get("temperature"),
            temp_target=info.get("temperature_setpoint"),
            humidity=info.get("humidity"),
            power_watts=info.get("power"),
            mode=info.get("mode"),
            on_off=info.get("on_off"),
            vendor_data={"battery_percent": info.get("battery")},
            source="device",
            quality="good",
        )

    async def send_command(self, device_id: str, command: Command) -> CommandResult:  # noqa: ARG002
        return CommandResult(success=False, error="Send not implemented")

    async def on_device_connect(self, device_id: str, initial_data: dict) -> None:
        self._devices[device_id] = initial_data
        logger.info(
            "Zigbee device connected: %s (ieee=%s)", device_id, initial_data.get("ieee_address")
        )

    async def on_device_disconnect(self, device_id: str) -> None:
        self._devices.pop(device_id, None)
        logger.info("Zigbee device disconnected: %s", device_id)

"""
Example Protocol Adapter - Reference Implementation.

Demonstrates how to implement a protocol adapter for an HVAC device
that uses MQTT and HTTP/HTTPS with Data Point (DP) code-based protocol.
This is a reference example — users/community choose their own database names.
"""

from __future__ import annotations

import re
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


class ExampleProtocolAdapter(ProtocolAdapter):
    """Example protocol adapter for HVAC devices — reference implementation."""

    # Vendor code (example — users choose their own)
    VENDOR_CODE = "example"

    # Example cloud hostnames (placeholder — users replace with their own)
    VENDOR_HOSTNAMES = [
        "mqtt.example.com",
        "mqtt.example.cn",
        "api.example.com",
        "api.example.cn",
        "openapi.example.com",
        "openapi.example.cn",
    ]

    # Data Point (DP) codes — common HVAC DP codes used by many protocols
    # These can vary by device model — should be discovered per device.
    # Users/community define their own DP code mappings for their database.
    DP_CODES = {
        "power": "1",  # Boolean: true=on, false=off
        "mode": "2",  # Enum: "cold", "hot", "wet", "wind", "auto"
        "temp_set": "3",  # Integer: target temp * 10 (e.g., 240 = 24.0°C)
        "temp_current": "4",  # Integer: current temp * 10
        "fan_speed": "5",  # Enum: "low", "medium", "high", "auto"
        "swing": "6",  # Boolean: true=on, false=off
        "humidity": "7",  # Integer: humidity %
        "outdoor_temp": "8",  # Integer: outdoor temp * 10
        "power_consumption": "9",  # Integer: watts
        "eco_mode": "10",  # Boolean
        "sleep_mode": "11",  # Boolean
        "filter_life": "12",  # Integer: percentage
        "error_code": "13",  # String: error code
    }

    # Reverse mapping
    DP_CODES_REV = {v: k for k, v in DP_CODES.items()}

    # Mode mapping: vendor-specific -> standard
    MODE_VENDOR_TO_STD = {
        "cold": "cool",
        "hot": "heat",
        "wet": "dry",
        "wind": "fan",
        "auto": "auto",
    }

    MODE_STD_TO_VENDOR = {v: k for k, v in MODE_VENDOR_TO_STD.items()}

    # Fan speed mapping
    FAN_VENDOR_TO_STD = {
        "low": "low",
        "medium": "medium",
        "high": "high",
        "auto": "auto",
    }

    FAN_STD_TO_VENDOR = {v: k for k, v in FAN_VENDOR_TO_STD.items()}

    def __init__(self, vendor: str, config: dict[str, Any]) -> None:
        super().__init__(vendor, config)
        self.region = config.get("region", "eu")
        self.api_version = config.get("api_version", "v1.0")
        self._device_dp_codes: dict[str, dict[str, str]] = {}  # Per-device DP code overrides

    @property
    def supported_protocols(self) -> list[ProtocolType]:
        return [ProtocolType.MQTT, ProtocolType.MQTTS, ProtocolType.HTTPS, ProtocolType.HTTP]

    @property
    def vendor_hostnames(self) -> list[str]:
        return self.VENDOR_HOSTNAMES

    # ══════════════════════════════════════════════════════════════════════════════
    # REQUEST PARSING
    # ══════════════════════════════════════════════════════════════════════════════

    async def parse_request(self, request: InterceptedRequest) -> InterceptedRequest:
        """Parse request and extract intent."""

        if request.protocol in (ProtocolType.MQTT, ProtocolType.MQTTS):
            return await self._parse_mqtt_request(request)
        if request.protocol in (ProtocolType.HTTP, ProtocolType.HTTPS):
            return await self._parse_http_request(request)

        request.parsed_intent = CommandType.UNKNOWN
        return request

    async def _parse_mqtt_request(self, request: InterceptedRequest) -> InterceptedRequest:
        """Parse MQTT message."""
        if not request.topic or not request.body:
            request.parsed_intent = CommandType.UNKNOWN
            return request

            # Example MQTT topics:
        # - thing/status/device_id (device reports state)
        # - thing/command/device_id (cloud sends command)
        # - thing/property/device_id (property updates)

        topic_parts = request.topic.split("/")
        if len(topic_parts) < 3:  # noqa: PLR2004
            request.parsed_intent = CommandType.UNKNOWN
            return request

        msg_type = topic_parts[1]  # status, command, property
        device_id = topic_parts[2]
        request.device_id = device_id

        # Extract DP data from body
        dp_data = {}
        if "data" in request.body and isinstance(request.body["data"], dict):
            dp_data = request.body["data"]
        elif "dps" in request.body and isinstance(request.body["dps"], dict):
            dp_data = request.body["dps"]

        # Map DP codes to standard params
        params = {}
        for dp_code, value in dp_data.items():
            std_name = self.DP_CODES_REV.get(dp_code, dp_code)
            params[std_name] = value

        # Determine intent based on message type and content
        if msg_type == "command":
            # Cloud -> Device command
            intent = self._dp_params_to_intent(params)
            request.parsed_intent = intent
            request.parsed_params = params
        elif msg_type in ("status", "property"):
            # Device -> Cloud state report
            request.parsed_intent = CommandType.GET_STATE
            request.parsed_params = params

        return request

    async def _parse_http_request(self, request: InterceptedRequest) -> InterceptedRequest:
        """Parse HTTP/HTTPS API request."""
        if not request.path:
            request.parsed_intent = CommandType.UNKNOWN
            return request

            # Example API endpoints:
        # GET  /v1.0/devices/{device_id}                    - Get device info
        # GET  /v1.0/devices/{device_id}/status             - Get device status
        # POST /v1.0/devices/{device_id}/commands           - Send command
        # GET  /v1.0/devices/{device_id}/specifications     - Get device specs (DP codes)
        # POST /v1.0/devices/{device_id}/firmware/upgrade   - Firmware upgrade

        # Extract device_id from path
        match = re.search(r"/devices/([^/]+)", request.path)
        if match:
            request.device_id = match.group(1)

        method = request.method.upper() if request.method else "GET"

        if "/status" in request.path and method == "GET":
            request.parsed_intent = CommandType.GET_STATE
        elif "/commands" in request.path and method == "POST":
            request.parsed_intent = CommandType.UNKNOWN  # Will parse from body
            if request.body and "commands" in request.body:
                params = {}
                for cmd in request.body["commands"]:
                    dp_code = cmd.get("code")
                    value = cmd.get("value")
                    if dp_code:
                        std_name = self.DP_CODES_REV.get(dp_code, dp_code)
                        params[std_name] = value
                intent = self._dp_params_to_intent(params)
                request.parsed_intent = intent
                request.parsed_params = params
        elif "/firmware" in request.path:
            request.parsed_intent = CommandType.FIRMWARE_UPDATE
        elif "/specifications" in request.path:
            request.parsed_intent = CommandType.GET_STATE  # Device info request
        else:
            request.parsed_intent = CommandType.UNKNOWN

        return request

    def _dp_params_to_intent(self, params: dict[str, Any]) -> CommandType:
        """Map DP params to command intent."""
        if "power" in params:
            return CommandType.TURN_ON if params["power"] else CommandType.TURN_OFF
        if "temp_set" in params:
            return CommandType.SET_TEMPERATURE
        if "mode" in params:
            return CommandType.SET_MODE
        if "fan_speed" in params:
            return CommandType.SET_FAN_SPEED
        if "swing" in params:
            return CommandType.SET_SWING
        return CommandType.UNKNOWN

    # ══════════════════════════════════════════════════════════════════════════════
    # REQUEST HANDLING (EDGE INFERENCE)
    # ══════════════════════════════════════════════════════════════════════════════

    async def handle_request(self, request: InterceptedRequest) -> CommandResult:
        """Handle request locally via edge AI/control logic."""
        # This is where edge inference would happen
        # For now, return a basic response indicating local handling

        if request.parsed_intent == CommandType.GET_STATE:
            # Return current known state (would come from vendor DB)
            state = await self.get_device_state(request.device_id)
            if state:
                response_data = await self._state_to_response(state)
                return CommandResult(
                    success=True,
                    response=response_data,
                    forwarded=False,
                )

        elif request.parsed_intent in (
            CommandType.SET_TEMPERATURE,
            CommandType.SET_MODE,
            CommandType.SET_FAN_SPEED,
            CommandType.SET_SWING,
            CommandType.TURN_ON,
            CommandType.TURN_OFF,
        ):
            # Convert to standard command
            command = Command(
                device_id=request.device_id,
                command_type=request.parsed_intent,
                params=request.parsed_params,
                source="edge_auto",
            )
            # In real implementation, this would go through policy engine
            return await self.send_command(request.device_id, command)

        # Default: forward to cloud
        return await self.forward_to_cloud(request)

    async def forward_to_cloud(self, _request: InterceptedRequest) -> CommandResult:
        """Forward request to real cloud."""
        # Implementation would use the vendor's Cloud API
        # For now, return placeholder
        return CommandResult(
            success=False,
            error="Cloud forward not implemented",
            forwarded=True,
        )

    # ══════════════════════════════════════════════════════════════════════════════
    # RESPONSE BUILDING
    # ══════════════════════════════════════════════════════════════════════════════

    async def build_response(
        self, request: InterceptedRequest, result: CommandResult
    ) -> dict[str, Any]:
        """Build vendor-compatible response."""
        if request.protocol in (ProtocolType.MQTT, ProtocolType.MQTTS):
            return await self._build_mqtt_response(request, result)
        return await self._build_http_response(request, result)

    async def _build_mqtt_response(
        self, request: InterceptedRequest, result: CommandResult
    ) -> dict[str, Any]:
        """Build MQTT response."""
        if not result.success:
            return {"success": False, "error": result.error}

        # Example MQTT command response format
        device_id = request.device_id
        t = int(int(datetime.now(UTC).timestamp() * 1000))

        if request.parsed_intent == CommandType.GET_STATE:
            # Status response
            state_data = result.response or {}
            dps = {}
            for std_key, value in state_data.items():
                dp_code = self.DP_CODES.get(std_key)
                if dp_code:
                    dps[dp_code] = value

            return {
                "tid": f"edge_{t}",
                "bid": device_id,
                "type": "thing.status",
                "data": {"dps": dps},
                "time": t,
            }
        # Command response
        return {
            "tid": f"edge_{t}",
            "bid": device_id,
            "type": "thing.command.response",
            "data": {"success": result.success},
            "time": t,
        }

    async def _build_http_response(
        self, request: InterceptedRequest, result: CommandResult
    ) -> dict[str, Any]:
        """Build HTTP API response."""
        if not result.success:
            return {
                "success": False,
                "error_code": "EDGE_ERROR",
                "msg": result.error or "Edge processing failed",
                "t": int(int(datetime.now(UTC).timestamp() * 1000)),
            }

        if request.parsed_intent == CommandType.GET_STATE:
            state_data = result.response or {}
            # Convert to vendor status format
            dps = {}
            for std_key, value in state_data.items():
                dp_code = self.DP_CODES.get(std_key)
                if dp_code:
                    dps[dp_code] = value

            return {
                "success": True,
                "t": int(int(datetime.now(UTC).timestamp() * 1000)),
                "result": {
                    "status": [{"code": k, "value": v} for k, v in dps.items()],
                },
            }
        return {
            "success": True,
            "t": int(int(datetime.now(UTC).timestamp() * 1000)),
            "result": {},
        }

    # ══════════════════════════════════════════════════════════════════════════════
    # DEVICE INFO & STATE
    # ══════════════════════════════════════════════════════════════════════════════

    async def get_device_info(self, device_id: str) -> DeviceInfo | None:
        """Get device info (would query vendor DB)."""
        # Placeholder - would query vendor DB
        return DeviceInfo(
            device_id=device_id,
            vendor=self.vendor,
            device_type="ac",
            capabilities=[
                DeviceCapability.TEMPERATURE_CONTROL,
                DeviceCapability.MODE_CONTROL,
                DeviceCapability.FAN_SPEED_CONTROL,
                DeviceCapability.SWING_CONTROL,
                DeviceCapability.POWER_MONITORING,
            ],
        )

    async def get_device_state(self, _device_id: str) -> DeviceState | None:
        """Get device state (would query vendor DB)."""
        # Placeholder - would query vendor DB for latest reading
        return None

    # ══════════════════════════════════════════════════════════════════════════════
    # COMMAND EXECUTION
    # ══════════════════════════════════════════════════════════════════════════════

    async def send_command(self, _device_id: str, command: Command) -> CommandResult:
        """Send command to device via cloud API."""
        # Convert standard command to DP format
        vendor_commands = self._command_to_dps(command)

        # In real implementation, call the cloud API:
        #   POST /v1.0/devices/{device_id}/commands with the DP commands payload.

        return CommandResult(
            success=True,
            response={"commands": vendor_commands},
            forwarded=False,
        )

    def _command_to_dps(self, command: Command) -> list[dict[str, Any]]:  # noqa: C901
        """Convert standard command to DP commands."""
        cmds = []
        params = command.params

        if command.command_type == CommandType.TURN_ON:
            cmds.append({"code": self.DP_CODES["power"], "value": True})
        elif command.command_type == CommandType.TURN_OFF:
            cmds.append({"code": self.DP_CODES["power"], "value": False})
        elif command.command_type == CommandType.SET_TEMPERATURE:
            temp = params.get("temperature") or params.get("temp_set")
            if temp is not None:
                cmds.append({"code": self.DP_CODES["temp_set"], "value": int(float(temp) * 10)})
        elif command.command_type == CommandType.SET_MODE:
            mode = params.get("mode")
            if mode and mode in self.MODE_STD_TO_VENDOR:
                cmds.append({"code": self.DP_CODES["mode"], "value": self.MODE_STD_TO_VENDOR[mode]})
        elif command.command_type == CommandType.SET_FAN_SPEED:
            fan = params.get("fan_speed")
            if fan and fan in self.FAN_STD_TO_VENDOR:
                cmds.append(
                    {"code": self.DP_CODES["fan_speed"], "value": self.FAN_STD_TO_VENDOR[fan]}
                )
        elif command.command_type == CommandType.SET_SWING:
            swing = params.get("swing")
            if swing is not None:
                cmds.append({"code": self.DP_CODES["swing"], "value": bool(swing)})

        return cmds

    # ══════════════════════════════════════════════════════════════════════════════
    # STATE CONVERSION
    # ══════════════════════════════════════════════════════════════════════════════

    async def _state_to_response(self, state: DeviceState) -> dict[str, Any]:
        """Convert standard state to DP format."""
        dps = {}

        if state.on_off is not None:
            dps[self.DP_CODES["power"]] = state.on_off
            if state.mode and state.mode in self.MODE_STD_TO_VENDOR:
                dps[self.DP_CODES["mode"]] = self.MODE_STD_TO_VENDOR[state.mode]
        if state.temp_target is not None:
            dps[self.DP_CODES["temp_set"]] = int(state.temp_target * 10)
        if state.temp_actual is not None:
            dps[self.DP_CODES["temp_current"]] = int(state.temp_actual * 10)
            if state.fan_speed and state.fan_speed in self.FAN_STD_TO_VENDOR:
                dps[self.DP_CODES["fan_speed"]] = self.FAN_STD_TO_VENDOR[state.fan_speed]
        if state.humidity is not None:
            dps[self.DP_CODES["humidity"]] = int(state.humidity)
        if state.temp_outdoor is not None:
            dps[self.DP_CODES["outdoor_temp"]] = int(state.temp_outdoor * 10)
        if state.power_watts is not None:
            dps[self.DP_CODES["power_consumption"]] = int(state.power_watts)

        return {"dps": dps}

    async def _response_to_state(self, data: dict[str, Any]) -> DeviceState:
        """Convert DP data to standard state."""
        dps = data.get("dps", data.get("data", {}))

        return DeviceState(
            device_id="",  # Set by caller
            timestamp=datetime.now(UTC),
            on_off=dps.get(self.DP_CODES["power"]),
            mode=self.MODE_VENDOR_TO_STD.get(dps.get(self.DP_CODES["mode"])),
            temp_target=dps.get(self.DP_CODES["temp_set"], 0) / 10
            if self.DP_CODES["temp_set"] in dps
            else None,
            temp_actual=dps.get(self.DP_CODES["temp_current"], 0) / 10
            if self.DP_CODES["temp_current"] in dps
            else None,
            fan_speed=self.FAN_VENDOR_TO_STD.get(dps.get(self.DP_CODES["fan_speed"])),
            humidity=dps.get(self.DP_CODES["humidity"]),
            temp_outdoor=dps.get(self.DP_CODES["outdoor_temp"], 0) / 10
            if self.DP_CODES["outdoor_temp"] in dps
            else None,
            power_watts=dps.get(self.DP_CODES["power_consumption"]),
            vendor_data=data,
        )

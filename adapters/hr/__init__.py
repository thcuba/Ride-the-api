"""
HR (Haier) Protocol Adapter - Stub Implementation.
Haier Smart Home (including Candy, Hoover, GE Appliances).

References:
- Haier U+ Smart Life API (private)
- MQTT-based communication
"""

from __future__ import annotations

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


class HRProtocolAdapter(ProtocolAdapter):
    """HR (Haier) smart HVAC protocol adapter."""
    
    VENDOR_HOSTNAMES = [
        "api.haier.com",
        "api.haier.net",
        "mqtt.haier.com",
        "uplus.haier.com",
        "smartlife.haier.com",
    ]
    
    def __init__(self, vendor: str, config: dict[str, Any]):
        super().__init__(vendor, config)
    
    @property
    def supported_protocols(self) -> list[ProtocolType]:
        return [ProtocolType.HTTPS, ProtocolType.HTTP, ProtocolType.MQTT, ProtocolType.MQTTS]
    
    @property
    def vendor_hostnames(self) -> list[str]:
        return self.VENDOR_HOSTNAMES
    
    async def parse_request(self, request: InterceptedRequest) -> InterceptedRequest:
        request.parsed_intent = CommandType.UNKNOWN
        return request
    
    async def handle_request(self, request: InterceptedRequest) -> CommandResult:
        return await self.forward_to_cloud(request)
    
    async def forward_to_cloud(self, request: InterceptedRequest) -> CommandResult:
        return CommandResult(
            success=False,
            error="Haier cloud forward not implemented",
            forwarded=True,
        )
    
    async def build_response(self, request: InterceptedRequest, result: CommandResult) -> dict[str, Any]:
        return {"success": result.success, "error": result.error}
    
    async def get_device_info(self, device_id: str) -> DeviceInfo | None:
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
                DeviceCapability.SELF_CLEAN,
                DeviceCapability.SCHEDULING,
            ],
        )
    
    async def get_device_state(self, device_id: str) -> DeviceState | None:
        return None
    
    async def send_command(self, device_id: str, command: Command) -> CommandResult:
        return CommandResult(success=False, error="Not implemented")
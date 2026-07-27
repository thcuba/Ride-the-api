"""
ZH (Zehnder) Protocol Adapter - Stub Implementation.
Zehnder ComfoAir / ComfoControl ventilation systems.

References:
- Zehnder Cloud API (private)
- Modbus RTU/TCP for local control
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


class ZHProtocolAdapter(ProtocolAdapter):
    """ZH (Zehnder) ventilation system protocol adapter."""
    
    VENDOR_HOSTNAMES = [
        "api.zehndercloud.com",
        "cloud.zehnder.com",
        "my.zehnder.com",
    ]
    
    def __init__(self, vendor: str, config: dict[str, Any]):
        super().__init__(vendor, config)
    
    @property
    def supported_protocols(self) -> list[ProtocolType]:
        return [ProtocolType.HTTPS, ProtocolType.HTTP, ProtocolType.TCPIP]  # Modbus
    
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
            error="Zehnder cloud forward not implemented",
            forwarded=True,
        )
    
    async def build_response(self, request: InterceptedRequest, result: CommandResult) -> dict[str, Any]:
        return {"success": result.success, "error": result.error}
    
    async def get_device_info(self, device_id: str) -> DeviceInfo | None:
        return DeviceInfo(
            device_id=device_id,
            vendor=self.vendor,
            device_type="ventilator",
            capabilities=[
                DeviceCapability.TEMPERATURE_CONTROL,
                DeviceCapability.FAN_SPEED_CONTROL,
                DeviceCapability.HEAT_RECOVERY,
                DeviceCapability.AIR_QUALITY_SENSOR,
                DeviceCapability.HUMIDITY_SENSOR,
            ],
        )
    
    async def get_device_state(self, device_id: str) -> DeviceState | None:
        return None
    
    async def send_command(self, device_id: str, command: Command) -> CommandResult:
        return CommandResult(success=False, error="Not implemented")
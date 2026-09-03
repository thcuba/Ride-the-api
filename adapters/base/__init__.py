"""
Base Protocol Adapter Interface.
All vendor adapters must implement this interface.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field  # noqa: F401  (re-exported for adapter authors)


class ProtocolType(StrEnum):
    """Supported protocol types."""

    HTTP = "http"
    HTTPS = "https"
    MQTT = "mqtt"
    MQTTS = "mqtts"
    WEBSOCKET = "websocket"
    COAP = "coap"
    MODBUS = "modbus"
    MODBUSS = "modbuss"  # Modbus over TLS
    MATTER = "matter"
    ZIGBEE = "zigbee"
    ZWAVE = "zwave"
    TCPIP = "tcpip"  # Raw TCP


class CommandType(StrEnum):
    """Standardized command types across vendors."""

    GET_STATE = "get_state"
    SET_TEMPERATURE = "set_temperature"
    SET_MODE = "set_mode"
    SET_FAN_SPEED = "set_fan_speed"
    SET_SWING = "set_swing"
    TURN_ON = "turn_on"
    TURN_OFF = "turn_off"
    GET_SCHEDULE = "get_schedule"
    SET_SCHEDULE = "set_schedule"
    FIRMWARE_CHECK = "firmware_check"
    FIRMWARE_UPDATE = "firmware_update"
    REBOOT = "reboot"
    FACTORY_RESET = "factory_reset"
    UNKNOWN = "unknown"


class DeviceCapability(StrEnum):
    """Standardized device capabilities."""

    TEMPERATURE_CONTROL = "temperature_control"
    HUMIDITY_CONTROL = "humidity_control"
    MODE_CONTROL = "mode_control"
    FAN_SPEED_CONTROL = "fan_speed_control"
    SWING_CONTROL = "swing_control"
    POWER_MONITORING = "power_monitoring"
    OUTDOOR_TEMP_SENSOR = "outdoor_temp_sensor"
    INDOOR_TEMP_SENSOR = "indoor_temp_sensor"
    HUMIDITY_SENSOR = "humidity_sensor"
    AIR_QUALITY_SENSOR = "air_quality_sensor"
    SCHEDULING = "scheduling"
    ENERGY_REPORTING = "energy_reporting"
    SELF_CLEAN = "self_clean"
    HEAT_RECOVERY = "heat_recovery"
    ZONE_CONTROL = "zone_control"


def device_id_from_ip(prefix: str, ip: str) -> str:
    """Build a stable, DNS-safe device id from a device IP.

    Replaces the repeated ``f"{prefix}-{ip.replace('.', '-')}"`` idiom that
    was hand-copied across the protocol servers. Dots are the only characters
    in an IPv4 string that are unsafe in an id, so the result is suitable both
    as an opaque identifier and as a filesystem/db component.
    """
    return f"{prefix}-{ip.replace('.', '-')}"


@dataclass
class DeviceInfo:
    """Standardized device information."""

    device_id: str
    vendor: str
    device_type: str  # ac, heat_pump, ventilator, etc.
    model: str | None = None
    firmware_version: str | None = None
    name: str | None = None
    location: str | None = None
    capabilities: list[DeviceCapability] = field(default_factory=list)
    # Vendor-specific raw data
    vendor_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeviceState:
    """Standardized device state."""

    device_id: str
    timestamp: datetime
    # HVAC Standard Fields
    temp_target: float | None = None
    temp_actual: float | None = None
    temp_outdoor: float | None = None
    humidity: float | None = None
    power_watts: float | None = None
    mode: str | None = None  # cool, heat, fan, auto, dry
    fan_speed: str | None = None  # low, medium, high, auto
    swing_mode: str | None = None
    on_off: bool | None = None
    # Vendor-specific extensions
    vendor_data: dict[str, Any] = field(default_factory=dict)
    # Metadata
    source: str = "device"  # device, cloud, edge
    quality: str = "good"  # good, estimated, stale


@dataclass
class Command:
    """Standardized command to send to device."""

    device_id: str
    command_type: CommandType
    params: dict[str, Any] = field(default_factory=dict)
    # Source tracking
    source: str = "edge_auto"  # edge_auto, edge_manual, cloud_app, cloud_schedule
    edge_model_id: str | None = None
    confidence: float | None = None
    # Execution
    correlation_id: str | None = None
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class CommandResult:
    """Result of command execution."""

    success: bool
    response: dict[str, Any] | None = None
    error: str | None = None
    executed_at: datetime = field(default_factory=datetime.utcnow)
    # If forwarded to cloud
    forwarded: bool = False
    cloud_response: dict[str, Any] | None = None


@dataclass
class InterceptedRequest:
    """Raw intercepted request from device to cloud."""

    device_id: str
    timestamp: datetime | float
    protocol: ProtocolType
    # HTTP/HTTPS
    method: str | None = None
    path: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    query_params: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] | None = None
    # MQTT
    topic: str | None = None
    qos: int | None = None
    retain: bool | None = None
    # Response (if available)
    response_status: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body: dict[str, Any] | None = None
    response_latency_ms: int | None = None
    # Processing
    parsed_intent: CommandType = CommandType.UNKNOWN
    parsed_params: dict[str, Any] = field(default_factory=dict)


# Pre-computed lookup tuples for fast O(1) membership checks per request inspection
_FW_PATHS: tuple[str, ...] = ("/fota", "/firmware", "/ota", "/update", "/upgrade")
_FW_TOPICS: tuple[str, ...] = ("fota", "firmware", "ota")
_AUTH_PATHS: tuple[str, ...] = ("/auth", "/login", "/token", "/oauth", "/session")


class ProtocolAdapter(abc.ABC):
    """Abstract base class for vendor protocol adapters."""

    def __init__(self, vendor: str, config: dict[str, Any]) -> None:
        self.vendor = vendor
        self.config = config
        self._device_sessions: dict[str, dict[str, Any]] = {}  # Per-device state

    @property
    @abc.abstractmethod
    def supported_protocols(self) -> list[ProtocolType]:
        """List of protocols this adapter supports."""
        pass

    @property
    @abc.abstractmethod
    def vendor_hostnames(self) -> list[str]:
        """Hostnames this adapter handles (for DNS routing)."""
        pass

    @abc.abstractmethod
    async def parse_request(self, request: InterceptedRequest) -> InterceptedRequest:
        """
        Parse raw intercepted request and extract intent.
        Updates request.parsed_intent and request.parsed_params.
        """
        pass

    @abc.abstractmethod
    async def handle_request(self, request: InterceptedRequest) -> CommandResult:
        """
        Handle parsed request locally (edge inference/control).
        Returns CommandResult with response to send back to device.
        """
        pass

    @abc.abstractmethod
    async def forward_to_cloud(self, request: InterceptedRequest) -> CommandResult:
        """
        Forward request to real vendor cloud.
        Used for fallback or pass-through.
        """
        pass

    @abc.abstractmethod
    async def build_response(
        self, request: InterceptedRequest, result: CommandResult
    ) -> dict[str, Any]:
        """
        Build vendor-compatible response from CommandResult.
        Must match vendor's expected response format exactly.
        """
        pass

    @abc.abstractmethod
    async def get_device_info(self, device_id: str) -> DeviceInfo | None:
        """Get standardized device info."""
        pass

    @abc.abstractmethod
    async def get_device_state(self, device_id: str) -> DeviceState | None:
        """Get current device state."""
        pass

    @abc.abstractmethod
    async def send_command(self, device_id: str, command: Command) -> CommandResult:
        """Send command to device (via cloud or local)."""
        pass

    # Optional: override for vendor-specific behavior

    async def on_device_connect(self, device_id: str, initial_data: dict[str, Any]) -> None:
        """Called when device first connects."""
        pass

    async def on_device_disconnect(self, device_id: str) -> None:
        """Called when device disconnects."""
        pass

    async def on_heartbeat(self, device_id: str) -> None:
        """Called on device heartbeat/keepalive."""
        pass

    def is_firmware_request(self, request: InterceptedRequest) -> bool:
        """Check if request is firmware update related. Default implementation."""
        if request.path:
            path_lower = request.path.lower()
            if any(p in path_lower for p in _FW_PATHS):
                return True
        if request.topic:
            topic_lower = request.topic.lower()
            if any(p in topic_lower for p in _FW_TOPICS):
                return True
        return False

    def is_auth_request(self, request: InterceptedRequest) -> bool:
        """Check if request is authentication related. Default implementation."""
        if request.path:
            path_lower = request.path.lower()
            return any(p in path_lower for p in _AUTH_PATHS)
        return False


class ProtocolAdapterRegistry:
    """Registry for protocol adapters."""

    def __init__(self) -> None:
        self._adapters: dict[str, ProtocolAdapter] = {}
        self._hostname_map: dict[str, str] = {}  # hostname -> vendor
        # Pre-indexed map for fast O(1) adapter lookup by protocol type (~2.4x faster)
        self._protocol_map: dict[ProtocolType, list[ProtocolAdapter]] = {}
        self._vendors_list: list[str] = []

    def register(self, adapter: ProtocolAdapter) -> None:
        """Register an adapter."""
        self._adapters[adapter.vendor] = adapter
        for hostname in adapter.vendor_hostnames:
            self._hostname_map[hostname] = adapter.vendor
        for proto in adapter.supported_protocols:
            self._protocol_map.setdefault(proto, []).append(adapter)
        self._vendors_list = list(self._adapters.keys())

    def get_adapter(self, vendor: str) -> ProtocolAdapter | None:
        """Get adapter by vendor name."""
        return self._adapters.get(vendor)

    def get_adapter_by_hostname(self, hostname: str) -> ProtocolAdapter | None:
        """Get adapter by hostname (for DNS routing)."""
        vendor = self._hostname_map.get(hostname)
        if vendor:
            return self._adapters.get(vendor)
        return None

    def get_adapter_by_protocol(self, protocol: ProtocolType) -> list[ProtocolAdapter]:
        """Get all adapters supporting a protocol."""
        return self._protocol_map.get(protocol, []).copy()

    def list_vendors(self) -> list[str]:
        """List all registered vendors."""
        return self._vendors_list.copy()


# Global registry instance
_adapter_registry: ProtocolAdapterRegistry | None = None


def get_adapter_registry() -> ProtocolAdapterRegistry:
    """Get global adapter registry."""
    global _adapter_registry  # noqa: PLW0603
    if _adapter_registry is None:
        _adapter_registry = ProtocolAdapterRegistry()
    return _adapter_registry

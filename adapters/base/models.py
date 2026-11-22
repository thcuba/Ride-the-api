"""Standardized data structures, enums, and request/command models for adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.pattern_db.schemas import ObservationKind, TransportMeta


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
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class CommandResult:
    """Result of command execution."""

    success: bool
    response: dict[str, Any] | None = None
    error: str | None = None
    executed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # If forwarded to cloud
    forwarded: bool = False
    cloud_response: dict[str, Any] | None = None
    # Upstream response, preserved verbatim when the request was forwarded.
    # A proxy must replay the conversation as it happened: dropping the status
    # turns a vendor 401/429/500 into a fake "200 OK" for the device, and
    # dropping the headers loses cookies, retry hints and content negotiation.
    status_code: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    # Raw upstream body bytes. ``response`` holds the JSON-decoded view (for
    # parsing/learning); ``body_bytes`` is what actually goes back on the wire,
    # so non-JSON payloads survive byte-for-byte.
    body_bytes: bytes | None = None


@dataclass
class InterceptedRequest:
    """Raw intercepted request from device to cloud."""

    device_id: str
    timestamp: datetime | float
    protocol: ProtocolType
    # Source address when the protocol server can observe it (TCP/CoAP/WS/HTTP2).
    # ``None`` for protocols without a source IP (MQTT by client_id, Modbus by
    # unit, Zigbee/Matter/Z-Wave). Used for per-IP bypass and routing decisions.
    client_ip: str | None = None
    # HTTP/HTTPS
    method: str | None = None
    path: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    query_params: dict[str, str] = field(default_factory=dict)
    # The request target exactly as received (``/path?query``, still
    # percent-encoded). ``path``/``query_params`` are decoded views for
    # matching and learning; a forward replays this instead, so "%3F" stays
    # "%3F" and repeated query keys stay repeated.
    raw_target: str | None = None
    # Parsed body: dict (JSON), str (text) or None. Raw wire bytes live in
    # ``body_bytes`` below, never here.
    body: dict[str, Any] | str | None = None
    # Raw device body bytes, as received on the wire. ``body`` is the parsed
    # JSON view used for analysis/learning; ``body_bytes`` is what a forward
    # must replay so non-JSON payloads (XML, form data, protobuf, raw bytes)
    # are not destroyed by re-serialising them as JSON.
    body_bytes: bytes | None = None
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
    # Observation enrichment (D2). Populated by protocol servers so the
    # buffer/observation layer can build a v2 Observation without re-deriving
    # transport/security/identity/kind from the request.
    transport: TransportMeta | None = None
    security: str | None = None  # none | tls | dtls | mqtts | ...
    identity: str | None = None  # device identity hint when derivable
    kind: ObservationKind | None = None  # request | publish | event | frame | ...
    scheme: str | None = None  # http | https | ws | wss (from HTTP/2 :scheme)
    # Modification engine tracking
    metadata: dict[str, Any] = field(default_factory=dict)
    modifications: list[dict[str, Any]] = field(default_factory=list)
    blocked: bool = False
    block_reason: str | None = None

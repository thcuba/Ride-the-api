"""
Portable Pattern Database — Pydantic schemas for .ride-capture.json and .ride-pattern.json.

These models define the portable, LLM-agnostic format for sharing device protocol
patterns between users and across different hardware.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# ═══════════════════════════════════════════════════════════════════════════════
# BUFFER DB — .ride-capture.json  (da decifrare)
# ═══════════════════════════════════════════════════════════════════════════════


class CaptureMeta(BaseModel):
    """Metadata for a raw capture file."""

    version: int = 1
    capture_id: str
    vendor: str
    device_type: str
    model: str = ""
    firmware_version: str = ""
    capture_date: datetime
    description: str = ""


class CaptureDeviceInfo(BaseModel):
    """Obfuscated device info for sharing."""

    device_id: str = "obfuscated"
    mac: str = "obfuscated"
    serial: str = "obfuscated"


class RawPair(BaseModel):
    """A single raw intercepted request/response pair."""

    pair_id: str
    timestamp: datetime
    protocol: str = "http"
    method: str = ""
    path: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    query_params: dict[str, str] = Field(default_factory=dict)
    body: Any = None


class RawResponse(BaseModel):
    """Response half of a raw pair."""

    status_code: int = 0
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    latency_ms: float = 0.0


class RawPairWithResponse(BaseModel):
    """A raw pair bundled with its response."""

    pair_id: str
    timestamp: datetime
    protocol: str = "http"
    method: str = ""
    path: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    query_params: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    response: RawResponse | None = None


class CaptureSession(BaseModel):
    """A logical session grouping related pairs (boot, command, polling...)."""

    session_id: str
    type: str = "capture"
    timestamp_start: datetime
    pairs: list[RawPairWithResponse] = Field(default_factory=list)


class CaptureDB(BaseModel):
    """Root model for .ride-capture.json — raw buffer export."""

    schema_url: str = Field(
        "https://ride-the-api.dev/capture-schema/v1",
        alias="$schema",
    )
    meta: CaptureMeta
    device_info: CaptureDeviceInfo = Field(default_factory=CaptureDeviceInfo)
    sessions: list[CaptureSession] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


# ═══════════════════════════════════════════════════════════════════════════════
# DECIPHERED DB — .ride-pattern.json  (decifrato)
# ═══════════════════════════════════════════════════════════════════════════════


class PatternMeta(BaseModel):
    """Metadata for a deciphered pattern file."""

    version: int = 1
    pattern_id: str
    vendor: str
    device_type: str
    model: str = ""
    firmware_versions: list[str] = Field(default_factory=list)
    description: str = ""


class AuthConfig(BaseModel):
    """Authentication info for the device."""

    type: str = "bearer"
    token_endpoint: str = ""
    credentials: str = "stored_externally"


class EndpointVariant(BaseModel):
    """A variant of an endpoint for different firmware/hardware versions."""

    firmware: str = "*"
    path: str | None = None
    body_schema: dict[str, Any] | None = None


class BodySchemaProperty(BaseModel):
    """A single property in a JSON Schema body."""

    type: str = "string"
    description: str = ""
    enum: list[str] | None = None
    const: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    properties: dict[str, BodySchemaProperty] | None = None
    items: BodySchemaProperty | None = None
    required: list[str] | None = None
    min_items: int | None = None


class ClientEndpoint(BaseModel):
    """A single endpoint the device calls."""

    id: str
    intent: str
    method: str = "GET"
    path: str = ""
    path_pattern: str = ""
    headers: dict[str, list[str]] = Field(default_factory=lambda: {"required": []})
    query_params: list[str] = Field(default_factory=list)
    body_schema: dict[str, Any] | None = None
    response_fields: list[dict[str, str]] = Field(default_factory=list)
    variants: list[EndpointVariant] = Field(default_factory=list)


class ClientConfig(BaseModel):
    """Client section — describes what the device sends to the cloud."""

    protocols: list[str] = Field(default_factory=lambda: ["http"])
    base_url: str = ""
    mqtt_topic_prefix: str = ""
    authentication: AuthConfig | None = None
    endpoints: list[ClientEndpoint] = Field(default_factory=list)


class StateVariable(BaseModel):
    """A persistent state variable for the simulated device."""

    name: str
    type: str = "string"
    default: Any = None
    min: float | None = None
    max: float | None = None
    enum: list[str] | None = None
    unit: str = ""
    persist: bool = True
    description: str = ""


class FieldMapping(BaseModel):
    """Maps a request field to a response field or state variable."""

    source: str = ""
    target: str = ""
    transform: str = "direct"
    formula: str = ""
    mapping: dict[str, Any] | None = None
    description: str = ""


class ServerResponse(BaseModel):
    """A response template triggered by a client endpoint."""

    id: str
    triggers: list[str] = Field(default_factory=list)
    status_code: int = 200
    headers_template: dict[str, str] = Field(default_factory=dict)
    body_template: dict[str, Any] = Field(default_factory=dict)
    field_mappings: list[FieldMapping] = Field(default_factory=list)


class VirtualSensor(BaseModel):
    """A simulated sensor that generates realistic data."""

    name: str
    type: str = "integer"
    behavior: str = "static"
    baseline: str = ""
    drift_range: list[float] | None = None
    period_s: float = 0
    amplitude: float | None = None
    update_interval_s: float = 60
    description: str = ""


class ServerConfig(BaseModel):
    """Server section — describes what the proxy should respond."""

    state_variables: list[StateVariable] = Field(default_factory=list)
    responses: list[ServerResponse] = Field(default_factory=list)
    virtual_sensors: list[VirtualSensor] = Field(default_factory=list)


class PatternDB(BaseModel):
    """Root model for .ride-pattern.json — deciphered protocol patterns."""

    schema_url: str = Field(
        "https://ride-the-api.dev/pattern-schema/v1",
        alias="$schema",
    )
    meta: PatternMeta
    client: ClientConfig = Field(default_factory=ClientConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)

    model_config = {"populate_by_name": True}


# ═══════════════════════════════════════════════════════════════════════════════
# DEVICE META — per-device header written at first LLM flush
# ═══════════════════════════════════════════════════════════════════════════════


class DeviceMeta(BaseModel):
    """Per-device header persisted in the device DB.

    Written once at the first LLM flush, following the same ``meta`` +
    ``protocols`` conventions as the portable PatternDB so the stored header
    is structured, common and readable in clear text. After it is set it is
    stable: later flushes do not re-derive the protocol.

    ``connection_mode`` is the operational ingress decision that the server
    reads to pick the right handler / routing (``auto`` = decided at the first
    flush). ``protocols`` mirrors ``ClientConfig.protocols`` for the same
    device as a cross-check of what it actually speaks.
    """

    version: int = 1
    vendor: str = "unknown"
    device_type: str = "unknown"
    model: str = ""
    protocols: list[str] = Field(default_factory=lambda: [""])
    connection_mode: str = "auto"  # auto | tls | http | mqtt | coap | modbus
    detected_at: datetime | None = None
    source: str = "llm"  # llm | config_override
    # Full ProtocolInfo from the first-flush mode="auto" identification, kept in
    # the persisted header so export_device_model can rebuild the portable
    # ProtocolInfo losslessly (transport/security/proprietary/identity/ports/
    # confidence are otherwise defaulted on export).
    transport: str = ""
    security: str = ""
    proprietary: bool = False
    identity: str = ""  # vendor/model identity derived by the LLM (also mirrors ``model``)
    ports: list[int] = Field(default_factory=list)
    confidence: float = 0.0
    # Canonical home for behavioural config that has no SQL table. Persisted so
    # export recovers it even without the engine's in-memory applied PatternDB
    # (which is the only other place state_variables/virtual_sensors live).
    state_variables: list[StateVariable] = Field(default_factory=list)
    virtual_sensors: list[VirtualSensor] = Field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════════
# DEVICE MODEL — v2 portable knowledge of a device (schema_url pattern-schema/v2)
#
# These models are ADDITIVE: they reuse the existing building blocks
# (PatternMeta, AuthConfig, StateVariable, VirtualSensor, FieldMapping,
# ServerResponse) instead of redefining them, so v1 .ride-pattern.json data
# keeps parsing unchanged. They add the pieces the current model lacks:
#   * Observation — a generic uni-directional traffic event (request,
#     response, MQTT publish, event, Modbus frame, telemetry…) that no longer
#     assumes a request/response pair.
#   * ProtocolInfo — the identification result produced by the first LLM flush
#     in mode="auto": transport, protocol, security, standard/proprietary,
#     suggested handler and confidence.
#   * DeviceModel — the root portable record ("what is this device and how
#     does it behave") intended to become the new .ride-pattern.json; it is
#     sufficient to clone a device on another installation without re-learning.
#
# Observation and DeviceModel are exactly the two concepts proposed: raw
# traffic observations stay out of the model (.ride-capture.json) and only the
# learned knowledge is exported as a DeviceModel (.ride-pattern.json v2).
# ═══════════════════════════════════════════════════════════════════════════════


class ObservationKind(StrEnum):
    """Direction/semantics of a raw traffic observation.

    Intentionally broader than a request/response pair: MQTT publish, events,
    WebSocket messages, Modbus frames, telemetry and last-will are all
    uni-directional and should not be forced into a ``CorrelatedPair``.
    """

    REQUEST = "request"
    RESPONSE = "response"
    EVENT = "event"
    PUBLISH = "publish"
    SUBSCRIBE = "subscribe"
    FRAME = "frame"
    TELEMETRY = "telemetry"
    WILL = "will"
    KEEPALIVE = "keepalive"


class TransportMeta(BaseModel):
    """Transport/connection details of a raw observation (protocol-agnostic).

    Fields that don't apply to a protocol are simply left as their default, so
    the same model carries HTTP, MQTT, CoAP, Modbus and WebSocket metadata.
    """

    port: int = 0
    tls: bool = False  # TLS/DTLS encrypted
    topic: str = ""  # MQTT / CoAP observe
    qos: int | None = None  # MQTT
    retain: bool | None = None  # MQTT
    func_code: int | None = None  # Modbus
    reg_address: int | None = None  # Modbus (Coils / Holding Registers)


class Observation(BaseModel):
    """A single raw, uni-directional traffic observation captured from a device.

    This is the buffer/raw-capture unit that generalises :class:`CorrelatedPair`
    without assuming a request/response shape. It is the payload of
    ``.ride-capture.json`` and the input to the LLM on flush. It is NOT part of
    the learned ``DeviceModel``.
    """

    id: str
    device_id: str
    timestamp: datetime
    protocol: str = "http"
    kind: ObservationKind = ObservationKind.REQUEST
    transport: TransportMeta = Field(default_factory=TransportMeta)
    content: Any = None
    in_reply_to: str | None = None  # id of the observation this correlates with (optional)
    confidence: float = 0.0  # correlation/processing confidence


class ProtocolInfo(BaseModel):
    """Identification result of the first LLM flush in ``mode="auto"``.

    Carries everything the LLM can determine at first contact: the protocol
    and how it should be handled. ``proprietary`` flags protocols that are NOT
    a known standard (Modbus/MQTT/CoAP/HTTP/WebSocket) so the runtime knows it
    may need raw Observation analysis instead of a standard protocol handler.
    ``confidence`` reflects how sure the LLM is about the whole identification.
    """

    version: int = 1
    transport: str = ""  # tcp | udp | websocket (empty if unknown)
    protocol: str = ""  # http | https | mqtt | coap | modbus | websocket | proprietary …
    proprietary: bool = False  # True when NOT a known standard
    security: str = ""  # none | tls | mqtts | dtls | …
    handler: str = "auto"  # suggested protocol server / MITM handler
    identity: str = ""  # vendor/model identity derived by the LLM, when any
    ports: list[int] = Field(default_factory=list)
    confidence: float = 0.0


class Command(BaseModel):
    """A device capability/action in protocol-agnostic form.

    Generalises ``ClientEndpoint``: a command is no longer bound to an HTTP
    path — it may be a GET, a publish on a MQTT topic, a CoAP PUT, a Modbus
    register write, a WebSocket send, etc. ``kind`` carries the semantic intent
    (get_state, set_temperature, turn_on, publish…) whether it came from an HTTP
    endpoint or was derived by the LLM from a proprietary protocol.
    """

    id: str
    kind: str  # get_state | set_temperature | publish | send | … (semantic intent)
    protocol: str = "http"
    method: str = "GET"
    path: str = ""  # HTTP path or CoAP path ("" for topic-only protocols)
    path_pattern: str = ""
    topic: str = ""  # MQTT / WebSocket topic
    headers: dict[str, list[str]] = Field(
        default_factory=lambda: {"required": []}
    )
    query_params: list[str] = Field(default_factory=list)
    body_schema: dict[str, Any] | None = None
    confidence: float = 0.5


class DeviceModel(BaseModel):
    """Root portable record describing a device ("what it is, how it behaves").

    Intended to become the new ``.ride-pattern.json`` (schema_url
    ``pattern-schema/v2``). It is the learned, portable DeviceModel that is
    sufficient to clone a device on another install without re-learning:
    identity (meta), identification (protocol), capabilities (commands),
    response templates + field mappings (responses/interactions) and simulated
    behaviour (state_variables, virtual_sensors).

    Reuses the existing v1 building blocks so v1 knowledge maps cleanly.
    """

    schema_url: str = Field(
        "https://ride-the-api.dev/pattern-schema/v2",
        alias="$schema",
    )
    meta: PatternMeta
    protocol: ProtocolInfo = Field(default_factory=ProtocolInfo)
    commands: list[Command] = Field(default_factory=list)
    responses: list[ServerResponse] = Field(default_factory=list)
    interactions: list[FieldMapping] = Field(default_factory=list)
    state_variables: list[StateVariable] = Field(default_factory=list)
    virtual_sensors: list[VirtualSensor] = Field(default_factory=list)
    # Learned traffic history. Grounding for extrapolating replies to requests
    # never seen before: near-duplicate observations give a plausible basis
    # when no exact Command matches. Kept out of the runtime critical path
    # (used only for synthesis) and portable across installations.
    observation_history: list[Observation] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    def to_pattern_db(self) -> PatternDB:
        """Map this v2 DeviceModel to a v1 PatternDB for the runtime engine.

        The runtime (:class:`core.pattern_db.pattern_engine.PatternEngine`)
        matches on the v1 ``PatternDB`` shape (client endpoints / server
        responses + state). v2 is a superset; this projection keeps the engine
        stable while the portable artifact carries the richer model. Lossless
        for the v1-equivalent sections: v1 has no home for ``protocol`` or
        ``observation_history``, so those are intentionally not projected.
        """
        endpoints = [
            ClientEndpoint(
                id=c.id,
                intent=c.kind,
                method=c.method or "GET",
                path=c.path or c.path_pattern or (c.topic or ""),
                path_pattern=c.path_pattern or c.path or (c.topic or ""),
                headers=c.headers or {"required": []},
                query_params=c.query_params or [],
                body_schema=c.body_schema,
            )
            for c in self.commands
        ]
        protocols = [self.protocol.protocol] if self.protocol.protocol else ["http"]
        return PatternDB(
            meta=self.meta,
            client=ClientConfig(protocols=protocols, endpoints=endpoints),
            server=ServerConfig(
                responses=self.responses,
                state_variables=self.state_variables,
                virtual_sensors=self.virtual_sensors,
            ),
        )


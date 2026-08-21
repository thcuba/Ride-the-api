"""
Portable Pattern Database — Pydantic schemas for .ride-capture.json and .ride-pattern.json.

These models define the portable, LLM-agnostic format for sharing device protocol
patterns between users and across different hardware.
"""

from __future__ import annotations

from datetime import datetime
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
    schema: str = Field(
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
    schema: str = Field(
        "https://ride-the-api.dev/pattern-schema/v1",
        alias="$schema",
    )
    meta: PatternMeta
    client: ClientConfig = Field(default_factory=ClientConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)

    model_config = {"populate_by_name": True}

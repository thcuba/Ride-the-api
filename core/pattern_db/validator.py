"""
Portable Pattern Database — JSON Schema validation for .ride-capture.json and .ride-pattern.json.

Validates incoming files against their JSON Schemas before import,
with protocol-aware checks for all supported protocols:
http, https, mqtt, coap, modbus, websocket, raw_tcp, http2, zigbee, zwave, matter.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema import ValidationError as JsonSchemaValidationError

from core.paths import resource_path

logger = logging.getLogger(__name__)


class ValidationError(Exception):
    """Raised when a capture/pattern file fails JSON Schema validation."""

    def __init__(
        self,
        result: ValidationResult | None = None,
        message: str = "",
        errors: list[str] | None = None,
    ) -> None:
        self.result = result
        if errors is not None:
            self.errors = errors
        elif result is not None:
            self.errors = result.errors
        else:
            self.errors = []
        super().__init__(message or f"Validation failed: {self.errors}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": False,
            "errors": self.errors,
            "warnings": self.result.warnings if self.result is not None else [],
        }


# Load schemas from bundled files (bundle-aware: works in source and PyInstaller)
_SCHEMA_DIR = resource_path("core/pattern_db/schemas")

_CAPTURE_SCHEMA: dict[str, Any] | None = None
_PATTERN_SCHEMA: dict[str, Any] | None = None


def _load_schema(name: str) -> dict[str, Any]:
    """Load a JSON Schema from the bundled schemas directory."""
    path = _SCHEMA_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"JSON Schema not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def get_capture_schema() -> dict[str, Any]:
    """Get the capture schema, loading it on first access."""
    global _CAPTURE_SCHEMA  # noqa: PLW0603
    if _CAPTURE_SCHEMA is None:
        _CAPTURE_SCHEMA = _load_schema("capture-schema-v1.json")
    return _CAPTURE_SCHEMA


def get_pattern_schema() -> dict[str, Any]:
    """Get the pattern schema, loading it on first access."""
    global _PATTERN_SCHEMA  # noqa: PLW0603
    if _PATTERN_SCHEMA is None:
        _PATTERN_SCHEMA = _load_schema("pattern-schema-v1.json")
    return _PATTERN_SCHEMA


# v2 (DeviceModel) schemas cached independently from v1.
_CAPTURE_SCHEMA_V2: dict[str, Any] | None = None
_PATTERN_SCHEMA_V2: dict[str, Any] | None = None


def get_pattern_schema_v2() -> dict[str, Any]:
    """Get the v2 DeviceModel schema (pattern-schema-v2.json), loading on first access."""
    global _PATTERN_SCHEMA_V2  # noqa: PLW0603
    if _PATTERN_SCHEMA_V2 is None:
        _PATTERN_SCHEMA_V2 = _load_schema("pattern-schema-v2.json")
    return _PATTERN_SCHEMA_V2


# ── Protocol-specific validation helpers ────────────────────────────────────

# Valid HTTP methods
HTTP_METHODS = {
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "HEAD",
    "OPTIONS",
}

# Valid MQTT verbs
MQTT_METHODS = {"publish", "subscribe", "unsubscribe", "connect"}

# Valid CoAP methods (RFC 7252)
COAP_METHODS = {"get", "post", "put", "delete"}

# Valid Modbus operations
MODBUS_METHODS = {"read", "write", "read_write"}

# Valid WebSocket verbs
WEBSOCKET_METHODS = {"send", "subscribe", "unsubscribe", "ping", "pong"}

# Valid Raw TCP verbs
RAW_TCP_METHODS = {"send", "receive"}

# Bridge protocols (go through external software)
BRIDGE_METHODS: dict[str, set[str]] = {
    "zigbee": {"report", "command", "bridge_event"},
    "zwave": {"report", "command", "wake_up"},
    "matter": {"invoke", "subscribe", "read", "write"},
}

PROTOCOL_METHODS: dict[str, set[str]] = {
    "http": HTTP_METHODS,
    "https": HTTP_METHODS,
    "http2": HTTP_METHODS,
    "mqtt": MQTT_METHODS,
    "coap": COAP_METHODS,
    "modbus": MODBUS_METHODS,
    "websocket": WEBSOCKET_METHODS,
    "raw_tcp": RAW_TCP_METHODS,
    **BRIDGE_METHODS,
}

# Pre-computed uppercase sets for fast O(1) membership checking per pair check
PROTOCOL_METHODS_UPPER: dict[str, set[str]] = {
    p: {m.upper() for m in methods} for p, methods in PROTOCOL_METHODS.items()
}

ALL_PROTOCOLS = set(PROTOCOL_METHODS.keys())


def _validate_method_for_protocol(protocol: str, method: str, _path: str) -> list[str]:
    """Check that the method is valid for the given protocol."""
    errors: list[str] = []
    valid = PROTOCOL_METHODS.get(protocol)
    valid_upper = PROTOCOL_METHODS_UPPER.get(protocol)
    if valid and valid_upper and method and method.upper() not in valid_upper:
        errors.append(
            f"Method '{method}' is not valid for protocol '{protocol}'. "
            f"Expected one of: {', '.join(sorted(valid))}"
        )
    return errors


def _validate_path_for_protocol(protocol: str, path: str) -> list[str]:
    """Check that the path format is sensible for the protocol."""
    errors: list[str] = []
    if protocol in ("http", "https", "http2") and path and not path.startswith("/"):
        errors.append(f"Path '{path}' should start with '/' for protocol '{protocol}'")
    if protocol == "modbus" and path:
        try:
            int(path)
        except ValueError:
            errors.append(
                f"Path '{path}' should be a numeric register address for protocol 'modbus'"
            )
    return errors


# ── Validation result ──────────────────────────────────────────────────────


class ValidationResult:
    """Result of a JSON Schema validation."""

    def __init__(
        self, valid: bool = True, errors: list[str] | None = None, warnings: list[str] | None = None
    ) -> None:
        self.valid = valid
        self.errors = errors or []
        self.warnings = warnings or []

    def __bool__(self) -> bool:
        return self.valid

    def __repr__(self) -> str:
        return (
            f"ValidationResult(valid={self.valid}, errors={self.errors}, warnings={self.warnings})"
        )

    def merge(self, other: ValidationResult) -> ValidationResult:
        """Merge another result into this one."""
        self.valid = self.valid and other.valid
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
        }


# ── Main validation functions ──────────────────────────────────────────────


def validate_capture(data: dict[str, Any]) -> ValidationResult:
    """
    Validate a .ride-capture.json dictionary against the capture schema.

    Includes protocol-aware checks (method, path validity per protocol).
    """
    result = ValidationResult()
    schema = get_capture_schema()

    # JSON Schema validation
    try:
        jsonschema.validate(data, schema)
    except JsonSchemaValidationError as e:
        result.valid = False
        result.errors.append(f"Schema validation failed: {e.message}")
        return result

    # Protocol-aware validation per pair
    sessions = data.get("sessions", [])
    for session in sessions:
        session_id = session.get("session_id", "?")
        for idx, pair in enumerate(session.get("pairs", [])):
            protocol = pair.get("protocol", "http")
            method = pair.get("method", "")
            path = pair.get("path", "")

            # Method validation
            result.errors.extend(
                f"Session '{session_id}', pair [{idx}]: {e}"
                for e in _validate_method_for_protocol(protocol, method, path)
            )

            # Path validation
            result.errors.extend(
                f"Session '{session_id}', pair [{idx}]: {e}"
                for e in _validate_path_for_protocol(protocol, path)
            )

    if result.errors:
        result.valid = False

    return result


def validate_pattern(data: dict[str, Any]) -> ValidationResult:  # noqa: C901, PLR0912
    """
    Validate a .ride-pattern.json dictionary against the pattern schema.

    Includes:
    - JSON Schema conformance
    - Protocol consistency checks
    - Cross-reference warnings (e.g. endpoint triggers without matching response)
    """
    result = ValidationResult()
    schema = get_pattern_schema()

    # JSON Schema validation
    try:
        jsonschema.validate(data, schema)
    except JsonSchemaValidationError as e:
        result.valid = False
        result.errors.append(f"Schema validation failed: {e.message}")
        return result

    client = data.get("client", {})
    server = data.get("server", {})
    protocols = client.get("protocols", ["http"])

    # Check for unknown protocols
    for p in protocols:
        if p not in ALL_PROTOCOLS:
            result.warnings.append(f"Unknown protocol '{p}' in client.protocols")

    # MQTT topic prefix check
    if "mqtt" in protocols and not client.get("mqtt_topic_prefix"):
        result.warnings.append("Protocol 'mqtt' is declared but no 'mqtt_topic_prefix' is set")

    # Endpoint cross-checks
    endpoint_intents = {ep.get("id"): ep.get("intent") for ep in client.get("endpoints", [])}
    response_triggers = set()
    for resp in server.get("responses", []):
        for trigger in resp.get("triggers", []):
            response_triggers.add(trigger)

    for ep in client.get("endpoints", []):
        intent = ep.get("intent", "")
        ep_id = ep.get("id", "?")
        if intent and intent not in response_triggers:
            result.warnings.append(
                f"Endpoint '{ep_id}' has intent '{intent}' but no server response triggers it"
            )

    # Check for orphaned responses (no matching endpoint)
    for resp in server.get("responses", []):
        for trigger in resp.get("triggers", []):
            if trigger not in endpoint_intents.values():
                result.warnings.append(
                    f"Response '{resp.get('id', '?')} triggers intent '{trigger}' "
                    f"but no client endpoint has that intent"
                )

    # Virtual sensor baseline references
    state_var_names = {sv.get("name") for sv in server.get("state_variables", [])}
    for vs in server.get("virtual_sensors", []):
        baseline = vs.get("baseline", "")
        if baseline.startswith("{state."):
            ref = baseline[7:-1]
            if ref not in state_var_names:
                result.warnings.append(
                    f"Virtual sensor '{vs.get('name', '?')}' references state variable "
                    f"'{ref}' which is not defined"
                )

    if result.errors:
        result.valid = False

    return result


def validate_capture_file(filepath: str) -> ValidationResult:
    """Load and validate a .ride-capture.json file from disk."""
    path = Path(filepath)
    if not path.exists():
        return ValidationResult(valid=False, errors=[f"File not found: {filepath}"])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return ValidationResult(valid=False, errors=[f"Invalid JSON: {e}"])
    return validate_capture(data)


def validate_pattern_file(filepath: str) -> ValidationResult:
    """Load and validate a .ride-pattern.json file from disk."""
    path = Path(filepath)
    if not path.exists():
        return ValidationResult(valid=False, errors=[f"File not found: {filepath}"])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return ValidationResult(valid=False, errors=[f"Invalid JSON: {e}"])
    return validate_pattern(data)

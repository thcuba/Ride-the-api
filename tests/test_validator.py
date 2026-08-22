"""
Tests for the JSON Schema validation of portable pattern database files.
"""

import json

import pytest

from core.pattern_db.validator import (
    ValidationError,
    get_capture_schema,
    get_pattern_schema,
    validate_capture,
    validate_capture_file,
    validate_pattern,
    validate_pattern_file,
)

# ═══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def valid_capture() -> dict:
    return {
        "$schema": "https://ride-the-api.dev/capture-schema/v1",
        "meta": {
            "version": 1,
            "capture_id": "acme-ac-capture-2024-12",
            "vendor": "acme",
            "device_type": "ac",
            "capture_date": "2024-12-15T10:00:00Z",
        },
        "device_info": {"device_id": "obfuscated"},
        "sessions": [
            {
                "session_id": "boot_001",
                "type": "boot",
                "timestamp_start": "2024-12-15T10:00:00Z",
                "pairs": [
                    {
                        "pair_id": "pair_001",
                        "timestamp": "2024-12-15T10:00:01Z",
                        "protocol": "http",
                        "method": "POST",
                        "path": "/v1.0/auth/login",
                        "headers": {"Content-Type": "application/json"},
                        "response": {
                            "status_code": 200,
                            "body": {"access_token": "eyJ..."},
                            "latency_ms": 234,
                        },
                    }
                ],
            }
        ],
    }


@pytest.fixture
def valid_pattern() -> dict:
    return {
        "$schema": "https://ride-the-api.dev/pattern-schema/v1",
        "meta": {
            "version": 1,
            "pattern_id": "acme-ac-smartcool-v3",
            "vendor": "acme",
            "device_type": "ac",
        },
        "client": {
            "protocols": ["http", "mqtt"],
            "base_url": "https://api.acme.com",
            "mqtt_topic_prefix": "thing/",
            "endpoints": [
                {
                    "id": "login",
                    "intent": "authenticate",
                    "method": "POST",
                    "path": "/v1.0/auth/login",
                }
            ],
        },
        "server": {
            "state_variables": [
                {"name": "power", "type": "boolean", "default": False}
            ],
            "responses": [
                {
                    "id": "rsp_login",
                    "triggers": ["authenticate"],
                    "status_code": 200,
                    "body_template": {"token": "{uuid}"},
                }
            ],
            "virtual_sensors": [],
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# SCHEMA LOADING
# ═══════════════════════════════════════════════════════════════════════════════

def test_schemas_load():
    """Both JSON Schemas should load from disk."""
    capture = get_capture_schema()
    pattern = get_pattern_schema()
    assert capture["$id"] == "https://ride-the-api.dev/capture-schema/v1"
    assert pattern["$id"] == "https://ride-the-api.dev/pattern-schema/v1"


# ═══════════════════════════════════════════════════════════════════════════════
# VALID CAPTURE
# ═══════════════════════════════════════════════════════════════════════════════

def test_valid_capture(valid_capture):
    result = validate_capture(valid_capture)
    assert result.valid, result.errors
    assert result.errors == []
    assert result.warnings == []


def test_capture_missing_required_field(valid_capture):
    del valid_capture["meta"]["vendor"]
    result = validate_capture(valid_capture)
    assert not result.valid
    assert any("vendor" in e for e in result.errors)


def test_capture_bad_protocol(valid_capture):
    valid_capture["sessions"][0]["pairs"][0]["protocol"] = "smtp"
    result = validate_capture(valid_capture)
    assert not result.valid


def test_capture_invalid_method_for_protocol(valid_capture):
    # PATCH is not valid for CoAP
    valid_capture["sessions"][0]["pairs"][0]["protocol"] = "coap"
    valid_capture["sessions"][0]["pairs"][0]["method"] = "PATCH"
    result = validate_capture(valid_capture)
    assert not result.valid
    assert any("not valid" in e for e in result.errors)


def test_capture_bad_version(valid_capture):
    valid_capture["meta"]["version"] = 2
    result = validate_capture(valid_capture)
    assert not result.valid


def test_capture_path_for_http(valid_capture):
    valid_capture["sessions"][0]["pairs"][0]["path"] = "auth/login"  # missing /
    result = validate_capture(valid_capture)
    assert not result.valid


# ═══════════════════════════════════════════════════════════════════════════════
# MQTT / CoAP / MODBUS CAPTURES
# ═══════════════════════════════════════════════════════════════════════════════

def test_capture_mqtt_valid():
    data = {
        "$schema": "https://ride-the-api.dev/capture-schema/v1",
        "meta": {
            "version": 1,
            "capture_id": "c1",
            "vendor": "acme",
            "device_type": "plug",
            "capture_date": "2024-12-15T10:00:00Z",
        },
        "sessions": [
            {
                "session_id": "s1",
                "timestamp_start": "2024-12-15T10:00:00Z",
                "pairs": [
                    {
                        "pair_id": "p1",
                        "timestamp": "2024-12-15T10:00:01Z",
                        "protocol": "mqtt",
                        "method": "publish",
                        "path": "thing/device1/state",
                    }
                ],
            }
        ],
    }
    result = validate_capture(data)
    assert result.valid, result.errors


def test_capture_mqtt_invalid_method():
    data = {
        "$schema": "https://ride-the-api.dev/capture-schema/v1",
        "meta": {
            "version": 1,
            "capture_id": "c1",
            "vendor": "acme",
            "device_type": "plug",
            "capture_date": "2024-12-15T10:00:00Z",
        },
        "sessions": [
            {
                "session_id": "s1",
                "timestamp_start": "2024-12-15T10:00:00Z",
                "pairs": [
                    {
                        "pair_id": "p1",
                        "timestamp": "2024-12-15T10:00:01Z",
                        "protocol": "mqtt",
                        "method": "GET",  # not a valid MQTT verb
                        "path": "thing/device1/state",
                    }
                ],
            }
        ],
    }
    result = validate_capture(data)
    assert not result.valid


def test_capture_coap_valid():
    data = {
        "$schema": "https://ride-the-api.dev/capture-schema/v1",
        "meta": {
            "version": 1,
            "capture_id": "c2",
            "vendor": "acme",
            "device_type": "sensor",
            "capture_date": "2024-12-15T10:00:00Z",
        },
        "sessions": [
            {
                "session_id": "s1",
                "timestamp_start": "2024-12-15T10:00:00Z",
                "pairs": [
                    {
                        "pair_id": "p1",
                        "timestamp": "2024-12-15T10:00:01Z",
                        "protocol": "coap",
                        "method": "get",
                        "path": "/sensors/temp",
                    }
                ],
            }
        ],
    }
    result = validate_capture(data)
    assert result.valid, result.errors


def test_capture_modbus_valid():
    data = {
        "$schema": "https://ride-the-api.dev/capture-schema/v1",
        "meta": {
            "version": 1,
            "capture_id": "c3",
            "vendor": "acme",
            "device_type": "plc",
            "capture_date": "2024-12-15T10:00:00Z",
        },
        "sessions": [
            {
                "session_id": "s1",
                "timestamp_start": "2024-12-15T10:00:00Z",
                "pairs": [
                    {
                        "pair_id": "p1",
                        "timestamp": "2024-12-15T10:00:01Z",
                        "protocol": "modbus",
                        "method": "read",
                        "path": "40001",
                    }
                ],
            }
        ],
    }
    result = validate_capture(data)
    assert result.valid, result.errors


def test_capture_modbus_invalid_path():
    data = {
        "$schema": "https://ride-the-api.dev/capture-schema/v1",
        "meta": {
            "version": 1,
            "capture_id": "c3",
            "vendor": "acme",
            "device_type": "plc",
            "capture_date": "2024-12-15T10:00:00Z",
        },
        "sessions": [
            {
                "session_id": "s1",
                "timestamp_start": "2024-12-15T10:00:00Z",
                "pairs": [
                    {
                        "pair_id": "p1",
                        "timestamp": "2024-12-15T10:00:01Z",
                        "protocol": "modbus",
                        "method": "read",
                        "path": "coil_A",  # not numeric
                    }
                ],
            }
        ],
    }
    result = validate_capture(data)
    assert not result.valid


# ═══════════════════════════════════════════════════════════════════════════════
# VALID PATTERN
# ═══════════════════════════════════════════════════════════════════════════════

def test_valid_pattern(valid_pattern):
    result = validate_pattern(valid_pattern)
    assert result.valid, result.errors


def test_pattern_missing_endpoint_id(valid_pattern):
    del valid_pattern["client"]["endpoints"][0]["id"]
    result = validate_pattern(valid_pattern)
    assert not result.valid


def test_pattern_unknown_protocol_is_rejected(valid_pattern):
    """Unknown protocols are rejected by the JSON Schema enum."""
    valid_pattern["client"]["protocols"] = ["ftp"]
    result = validate_pattern(valid_pattern)
    assert not result.valid
    assert any("ftp" in e for e in result.errors)


def test_pattern_mqtt_without_prefix_warning(valid_pattern):
    valid_pattern["client"]["protocols"] = ["mqtt"]
    valid_pattern["client"]["mqtt_topic_prefix"] = ""
    result = validate_pattern(valid_pattern)
    assert result.valid
    assert any("mqtt_topic_prefix" in w for w in result.warnings)


def test_pattern_orphaned_response_warning(valid_pattern):
    valid_pattern["server"]["responses"][0]["triggers"] = ["nonexistent_intent"]
    result = validate_pattern(valid_pattern)
    assert result.valid
    assert any("no client endpoint" in w for w in result.warnings)


def test_pattern_sensor_baseline_reference_warning(valid_pattern):
    valid_pattern["server"]["virtual_sensors"] = [
        {
            "name": "temp_actual",
            "type": "integer",
            "behavior": "drift",
            "baseline": "{state.temp_target}",
        }
    ]
    result = validate_pattern(valid_pattern)
    assert result.valid
    assert any("temp_target" in w for w in result.warnings)


def test_pattern_bad_sensor_behavior(valid_pattern):
    valid_pattern["server"]["virtual_sensors"] = [
        {
            "name": "temp_actual",
            "type": "integer",
            "behavior": "teleport",
        }
    ]
    result = validate_pattern(valid_pattern)
    assert not result.valid


def test_pattern_bad_field_mapping_transform(valid_pattern):
    valid_pattern["server"]["responses"][0]["field_mappings"] = [
        {"source": "request.body.x", "target": "state.y", "transform": "reverse"}
    ]
    result = validate_pattern(valid_pattern)
    assert not result.valid


# ═══════════════════════════════════════════════════════════════════════════════
# FILE VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════

def test_validate_capture_file(tmp_path, valid_capture):
    path = tmp_path / "capture.ride-capture.json"
    path.write_text(json.dumps(valid_capture), encoding="utf-8")
    result = validate_capture_file(str(path))
    assert result.valid


def test_validate_pattern_file(tmp_path, valid_pattern):
    path = tmp_path / "pattern.ride-pattern.json"
    path.write_text(json.dumps(valid_pattern), encoding="utf-8")
    result = validate_pattern_file(str(path))
    assert result.valid


def test_validate_file_missing(tmp_path):
    result = validate_capture_file(str(tmp_path / "does-not-exist.json"))
    assert not result.valid
    assert any("not found" in e for e in result.errors)


def test_validate_file_invalid_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{ not valid json", encoding="utf-8")
    result = validate_pattern_file(str(path))
    assert not result.valid
    assert any("Invalid JSON" in e for e in result.errors)


# ═══════════════════════════════════════════════════════════════════════════════
# ValidationError
# ═══════════════════════════════════════════════════════════════════════════════

def test_validation_error_dict(valid_capture):
    """ValidationError should include errors from protocol-specific checks."""
    # This passes schema validation but fails protocol-specific validation
    valid_capture["sessions"][0]["pairs"][0]["protocol"] = "mqtt"
    valid_capture["sessions"][0]["pairs"][0]["method"] = "GET"  # invalid for MQTT
    result = validate_capture(valid_capture)
    assert not result.valid
    assert len(result.errors) > 0
    err = ValidationError(result=result)
    d = err.to_dict()
    assert d["valid"] is False
    assert len(d["errors"]) > 0
    assert any("not valid" in e for e in d["errors"])

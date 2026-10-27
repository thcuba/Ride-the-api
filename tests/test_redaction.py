"""Unit tests for the capture redaction module (F-02)."""

from __future__ import annotations

from core.redaction import (
    REDACTED,
    redact_body,
    redact_headers,
    redact_pair_headers,
    redact_query,
    redact_value,
)


def test_redact_headers_redacts_sensitive():
    headers = {
        "Authorization": "Bearer abc123",
        "X-API-Key": "secret-key",
        "Cookie": "session=abc",
        "Content-Type": "application/json",
    }
    out = redact_headers(headers)
    assert out["Authorization"] == REDACTED
    assert out["X-API-Key"] == REDACTED
    assert out["Cookie"] == REDACTED
    assert out["Content-Type"] == "application/json"
    # Original dict untouched (non-destructive).
    assert headers["Authorization"] == "Bearer abc123"


def test_redact_headers_case_insensitive():
    out = redact_headers({"authorization": "x", "AUTHORIZATION": "y"})
    assert out["authorization"] == REDACTED
    assert out["AUTHORIZATION"] == REDACTED


def test_redact_headers_none():
    assert redact_headers(None) is None


def test_redact_headers_keeps_empty_values():
    out = redact_headers({"Authorization": ""})
    assert out["Authorization"] == ""


def test_redact_pair_headers_alias():
    """Verify redact_pair_headers functions identically to redact_headers."""
    headers = {
        "Authorization": "Bearer secret_token",
        "X-API-Key": "key_12345",
        "User-Agent": "IoT-Device/1.0",
    }
    out = redact_pair_headers(headers)
    assert out == redact_headers(headers)
    assert out["Authorization"] == REDACTED
    assert out["X-API-Key"] == REDACTED
    assert out["User-Agent"] == "IoT-Device/1.0"
    # Original untouched
    assert headers["Authorization"] == "Bearer secret_token"


def test_redact_pair_headers_none_and_empty():
    assert redact_pair_headers(None) is None
    assert redact_pair_headers({}) == {}
    assert redact_pair_headers({"Authorization": ""}) == {"Authorization": ""}


def test_redact_query_redacts_sensitive_params():
    out = redact_query({"token": "abc", "device": "shelly-1", "sig": "xyz"})
    assert out["token"] == REDACTED
    assert out["sig"] == REDACTED
    assert out["device"] == "shelly-1"


def test_redact_query_none():
    assert redact_query(None) is None


def test_redact_body_nested_dict():
    body = {
        "username": "admin",
        "password": "hunter2",
        "meta": {"api_key": "k123", "ok": True},
    }
    out = redact_body(body)
    assert out["username"] == "admin"
    assert out["password"] == REDACTED
    assert out["meta"]["api_key"] == REDACTED
    assert out["meta"]["ok"] is True
    # Non-destructive.
    assert body["password"] == "hunter2"


def test_redact_body_list():
    out = redact_body([{"token": "t1"}, {"token": "t2", "name": "x"}])
    assert out[0]["token"] == REDACTED
    assert out[1]["token"] == REDACTED
    assert out[1]["name"] == "x"


def test_redact_body_json_string():
    out = redact_body('{"token": "abc", "user": "bob"}')
    assert '"token": "[REDACTED]"' in out
    assert "bob" in out


def test_redact_body_json_string_fast_path_keeps_formatting():
    """Secret-free JSON string bodies bypass the json round-trip untouched."""
    body = ' {\n  "temperature": 21.5,\n  "online": true\n} '
    assert redact_body(body) == body


def test_redact_body_json_string_with_key_inside_value_still_redacts():
    """A sensitive key elsewhere keeps the payload safe even if a value echoes it."""
    out = redact_body('{"data": "token=abc", "api_key": "k1"}')
    assert '"api_key": "[REDACTED]"' in out
    assert "k1" not in out


def test_redact_body_json_string_case_insensitive_key():
    out = redact_body('{"API_KEY": "k1", "Name": "bob"}')
    assert '"API_KEY": "[REDACTED]"' in out
    assert "bob" in out


def test_redact_body_key_value_string():
    out = redact_body("token=abc&device=shelly")
    assert "token=[REDACTED]" in out
    assert "device=shelly" in out


def test_redact_body_scalar_passthrough():
    assert redact_body(42) == 42  # noqa: PLR2004
    assert redact_body("plain text") == "plain text"


def test_redact_value_sensitive_key():
    assert redact_value("password", "hunter2") == REDACTED
    assert redact_value("serial_number", "SN-123") == REDACTED


def test_redact_body_non_string_sensitive_values():
    body = {
        "pin": 1234,
        "security_code": 9876,
        "otp": 555555,
        "temperature": 21.5,
    }
    out = redact_body(body)
    assert out["pin"] == REDACTED
    assert out["security_code"] == REDACTED
    assert out["otp"] == REDACTED
    assert out["temperature"] == 21.5  # noqa: PLR2004


def test_redact_value_innocuous_key():
    assert redact_value("name", "bob") == "bob"


def test_redact_body_nested_authorization_headers():
    payload = {"headers": {"Authorization": "Bearer SECRET_TOKEN", "Host": "example.com"}}
    scrubbed = redact_body(payload)
    assert scrubbed["headers"]["Authorization"] == REDACTED
    assert scrubbed["headers"]["Host"] == "example.com"

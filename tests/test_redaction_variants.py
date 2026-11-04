"""Redaction of secret shapes that exact key matching used to miss (S-4)."""

from __future__ import annotations

import json

import pytest

from core.redaction import REDACTED, redact_body, redact_headers, redact_query


@pytest.mark.parametrize(
    "key",
    ["accessToken", "refreshToken", "clientSecret", "sessionToken", "localKey", "AccessToken"],
)
def test_camel_and_pascal_case_keys_are_redacted(key):
    out = redact_body({"device": {"auth": {key: "S3CRET"}}})
    assert out["device"]["auth"][key] == REDACTED


def test_oauth_form_body_is_redacted():
    body = "grant_type=refresh_token&refresh_token=R-SECRET&client_id=app&client_secret=C-SECRET"
    out = redact_body(body)
    assert "R-SECRET" not in out
    assert "C-SECRET" not in out
    # Non-secret fields and the grant type value survive.
    assert "grant_type=refresh_token" in out
    assert "client_id=app" in out


def test_form_string_inside_json_value_is_redacted():
    out = redact_body({"cfg": "ssid=home&password=W-SECRET"})
    assert "W-SECRET" not in out["cfg"]
    assert "ssid=home" in out["cfg"]


def test_json_string_with_leading_space_is_redacted():
    out = redact_body({"data": ' {"password": "P-SECRET"}'})
    assert "P-SECRET" not in out["data"]


def test_newline_separated_pairs_are_redacted():
    out = redact_body("user=a\npassword=N-SECRET\nmode=eco")
    assert out == f"user=a\npassword={REDACTED}\nmode=eco"


def test_unicode_escaped_key_does_not_bypass_redaction():
    body = '{"pass\\u0077ord": "hunter2"}'
    out = redact_body(body)
    assert "hunter2" not in out
    assert json.loads(out)["password"] == REDACTED


@pytest.mark.parametrize(
    "header", ["X-Session-Token", "X-Amz-Security-Token", "X-Goog-Api-Key", "Set-Cookie"]
)
def test_token_bearing_headers_are_redacted(header):
    assert redact_headers({header: "S3CRET"})[header] == REDACTED


@pytest.mark.parametrize(
    "header", ["Content-Type", "User-Agent", "Host", "Sec-WebSocket-Key", "If-None-Match"]
)
def test_ordinary_headers_are_kept(header):
    assert redact_headers({header: "value"})[header] == "value"


@pytest.mark.parametrize(
    "param", ["refresh_token", "X-Amz-Credential", "X-Amz-Signature", "client_secret"]
)
def test_sensitive_query_params_are_redacted(param):
    assert redact_query({param: "S3CRET"})[param] == REDACTED


@pytest.mark.parametrize("key", ["username", "name", "tokenExpiry", "temperature", "keyboard"])
def test_innocuous_keys_are_not_over_redacted(key):
    assert redact_body({key: "v"})[key] == "v"

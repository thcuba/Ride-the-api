"""Redaction of sensitive data before capture persistence and LLM prompts.

Devices frequently send credentials — ``Authorization`` headers, cookies,
bearer tokens, serials, Wi-Fi passphrases, API keys — in the captured request
and response pairs. These pairs are persisted in the local database and sent
to a configured (potentially external) LLM for analysis. Redacting the values
before both persistence *and* prompting prevents credential / personal-data
leakage while preserving the structure the learning pipeline needs.

All functions are pure and non-destructive: they return *copies* with
sensitive values replaced by ``[REDACTED]``.
"""

from __future__ import annotations

from typing import Any

# Header names whose values are always secrets (matched case-insensitively).
SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "apikey",
        "x-auth-token",
        "x-auth",
        "x-access-token",
        "x-request-token",
        "token",
        "password",
        "passwd",
        "pwd",
        "x-password",
        "secret",
        "client-secret",
        "client_secret",
        "credential",
        "credentials",
    }
)

# JSON keys whose values are redacted (matched case-insensitively).
SENSITIVE_KEYS = frozenset(
    {
        "token",
        "access_token",
        "access-token",
        "auth_token",
        "refresh_token",
        "id_token",
        "api_key",
        "apikey",
        "api-key",
        "password",
        "passwd",
        "pwd",
        "secret",
        "client_secret",
        "client-secret",
        "credential",
        "credentials",
        "authorization",
        "cookie",
        "session_id",
        "session-key",
        "serial",
        "serial_number",
        "wifi_password",
        "wifi_pass",
        "wifipassword",
        "security_code",
        "pin",
        "otp",
        "mfa",
        "totp",
        "private_key",
        "key_file",
    }
)

# Query string parameter names whose values are redacted.
SENSITIVE_QUERY_PARAMS = frozenset(
    {
        "token",
        "access_token",
        "access-token",
        "api_key",
        "apikey",
        "api-key",
        "key",
        "auth",
        "auth_token",
        "signature",
        "sig",
        "code",
        "password",
        "passwd",
        "pwd",
        "secret",
        "credential",
        "session",
        "session_id",
    }
)

REDACTED = "[REDACTED]"


def redact_headers(headers: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a copy of ``headers`` with sensitive values redacted."""
    if headers is None:
        return None
    result: dict[str, Any] = {}
    for name, value in headers.items():
        key = str(name).lower()
        if key in SENSITIVE_HEADERS and value not in (None, ""):
            result[name] = REDACTED
        else:
            result[name] = value
    return result


def redact_query(query_params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a copy of ``query_params`` with sensitive values redacted."""
    if query_params is None:
        return None
    result: dict[str, Any] = {}
    for name, value in query_params.items():
        key = str(name).lower()
        if key in SENSITIVE_QUERY_PARAMS and value not in (None, ""):
            result[name] = REDACTED
        else:
            result[name] = value
    return result


def redact_body(body: Any, *, in_headers: bool = True) -> Any:  # noqa: ANN401
    """Return a copy of ``body`` with sensitive values redacted.

    Recursively walks dicts and lists. A value is redacted when its key is a
    sensitive name. String bodies are parsed as JSON when possible so nested
    credentials inside JSON payloads are caught too; unparseable strings are
    passed through unchanged. ``in_headers`` mirrors the audit requirement
    that header-derived auth context (e.g. a JSON body directly reflecting the
    Authorization header) is never enough to skip redaction.
    """
    del in_headers  # reserved for future header-aware decisions
    if isinstance(body, dict):
        return {k: redact_value(k, v) for k, v in body.items()}
    if isinstance(body, list):
        return [redact_body(v) for v in body]
    if isinstance(body, str):
        return _redact_json_string(body)
    return body


def redact_value(key: Any, value: Any) -> Any:  # noqa: ANN401
    """Redact ``value`` if ``key`` names a sensitive field; else recurse."""
    if isinstance(value, (dict, list)):
        return redact_body(value)
    if isinstance(value, str) and _is_sensitive_key(key):
        return REDACTED
    return value


def _is_sensitive_key(key: Any) -> bool:
    return str(key).strip().lower() in SENSITIVE_KEYS


def _redact_json_string(text: str) -> str:
    """Redact sensitive fields inside a JSON-encoded string body.

    Falls back to a regex-free heuristic for non-JSON strings (e.g.
    ``token=abc123`` query-style bodies): sensitive ``name=value`` pairs are
    replaced with ``[REDACTED]``.
    """
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            import json

            parsed = json.loads(stripped)
            redacted = redact_body(parsed)
            return json.dumps(redacted, ensure_ascii=False)
        except (ValueError, TypeError):
            return text
    return _redact_key_value_pairs(text)


def _redact_key_value_pairs(text: str) -> str:
    """Best-effort redaction of ``key=value`` pairs in scalar string bodies."""
    parts = text.split("&")
    out = []
    for part in parts:
        if "=" in part and not part.lstrip().startswith(("{", "[")):
            name = part.split("=", 1)[0].strip().lower()
            if name in SENSITIVE_QUERY_PARAMS:
                out.append(part.split("=", 1)[0] + "=[REDACTED]")
                continue
        out.append(part)
    return "&".join(out)


def redact_pair_headers(headers: dict[str, Any] | None) -> dict[str, Any] | None:
    """Alias kept for readability in capture contexts."""
    return redact_headers(headers)
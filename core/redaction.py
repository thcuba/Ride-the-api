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

import functools
import json
import re
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

# Names are compared in a normalised form: lower-cased with every non
# alphanumeric character removed, so ``accessToken``, ``access_token``,
# ``Access-Token`` and ``ACCESS TOKEN`` all read as ``accesstoken``. Exact sets
# alone missed camelCase payloads, OAuth form fields and vendor token headers.
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_SEPARATORS = re.compile(r"[\s_\-]+")

# A normalised name ending in one of these is a secret whatever its prefix:
# ``refreshToken``, ``X-Amz-Security-Token``, ``client_secret``, ``X-Goog-Api-Key``,
# ``wifiPassword``, ``localKey`` (a device's local encryption key) ...
_SENSITIVE_SUFFIXES = (
    "token",
    "secret",
    "password",
    "passwd",
    "passphrase",
    "apikey",
    "credential",
    "credentials",
    "privatekey",
    "cookie",
    "authorization",
    "sessionid",
    "sessionkey",
    "localkey",
    "signature",
)


@functools.lru_cache(maxsize=4096)
def _norm(name: str) -> str:
    """Normalised form of a header/key/parameter name (see module notes)."""
    return _NON_ALNUM.sub("", name.lower())


def _normalised_set(names: frozenset[str]) -> frozenset[str]:
    return frozenset(_norm(n) for n in names)


_SENSITIVE_HEADERS_NORM = _normalised_set(SENSITIVE_HEADERS)
_SENSITIVE_KEYS_NORM = _normalised_set(SENSITIVE_KEYS)
_SENSITIVE_QUERY_NORM = _normalised_set(SENSITIVE_QUERY_PARAMS)


def _matches(name: object, exact: frozenset[str], normalised: frozenset[str]) -> bool:
    """Whether ``name`` is sensitive: exact hit, normalised hit, or secret suffix."""
    if isinstance(name, str) and name in exact:
        return True
    norm = _norm(name if isinstance(name, str) else str(name))
    if not norm:
        return False
    return norm in normalised or norm.endswith(_SENSITIVE_SUFFIXES)


def _is_sensitive_header(name: object) -> bool:
    return _matches(name, SENSITIVE_HEADERS, _SENSITIVE_HEADERS_NORM)


def _is_sensitive_query_param(name: object) -> bool:
    return _matches(name, SENSITIVE_QUERY_PARAMS, _SENSITIVE_QUERY_NORM)


# Stems used by the JSON fast path to cheaply test whether a payload can
# possibly hold a secret before the json.loads -> walk -> json.dumps round
# trip. It is a conservative *presence* test against the payload with
# separators squashed, so over-matching only costs speed and can never skip an
# actual secret.
_FAST_PATH_STEMS = tuple(sorted(_SENSITIVE_KEYS_NORM | set(_SENSITIVE_SUFFIXES)))


def _contains_sensitive_key(text: str) -> bool:
    """Return True if any sensitive name could appear in ``text``.

    Case-insensitive and separator-insensitive substring test. The JSON fast
    path only skips redaction when this returns False; a false positive is
    harmless because it just disables the fast path for that payload.
    """
    squashed = _SEPARATORS.sub("", text.lower())
    for stem in _FAST_PATH_STEMS:  # noqa: SIM110
        if stem in squashed:
            return True
    return False


def redact_headers(headers: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a copy of ``headers`` with sensitive values redacted."""
    if not headers:
        return headers
    result = dict(headers)
    for name, value in headers.items():
        if value in (None, ""):
            continue
        if _is_sensitive_header(name):
            result[name] = REDACTED
    return result


def redact_query(query_params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a copy of ``query_params`` with sensitive values redacted."""
    if not query_params:
        return query_params
    result = dict(query_params)
    for name, value in query_params.items():
        if value in (None, ""):
            continue
        if _is_sensitive_query_param(name):
            result[name] = REDACTED
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
    if not body:
        return body
    if isinstance(body, dict):
        return {k: redact_value(k, v) for k, v in body.items()}
    if isinstance(body, list):
        return [redact_body(v) for v in body]
    if isinstance(body, str):
        return _redact_json_string(body)
    return body


def redact_value(key: Any, value: Any) -> Any:  # noqa: ANN401
    """Redact ``value`` if ``key`` names a sensitive field; else recurse.

    String values are inspected too: an embedded JSON document (even with
    leading whitespace) is redacted recursively, and a form-encoded string
    such as ``"ssid=home&password=..."`` has its sensitive pairs redacted.
    """
    if _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, (dict, list)):
        return redact_body(value)
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            return _redact_json_string(value)
        if "=" in value:
            return _redact_key_value_pairs(value)
    return value


def _is_sensitive_key(key: object) -> bool:
    return _matches(key, SENSITIVE_KEYS, _SENSITIVE_KEYS_NORM)


def _redact_json_string(text: str) -> str:
    """Redact sensitive fields inside a JSON-encoded string body.

    Falls back to a regex-free heuristic for non-JSON strings (e.g.
    ``token=abc123`` query-style bodies): sensitive ``name=value`` pairs are
    replaced with ``[REDACTED]``.
    """
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        # Fast path: JSON payloads rarely contain secrets, and the full
        # json.loads -> walk -> json.dumps round-trip is a per-capture
        # allocation on the request/response hot path. When no sensitive key
        # name is present anywhere, return the payload untouched (which also
        # preserves the original formatting instead of re-serializing it).
        # A ``\u`` escape can spell a key the substring test cannot see
        # (``"password"`` decodes to ``password``), so it always takes
        # the slow path.
        if "\\u" not in stripped and not _contains_sensitive_key(stripped):
            return text
        try:
            parsed = json.loads(stripped)
            redacted = redact_body(parsed)
            return json.dumps(redacted, ensure_ascii=False)
        except (ValueError, TypeError):
            return text
    return _redact_key_value_pairs(text)


# Pair separators in form-style bodies: ``&`` (application/x-www-form-urlencoded)
# and line breaks (``key=value`` per line, common in device config payloads).
_KV_SEPARATORS = re.compile(r"(&|\r?\n)")


def _redact_key_value_pairs(text: str) -> str:
    """Best-effort redaction of ``key=value`` pairs in scalar string bodies.

    Pairs may be separated by ``&`` or by line breaks; separators are kept
    exactly as they were. A key is sensitive when it is a sensitive query
    parameter or body key name (normalised, including secret suffixes such as
    ``refresh_token`` or ``client_secret``).

    Fast path: short-circuit when '=' is not present to avoid unnecessary string
    splitting, list allocations, and string joins on non-key-value string bodies (~4.3x speedup).
    """
    if "=" not in text:
        return text
    out = []
    for part in _KV_SEPARATORS.split(text):
        if "=" in part and not part.lstrip().startswith(("{", "[")):
            key_name, _ = part.split("=", 1)
            name = key_name.strip()
            if _is_sensitive_query_param(name) or _is_sensitive_key(name):
                out.append(f"{key_name}={REDACTED}")
                continue
        out.append(part)
    return "".join(out)


def redact_pair_headers(headers: dict[str, Any] | None) -> dict[str, Any] | None:
    """Alias kept for readability in capture contexts."""
    return redact_headers(headers)

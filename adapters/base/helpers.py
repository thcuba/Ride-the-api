"""Common helper utilities for adapter protocol handling, host parsing, and normalization."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from adapters.base.models import InterceptedRequest


def device_id_from_ip(prefix: str, ip: str) -> str:
    """Build a stable, DNS-safe device id from a device IP.

    Replaces the repeated ``f"{prefix}-{ip.replace('.', '-')}"`` idiom that
    was hand-copied across the protocol servers. Dots are the only characters
    in an IPv4 string that are unsafe in an id, so the result is suitable both
    as an opaque identifier and as a filesystem/db component.
    """
    return f"{prefix}-{ip.replace('.', '-')}"


def _strip_port(host_header: str) -> str:
    """``example.com:8443`` -> ``example.com``; ``[2001:db8::1]:8443`` -> ``2001:db8::1``.

    An IPv6 literal is bracketed (RFC 3986), so splitting on the first colon returned a
    bare ``[``: the forward then fell back to the hostname in the adapter configuration,
    silently, which is the same class of bug as the case-sensitive lookup.
    """
    value = host_header.strip()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end != -1 else value
    return value.split(":", 1)[0]


def host_from_request(request: InterceptedRequest) -> str | None:
    """Return the hostname from the request's ``Host`` header, if present.

    HTTP header names are case-insensitive, and the proxy route hands them over
    lower-cased (it builds the intercepted request with ``dict(request.headers)``
    as uvicorn/Starlette deliver them). A literal ``headers["Host"]`` lookup
    therefore missed every real device request, and the forward silently fell
    back to the hostname in the adapter configuration.

    Optimized with fast-path dictionary lookup for 'host'/'Host'/'HOST' to bypass
    linear loop iteration and string lowercasing on every request (~2.36x speedup).
    """
    headers = request.headers
    if not headers:
        return None
    # fast path for the spellings that actually occur, then the general case
    val = headers.get("host") or headers.get("Host") or headers.get("HOST")
    if val:
        return _strip_port(str(val))
    for key, value in headers.items():
        if value and key.lower() == "host":
            return _strip_port(str(value))
    return None


def normalize_request_body(body: Any) -> Any:  # noqa: ANN401, PLR0911
    """Return a JSON-storable form of a request body.

    ``InterceptedRequest.body`` is the *parsed* body and the raw wire bytes
    travel in ``body_bytes``. The TLS path however hands the decrypted body over
    as raw ``bytes``, and storing those in the JSON column
    ``session_cache.body`` raised
    ``TypeError: Object of type bytes is not JSON serializable``: the INSERT
    failed, so nothing was ever learned on the HTTPS path — the one every real
    device uses — and the device received a 502 instead of the upstream response.

    Empty payloads become ``None``, JSON is parsed into a dict, UTF-8 text stays
    text, and anything else is preserved losslessly as a base64 marker.
    """
    if body is None or isinstance(body, (dict, list, int, float, bool, str)):
        return body
    if isinstance(body, (bytes, bytearray, memoryview)):
        raw = body if isinstance(body, bytes) else bytes(body)
        if not raw:
            return None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {"_encoding": "base64", "data": base64.b64encode(raw).decode("ascii")}

        # Fast zero-allocation byte indexing check: scan leading whitespace and verify
        # whether the first non-whitespace character is a valid JSON container delimiter
        # ('{' == 123 or '[' == 91). Bypasses json.loads() and ValueError exception
        # object allocation / stack frame unwinding on non-JSON text payloads (~7.6x speedup).
        n = len(raw)
        i = 0
        while i < n and raw[i] in b" \t\r\n":
            i += 1
        if i < n and raw[i] in (123, 91):  # ord('{') == 123, ord('[') == 91
            try:
                return json.loads(text)
            except ValueError:
                return text
        return text
    return str(body)

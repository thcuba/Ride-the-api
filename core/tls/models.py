"""
Data models and HTTP parsing helpers for TLS MITM operations.

Defines the core data structures used by TLSMITMServer to encapsulate
decrypted traffic and device connection metadata, along with safe HTTP/1.1
parsing and serialization via h11.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING

import h11

if TYPE_CHECKING:
    from datetime import datetime

# TLS version mapping from string to ssl.TLSVersion enum
TLS_VERSION_MAP: dict[str, ssl.TLSVersion] = {
    "TLSv1.0": ssl.TLSVersion.TLSv1,
    "TLSv1.1": ssl.TLSVersion.TLSv1_1,
    "TLSv1.2": ssl.TLSVersion.TLSv1_2,
    "TLSv1.3": ssl.TLSVersion.TLSv1_3,
}

# Cache of valid HTTP status codes (from stdlib) for fast lookup
_VALID_HTTP_STATUSES: set[int] = {s.value for s in HTTPStatus}

# Upper bound for a single decrypted HTTP request (headers + body). Prevents a
# compromised device from exhausting memory via an unbounded request body.
MAX_REQUEST_SIZE: int = 10 * 1024 * 1024  # 10 MiB
MAX_ENCRYPTED_REQUEST_SIZE: int = 2 * MAX_REQUEST_SIZE


@dataclass
class DecryptedRequest:
    """A fully decrypted HTTP request from a device."""

    client_ip: str
    client_port: int
    dst_port: int
    sni: str
    method: str
    path: str
    http_version: str
    headers: dict[str, str]
    body: bytes


@dataclass
class DevicePortInfo:
    """Metadata about a device's connection."""

    device_id: str
    ip: str
    port: int
    first_seen: datetime
    last_seen: datetime


def parse_decrypted_http_request(
    data: bytes,
) -> tuple[str, str, str, dict[str, str], bytes] | None:
    """Parse a complete HTTP/1.1 request using the h11 state machine.

    Returns ``(method, target, http_version, headers, body)`` once the full
    request is available, or ``None`` when more data is needed. Malformed
    requests also produce ``None`` (the caller can decide how to reply).
    ``headers`` is a case-insensitive mapping (lower-cased keys).
    """
    conn = h11.Connection(h11.SERVER)
    conn.receive_data(data)
    method = target = http_version = None
    headers: dict[str, str] = {}
    body = bytearray()
    got_end = False
    try:
        while True:
            event = conn.next_event()
            if event is h11.NEED_DATA:
                break
            if isinstance(event, h11.Request):
                method = event.method.decode("ascii", "replace")
                target = event.target.decode("ascii", "replace")
                http_version = event.http_version.decode("ascii", "replace")
                for name, value in event.headers:
                    key = name.decode("ascii", "replace").lower()
                    headers[key] = value.decode("iso-8859-1")
            elif isinstance(event, h11.Data):
                body.extend(event.data)
            elif isinstance(event, h11.EndOfMessage):
                got_end = True
                break
            elif isinstance(event, h11.ConnectionClosed):
                break
    except h11.RemoteProtocolError:
        return None

    if not got_end or not method or not target:
        return None
    return method, target, http_version, headers, bytes(body)


def serialize_http_response(
    status_code: int,
    headers: dict[str, str] | None,
    body: bytes,
) -> bytes:
    """Serialize an HTTP/1.1 response with h11 (Content-Length, keep-alive-off).

    Returns the raw bytes ready to write to the TLS stream. Raises on invalid
    status codes in the same way the state machine does.
    """
    reason = str(HTTPStatus(status_code).phrase) if status_code in _VALID_HTTP_STATUSES else "OK"
    out_headers: list[tuple[bytes, bytes]] = [
        (b"content-length", str(len(body)).encode("ascii")),
        (b"connection", b"close"),
    ]
    for k, v in (headers or {}).items():
        out_headers.append(
            (
                k.encode("latin-1", errors="replace"),
                v.encode("latin-1", errors="replace"),
            )
        )
    conn = h11.Connection(h11.SERVER)
    raw = conn.send(
        h11.Response(
            status_code=status_code,
            reason=reason,
            headers=out_headers,
        )
    )
    if body:
        raw += conn.send(h11.Data(data=body))
    raw += conn.send(h11.EndOfMessage())
    return raw

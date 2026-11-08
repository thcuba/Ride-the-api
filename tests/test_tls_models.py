"""Tests for TLS MITM models and HTTP codecs (core.tls.models)."""

from __future__ import annotations

from datetime import UTC, datetime

from core.tls.models import (
    DecryptedRequest,
    DevicePortInfo,
    parse_decrypted_http_request,
    serialize_http_response,
)

DEFAULT_HTTPS_PORT = 443
CLIENT_PORT = 52341


def test_decrypted_request_instantiation():
    req = DecryptedRequest(
        client_ip="192.168.1.100",
        client_port=CLIENT_PORT,
        dst_port=DEFAULT_HTTPS_PORT,
        sni="api.vendor.com",
        method="POST",
        path="/v1/status",
        http_version="1.1",
        headers={"host": "api.vendor.com", "content-type": "application/json"},
        body=b'{"power": "on"}',
    )
    assert req.client_ip == "192.168.1.100"
    assert req.sni == "api.vendor.com"
    assert req.method == "POST"
    assert req.body == b'{"power": "on"}'


def test_device_port_info_instantiation():
    now = datetime.now(UTC)
    info = DevicePortInfo(
        device_id="ip-192-168-1-100",
        ip="192.168.1.100",
        port=DEFAULT_HTTPS_PORT,
        first_seen=now,
        last_seen=now,
    )
    assert info.device_id == "ip-192-168-1-100"
    assert info.port == DEFAULT_HTTPS_PORT


def test_parse_and_serialize_http():
    raw_response = serialize_http_response(
        status_code=200,
        headers={"x-test": "1"},
        body=b"OK",
    )
    assert b"200 OK" in raw_response
    assert b"content-length: 2" in raw_response
    assert b"connection: close" in raw_response
    assert b"x-test: 1" in raw_response
    assert raw_response.endswith(b"\r\n\r\nOK")

    raw_req = b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n"
    parsed = parse_decrypted_http_request(raw_req)
    assert parsed is not None
    method, target, version, headers, body = parsed
    assert method == "GET"
    assert target == "/health"
    assert headers.get("host") == "localhost"
    assert body == b""

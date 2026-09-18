"""Tests for the h11-based HTTP/1.1 request parsing and response serialization.

These cover the pure functions in ``core.tls_mitm`` that replaced the
hand-rolled regex parser and response serializer:
  * ``parse_decrypted_http_request`` — request-line framing, headers, body
    consumption (Content-Length and chunked), malformed input.
  * ``serialize_http_response`` — h11 state-machine response serialization,
    verified by round-tripping through a client-side h11 connection.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta

import h11
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from core.cert_manager import CertManager
from core.tls_mitm import (
    TLSMITMServer,
    parse_decrypted_http_request,
    serialize_http_response,
)


def _make_test_server() -> TLSMITMServer:
    """Build a TLSMITMServer with a throwaway CA for SSL context caching tests."""
    tmp = tempfile.mkdtemp()
    cm = CertManager(
        ca_cert_path=f"{tmp}/ca.pem",
        ca_key_path=f"{tmp}/ca.key",
        device_certs_dir=f"{tmp}/device_certs",
        external_certs_dir=f"{tmp}/external_certs",
    )
    cm.ensure_ca()
    server = TLSMITMServer(cert_manager=cm)
    # Purge the default port list so no sockets are bound.
    server.listen_ports = []
    return server



def _parse_via_client(raw: bytes) -> tuple[int, dict[str, str], bytes]:
    """Feed raw response bytes to a client-side h11 connection."""
    conn = h11.Connection(h11.CLIENT)
    conn.receive_data(raw)
    status = None
    headers: dict[str, str] = {}
    body = bytearray()
    got_end = False
    while True:
        event = conn.next_event()
        if event is h11.NEED_DATA:
            break
        if isinstance(event, h11.Response):
            status = event.status_code
            for k, v in event.headers:
                headers[k.decode("ascii").lower()] = v.decode("iso-8859-1")
        elif isinstance(event, h11.Data):
            body.extend(event.data)
        elif isinstance(event, h11.EndOfMessage):
            got_end = True
            break
        elif isinstance(event, h11.ConnectionClosed):
            break
    assert status is not None
    assert got_end

    return status, headers, bytes(body)


class TestParseDecryptedHttpRequest:
    def test_simple_get(self) -> None:
        raw = (
            b"GET /api/status?x=1 HTTP/1.1\r\nHost: device.local\r\nUser-Agent: test-agent\r\n\r\n"
        )
        parsed = parse_decrypted_http_request(raw)
        assert parsed is not None
        method, target, http_version, headers, body = parsed
        assert method == "GET"
        assert target == "/api/status?x=1"
        assert http_version == "1.1"
        assert headers["host"] == "device.local"
        assert headers["user-agent"] == "test-agent"
        assert body == b""

    def test_post_content_length(self) -> None:
        raw = (
            b"POST /telemetry HTTP/1.1\r\n"
            b"Host: device.local\r\n"
            b"Content-Length: 11\r\n"
            b"Content-Type: application/json\r\n"
            b"\r\n"
            b"hello world"
        )
        parsed = parse_decrypted_http_request(raw)
        assert parsed is not None
        _, target, _, headers, body = parsed
        assert target == "/telemetry"
        assert headers["content-type"] == "application/json"
        assert body == b"hello world"

    def test_header_case_normalized_to_lower(self) -> None:
        raw = (
            b"PUT /config HTTP/1.1\r\n"
            b"Host: device.local\r\n"
            b"X-Custom-Header: Value\r\n"
            b"ANOTHER-ONE: 42\r\n"
            b"\r\n"
        )
        parsed = parse_decrypted_http_request(raw)
        assert parsed is not None
        _, _, _, headers, _ = parsed
        assert headers["x-custom-header"] == "Value"
        assert headers["another-one"] == "42"

    def test_chunked_transfer_encoding(self) -> None:
        raw = (
            b"POST /upload HTTP/1.1\r\n"
            b"Host: device.local\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            b"5\r\nhello\r\n"
            b"6\r\n world\r\n"
            b"0\r\n\r\n"
        )
        parsed = parse_decrypted_http_request(raw)
        assert parsed is not None
        _, target, _, _, body = parsed
        assert target == "/upload"
        assert body == b"hello world"

    def test_request_split_across_calls(self) -> None:
        """Parsing only succeeds once the whole request (incl. body) is buffered."""
        first = b"POST /split HTTP/1.1\r\nHost: device.local\r\nContent-Length: 5\r\n\r\n"
        assert parse_decrypted_http_request(first) is None
        complete = first + b"abcde"
        parsed = parse_decrypted_http_request(complete)
        assert parsed is not None
        _, _, _, _, body = parsed
        assert body == b"abcde"

    def test_malformed_request_returns_none(self) -> None:
        raw = b"NOT-A-REQUEST\r\n\r\n"
        assert parse_decrypted_http_request(raw) is None

    def test_empty_input_returns_none(self) -> None:
        assert parse_decrypted_http_request(b"") is None

    def test_incomplete_headers_returns_none(self) -> None:
        raw = b"GET / HTTP/1.1\r\nHost: device"
        assert parse_decrypted_http_request(raw) is None

    def test_method_and_target_are_decoded(self) -> None:
        raw = b"GET /path%20with%20spaces HTTP/1.1\r\nHost: x\r\n\r\n"
        parsed = parse_decrypted_http_request(raw)
        assert parsed is not None
        assert parsed[1] == "/path%20with%20spaces"

    def test_multiple_requests_same_buffer(self) -> None:
        """Extra pipelined data is simply ignored by the parser."""
        raw = b"GET /one HTTP/1.1\r\nHost: x\r\n\r\nGET /two HTTP/1.1\r\nHost: x\r\n\r\n"
        parsed = parse_decrypted_http_request(raw)
        assert parsed is not None
        method, target, _, _, _ = parsed
        assert method == "GET"
        assert target == "/one"


class TestSerializeHttpResponse:
    def test_round_trip_status_and_body(self) -> None:
        raw = serialize_http_response(200, {"content-type": "application/json"}, b'{"ok": true}')
        status, headers, body = _parse_via_client(raw)
        assert status == 200  # noqa: PLR2004
        assert headers["content-length"] == str(len(b'{"ok": true}'))
        assert headers["content-type"] == "application/json"
        assert body == b'{"ok": true}'

    def test_empty_body(self) -> None:
        raw = serialize_http_response(204, None, b"")
        status, headers, body = _parse_via_client(raw)
        assert status == 204  # noqa: PLR2004
        assert headers["content-length"] == "0"
        assert body == b""

    def test_unknown_status_code_uses_ok_reason(self) -> None:
        raw = serialize_http_response(599, None, b"")
        status, _, _ = _parse_via_client(raw)
        assert status == 599  # noqa: PLR2004

    def test_connection_close_header_present(self) -> None:
        raw = serialize_http_response(200, None, b"x")
        _, headers, _ = _parse_via_client(raw)
        assert headers["connection"] == "close"

    @pytest.mark.parametrize("status", [200, 301, 404, 500, 502])
    def test_common_status_codes(self, status: int) -> None:
        raw = serialize_http_response(status, None, b"payload")
        parsed_status, _, body = _parse_via_client(raw)
        assert parsed_status == status
        assert body == b"payload"

    def test_body_bytes_roundtrip_exact(self) -> None:
        payload = bytes(range(256))  # binary-safe body
        raw = serialize_http_response(200, None, payload)
        _, _, body = _parse_via_client(raw)
        assert body == payload


class TestSslContextCaching:
    def test_same_cert_returns_cached_context(self) -> None:
        server = _make_test_server()
        hostname = "device.local"
        cert_pem, key_pem = server.cert_manager.get_cert_for_hostname(hostname)

        ctx_a, temp_a = server._make_ssl_context(cert_pem, key_pem)
        ctx_b, temp_b = server._make_ssl_context(cert_pem, key_pem)

        # Same material -> same context object, and no temp file on the hit.
        assert ctx_a is ctx_b
        assert temp_a is not None
        assert temp_b is None

    def test_cache_holds_one_entry_per_cert(self) -> None:
        server = _make_test_server()
        cert_pem, key_pem = server.cert_manager.get_cert_for_hostname("one.local")
        cert_pem2, key_pem2 = server.cert_manager.get_cert_for_hostname("two.local")

        ctx_one, _ = server._make_ssl_context(cert_pem, key_pem)
        ctx_two, _ = server._make_ssl_context(cert_pem2, key_pem2)
        ctx_one_again, _ = server._make_ssl_context(cert_pem, key_pem)

        assert ctx_one is ctx_one_again
        assert ctx_one is not ctx_two

    def test_rotated_cert_builds_fresh_context(self) -> None:
        """Changing the PEM (simulating cert rotation) must not reuse the old ctx."""
        server = _make_test_server()
        hostname = "rot.local"
        cert_pem, key_pem = server.cert_manager.get_cert_for_hostname(hostname)
        ctx_a, _ = server._make_ssl_context(cert_pem, key_pem)

        # Build a genuinely new self-signed cert (a "rotated" replacement).
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(UTC)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
        rotated = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        rotated_cert = rotated.public_bytes(serialization.Encoding.PEM).decode()
        rotated_key = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ).decode()

        ctx_b, _ = server._make_ssl_context(rotated_cert, rotated_key)

        # Different PEM for the same hostname -> different context.
        assert ctx_a is not ctx_b
        # Both are real, usable server contexts.
        assert ctx_a is not None
        assert ctx_b is not None
        assert ctx_b.maximum_version == ctx_a.maximum_version


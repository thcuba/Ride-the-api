"""Tests for zero-overhead TLS ClientHello SNI parser (core.tls.sni)."""

from __future__ import annotations

import struct

from core.tls.sni import (
    TLS_CLIENT_HELLO,
    TLS_EXTENSION_SNI,
    TLS_HANDSHAKE,
    TLS_RECORD_HEADER_SIZE,
    extract_sni_from_client_hello,
)


def _build_client_hello(hostname: str | None = None) -> bytes:
    """Construct a minimal synthetic ClientHello TLS packet."""
    extensions = bytearray()
    if hostname is not None:
        host_bytes = hostname.encode("utf-8")
        server_name_entry = b"\x00" + struct.pack("!H", len(host_bytes)) + host_bytes
        server_name_list = struct.pack("!H", len(server_name_entry)) + server_name_entry
        extensions.extend(
            struct.pack("!H", TLS_EXTENSION_SNI)
            + struct.pack("!H", len(server_name_list))
            + server_name_list
        )

    # Legacy version (2) + random (32) + session_id_len (1) + cipher_suites (2+2) + comp (1+1)
    hello_body = bytearray()
    hello_body.extend(b"\x03\x03")  # TLS 1.2
    hello_body.extend(b"\x00" * 32)  # random
    hello_body.extend(b"\x00")  # session id length = 0
    hello_body.extend(struct.pack("!H", 2) + b"\x00\x9c")  # 1 cipher suite
    hello_body.extend(b"\x01\x00")  # 1 compression method (null)
    if extensions:
        hello_body.extend(struct.pack("!H", len(extensions)) + extensions)

    # Handshake header: type (1) + length (3)
    handshake = bytearray()
    handshake.append(TLS_CLIENT_HELLO)
    handshake.extend(struct.pack("!I", len(hello_body))[1:])
    handshake.extend(hello_body)

    # Record header: type (1) + version (2) + length (2)
    record = bytearray()
    record.append(TLS_HANDSHAKE)
    record.extend(b"\x03\x01")  # record version
    record.extend(struct.pack("!H", len(handshake)))
    record.extend(handshake)
    return bytes(record)


def test_extract_sni_valid():
    packet = _build_client_hello("device.example.local")
    sni = extract_sni_from_client_hello(packet)
    assert sni == "device.example.local"


def test_extract_sni_none_when_no_sni_extension():
    packet = _build_client_hello(None)
    sni = extract_sni_from_client_hello(packet)
    assert sni is None


def test_extract_sni_malformed_truncated():
    assert extract_sni_from_client_hello(b"") is None
    assert extract_sni_from_client_hello(b"\x16\x03\x01") is None
    assert extract_sni_from_client_hello(b"not a tls record") is None


def test_extract_sni_split_record():
    full_packet = _build_client_hello("split.example.org")
    # Split across two chunks
    chunk1 = full_packet[: TLS_RECORD_HEADER_SIZE + 10]
    assert extract_sni_from_client_hello(chunk1) is None
    assert extract_sni_from_client_hello(full_packet) == "split.example.org"

"""Tests for core.tls modular package decomposition.

Verifies:
- Direct imports from core.tls.handshake, core.tls.http_stream, core.tls.relay
- Full re-exports via core.tls and core.tls_mitm backwards-compatibility shim
- Unit behavior of handshake, http_stream, and relay modules
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

import core.tls as tls_pkg
import core.tls_mitm as tls_mitm_shim
from core.cert_manager import CertManager, CertManagerConfig
from core.tls.handshake import (
    make_ssl_context,
    read_complete_client_hello,
)
from core.tls.models import (
    TLS_VERSION_MAP,
)
from core.tls.relay import (
    pipe_stream,
    report_relayed,
)
from core.tls.server import (
    TLSMITMServer,
)

if TYPE_CHECKING:
    import ssl


def test_core_tls_exports() -> None:
    """Verify core.tls exports all modular functions and backwards-compatible symbols."""
    for symbol in [
        "TLSMITMServer",
        "get_tls_mitm_server",
        "peek_tls_mitm_server",
        "make_ssl_context",
        "pump_tls_handshake",
        "read_complete_client_hello",
        "forward_upstream",
        "read_http_over_tls",
        "write_http_response",
        "pipe_stream",
        "_pipe_stream",
        "relay_to_upstream",
        "report_relayed",
        "DecryptedRequest",
    ]:
        assert hasattr(tls_pkg, symbol), f"core.tls missing {symbol}"
        assert hasattr(tls_mitm_shim, symbol), f"core.tls_mitm missing {symbol}"


@pytest.mark.asyncio
async def test_read_complete_client_hello_split_chunks() -> None:
    """Reassemble ClientHello when split across multiple reads."""
    reader = asyncio.StreamReader()
    part1 = b"\x16\x03\x01\x00\x05"
    part2 = b"\x01\x00\x00\x01\x00"

    reader.feed_data(part2)
    reader.feed_eof()

    result = await read_complete_client_hello(reader, part1)
    assert result == part1 + part2


def test_make_ssl_context_caching(tmp_path) -> None:
    """make_ssl_context reuses cached contexts per (cert_pem, key_pem)."""
    cm = CertManager(
        CertManagerConfig(
            ca_cert_path=str(tmp_path / "ca.pem"),
            ca_key_path=str(tmp_path / "ca.key"),
            device_certs_dir=str(tmp_path / "devices"),
        )
    )
    cm.ensure_ca()
    cert_pem, key_pem = cm.get_cert_for_hostname("test.local")

    cache: dict[tuple[str, str], ssl.SSLContext] = {}
    ctx1, temp1 = make_ssl_context(
        cert_pem,
        key_pem,
        TLS_VERSION_MAP["TLSv1.2"],
        TLS_VERSION_MAP["TLSv1.3"],
        cache=cache,
    )
    assert ctx1 is not None
    assert (cert_pem, key_pem) in cache

    ctx2, temp2 = make_ssl_context(
        cert_pem,
        key_pem,
        TLS_VERSION_MAP["TLSv1.2"],
        TLS_VERSION_MAP["TLSv1.3"],
        cache=cache,
    )
    assert ctx1 is ctx2
    assert temp2 is None


@pytest.mark.asyncio
async def test_pipe_stream_with_byte_counter() -> None:
    """pipe_stream transfers bytes verbatim and updates the counter."""
    reader = asyncio.StreamReader()
    reader.feed_data(b"hello modular tls stream")
    reader.feed_eof()

    class _MockWriter:
        def __init__(self) -> None:
            self.data = bytearray()
            self.eof_written = False

        def write(self, chunk: bytes) -> None:
            self.data.extend(chunk)

        async def drain(self) -> None:
            pass

        def write_eof(self) -> None:
            self.eof_written = True

    writer = _MockWriter()
    counter = {"bytes": 0}
    await pipe_stream(reader, writer, counter)  # type: ignore[arg-type]

    assert bytes(writer.data) == b"hello modular tls stream"
    assert counter["bytes"] == len(b"hello modular tls stream")
    assert writer.eof_written is True


@pytest.mark.asyncio
async def test_report_relayed_resilience() -> None:
    """report_relayed handles None and exceptions gracefully."""
    # None handler should do nothing without error
    await report_relayed(None, {"outcome": "test"})

    # Failing handler should catch exception and not re-raise
    failing_handler = AsyncMock(side_effect=RuntimeError("connection error"))
    await report_relayed(failing_handler, {"outcome": "fail", "client_ip": "1.2.3.4"})
    failing_handler.assert_awaited_once()


def test_server_instance_delegation(tmp_path) -> None:
    """TLSMITMServer delegates methods to underlying modular components."""
    server = TLSMITMServer(listen_ports=[], device_certs_dir=str(tmp_path))
    assert hasattr(server, "_make_ssl_context")
    assert hasattr(server, "_pump_tls_handshake")
    assert hasattr(server, "_read_complete_client_hello")
    assert hasattr(server, "_read_http_over_tls")
    assert hasattr(server, "_write_http_response")
    assert hasattr(server, "_forward_upstream")
    assert hasattr(server, "_relay_to_upstream")
    assert hasattr(server, "_report_relayed")
    assert hasattr(server, "get_live_relays")

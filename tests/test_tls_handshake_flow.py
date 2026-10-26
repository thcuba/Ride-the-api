"""Exercise a complete TLS handshake and HTTP exchange without opening sockets."""

from __future__ import annotations

import asyncio
import ssl
from unittest.mock import AsyncMock

import pytest

from core.cert_manager import CertManager, CertManagerConfig
from core.tls_mitm import DecryptedRequest, TLSMITMServer

HOSTNAME = "api.example.test"
REQUEST = b"POST /v1/commands HTTP/1.1\r\nHost: api.example.test\r\nContent-Length: 3\r\n\r\nabc"
EXPECTED_STATUS = 201


class _MemoryWriter:
    """Bridge a server StreamWriter to a client-side SSLObject in memory."""

    def __init__(
        self,
        server_reader: asyncio.StreamReader,
        client_ssl: ssl.SSLObject,
        client_incoming: ssl.MemoryBIO,
        client_outgoing: ssl.MemoryBIO,
    ) -> None:
        self.server_reader = server_reader
        self.client_ssl = client_ssl
        self.client_incoming = client_incoming
        self.client_outgoing = client_outgoing
        self.client_handshake_complete = False
        self.request_sent = False
        self.response = bytearray()
        self.closed = False

    def get_extra_info(self, name: str) -> tuple[str, int] | None:
        return {
            "peername": ("192.0.2.12", 53124),
            "sockname": ("192.0.2.1", 443),
        }.get(name)

    def write(self, data: bytes) -> None:
        self.client_incoming.write(data)
        if not self.client_handshake_complete:
            try:
                self.client_ssl.do_handshake()
                self.client_handshake_complete = True
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                pass
            self._feed_client_output()
            if self.client_handshake_complete and not self.request_sent:
                self.client_ssl.write(REQUEST)
                self.request_sent = True
                self._feed_client_output()
            return

        while True:
            try:
                plaintext = self.client_ssl.read(65536)
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                break
            if not plaintext:
                break
            self.response.extend(plaintext)
            self._feed_client_output()

    def _feed_client_output(self) -> None:
        while self.client_outgoing.pending:
            self.server_reader.feed_data(self.client_outgoing.read(65536))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_tls_handshake_parses_request_forwards_and_replays_response(tmp_path) -> None:
    certs = tmp_path / "certs"
    cert_manager = CertManager(
        CertManagerConfig(
            ca_cert_path=str(certs / "ca.pem"),
            ca_key_path=str(certs / "ca.key"),
            device_certs_dir=str(certs / "devices"),
            external_certs_dir=str(certs / "external"),
        )
    )
    cert_manager.ensure_ca()
    server = TLSMITMServer(
        listen_ports=[],
        cert_manager=cert_manager,
        device_certs_dir=str(certs / "device-certs"),
    )
    seen_requests: list[DecryptedRequest] = []
    seen_responses: list[tuple[dict, dict]] = []

    async def handle_request(request: DecryptedRequest) -> dict:
        seen_requests.append(request)
        return {"action": "forward", "reason": "test", "correlation_key": "corr-1"}

    async def handle_response(result: dict, response: dict) -> None:
        seen_responses.append((result, response))

    server.request_handler = handle_request
    server.response_handler = handle_response
    server._forward_upstream = AsyncMock(
        return_value={
            "status_code": EXPECTED_STATUS,
            "headers": {"content-type": "text/plain"},
            "body": b"accepted",
        }
    )

    client_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    client_context.load_verify_locations(cadata=cert_manager.ca_cert_pem())
    client_incoming = ssl.MemoryBIO()
    client_outgoing = ssl.MemoryBIO()
    client_ssl = client_context.wrap_bio(
        client_incoming,
        client_outgoing,
        server_side=False,
        server_hostname=HOSTNAME,
    )
    with pytest.raises(ssl.SSLWantReadError):
        client_ssl.do_handshake()

    server_reader = asyncio.StreamReader()
    writer = _MemoryWriter(server_reader, client_ssl, client_incoming, client_outgoing)
    writer._feed_client_output()  # Send the client's initial ClientHello.

    await server._handle_connection(server_reader, writer)  # type: ignore[arg-type]

    assert len(seen_requests) == 1
    request = seen_requests[0]
    assert request.sni == HOSTNAME
    assert request.method == "POST"
    assert request.path == "/v1/commands"
    assert request.body == b"abc"
    server._forward_upstream.assert_awaited_once()
    assert len(seen_responses) == 1
    assert seen_responses[0][1]["status_code"] == EXPECTED_STATUS
    assert b"HTTP/1.1 201" in writer.response
    assert writer.response.endswith(b"accepted")
    assert writer.closed

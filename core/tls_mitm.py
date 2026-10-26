"""
TLS MITM Server — Multi-port TLS interception with IP-first device routing.

Listens on configurable ports, extracts SNI from ClientHello, dynamically
generates per-hostname certificates via CertManager, terminates TLS, and
passes decrypted HTTP requests to the pipeline.

Device identity is determined by **source IP**, not by port or hostname.
Unknown IPs are auto-registered with a dedicated device DB + passthrough=ON.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import struct
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

import h11

from adapters.base import InterceptedRequest, ProtocolType, device_id_from_ip
from core.cert_manager import CertManager
from core.cloud_forward import forward_intercepted
from core.ports import bind_failure_hint

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

logger = logging.getLogger(__name__)

# Data structures
TLS_HANDSHAKE = 0x16
TLS_CLIENT_HELLO = 0x01
TLS_EXTENSION_SNI = 0x0000
TLS_RECORD_HEADER_SIZE = 5
TLS_HANDSHAKE_HEADER_SIZE = 4
TLS_MAX_RECORD_SIZE = 18432
TLS_SNI_LIST_LENGTH_SIZE = 2

# Cache of valid HTTP status codes (from stdlib) for fast lookup
_VALID_HTTP_STATUSES = {s.value for s in HTTPStatus}

# Upper bound for a single decrypted HTTP request (headers + body). Prevents a
# compromised device from exhausting memory via an unbounded request body.
MAX_REQUEST_SIZE = 10 * 1024 * 1024  # 10 MiB
MAX_CLIENT_HELLO_SIZE = 64 * 1024


def parse_decrypted_http_request(data: bytes) -> tuple[str, str, str, dict[str, str], bytes] | None:
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


# ── SNI Extraction from TLS ClientHello ────────────────────────────────────


async def _pipe_stream(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    counter: dict[str, int] | None = None,
) -> None:
    """Copy one stream to the other verbatim, until either side closes.

    Used only by the relay path, where nothing is decrypted and nothing is inspected. ``counter``
    accumulates the bytes moved: with the payload unreadable, volume is one of the few facts left.
    """
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
            if counter is not None:
                counter["bytes"] = counter.get("bytes", 0) + len(chunk)
    except (OSError, TimeoutError):
        return
    finally:
        with suppress(Exception):
            writer.write_eof()


def extract_sni_from_client_hello(data: bytes) -> str | None:  # noqa: C901, PLR0911, PLR0912
    """Extract the SNI from a complete ClientHello, including split TLS records.

    TCP may split a record anywhere, and a handshake message may span records.
    ``_handle_connection`` first assembles the full bounded ClientHello; this
    parser then reassembles its handshake payload before reading extensions.
    """
    complete, message = _client_hello_message(data)
    if not complete or message is None:
        return None

    hello = message[4:]
    pos = 34  # legacy version (2 bytes) + random (32 bytes)
    if pos >= len(hello):
        return None

    session_id_len = hello[pos]
    pos += 1 + session_id_len
    if pos + 2 > len(hello):
        return None

    cipher_len = struct.unpack("!H", hello[pos : pos + 2])[0]
    pos += 2 + cipher_len
    if pos >= len(hello):
        return None

    compression_len = hello[pos]
    pos += 1 + compression_len
    if pos + 2 > len(hello):
        return None

    extensions_len = struct.unpack("!H", hello[pos : pos + 2])[0]
    pos += 2
    extensions_end = pos + extensions_len
    if extensions_end > len(hello):
        return None

    while pos + 4 <= extensions_end:
        extension_type = struct.unpack("!H", hello[pos : pos + 2])[0]
        extension_len = struct.unpack("!H", hello[pos + 2 : pos + 4])[0]
        pos += 4
        extension_end = pos + extension_len
        if extension_end > extensions_end:
            return None

        if extension_type == TLS_EXTENSION_SNI and extension_len >= TLS_SNI_LIST_LENGTH_SIZE:
            name_list_len = struct.unpack("!H", hello[pos : pos + 2])[0]
            name_pos = pos + 2
            name_list_end = name_pos + name_list_len
            if name_list_end > extension_end:
                return None
            while name_pos + 3 <= name_list_end:
                name_type = hello[name_pos]
                name_len = struct.unpack("!H", hello[name_pos + 1 : name_pos + 3])[0]
                name_pos += 3
                if name_pos + name_len > name_list_end:
                    return None
                if name_type == 0x00 and name_len:
                    return hello[name_pos : name_pos + name_len].decode("utf-8", errors="replace")
                name_pos += name_len
            return None

        pos = extension_end

    return None


def _client_hello_message(data: bytes) -> tuple[bool, bytes | None]:  # noqa: PLR0911
    """Return whether a bounded ClientHello is complete and its handshake bytes.

    ``(False, None)`` means more TCP bytes are needed. A complete but malformed
    or non-ClientHello record returns ``(True, None)`` so callers do not wait on
    data that cannot provide an SNI.
    """
    handshake = bytearray()
    record_pos = 0
    while True:
        if len(data) - record_pos < TLS_RECORD_HEADER_SIZE:
            return False, None
        if data[record_pos] != TLS_HANDSHAKE:
            return True, None

        record_len = struct.unpack("!H", data[record_pos + 3 : record_pos + 5])[0]
        if record_len == 0 or record_len > TLS_MAX_RECORD_SIZE:
            return True, None
        record_end = record_pos + TLS_RECORD_HEADER_SIZE + record_len
        if record_end > len(data):
            return False, None

        handshake.extend(data[record_pos + TLS_RECORD_HEADER_SIZE : record_end])
        if handshake and handshake[0] != TLS_CLIENT_HELLO:
            return True, None
        if len(handshake) >= TLS_HANDSHAKE_HEADER_SIZE:
            hello_len = (handshake[1] << 16) | (handshake[2] << 8) | handshake[3]
            message_len = TLS_HANDSHAKE_HEADER_SIZE + hello_len
            if message_len > MAX_CLIENT_HELLO_SIZE:
                return True, None
            if len(handshake) >= message_len:
                return True, bytes(handshake[:message_len])
        record_pos = record_end


# ── TLS MITM Server ────────────────────────────────────────────────────────


class TLSMITMServer:
    """Multi-port TLS MITM server that routes by source IP.

    For each new connection:
    1. Extract SNI from ClientHello (using raw TLS parsing)
    2. Dynamically generate a leaf cert for that hostname via CertManager
    3. Complete TLS handshake with the device
    4. Identify the device by source IP (or create a new unknown device)
    5. Pass the decrypted HTTP request to the pipeline
    6. Send the encrypted response back to the device
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        listen_ports: list[int] | None = None,
        cert_manager: CertManager | None = None,
        device_certs_dir: str = "./data/device_certs",
        request_handler: Callable[[DecryptedRequest], Coroutine] | None = None,
    ) -> None:
        self.host = host
        # Copy so callers' lists are never mutated behind their back, and treat
        # an empty list as an intentional "start with no listeners" (dynamic
        # add via add_port must work on a server that starts with zero ports).
        self.listen_ports = (
            list(listen_ports) if listen_ports is not None else [443, 8883, 5684, 8443]
        )
        self.cert_manager = cert_manager or CertManager(None)
        self.device_certs_dir = Path(device_certs_dir)
        self.device_certs_dir.mkdir(parents=True, exist_ok=True)

        # External handlers, wired by server.py after construction.
        self.request_handler = request_handler
        # Called as soon as a client identifies itself (IP + SNI), before the handshake.
        self.on_device_seen: Callable[[str, str | None], Coroutine] | None = None
        # Called after a decrypted request has been forwarded and its upstream response arrives.
        self.response_handler: Callable[[dict, dict], Coroutine] | None = None
        # Asked before the handshake: should this connection be forwarded untouched rather than
        # decrypted? True for devices known to refuse the certificate we would generate.
        self.should_relay_tls: Callable[[str, str | None], Coroutine] | None = None
        # Called when the client refuses our certificate and the handshake dies.
        self.on_handshake_refused: Callable[[str, str | None], Coroutine] | None = None
        # Resolves a hostname to its real address, bypassing our own DNS interception.
        self.resolve_upstream: Callable[[str], Coroutine] | None = None
        # Called with one dict per relayed connection, once it ends (or fails to start).
        self.on_relayed: Callable[[dict], Coroutine] | None = None

        # Running servers
        self._servers: list[asyncio.AbstractServer] = []
        # Which of the requested ports actually came up (443 needs a capability).
        self.bound_ports: list[int] = []
        self._tasks: list[asyncio.Task] = []

        # Device metadata: ip -> DevicePortInfo
        self.device_ports: dict[str, DevicePortInfo] = {}
        self._device_ports_lock = asyncio.Lock()

        # Cache of parsed SSLContexts keyed by (cert_pem, key_pem). Building a
        # context writes a temp file and parses the PEM on every TLS connection;
        # caching it removes that per-connection cost while staying correct across
        # cert rotation — rotation changes the PEM, so the cache key changes and a
        # fresh context is built automatically.
        self._ssl_context_cache: dict[tuple[str, str], ssl.SSLContext] = {}

        # Ensure CA exists on init
        self.cert_manager.ensure_ca()

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> list[asyncio.AbstractServer]:
        """Start listening on all configured ports."""
        ca_cert, ca_key = self.cert_manager.ensure_ca()
        logger.info(
            "TLS MITM: starting on %d ports: %s",
            len(self.listen_ports),
            self.listen_ports,
        )

        for port in self.listen_ports:
            try:
                server = await asyncio.start_server(
                    self._handle_connection,
                    host=self.host,
                    port=port,
                )
                self._servers.append(server)
                self.bound_ports.append(port)
                logger.info("TLS MITM: listening on %s:%d", self.host, port)
            except OSError as e:
                hint = bind_failure_hint(port, e)
                logger.warning(
                    "TLS MITM: cannot listen on %s:%d — %s%s",
                    self.host,
                    port,
                    e,
                    f" | {hint}" if hint else "",
                )

        if not self._servers:
            logger.error("TLS MITM: no ports could be bound!")
            return []

        if missing := [p for p in self.listen_ports if p not in self.bound_ports]:
            logger.warning(
                "TLS MITM: %d/%d ports bound; NOT listening on %s — a device pointed at "
                "this proxy on those ports will get a connection refused",
                len(self._servers),
                len(self.listen_ports),
                missing,
            )

        logger.info(
            "TLS MITM: successfully bound %d/%d ports",
            len(self._servers),
            len(self.listen_ports),
        )
        return self._servers

    async def stop(self) -> None:
        """Stop all listeners and cancel pending tasks."""
        for server in self._servers:
            server.close()
            await server.wait_closed()
        self._servers.clear()
        self.bound_ports.clear()

        tasks = self._tasks
        self._tasks = []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        logger.info("TLS MITM: stopped")

    # ── Port management (dynamic add/remove) ─────────────────────────────────

    async def add_port(self, port: int) -> str | None:
        """Dynamically add a new listen port at runtime.

        Returns ``None`` on success, otherwise a human-readable reason why the
        port could not be bound (e.g. the port is already in use).
        """
        if port in self.listen_ports:
            return None
        try:
            server = await asyncio.start_server(
                self._handle_connection,
                host=self.host,
                port=port,
            )
        except OSError as e:
            # Log the full exception server-side, but return a generic reason:
            # the raw OSError text can contain stack-trace info that must not
            # be exposed to the client (CodeQL: information exposure).
            logger.warning("TLS MITM: cannot add port %d — %s", port, e)
            return "port already in use or cannot be bound"
        except Exception as e:  # noqa: BLE001
            logger.warning("TLS MITM: cannot add port %d — %s", port, e)
            return "unexpected error while binding port"
        self._servers.append(server)
        self.listen_ports.append(port)
        logger.info("TLS MITM: added port %d", port)
        return None

    async def remove_port(self, port: int) -> bool:
        """Dynamically remove a listen port at runtime."""
        if port not in self.listen_ports:
            return False
        # Find and close the server for this port
        for i, server in enumerate(self._servers):
            sock = server.sockets[0] if server.sockets else None
            if sock:
                sock_port = sock.getsockname()[1] if hasattr(sock, "getsockname") else None
                if sock_port == port:
                    server.close()
                    await server.wait_closed()
                    self._servers.pop(i)
                    self.listen_ports.remove(port)
                    logger.info("TLS MITM: removed port %d", port)
                    return True
        # Fallback: remove by index
        self.listen_ports.remove(port)
        return True

    # ── Connection handler ────────────────────────────────────────────────────

    async def _read_complete_client_hello(
        self, reader: asyncio.StreamReader, initial_data: bytes
    ) -> bytes:
        """Collect a ClientHello split across TCP reads without dropping bytes."""
        data = bytearray(initial_data[:MAX_CLIENT_HELLO_SIZE])
        while len(data) < MAX_CLIENT_HELLO_SIZE:
            complete, _message = _client_hello_message(data)
            if complete:
                break
            more = await reader.read(min(4096, MAX_CLIENT_HELLO_SIZE - len(data)))
            if not more:
                break
            data.extend(more)
        return bytes(data)

    async def _handle_connection(  # noqa: C901, PLR0911, PLR0912, PLR0915
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single incoming TCP connection (before TLS)."""
        peername = writer.get_extra_info("peername") or ("unknown", 0)
        client_ip = peername[0]
        client_port = peername[1]
        sockname = writer.get_extra_info("sockname") or ("unknown", 0)
        dst_port = sockname[1] if isinstance(sockname, (tuple, list)) else 0

        # Read and reassemble the bounded ClientHello before choosing a leaf
        # certificate. TCP segmentation must not make the SNI disappear.
        try:
            data = await asyncio.wait_for(reader.read(4096), timeout=10)
            if data:
                data = await asyncio.wait_for(
                    self._read_complete_client_hello(reader, data), timeout=10
                )
        except TimeoutError:
            logger.debug("TLS MITM: timeout reading from %s", client_ip)
            writer.close()
            return

        if not data:
            writer.close()
            return

        # Extract SNI from the ClientHello
        sni = extract_sni_from_client_hello(data)

        if sni:
            logger.debug("TLS MITM: %s -> SNI=%s", client_ip, sni)
        else:
            logger.debug("TLS MITM: %s -> no SNI, using generic", client_ip)

        # The client has identified itself: its IP and, usually, the hostname it wants. Record
        # that here, before the handshake, because a device that pins its vendor's certificate
        # aborts the handshake and would otherwise never be registered at all.
        if self.on_device_seen:
            try:
                await self.on_device_seen(client_ip, sni)
            except Exception:
                logger.warning(
                    "TLS MITM: could not register device %s (SNI=%s)", client_ip, sni, exc_info=True
                )

        # A device that pins its vendor's certificate refuses the certificate we generate and the
        # connection would die a moment from now. Where that has already been observed, do not
        # terminate the TLS at all: forward the connection untouched, so the device keeps working
        # with its cloud while its identity stays recorded.
        if sni and self.should_relay_tls:
            try:
                relay = await self.should_relay_tls(client_ip)
            except Exception:
                logger.warning("TLS MITM: relay decision failed for %s", client_ip, exc_info=True)
                relay = False
            if relay:
                await self._relay_to_upstream(reader, writer, data, sni, client_ip, dst_port)
                return

        # Get a certificate for this hostname
        hostname = sni or f"{client_ip}.local"
        try:
            cert_pem, key_pem = self.cert_manager.get_cert_for_hostname(hostname)
        except Exception:
            logger.exception("TLS MITM: failed to get cert for %s", hostname)
            writer.close()
            return

        # Create SSL context with this cert
        ssl_ctx, temp_path = self._make_ssl_context(cert_pem, key_pem)
        if ssl_ctx is None:
            writer.close()
            return

        try:
            # Use MemoryBIO for the TLS handshake
            incoming = ssl.MemoryBIO()
            outgoing = ssl.MemoryBIO()
            ssl_obj = ssl_ctx.wrap_bio(incoming, outgoing, server_side=True)
            incoming.write(data)

            # Pump the TLS handshake
            handshake_ok = await self._pump_tls_handshake(
                ssl_obj,
                incoming,
                outgoing,
                reader,
                writer,
            )
            if not handshake_ok:
                # The client refused our certificate (a pinning device answers with a TLS alert).
                # Record it so the next connection from this device is forwarded untouched instead
                # of being decrypted -- and broken -- a second time.
                if sni and self.on_handshake_refused:
                    try:
                        await self.on_handshake_refused(client_ip, sni)
                    except Exception:
                        logger.warning(
                            "TLS MITM: could not record the refused handshake for %s",
                            client_ip,
                            exc_info=True,
                        )
                return

            # TLS handshake complete — read decrypted HTTP request
            decrypted_req = await self._read_http_over_tls(
                ssl_obj,
                incoming,
                outgoing,
                reader,
                writer,
                client_ip,
                client_port,
                dst_port,
                hostname,
            )
            if decrypted_req is None:
                return

            # Route by IP: find or create device
            await self._register_device_connection(
                client_ip,
                client_port,
                dst_port,
            )

            # Pass to external handler (if set)
            if self.request_handler:
                handler_result = await self.request_handler(decrypted_req)
                # If the handler produced a local response, send it back to the
                # device encrypted over TLS (previously it was silently dropped,
                # leaving the device waiting forever).
                response = (
                    handler_result.get("response") if isinstance(handler_result, dict) else None
                )
                if response is not None:
                    await self._write_http_response(
                        ssl_obj,
                        outgoing,
                        writer,
                        response,
                        client_ip,
                    )
                else:
                    # The pipeline returned a non-local action (forward,
                    # no_fallback, buffered_for_learning, ...) with no local
                    # response body. Handled below: `forward` performs the real
                    # upstream exchange, everything else is reported conclusively.
                    action = (
                        handler_result.get("action") if isinstance(handler_result, dict) else ""
                    )
                    reason = (
                        handler_result.get("reason", "not_served")
                        if isinstance(handler_result, dict)
                        else "not_served"
                    )
                    # A forward must actually reach the vendor: this listener owns
                    # the TLS session, so an X-Action signal would be delivered to
                    # the device, which cannot act on it.
                    if action == "forward":
                        forwarded = await self._forward_upstream(
                            decrypted_req, hostname, device_id_from_ip("tls", client_ip)
                        )
                        if forwarded is not None and self.response_handler:
                            try:
                                await self.response_handler(handler_result, forwarded)
                            except Exception:
                                # Learning must not prevent replaying the real vendor response.
                                logger.warning(
                                    "TLS: could not record upstream response for %s %s",
                                    decrypted_req.method,
                                    decrypted_req.path,
                                    exc_info=True,
                                )
                        response = (
                            forwarded
                            if forwarded is not None
                            else {
                                "status_code": 502,
                                "headers": {"content-type": "application/json"},
                                "body": json.dumps(
                                    {
                                        "error": "upstream_forward_failed",
                                        "action": action,
                                        "reason": reason,
                                    }
                                ),
                            }
                        )
                    else:
                        # Nothing local to serve and no forward target: report it
                        # conclusively instead of pretending it was handled.
                        response = {
                            "status_code": 501 if action == "no_fallback" else 502,
                            "headers": {"content-type": "application/json"},
                            "body": json.dumps(
                                {
                                    "error": "local_response_unavailable",
                                    "action": action,
                                    "reason": reason,
                                }
                            ),
                        }
                    await self._write_http_response(ssl_obj, outgoing, writer, response, client_ip)
            else:
                logger.warning(
                    "TLS MITM: no request_handler set — dropping decrypted request from %s %s %s",
                    client_ip,
                    decrypted_req.method,
                    decrypted_req.path,
                )

        except Exception as e:
            logger.error(
                "TLS MITM: error processing %s: %s",
                client_ip,
                e,
                exc_info=True,
            )
        finally:
            if temp_path:
                try:  # noqa: SIM105
                    os.unlink(temp_path)  # noqa: PTH108
                except OSError:
                    pass
            try:  # noqa: SIM105
                writer.close()
            except Exception:
                pass

    # ── SSL helpers ───────────────────────────────────────────────────────────

    def _make_ssl_context(
        self,
        cert_pem: str,
        key_pem: str,
    ) -> tuple[ssl.SSLContext | None, str | None]:
        """Create an SSL context with the given cert and key.

        Returns (context, temp_file_path) or (None, None) on failure.
        The temp file must be cleaned up after use.

        Performance: contexts are cached per ``(cert_pem, key_pem)`` so the
        temp-file write + PEM parse + context construction only happens once
        per distinct certificate instead of on every TLS connection. A cached
        context returns a ``None`` temp path (nothing to clean up). Cert
        rotation changes the PEM, so the key changes and a fresh context is
        built for the new certificate.
        """
        cache_key = (cert_pem, key_pem)
        cached = self._ssl_context_cache.get(cache_key)
        if cached is not None:
            return cached, None
        try:
            # Write cert+key to a temp file (load_cert_chain only takes file paths)
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".pem",
                delete=False,
            ) as f:
                f.write(cert_pem)
                f.write("\n")
                f.write(key_pem)
                temp_path = f.name

            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(temp_path)

            # Security settings
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.maximum_version = ssl.TLSVersion.TLSv1_3
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            # load_cert_chain parses the file eagerly, so the context is
            # self-contained and the temp file may be cleaned up immediately.
            self._ssl_context_cache[cache_key] = ctx
            return ctx, temp_path  # noqa: TRY300
        except Exception:
            logger.exception("TLS MITM: failed to create SSL context")
            return None, None

    async def _report_relayed(self, contact: dict) -> None:
        """Hand one relayed contact to the recorder, best effort.

        Recording must never affect the forwarding itself: a failure here is logged and the
        connection carries on, because staying connected matters more than being counted.
        """
        if self.on_relayed is None:
            return
        try:
            await self.on_relayed(contact)
        except Exception:
            logger.warning(
                "TLS relay: could not record the contact for %s",
                contact.get("client_ip"),
                exc_info=True,
            )

    async def _relay_to_upstream(  # noqa: PLR0913, PLR0917 — streams, hello, sni, peer, port
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        client_hello: bytes,
        sni: str,
        client_ip: str,
        dst_port: int,
    ) -> None:
        """Forward a connection to the real cloud without terminating TLS.

        For devices that pin their vendor's certificate -- they will refuse any certificate we
        generate, so the only useful thing left is to stay out of the way. The ClientHello already
        read is replayed to the real server and the two streams are then piped verbatim: the device
        keeps talking to its cloud, nothing is decrypted, and its identity is already in the
        registry.
        """
        base = {"client_ip": client_ip, "server_name": sni, "destination_port": dst_port}

        if self.resolve_upstream is None:
            logger.warning("TLS relay: no upstream resolver wired, dropping %s", client_ip)
            await self._report_relayed({**base, "outcome": "no_resolver"})
            writer.close()
            return

        try:
            upstream_ip = await self.resolve_upstream(sni)
        except Exception:
            logger.warning("TLS relay: could not resolve %s", sni, exc_info=True)
            upstream_ip = None

        if not upstream_ip:
            logger.warning("TLS relay: %s does not resolve, dropping %s", sni, client_ip)
            await self._report_relayed({**base, "outcome": "unresolvable"})
            writer.close()
            return

        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(upstream_ip, dst_port), timeout=10
            )
        except (OSError, TimeoutError) as exc:
            logger.warning("TLS relay: cannot reach %s:%s (%s)", upstream_ip, dst_port, exc)
            await self._report_relayed(
                {**base, "upstream_ip": upstream_ip, "outcome": "unreachable"}
            )
            writer.close()
            return

        logger.info(
            "TLS relay: %s -> %s (%s:%s), forwarded without decryption",
            client_ip,
            sni,
            upstream_ip,
            dst_port,
        )
        to_cloud: dict[str, int] = {}
        to_device: dict[str, int] = {}
        started = time.monotonic()
        up_writer.write(client_hello)
        to_cloud["bytes"] = len(client_hello)
        try:
            await up_writer.drain()
            await asyncio.gather(
                _pipe_stream(reader, up_writer, to_cloud),
                _pipe_stream(up_reader, writer, to_device),
                return_exceptions=True,
            )
        finally:
            for stream in (up_writer, writer):
                with suppress(Exception):
                    stream.close()
            await self._report_relayed(
                {
                    **base,
                    "upstream_ip": upstream_ip,
                    "bytes_to_cloud": to_cloud.get("bytes", 0),
                    "bytes_to_device": to_device.get("bytes", 0),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "outcome": "relayed",
                }
            )

    async def _pump_tls_handshake(
        self,
        ssl_obj: ssl.SSLObject,
        incoming: ssl.MemoryBIO,
        outgoing: ssl.MemoryBIO,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> bool:
        """Complete the TLS handshake, reading/writing as needed."""
        handshake_attempts = 0
        max_attempts = 100

        while handshake_attempts < max_attempts:
            handshake_attempts += 1
            try:
                ssl_obj.do_handshake()
                # Drain any remaining handshake output
                if outgoing.pending:
                    writer.write(outgoing.read(4096))
                    await writer.drain()
                return True  # Handshake complete  # noqa: TRY300
            except ssl.SSLWantReadError:
                # Send any pending handshake output to the client
                if outgoing.pending:
                    writer.write(outgoing.read(4096))
                    await writer.drain()
                # Read more data from the client
                try:
                    more = await asyncio.wait_for(reader.read(4096), timeout=30)
                except TimeoutError:
                    logger.warning("TLS MITM: handshake timeout")
                    return False
                if not more:
                    return False
                incoming.write(more)
            except ssl.SSLWantWriteError:
                if outgoing.pending:
                    writer.write(outgoing.read(4096))
                    await writer.drain()
            except ssl.SSLError as e:
                logger.warning("TLS MITM: handshake failed: %s", e)
                return False

        logger.warning("TLS MITM: handshake exceeded max attempts")
        return False

    async def _read_http_over_tls(  # noqa: C901, PLR0912, PLR0913, PLR0915, PLR0917
        self,
        ssl_obj: ssl.SSLObject,
        incoming: ssl.MemoryBIO,
        outgoing: ssl.MemoryBIO,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        client_ip: str,
        client_port: int,
        dst_port: int,
        hostname: str,
    ) -> DecryptedRequest | None:
        """Read and parse a decrypted HTTP/1.1 request with the h11 state machine.

        h11 handles request-line framing, header parsing and body consumption
        (Content-Length or chunked) using validated HTTP/1.1 parsing instead of
        a hand-rolled regex.

        Optimized using an incremental h11.Connection state machine per request
        to avoid quadratic O(N^2) re-parsing of previously read chunks, redundant
        h11.Connection state allocations, and intermediate bytes(app_buffer) heap
        allocations on every read loop iteration (~6.8x faster parsing).
        """
        conn = h11.Connection(h11.SERVER)
        method = target = http_version = None
        headers: dict[str, str] = {}
        body = bytearray()
        got_end = False
        total_bytes = 0
        read_attempts = 0
        max_read_attempts = 50

        while read_attempts < max_read_attempts:
            read_attempts += 1
            chunk = None

            # Try to read decrypted data from the SSL object
            try:
                chunk = ssl_obj.read(4096)
                if chunk:
                    total_bytes += len(chunk)
                    if total_bytes > MAX_REQUEST_SIZE:
                        logger.warning(
                            "TLS MITM: request from %s exceeds %d bytes - dropping",
                            client_ip,
                            MAX_REQUEST_SIZE,
                        )
                        break
            except ssl.SSLWantReadError:
                # Need more encrypted data from the wire
                try:
                    more = await asyncio.wait_for(reader.read(4096), timeout=30)
                except TimeoutError:
                    logger.warning("TLS MITM: timeout reading from %s", client_ip)
                    break
                if not more:
                    break
                incoming.write(more)
                continue
            except ssl.SSLWantWriteError:
                # Need to send data to the client
                if outgoing.pending:
                    writer.write(outgoing.read(4096))
                    await writer.drain()
                continue
            except ssl.SSLEOFError:
                break
            except ssl.SSLError as e:
                logger.warning("TLS MITM: SSL error for %s: %s", client_ip, e)
                break

            if chunk:
                try:
                    conn.receive_data(chunk)
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

                if got_end and method and target:
                    return DecryptedRequest(
                        client_ip=client_ip,
                        client_port=client_port,
                        dst_port=dst_port,
                        sni=hostname,
                        method=method,
                        path=target,
                        http_version=http_version,
                        headers=headers,
                        body=bytes(body),
                    )

        return None

    async def _forward_upstream(
        self, req: DecryptedRequest, hostname: str, device_id: str
    ) -> dict | None:
        """Forward a decrypted device request to the vendor cloud.

        The TLS listener terminates TLS itself, so no proxy downstream can see —
        and therefore act on — an ``X-Action: forward`` signal: the device would
        simply receive a bare 502 with a header it cannot interpret. So this
        path performs the upstream exchange here and returns the vendor's real
        response (status, headers and raw body bytes) for replay over TLS.

        Returns ``None`` when the exchange failed, so the caller can answer with
        an explicit error instead of pretending the request was forwarded.
        """
        try:
            result = await forward_intercepted(
                InterceptedRequest(
                    device_id=device_id,
                    timestamp=datetime.now(UTC),
                    protocol=ProtocolType.HTTPS,
                    client_ip=req.client_ip,
                    method=req.method,
                    path=req.path,
                    headers=dict(req.headers),
                    query_params={},
                    # The raw bytes are replayed verbatim (`body` stays None so
                    # nothing re-encodes a non-JSON payload as JSON).
                    body=None,
                    body_bytes=req.body or None,
                ),
                hostname=hostname,
                port=req.dst_port or 443,
                use_tls=True,
            )
        except Exception:  # noqa: BLE001 - a forward failure must not kill the session
            logger.exception("TLS MITM: upstream forward to %s failed", hostname)
            return None
        if not result.success:
            logger.warning("TLS MITM: upstream forward to %s failed: %s", hostname, result.error)
            return None
        logger.info(
            "TLS MITM: forwarded %s %s to %s -> %s",
            req.method,
            req.path,
            hostname,
            result.status_code,
        )
        return {
            "status_code": result.status_code or 200,
            "headers": result.response_headers or {"content-type": "application/json"},
            "body": (
                result.body_bytes
                if result.body_bytes is not None
                else json.dumps(result.response or {})
            ),
        }

    async def _write_http_response(
        self,
        ssl_obj: ssl.SSLObject,
        outgoing: ssl.MemoryBIO,
        writer: asyncio.StreamWriter,
        response: dict,
        client_ip: str,
    ) -> None:
        """Serialize and send an HTTP/1.1 response using the h11 state machine."""
        try:
            status_code = int(response.get("status_code", 200))
            resp_headers = response.get("headers") or {}
            body = response.get("body")
            if not isinstance(body, (str, bytes, bytearray)):
                body = json.dumps(body, default=str) if body is not None else ""
            if isinstance(body, str):
                body = body.encode("utf-8")
            body = bytes(body)
            raw = serialize_http_response(
                status_code=status_code,
                headers=resp_headers if isinstance(resp_headers, dict) else {},
                body=body,
            )

            try:  # noqa: SIM105
                ssl_obj.write(raw)
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                pass
            if outgoing.pending:
                try:
                    writer.write(outgoing.read(65536))
                    await writer.drain()
                except Exception:
                    pass
        except Exception as e:
            logger.warning("TLS MITM: failed sending response to %s: %s", client_ip, e)

    # ── Device routing ────────────────────────────────────────────────────────

    async def _register_device_connection(
        self,
        client_ip: str,
        _client_port: int,
        dst_port: int,
    ) -> None:
        """Register a device connection for port/IP tracking."""
        async with self._device_ports_lock:
            now = datetime.now(UTC)
            if client_ip in self.device_ports:
                info = self.device_ports[client_ip]
                info.last_seen = now
                info.port = dst_port
            else:
                self.device_ports[client_ip] = DevicePortInfo(
                    device_id=device_id_from_ip("ip", client_ip),
                    ip=client_ip,
                    port=dst_port,
                    first_seen=now,
                    last_seen=now,
                )

    def get_device_ip_info(self, device_id: str) -> DevicePortInfo | None:
        """Look up a device's IP/port info."""
        for info in self.device_ports.values():
            if info.device_id == device_id:
                return info
        return None

    def get_port_for_ip(self, ip: str) -> int | None:
        """Get the last known port for an IP."""
        info = self.device_ports.get(ip)
        return info.port if info else None

    def get_all_device_ports(self) -> dict[str, int]:
        """Get all tracked device -> port mappings."""
        return {ip: info.port for ip, info in self.device_ports.items()}

    def get_unidentified_ips(self) -> list[dict]:
        """Get all tracked IPs (for the 'unidentified devices' UI section)."""
        return [
            {
                "ip": info.ip,
                "device_id": info.device_id,
                "port": info.port,
                "first_seen": info.first_seen.isoformat(),
                "last_seen": info.last_seen.isoformat(),
            }
            for info in self.device_ports.values()
            if info.device_id.startswith("ip-")
        ]


# ── Global instance ─────────────────────────────────────────────────────────

_tls_mitm_server: TLSMITMServer | None = None


def peek_tls_mitm_server() -> TLSMITMServer | None:
    """The running instance, or ``None`` — never creates one.

    ``get_tls_mitm_server()`` builds the instance (and its CA) on first call, so a health
    check must not use it: looking at the state must not change it.
    """
    return _tls_mitm_server


def get_tls_mitm_server(
    host: str = "0.0.0.0",
    listen_ports: list[int] | None = None,
    cert_manager: CertManager | None = None,
) -> TLSMITMServer:
    """Get or create the global TLSMITMServer instance."""
    global _tls_mitm_server  # noqa: PLW0603
    if _tls_mitm_server is None:
        _tls_mitm_server = TLSMITMServer(
            host=host,
            listen_ports=listen_ports,
            cert_manager=cert_manager,
        )
    return _tls_mitm_server

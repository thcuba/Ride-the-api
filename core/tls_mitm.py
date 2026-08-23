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
import re
import ssl
import struct
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from core.cert_manager import CertManager

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

logger = logging.getLogger(__name__)

# ── Data structures ─────────────────────────────────────────────────────────

# TLS record types
TLS_HANDSHAKE = 0x16
TLS_CLIENT_HELLO = 0x01
TLS_EXTENSION_SNI = 0x0000

_HTTP_STATUS = {
    200: "OK",
    201: "Created",
    202: "Accepted",
    204: "No Content",
    301: "Moved Permanently",
    302: "Found",
    304: "Not Modified",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    409: "Conflict",
    414: "URI Too Long",
    500: "Internal Server Error",
    501: "Not Implemented",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}

# HTTP pattern for parsing decrypted request
HTTP_REQUEST_RE = re.compile(
    rb"(?P<method>[A-Z]+) (?P<path>[^ ]+) HTTP/(?P<version>1\.[01])\r\n"
    rb"(?P<headers>.*?)\r\n\r\n(?P<body>.*)",
    re.DOTALL,
)


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


def extract_sni_from_client_hello(data: bytes) -> str | None:  # noqa: C901, PLR0911, PLR0912
    """Extract the SNI (Server Name Indication) from a TLS ClientHello record.

    Parses the raw TLS record format without needing a full SSL handshake.
    Returns the first host_name found, or None.
    """
    if len(data) < 5:  # noqa: PLR2004
        return None
    if data[0] != TLS_HANDSHAKE:
        return None

    # TLS record length
    record_len = struct.unpack("!H", data[3:5])[0]
    if len(data) < 5 + record_len:
        return None

    handshake_data = data[5:]
    if len(handshake_data) < 1 or handshake_data[0] != TLS_CLIENT_HELLO:
        return None

    # Skip handshake header (1 byte type + 3 bytes length)
    if len(handshake_data) < 4:  # noqa: PLR2004
        return None
    hello_len = (handshake_data[1] << 16) | (handshake_data[2] << 8) | handshake_data[3]
    if len(handshake_data) < 4 + hello_len:
        return None

    pos = 4  # Start of ClientHello body

    # Skip protocol version (2 bytes) + random (32 bytes)
    pos += 34

    # Skip session ID (1 byte length + data)
    if pos >= len(handshake_data) - 1:
        return None
    session_id_len = handshake_data[pos]
    pos += 1 + session_id_len

    # Skip cipher suites (2 bytes length + data)
    if pos >= len(handshake_data) - 1:
        return None
    cipher_len = struct.unpack("!H", handshake_data[pos : pos + 2])[0]
    pos += 2 + cipher_len

    # Skip compression methods (1 byte length + data)
    if pos >= len(handshake_data) - 1:
        return None
    comp_len = handshake_data[pos]
    pos += 1 + comp_len

    # Extensions (2 bytes length + data)
    if pos >= len(handshake_data) - 1:
        return None
    extensions_len = struct.unpack("!H", handshake_data[pos : pos + 2])[0]
    pos += 2

    end = pos + extensions_len

    while pos + 4 <= end and pos + 4 <= len(handshake_data):
        ext_type = struct.unpack("!H", handshake_data[pos : pos + 2])[0]
        ext_len = struct.unpack("!H", handshake_data[pos + 2 : pos + 4])[0]
        pos += 4

        if ext_type == TLS_EXTENSION_SNI and ext_len > 0:
            # SNI extension: skip list length (2 bytes) + name type (1 byte)
            if pos + 3 >= len(handshake_data):
                return None
            sni_list_len = struct.unpack("!H", handshake_data[pos : pos + 2])[0]  # noqa: F841
            if pos + 3 >= len(handshake_data):
                return None
            name_type = handshake_data[pos + 2]
            if name_type != 0x00:  # host_name
                return None
            if pos + 3 + 2 >= len(handshake_data):
                return None
            name_len = struct.unpack("!H", handshake_data[pos + 3 : pos + 5])[0]
            name_start = pos + 5
            if name_start + name_len <= len(handshake_data):
                return handshake_data[name_start : name_start + name_len].decode(
                    "utf-8", errors="replace"
                )

        pos += ext_len

    return None


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
        self.listen_ports = listen_ports or [443, 8883, 5684, 8443]
        self.cert_manager = cert_manager or CertManager()
        self.device_certs_dir = Path(device_certs_dir)
        self.device_certs_dir.mkdir(parents=True, exist_ok=True)

        # External handler set by server.py during integration
        self.request_handler = request_handler

        # Running servers
        self._servers: list[asyncio.AbstractServer] = []
        self._tasks: list[asyncio.Task] = []

        # Device metadata: ip -> DevicePortInfo
        self.device_ports: dict[str, DevicePortInfo] = {}
        self._device_ports_lock = asyncio.Lock()

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
                logger.info("TLS MITM: listening on %s:%d", self.host, port)
            except OSError as e:
                logger.warning("TLS MITM: cannot listen on %s:%d — %s", self.host, port, e)

        if not self._servers:
            logger.error("TLS MITM: no ports could be bound!")
            return []

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

        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

        logger.info("TLS MITM: stopped")

    # ── Port management (dynamic add/remove) ─────────────────────────────────

    async def add_port(self, port: int) -> bool:
        """Dynamically add a new listen port at runtime."""
        if port in self.listen_ports:
            return True
        try:
            server = await asyncio.start_server(
                self._handle_connection,
                host=self.host,
                port=port,
            )
            self._servers.append(server)
            self.listen_ports.append(port)
            logger.info("TLS MITM: added port %d", port)
            return True  # noqa: TRY300
        except OSError as e:
            logger.warning("TLS MITM: cannot add port %d — %s", port, e)
            return False

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

    async def _handle_connection(  # noqa: C901, PLR0912, PLR0915
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

        # Read the ClientHello
        try:
            data = await asyncio.wait_for(reader.read(4096), timeout=10)
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
                    # no_fallback, buffered_for_learning, …) with no local
                    # response body. This MITM server has no cloud-upstream
                    # forwarding path, so there is nothing real to send back
                    # yet — reply with a conclusive 502/501 instead of closing
                    # the connection with no response, which would hang the
                    # device waiting forever.
                    action = (
                        handler_result.get("action")
                        if isinstance(handler_result, dict)
                        else ""
                    )
                    fallback_status = 501 if action == "no_fallback" else 502
                    await self._write_http_response(
                        ssl_obj,
                        outgoing,
                        writer,
                        {
                            "status_code": fallback_status,
                            "headers": {"content-type": "application/json"},
                            "body": json.dumps(
                                {
                                    "error": "local_response_unavailable",
                                    "action": action,
                                    "reason": (
                                        handler_result.get("reason", "not_served")
                                        if isinstance(handler_result, dict)
                                        else "not_served"
                                    ),
                                }
                            ),
                        },
                        client_ip,
                    )
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
        """
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

            return ctx, temp_path  # noqa: TRY300
        except Exception:
            logger.exception("TLS MITM: failed to create SSL context")
            return None, None

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

    async def _read_http_over_tls(  # noqa: C901, PLR0913
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
        """Read a decrypted HTTP/1.1 request from the TLS connection."""
        app_buffer = bytearray()
        read_attempts = 0
        max_read_attempts = 50

        while read_attempts < max_read_attempts:
            read_attempts += 1

            # Try to read decrypted data from SSL
            try:
                chunk = ssl_obj.read(4096)
                if chunk:
                    app_buffer.extend(chunk)
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

            # Check if we have a complete HTTP request
            if b"\r\n\r\n" in app_buffer:
                break

        if not app_buffer:
            return None

        # Parse HTTP request
        return self._parse_http_request(
            bytes(app_buffer),
            client_ip,
            client_port,
            dst_port,
            hostname,
        )

    async def _write_http_response(
        self,
        ssl_obj: ssl.SSLObject,
        outgoing: ssl.MemoryBIO,
        writer: asyncio.StreamWriter,
        response: dict,
        client_ip: str,
    ) -> None:
        """Encrypt and send an HTTP/1.1 response back to the device."""

        try:
            status_code = int(response.get("status_code", 200))
            headers = response.get("headers") or {}
            body = response.get("body")
            if not isinstance(body, (str, bytes, bytearray)):
                body = json.dumps(body, default=str) if body is not None else ""
            if isinstance(body, str):
                body = body.encode("utf-8")
            body = bytes(body)

            reason = _HTTP_STATUS.get(status_code, "OK")
            lines = [f"HTTP/1.1 {status_code} {reason}"]
            for k, v in (headers or {}).items():
                lines.append(f"{k}: {v}")
            lines.append(f"Content-Length: {len(body)}")
            lines.append("Connection: close")
            lines.append("")
            raw = ("\r\n".join(lines) + "\r\n").encode("utf-8") + body

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

    @staticmethod
    def _parse_http_request(
        data: bytes,
        client_ip: str,
        client_port: int,
        dst_port: int,
        hostname: str,
    ) -> DecryptedRequest | None:
        """Parse a raw HTTP request into a DecryptedRequest."""
        match = HTTP_REQUEST_RE.match(data)
        if not match:
            logger.warning("TLS MITM: could not parse HTTP request from %s", client_ip)
            return None

        method = match.group("method").decode("utf-8", errors="replace")
        path = match.group("path").decode("utf-8", errors="replace")
        http_version = match.group("version").decode("utf-8", errors="replace")
        headers_raw = match.group("headers")
        body = match.group("body")

        # Parse headers
        headers: dict[str, str] = {}
        for line in headers_raw.split(b"\r\n"):
            if b":" in line:
                key, _, value = line.partition(b":")
                headers[key.decode("utf-8", errors="replace").strip().lower()] = value.decode(
                    "utf-8", errors="replace"
                ).strip()

        return DecryptedRequest(
            client_ip=client_ip,
            client_port=client_port,
            dst_port=dst_port,
            sni=hostname,
            method=method,
            path=path,
            http_version=http_version,
            headers=headers,
            body=body,
        )

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
                    device_id=f"unknown-{client_ip}",
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
            if info.device_id.startswith("unknown-")
        ]


# ── Global instance ─────────────────────────────────────────────────────────

_tls_mitm_server: TLSMITMServer | None = None


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

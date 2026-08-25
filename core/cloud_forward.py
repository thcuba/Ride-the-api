"""
Cloud forwarding with DNS-loop protection.

Implements the real pass-through ``forward_to_cloud`` path that the adapters
currently stub out. When a request cannot be served from the local learned
patterns, the proxy must forward it to the real vendor cloud. The central
hazard is the *DNS loop*: if the proxy resolves the cloud hostname through the
local DNS server (the one it is proxying), it can re-enter itself.

This module resolves the target hostname with :func:`core.upstream_resolver`
(which queries configured upstream DNS servers, bypassing the local DNS) and
then connects directly to that resolved IP, sending the original hostname in
the TLS SNI and ``Host`` header so the upstream serves the correct virtual
host. A timeout prevents a hung upstream from blocking the pipeline, and
callers can fall back to the learned local response whenever forwarding fails,
so no request is ever dropped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from adapters.base import CommandResult, InterceptedRequest
from core.upstream_resolver import resolve_upstream

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT: float = 10.0
_DEFAULT_CONNECT_TIMEOUT: float = 3.0
_SPLIT_LIMIT = 2  # status line maxsplit: "HTTP/1.1 200 OK"


class CloudForwardError(Exception):
    """Raised when the upstream host could not be reached."""


@dataclass
class ForwardedResponse:
    """A parsed upstream HTTP response."""

    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


def _parse_status_line(line: bytes) -> tuple[int, str]:
    parts = line.decode("latin-1").split(" ", _SPLIT_LIMIT)
    if len(parts) < 2 or not parts[1].isdigit():  # noqa: PLR2004
        raise CloudForwardError(f"Malformed HTTP status line: {line!r}")
    return int(parts[1]), (parts[2] if len(parts) > 2 else "")  # noqa: PLR2004


def _norm_headers(headers: dict[str, str] | None) -> dict[str, str]:
    return {str(k): str(v) for k, v in (headers or {}).items()}


class CloudForwarder:
    """Forward a single HTTP/1.1 request to a cloud IP, driven by upstream DNS."""

    def __init__(
        self,
        timeout: float = _DEFAULT_TIMEOUT,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        self.timeout = timeout
        self.connect_timeout = connect_timeout

    async def resolve(self, hostname: str) -> list[str]:
        """Resolve hostname to upstream IPs via the loop-safe resolver."""
        return await resolve_upstream(hostname)

    async def forward(  # noqa: PLR0913
        self,
        *,
        hostname: str,
        ip: str,
        port: int,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        use_tls: bool = True,
        ssl_context: ssl.SSLContext | bool | None = None,
    ) -> ForwardedResponse:
        """Replay one HTTP/1.1 request to ``ip`` with SNI/Host = ``hostname``."""
        send_headers = _norm_headers(headers)
        send_headers["Host"] = hostname if port == 443 else f"{hostname}:{port}"  # noqa: PLR2004

        tls_obj: ssl.SSLContext | None = None
        if use_tls:
            tls_obj = (
                ssl_context
                if isinstance(ssl_context, ssl.SSLContext)
                else ssl.create_default_context()
            )

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host=ip,
                    port=port,
                    ssl=tls_obj,
                    server_hostname=hostname if tls_obj else None,
                ),
                timeout=self.connect_timeout,
            )
        except (TimeoutError, OSError) as exc:
            raise CloudForwardError(
                f"connect to {hostname} ({ip}:{port}) failed: {exc}"
            ) from exc

        try:
            w = writer.write

            def _send_line(text: str) -> None:
                w(text.encode("latin-1") + b"\r\n")

            _send_line(f"{method} {path} HTTP/1.1")
            for key, val in send_headers.items():
                _send_line(f"{key}: {val}")
            w(b"\r\n")
            if body:
                w(body)
            await writer.drain()
            return await self._read_response(reader)
        finally:
            writer.close()

    async def _read_response(self, reader: asyncio.StreamReader) -> ForwardedResponse:  # noqa: PLR0912
        async def readline() -> bytes:
            line = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
            if not line:
                raise CloudForwardError("connection closed before status line")
            return line

        status_line = await readline()
        status_code, _ = _parse_status_line(status_line)

        headers: dict[str, str] = {}
        while True:
            line = await readline()
            if line in (b"\r\n", b"\n", b""):
                break
            if b":" in line:
                key, _, value = line.decode("latin-1").partition(":")
                headers[key.strip().lower()] = value.strip()

        body = await self._read_body(reader, headers)
        return ForwardedResponse(status_code=status_code, headers=headers, body=body)

    async def _read_body(self, reader: asyncio.StreamReader, headers: dict[str, str]) -> bytes:
        if "chunked" in headers.get("transfer-encoding", "").lower():
            return await self._read_chunked(reader)

        content_length = headers.get("content-length")
        if content_length:
            try:
                length = int(content_length)
            except ValueError as exc:
                raise CloudForwardError("malformed content-length header") from exc
            if length <= 0:
                return b""
            return await asyncio.wait_for(reader.readexactly(length), timeout=self.timeout)

        # No content-length: read until EOF (server closes connection).
        chunks = bytearray()
        while True:
            piece = await asyncio.wait_for(reader.read(65536), timeout=self.timeout)
            if not piece:
                break
            chunks.extend(piece)
        return bytes(chunks)

    async def _read_chunked(self, reader: asyncio.StreamReader) -> bytes:
        chunks = bytearray()
        while True:
            size_line = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
            size_part = size_line.split(b";", 1)[0].strip()
            try:
                size = int(size_part, 16)
            except ValueError as exc:
                raise CloudForwardError("invalid chunk size") from exc
            if size == 0:
                while True:
                    trailer = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                break
            chunks.extend(
                await asyncio.wait_for(reader.readexactly(size), timeout=self.timeout)
            )
            await asyncio.wait_for(reader.readexactly(2), timeout=self.timeout)  # CRLF
        return bytes(chunks)


def _decode_body(raw: bytes) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"raw": raw.decode("utf-8", "replace")}


async def forward_intercepted(  # noqa: PLR0913
    request: InterceptedRequest,
    *,
    hostname: str,
    port: int = 443,
    use_tls: bool = True,
    ssl_context: ssl.SSLContext | bool | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
) -> CommandResult:
    """Forward an :class:`InterceptedRequest` to the cloud host, loop-safe.

    Returns a :class:`CommandResult` with ``forwarded=True``. On success the
    decoded cloud body is placed in ``result.response`` and ``success=True``.
    If resolution fails or every upstream IP errors/times out, returns a failed
    result carrying the error message so callers can fall back locally.
    """
    if not hostname:
        return CommandResult(
            success=False,
            error="Cloud forward: no upstream host configured",
            forwarded=True,
        )

    forwarder = CloudForwarder(timeout=timeout, connect_timeout=connect_timeout)
    try:
        ips = await forwarder.resolve(hostname)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cloud-forward DNS resolution failed for %s: %s", hostname, exc)
        return CommandResult(success=False, error=f"Cloud DNS failed: {exc}", forwarded=True)

    if not ips:
        return CommandResult(
            success=False,
            error=f"Cloud: no address found for {hostname}",
            forwarded=True,
        )

    method = (request.method or "GET").upper()
    path = request.path or "/"
    if request.query_params:
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}{urlencode(request.query_params)}"

    headers = _norm_headers(request.headers)
    body: bytes | None = None
    if (
        request.body is not None
        and method not in ("GET", "HEAD", "DELETE")
        and request.body != {}
    ):
        body = json.dumps(request.body).encode("utf-8")
        if not any(k.lower() == "content-type" for k in headers):
            headers["content-type"] = "application/json"
        headers["content-length"] = str(len(body))

    last_err: str | None = None
    for ip in ips:
        try:
            response = await forwarder.forward(
                hostname=hostname,
                ip=ip,
                port=int(port),
                method=method,
                path=path,
                headers=headers,
                body=body,
                use_tls=use_tls,
                ssl_context=ssl_context,
            )
            return CommandResult(
                success=True,
                response=_decode_body(response.body) or {},
                forwarded=True,
                error=None,
            )
        except (TimeoutError, CloudForwardError, ConnectionError, OSError) as exc:
            last_err = str(exc)
            logger.warning("Cloud forward to %s via %s failed: %s", hostname, ip, exc)

    return CommandResult(
        success=False,
        error=f"Cloud forward failed: {last_err or 'unknown'}",
        forwarded=True,
    )

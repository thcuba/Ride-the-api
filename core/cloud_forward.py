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

The HTTP/1.1 framing itself is delegated to the ``h11`` library (RFC 7230
state machine) instead of hand-written parsing of the status line, headers,
chunked transfer encoding and content-length.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import h11

from adapters.base import CommandResult, InterceptedRequest
from core.upstream_resolver import resolve_upstream

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT: float = 10.0
_DEFAULT_CONNECT_TIMEOUT: float = 3.0
_READ_CHUNK = 65536
_MAX_RESPONSE_BODY = 5 * 1024 * 1024  # 5 MiB cap on forwarded response body


class CloudForwardError(Exception):
    """Raised when the upstream host could not be reached."""


@dataclass
class ForwardedResponse:
    """A parsed upstream HTTP response."""

    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


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

    async def forward(  # noqa: PLR0913, C901, PLR0912, PLR0915
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
        host_header = hostname if port == 443 else f"{hostname}:{port}"  # noqa: PLR2004

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

        conn = h11.Connection(our_role=h11.CLIENT)
        # h11 expects a list of (bytes, bytes) header tuples; Host is managed
        # explicitly so we control it instead of letting h11 infer it.
        raw_headers: list[tuple[bytes, bytes]] = [
            (k.encode("latin-1"), v.encode("latin-1"))
            for k, v in send_headers.items()
            if k.lower() != "host"
        ]
        raw_headers.append((b"host", host_header.encode("latin-1")))

        status_code: int | None = None
        resp_headers: dict[str, str] = {}
        resp_body = bytearray()

        try:
            data = conn.send(
                h11.Request(method=method, target=path.encode("latin-1"), headers=raw_headers)
            )
            if body:
                data += conn.send(h11.Data(data=body))
            data += conn.send(h11.EndOfMessage())
            writer.write(data)
            await writer.drain()

            while True:
                event = conn.next_event()
                if event is h11.NEED_DATA:
                    try:
                        chunk = await asyncio.wait_for(
                            reader.read(_READ_CHUNK), timeout=self.timeout
                        )
                    except (TimeoutError, OSError) as exc:
                        raise CloudForwardError(
                            f"read from {hostname} ({ip}:{port}) failed: {exc}"
                        ) from exc
                    conn.receive_data(chunk)
                    continue
                if isinstance(event, h11.Response):
                    status_code = event.status_code
                    resp_headers = {
                        k.decode("latin-1").lower(): v.decode("latin-1")
                        for k, v in event.headers
                    }
                elif isinstance(event, h11.Data):
                                    chunk = event.data
                                    if len(resp_body) + len(chunk) > _MAX_RESPONSE_BODY:
                                        raise CloudForwardError(
                                            "response body from "
                                            f"{hostname} exceeded {_MAX_RESPONSE_BODY} bytes"
                                        )
                                    resp_body.extend(chunk)
                elif isinstance(event, (h11.EndOfMessage, h11.ConnectionClosed)):
                    break
        except (TimeoutError, OSError):
            raise
        except h11.RemoteProtocolError as exc:
            raise CloudForwardError(f"malformed response from {hostname}: {exc}") from exc
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

        if status_code is None:
            raise CloudForwardError("connection closed before response status line")

        return ForwardedResponse(
            status_code=status_code, headers=resp_headers, body=bytes(resp_body)
        )


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

"""
WebSocket Protocol Server — bidirectional MITM proxy & listener for device real-time communication.

Many IoT devices use WebSocket for real-time bidirectional communication
(status updates, command streaming, event notifications).

In MITM mode:
- Devices connect here instead of the vendor cloud (via DNS interception or local configuration).
- If an upstream cloud WebSocket endpoint is configured or resolved from the client's Host header,
  traffic is proxied bidirectionally between the device and the cloud.
- Client requests and cloud responses are correlated and buffered for LLM protocol analysis
  and pattern generation.
- When local patterns are matched (in production mode), responses are served locally.
- If upstream cloud is unreachable or disabled, falls back cleanly to local standalone operation.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import sys
import time
from collections import deque
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qsl, urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable

try:
    import websockets
    from websockets.exceptions import ConnectionClosed

    HAS_WEBSOCKETS = True
except ImportError:  # pragma: no cover - exercised at import time only
    HAS_WEBSOCKETS = False

from adapters.base import InterceptedRequest, ProtocolType, device_id_from_ip
from core.pattern_db.schemas import ObservationKind, TransportMeta
from core.protocol_servers import (
    ProtocolServerPlugin,
    call_handler,
    device_reply,
    encode_reply,
)
from core.upstream_resolver import (
    filter_safe_ips,
    is_loopback_ip,
    is_self_address,
    resolve_upstream,
)

logger = logging.getLogger(__name__)

# Longest wait for open connections to close when the server stops.
_STOP_TIMEOUT_S = 5
_HTTPS_PORT = 443
_HTTP_PORT = 80

_HOP_BY_HOP_WS_HEADERS = frozenset(
    {
        "upgrade",
        "connection",
        "sec-websocket-key",
        "sec-websocket-version",
        "sec-websocket-extensions",
        "host",
    }
)


def _parse_ws_payload(message: str | bytes) -> Any:  # noqa: ANN401
    """Parse a WebSocket frame payload into a JSON dict or raw representation."""
    body = None
    if isinstance(message, str):
        stripped = message.lstrip()
        if stripped and stripped[0] in ("{", "["):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                body = json.loads(stripped)
        if body is None:
            body = {"raw": message}
    elif isinstance(message, bytes):
        stripped_b = message.lstrip()
        if stripped_b and stripped_b[0] in (123, 91):
            with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError, TypeError):
                body = json.loads(stripped_b.decode("utf-8"))
        if body is None:
            body = {"raw": message.hex()}
    else:
        body = {"raw": str(message)}
    return body


def _extract_raw_headers(headers: Any) -> dict[str, str]:  # noqa: ANN401
    """Safely extract headers without MultipleValuesError on multi-mapping structures."""
    raw: dict[str, str] = {}
    if not headers:
        return raw
    if hasattr(headers, "raw_items"):
        for k, v in headers.raw_items():
            raw[k] = v
    else:
        with contextlib.suppress(Exception):
            for k, v in dict(headers).items():
                raw[k] = v
    return raw


def _get_default_response_handler() -> Callable | None:
    """Retrieve default protocol response handler from server or proxy package."""
    server = sys.modules.get("core.server")
    if server and hasattr(server, "handle_protocol_response"):
        return cast("Callable | None", getattr(server, "handle_protocol_response"))
    try:
        import core.proxy  # noqa: PLC0415
    except ImportError:
        return None
    else:
        return cast("Callable | None", getattr(core.proxy, "handle_protocol_response", None))


def _get_default_failure_handler() -> Callable | None:
    """Retrieve default protocol failure handler from server or proxy package."""
    server = sys.modules.get("core.server")
    if server and hasattr(server, "handle_protocol_failure"):
        return cast("Callable | None", getattr(server, "handle_protocol_failure"))
    try:
        from core.proxy.handler import handle_protocol_failure  # noqa: PLC0415
    except ImportError:
        return None
    else:
        return cast("Callable | None", handle_protocol_failure)


def _determine_observation_kind(body: Any) -> ObservationKind:  # noqa: ANN401
    """Determine if a WebSocket message is a request expecting a response or an event."""
    if isinstance(body, dict) and any(
        k in body for k in ("id", "cmd", "action", "method", "command")
    ):
        return ObservationKind.REQUEST
    return ObservationKind.EVENT


_PENDING_TIMEOUT_S = 60.0
_MAX_PENDING_REQUESTS = 256


async def _prune_and_bound_pending(
    pending_requests: deque[dict[str, Any]],
    fail_handler: Callable | None,
    now: float,
) -> None:
    """Prune expired pending requests and enforce max size to prevent memory leaks."""
    while pending_requests and (now - pending_requests[0].get("time", now)) > _PENDING_TIMEOUT_S:
        expired = pending_requests.popleft()
        if fail_handler:
            with contextlib.suppress(Exception):
                res = fail_handler(expired["result"], count_as_error=False)
                if inspect.isawaitable(res):
                    await res

    while len(pending_requests) >= _MAX_PENDING_REQUESTS:
        overflow = pending_requests.popleft()
        if fail_handler:
            with contextlib.suppress(Exception):
                res = fail_handler(overflow["result"], count_as_error=False)
                if inspect.isawaitable(res):
                    await res


def _match_pending_request(
    pending_requests: deque[dict[str, Any]],
    body: Any,  # noqa: ANN401
) -> dict[str, Any] | None:
    """Find and pop a pending request matching an upstream response ID or FIFO."""
    if not pending_requests:
        return None

    now = time.time()
    while pending_requests and (now - pending_requests[0].get("time", now)) > _PENDING_TIMEOUT_S:
        pending_requests.popleft()

    if isinstance(body, dict):
        resp_id = body.get("id") or body.get("msg_id") or body.get("response_id")
        if resp_id is not None:
            for item in pending_requests:
                if item.get("msg_id") == resp_id:
                    pending_requests.remove(item)
                    return item
            return None

    for item in pending_requests:
        if item.get("msg_id") is None:
            pending_requests.remove(item)
            return item

    return None


class WebSocketServerPlugin(ProtocolServerPlugin):
    """Native WebSocket server with transparent bidirectional MITM proxy capabilities."""

    name = "websocket"

    def __init__(
        self,
        config: Any,  # noqa: ANN401
        handler: Callable | None = None,
        response_handler: Callable | None = None,
    ) -> None:
        super().__init__(config)
        self.handler = handler
        self.response_handler = response_handler
        self._server: Any = None

    async def start(self) -> None:
        if not HAS_WEBSOCKETS:
            logger.warning("WebSocket: websockets not installed - pip install websockets")
            self._running = False
            return
        if self._server is not None:
            return
        cfg = self.config
        # Bind here, so a failure (port in use) raises to the caller. Binding
        # inside a background task left the plugin "running" on a dead task
        # that made every later start a no-op and every stop raise.
        self._server = await websockets.serve(
            self._handle_ws, cfg.host, cfg.port, max_size=cfg.max_message_size
        )
        self._running = True
        logger.info("WebSocket server listening on %s:%d%s", cfg.host, cfg.port, cfg.path)

    async def _resolve_upstream_target(  # noqa: C901, PLR0912, PLR0915
        self, connection: Any, client_path: str, query_string: str
    ) -> tuple[str, str | None, int | None, dict[str, str]] | None:
        """Resolve upstream WebSocket target URI, IP, port, and forward headers."""
        req = getattr(connection, "request", None)
        headers = getattr(req, "headers", None)
        raw_headers: dict[str, str] = _extract_raw_headers(headers)

        # 1. Explicit upstream_url in configuration
        configured_url = getattr(self.config, "upstream_url", None)
        if configured_url:
            parsed = urlsplit(configured_url)
            scheme = parsed.scheme or "ws"
            if scheme == "http":
                scheme = "ws"
            elif scheme == "https":
                scheme = "wss"
            host_header = parsed.netloc
            target_host = parsed.hostname or ""
            target_port = parsed.port
            target_path = parsed.path if parsed.path and parsed.path != "/" else client_path
            target_query = parsed.query or query_string
            full_path = target_path + (f"?{target_query}" if target_query else "")
            upstream_uri = f"{scheme}://{host_header}{full_path}"

            upstream_ip = None
            if target_host and not is_loopback_ip(target_host) and not is_self_address(target_host):
                try:
                    ips = await resolve_upstream(target_host)
                    if ips:
                        upstream_ip = ips[0]
                except Exception:
                    logger.debug("Failed to resolve upstream host %s via upstream DNS", target_host)

            forward_headers = {
                k: v for k, v in raw_headers.items() if k.lower() not in _HOP_BY_HOP_WS_HEADERS
            }
            forward_headers["Host"] = host_header
            return upstream_uri, upstream_ip, target_port, forward_headers

        # 2. Auto upstream based on client request's Host header
        if getattr(self.config, "auto_upstream", True):
            host_hdr = None
            for k, v in raw_headers.items():
                if k.lower() == "host":
                    host_hdr = v
                    break
            if not host_hdr:
                return None

            if ":" in host_hdr:
                raw_host, port_str = host_hdr.split(":", 1)
                try:
                    port: int | None = int(port_str)
                except ValueError:
                    port = None
            else:
                raw_host = host_hdr
                port = None

            # Guard against loops: do not MITM if host is loopback or self-address
            if is_loopback_ip(raw_host) or is_self_address(raw_host):
                return None

            try:
                ips = await resolve_upstream(raw_host)
            except Exception:
                ips = []
            safe_ips = filter_safe_ips(ips)
            if not safe_ips:
                logger.debug("WebSocket MITM auto_upstream: no safe IPs for %s", raw_host)
                return None
            upstream_ip = safe_ips[0]

            upstream_ssl = getattr(self.config, "upstream_ssl", None)
            if upstream_ssl is True:
                scheme = "wss"
            elif upstream_ssl is False:
                scheme = "ws"
            else:
                scheme = (
                    "wss"
                    if (port == _HTTPS_PORT or getattr(self.config, "tls_enabled", False))
                    else "ws"
                )

            port_suffix = f":{port}" if port and port not in (_HTTP_PORT, _HTTPS_PORT) else ""
            full_path = client_path + (f"?{query_string}" if query_string else "")
            upstream_uri = f"{scheme}://{raw_host}{port_suffix}{full_path}"

            forward_headers = {
                k: v for k, v in raw_headers.items() if k.lower() not in _HOP_BY_HOP_WS_HEADERS
            }
            forward_headers["Host"] = host_hdr
            return upstream_uri, upstream_ip, port, forward_headers

        return None

    async def _handle_ws(self, connection: Any) -> None:  # noqa: C901, PLR0912
        """Handle an individual WebSocket connection (websockets >= 14 API)."""
        remote_info = connection.remote_address
        remote_ip = remote_info[0] if remote_info else "unknown"
        device_id = device_id_from_ip("ws", remote_ip)
        target = urlsplit(getattr(getattr(connection, "request", None), "path", "") or "")
        path = target.path or self.config.path
        query_params = dict(parse_qsl(target.query))

        upstream_target = await self._resolve_upstream_target(connection, path, target.query)
        if upstream_target is not None:
            upstream_uri, upstream_ip, upstream_port, forward_headers = upstream_target
            subprotocols = [connection.subprotocol] if connection.subprotocol else None
            try:
                connect_kwargs: dict[str, Any] = {
                    "open_timeout": getattr(self.config, "connect_timeout", 10.0),
                }
                if forward_headers:
                    connect_kwargs["additional_headers"] = forward_headers
                if subprotocols:
                    connect_kwargs["subprotocols"] = subprotocols
                if upstream_ip:
                    connect_kwargs["host"] = upstream_ip
                    if upstream_port:
                        connect_kwargs["port"] = upstream_port

                async with websockets.connect(upstream_uri, **connect_kwargs) as upstream_ws:
                    logger.info("WebSocket MITM active for %s -> %s", device_id, upstream_uri)
                    await self._run_duplex_mitm(
                        connection, upstream_ws, device_id, remote_ip, path, query_params
                    )
                    return
            except (ConnectionClosed, asyncio.CancelledError):
                return
            except Exception as e:
                logger.warning(
                    "WebSocket MITM upstream connection to %s failed: %s; "
                    "falling back to local mode",
                    upstream_uri,
                    e,
                )

        # Fallback to local standalone mode
        await self._run_local_mode(connection, device_id, remote_ip, path, query_params)

    async def _run_duplex_mitm(  # noqa: C901, PLR0912, PLR0913, PLR0915, PLR0917
        self,
        client_ws: Any,
        upstream_ws: Any,
        device_id: str,
        remote_ip: str,
        path: str,
        query_params: dict[str, str],
    ) -> None:
        """Run bidirectional forwarding and learning between client and upstream."""
        pending_requests: deque[dict[str, Any]] = deque()
        resp_handler = self.response_handler or _get_default_response_handler()
        fail_handler = _get_default_failure_handler()

        async def client_to_upstream() -> None:
            try:
                async for message in client_ws:
                    body = _parse_ws_payload(message)
                    kind = _determine_observation_kind(body)
                    request = InterceptedRequest(
                        device_id=device_id,
                        timestamp=time.time(),
                        protocol=ProtocolType.WEBSOCKET,
                        client_ip=remote_ip,
                        method="WS",
                        path=path,
                        query_params=query_params,
                        body=body,
                        transport=TransportMeta(port=getattr(self.config, "port", 9000)),
                        security="none",
                        identity=device_id,
                        kind=kind,
                    )
                    handler_result = None
                    if self.handler:
                        try:
                            handler_result = await call_handler(self.handler, request)
                        except Exception:
                            logger.exception("WebSocket pipeline handler error:")

                    reply = device_reply(handler_result)
                    if reply is not None:
                        payload, is_json = encode_reply(reply)
                        await client_ws.send(payload.decode("utf-8") if is_json else payload)
                        continue

                    if (
                        isinstance(handler_result, dict)
                        and handler_result.get("action") == "forward"
                    ):
                        corr_key = handler_result.get("correlation_key")
                        if corr_key:
                            msg_id = (
                                (body.get("id") or body.get("msg_id"))
                                if isinstance(body, dict)
                                else None
                            )
                            now = time.time()
                            await _prune_and_bound_pending(pending_requests, fail_handler, now)
                            pending_requests.append(
                                {
                                    "result": handler_result,
                                    "msg_id": msg_id,
                                    "time": now,
                                }
                            )

                    await upstream_ws.send(message)
            except ConnectionClosed:
                pass

        async def upstream_to_client() -> None:
            try:
                async for message in upstream_ws:
                    body = _parse_ws_payload(message)
                    matched = _match_pending_request(pending_requests, body)

                    if matched and resp_handler:
                        try:
                            res = resp_handler(matched["result"], body)
                            if inspect.isawaitable(res):
                                await res
                        except Exception:
                            logger.exception("WebSocket response handler error:")

                    await client_ws.send(message)
            except ConnectionClosed:
                pass

        c_task = asyncio.create_task(client_to_upstream())
        u_task = asyncio.create_task(upstream_to_client())
        try:
            done, pending = await asyncio.wait(
                [c_task, u_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
        finally:
            if pending_requests:
                for item in pending_requests:
                    if fail_handler:
                        with contextlib.suppress(Exception):
                            res = fail_handler(item["result"], count_as_error=False)
                            if inspect.isawaitable(res):
                                await res
                    else:
                        server = sys.modules.get("core.server")
                        orchestrator = getattr(server, "orchestrator", None) if server else None
                        if orchestrator:
                            corr = (item.get("result") or {}).get("correlation_key")
                            if corr:
                                with contextlib.suppress(Exception):
                                    await orchestrator.discard_request(
                                        device_id, corr, count_as_error=False
                                    )

    async def _run_local_mode(
        self,
        connection: Any,
        device_id: str,
        remote_ip: str,
        path: str,
        query_params: dict[str, str],
    ) -> None:
        """Fallback local mode: process messages locally without upstream forwarding."""
        handler = self.handler
        try:
            async for message in connection:
                body = _parse_ws_payload(message)
                kind = _determine_observation_kind(body)
                request = InterceptedRequest(
                    device_id=device_id,
                    timestamp=time.time(),
                    protocol=ProtocolType.WEBSOCKET,
                    client_ip=remote_ip,
                    method="WS",
                    path=path,
                    query_params=query_params,
                    body=body,
                    transport=TransportMeta(port=getattr(self.config, "port", 9000)),
                    security="none",
                    identity=device_id,
                    kind=kind,
                )
                if handler:
                    try:
                        result = await call_handler(handler, request)
                        reply = device_reply(result)
                        if reply is not None:
                            payload, is_json = encode_reply(reply)
                            await connection.send(payload.decode("utf-8") if is_json else payload)
                    except ConnectionClosed:
                        break
                    except Exception:
                        logger.exception("WebSocket handler error:")
        except ConnectionClosed:
            return
        except Exception:
            logger.exception("WebSocket connection error:")

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(server.wait_closed(), _STOP_TIMEOUT_S)
        await super().stop()
        logger.info("WebSocket server stopped")

    async def get_status(self) -> dict:
        return {
            "name": self.name,
            "running": self._running,
            "host": self.config.host,
            "port": self.config.port,
            "path": self.config.path,
            "upstream_url": getattr(self.config, "upstream_url", None),
            "auto_upstream": getattr(self.config, "auto_upstream", True),
        }

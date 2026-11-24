"""
CoAP Protocol Server — UDP-based REST for constrained IoT devices with transparent MITM.

CoAP (Constrained Application Protocol) is like HTTP over UDP, with:
- Compact binary headers (4 bytes vs HTTP's hundreds)
- Built-in resource discovery (/.well-known/core)
- Observe mode (subscription to resource changes)
- Confirmable/Non-confirmable messages
- DTLS for encryption (CoAPS)

In MITM mode:
- Intercepts incoming CoAP requests on UDP port 5683 / 5684.
- In production mode: answers locally from learned patterns.
- In learning mode: forwards the request to the upstream vendor CoAP endpoint,
  correlates the response into the learning pipeline via handle_protocol_response,
  and returns the real cloud response to the device.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import ipaddress
import json
import logging
import sys
import time
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable

try:
    import aiocoap
    import aiocoap.resource
    from aiocoap import DELETE, GET, POST, PUT, Context, Message
    from aiocoap.numbers.contentformat import ContentFormat

    HAS_AIOCOAP = True
except ImportError:  # pragma: no cover - exercised at import time only
    HAS_AIOCOAP = False

from adapters.base import InterceptedRequest, ProtocolType
from core.pattern_db.schemas import ObservationKind, TransportMeta
from core.protocol_servers import (
    ProtocolServerPlugin,
    call_handler,
    device_reply,
    encode_reply,
)
from core.upstream_resolver import (
    is_loopback_ip,
    is_self_address,
    resolve_upstream,
)

logger = logging.getLogger(__name__)

_WELL_KNOWN_CORE = (".well-known", "core")


def _remote_ip(request: Any) -> str:  # noqa: ANN401
    """The client's IP address (aiocoap addresses have no ``host`` attribute)."""
    remote = getattr(request, "remote", None)
    sockaddr = getattr(remote, "sockaddr", None)
    if not sockaddr:
        return "unknown"
    host = str(sockaddr[0])
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return host
    return str(getattr(address, "ipv4_mapped", None) or address)


def _coap_code_to_http(code: Any) -> int:  # noqa: ANN401
    """Convert a CoAP response code integer to an approximate HTTP status code."""
    try:
        raw = int(code)
        class_num = raw >> 5
        detail_num = raw & 0x1F
        return class_num * 100 + detail_num
    except Exception:
        return 200


def _parse_coap_payload(payload_bytes: bytes | None) -> Any:  # noqa: ANN401
    """Parse CoAP payload into JSON or hex representation."""
    if not payload_bytes:
        return None
    i = 0
    n = len(payload_bytes)
    while i < n and payload_bytes[i] in (32, 9, 10, 13):
        i += 1
    if i < n and payload_bytes[i] in (123, 91):
        with contextlib.suppress(UnicodeDecodeError, json.JSONDecodeError, TypeError):
            parsed = json.loads(payload_bytes.decode("utf-8"))
            if isinstance(parsed, (dict, list)):
                return parsed
    return {"raw": payload_bytes.hex()}


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


if HAS_AIOCOAP:

    class _CoAPRoot(aiocoap.resource.Resource):
        """Root resource: discovery at /.well-known/core, everything else to the pipeline."""

        def __init__(self, plugin: CoAPServerPlugin) -> None:
            super().__init__()
            self.plugin = plugin

        async def render(self, request: Message) -> Message:
            path = tuple(request.opt.uri_path or ())
            if path == _WELL_KNOWN_CORE:
                return Message(
                    code=aiocoap.CONTENT,
                    payload=b"</>;ct=0,</.well-known/core>;ct=40",
                    content_format=ContentFormat.LINKFORMAT,
                )
            reply = await self.plugin.handle_coap_request(request, "/".join(path))
            return reply or Message(code=aiocoap.NOT_FOUND)


# Pre-computed CoAP method map for fast O(1) lookup without per-request dict creation
_COAP_METHOD_MAP = {GET: "GET", POST: "POST", PUT: "PUT", DELETE: "DELETE"} if HAS_AIOCOAP else {}


class CoAPServerPlugin(ProtocolServerPlugin):
    """CoAP server for device protocol interception with upstream MITM."""

    name = "coap"

    def __init__(
        self,
        config: Any,  # noqa: ANN401
        handler: Callable | None = None,
        response_handler: Callable | None = None,
    ) -> None:
        super().__init__(config)
        self.handler = handler
        self.response_handler = response_handler
        self._context: Context | None = None
        self._client_context: Context | None = None
        self._resources: dict[str, Callable] = {}

    async def _get_client_context(self) -> Context:
        if self._client_context is None:
            self._client_context = await Context.create_client_context()
        return self._client_context

    async def start(self) -> None:
        if not HAS_AIOCOAP:
            logger.warning("CoAP: aiocoap not installed - pip install aiocoap")
            self._running = False
            return
        if self._context is not None:
            return

        cfg = self.config
        site = _CoAPRoot(self)
        bind = (cfg.host, cfg.port)
        self._context = await Context.create_server_context(site=site, bind=bind)
        self._running = True
        logger.info("CoAP server listening on %s:%d", cfg.host, cfg.port)

    async def stop(self) -> None:
        if self._context:
            with contextlib.suppress(Exception):
                await self._context.shutdown()
            self._context = None
        if self._client_context:
            with contextlib.suppress(Exception):
                await self._client_context.shutdown()
            self._client_context = None
        await super().stop()
        logger.info("CoAP server stopped")

    async def _resolve_upstream_coap_target(
        self, request: Message, path: str, query_params: dict[str, str]
    ) -> str | None:
        """Resolve destination URI for upstream CoAP cloud endpoint."""
        configured_url = getattr(self.config, "upstream_url", None)
        if configured_url:
            parsed = urlsplit(configured_url)
            scheme = parsed.scheme or "coap"
            netloc = parsed.netloc
            target_path = (
                parsed.path.rstrip("/")
                if parsed.path and parsed.path != "/"
                else f"/{path.lstrip('/')}"
            )
            query_str = f"?{parsed.query}" if parsed.query else ""
            return f"{scheme}://{netloc}{target_path}{query_str}"

        if getattr(self.config, "auto_upstream", True) and request.opt.uri_host:
            raw_host = request.opt.uri_host
            if is_loopback_ip(raw_host) or is_self_address(raw_host):
                return None

            try:
                ips = await resolve_upstream(raw_host)
            except Exception:
                ips = []
            if not ips:
                return None

            port = request.opt.uri_port or (
                getattr(self.config, "dtls_port", 5684)
                if getattr(self.config, "dtls_enabled", False)
                else getattr(self.config, "port", 5683)
            )
            scheme = "coaps" if getattr(self.config, "dtls_enabled", False) else "coap"
            query_parts = [f"{k}={v}" if v else k for k, v in query_params.items()]
            query_str = f"?{'&'.join(query_parts)}" if query_parts else ""
            return f"{scheme}://{raw_host}:{port}/{path.lstrip('/')}{query_str}"

        return None

    async def handle_coap_request(  # noqa: C901, PLR0912
        self, request: Message, path: str
    ) -> Message | None:
        """Handle an incoming CoAP request and route to pipeline with upstream MITM."""
        if not self.handler:
            return None

        coap_method = _COAP_METHOD_MAP.get(request.code, "GET")
        query_params: dict[str, str] = {}
        for item in request.opt.uri_query or []:
            if "=" in item:
                k, _, v = item.partition("=")
                query_params[k] = v
            else:
                query_params[item] = ""
        remote_ip = _remote_ip(request)

        body = _parse_coap_payload(request.payload)

        intercepted = InterceptedRequest(
            device_id=f"coap-{remote_ip}",
            timestamp=time.time(),
            protocol=ProtocolType.COAP,
            client_ip=remote_ip,
            method=coap_method,
            path=f"/{path}",
            headers={},
            query_params=query_params,
            body=body,
            transport=TransportMeta(
                port=getattr(self.config, "port", 5683),
                tls=getattr(self.config, "dtls_enabled", False),
            ),
            security="dtls" if getattr(self.config, "dtls_enabled", False) else "none",
            identity=f"coap-{remote_ip}",
            kind=ObservationKind.REQUEST,
        )

        try:
            result = await call_handler(self.handler, intercepted)

            # 1. Local response (production mode / learned pattern)
            reply_body = device_reply(result)
            if reply_body is not None:
                payload, is_json = encode_reply(reply_body)
                response = Message(code=aiocoap.CONTENT, payload=payload)
                response.content_format = ContentFormat.JSON if is_json else ContentFormat.TEXT
                return response

            # 2. Forward to upstream cloud in learning mode
            if isinstance(result, dict) and result.get("action") == "forward":
                upstream_uri = await self._resolve_upstream_coap_target(request, path, query_params)
                if upstream_uri:
                    try:
                        client_ctx = await self._get_client_context()
                        upstream_msg = Message(
                            code=request.code,
                            uri=upstream_uri,
                            payload=request.payload or b"",
                        )
                        if request.opt.content_format is not None:
                            upstream_msg.opt.content_format = request.opt.content_format

                        timeout = getattr(self.config, "connect_timeout", 5.0)
                        upstream_resp = await asyncio.wait_for(
                            client_ctx.request(upstream_msg).response, timeout=timeout
                        )

                        resp_body = _parse_coap_payload(upstream_resp.payload)
                        resp_handler = self.response_handler or _get_default_response_handler()
                        if resp_handler and result.get("correlation_key"):
                            res = resp_handler(
                                result,
                                resp_body,
                                status_code=_coap_code_to_http(upstream_resp.code),
                            )
                            if inspect.isawaitable(res):
                                await res

                        client_reply = Message(
                            code=upstream_resp.code, payload=upstream_resp.payload
                        )
                        if upstream_resp.opt.content_format is not None:
                            client_reply.opt.content_format = upstream_resp.opt.content_format
                    except Exception as e:
                        logger.warning(
                            "CoAP upstream forward to %s failed: %s; returning 4.04",
                            upstream_uri,
                            e,
                        )
                    else:
                        return client_reply
        except Exception:
            logger.exception("CoAP handler error:")

        return Message(code=aiocoap.NOT_FOUND)

    async def get_status(self) -> dict:
        return {
            "name": self.name,
            "running": self._running,
            "host": self.config.host,
            "port": self.config.port,
            "dtls_enabled": self.config.dtls_enabled,
            "dtls_port": self.config.dtls_port,
            "upstream_url": getattr(self.config, "upstream_url", None),
            "auto_upstream": getattr(self.config, "auto_upstream", True),
        }

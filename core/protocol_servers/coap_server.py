"""
CoAP Protocol Server ? UDP-based REST for constrained IoT devices.

CoAP (Constrained Application Protocol) is like HTTP over UDP, with:
- Compact binary headers (4 bytes vs HTTP's hundreds)
- Built-in resource discovery (/.well-known/core)
- Observe mode (subscription to resource changes)
- Confirmable/Non-confirmable messages
- DTLS for encryption (CoAPS)

This server listens on UDP port 5683 (CoAP) via the validated ``aiocoap``
library. Requests are routed to the pipeline handler and answered from the
learned local response.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

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
from core.protocol_servers import ProtocolServerPlugin

logger = logging.getLogger(__name__)


class CoAPServerPlugin(ProtocolServerPlugin):
    """CoAP server for device protocol interception."""

    name = "coap"

    def __init__(self, config: Any, handler: Callable | None = None) -> None:  # noqa: ANN401
        super().__init__(config)
        self.handler = handler
        self._context: Context | None = None
        self._resources: dict[str, Callable] = {}

    async def start(self) -> None:
        if not HAS_AIOCOAP:
            logger.warning("CoAP: aiocoap not installed ? pip install aiocoap")
            self._running = False
            return
        if self._context is not None:
            return

        cfg = self.config

        class _Wildcard(aiocoap.resource.Resource):
            plugin = self

            async def render(self, request: Message) -> Message:
                segs = request.opt.uri_path
                joined = "/".join(segs) if segs else ""
                return (
                    await type(self).plugin.handle_coap_request(request, joined)
                    or Message(code=aiocoap.NOT_FOUND)
                )

        class _WellKnown(aiocoap.resource.Resource):
            plugin = self

            async def render(self, request: Message) -> Message:  # noqa: N805
                payload = b"</>;ct=0,</.well-known/core>;ct=40"
                return Message(code=aiocoap.CONTENT, payload=payload,
                               content_format=ContentFormat.LINKFORMAT)

        site = aiocoap.resource.Site()
        site.add_resource([".well-known", "core"], _WellKnown())
        site.add_resource(["*"], _Wildcard())

        bind = (cfg.host, cfg.port)
        self._context = await Context.create_server_context(site=site, bind=bind)
        self._running = True
        logger.info("CoAP server listening on %s:%d", cfg.host, cfg.port)

    async def stop(self) -> None:
        if self._context:
            with contextlib.suppress(Exception):
                await self._context.shutdown()
            self._context = None
        await super().stop()
        logger.info("CoAP server stopped")

    async def handle_coap_request(self, request: Message, path: str) -> Message | None:
        """Handle an incoming CoAP request and route to pipeline."""
        if not self.handler:
            return None

        method_map = {GET: "GET", POST: "POST", PUT: "PUT", DELETE: "DELETE"}
        coap_method = method_map.get(request.code, "GET")

        query_params = {}
        for item in request.opt.uri_query or []:
            if "=" in item:
                k, _, v = item.partition("=")
                query_params[k] = v
            else:
                query_params[item] = ""
        remote = getattr(request, "remote", None)
        remote_ip = getattr(remote, "host", "unknown")

        body: dict | None = None
        if request.payload:
            try:
                body = json.loads(request.payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = {"raw": request.payload.hex()}

        intercepted = InterceptedRequest(
            device_id=f"coap-{remote_ip}",
            timestamp=datetime.now(UTC).timestamp(),
            protocol=ProtocolType.COAP,
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
            if asyncio.iscoroutinefunction(self.handler):
                result = await self.handler(intercepted)
            else:
                result = self.handler(intercepted)

            if result and isinstance(result, dict):
                response = Message(code=aiocoap.CONTENT)
                response.payload = json.dumps(result.get("response", result)).encode("utf-8")
                response.content_format = ContentFormat.JSON
                return response
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
        }

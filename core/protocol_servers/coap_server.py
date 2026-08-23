"""
CoAP Protocol Server — UDP-based REST for constrained IoT devices.

CoAP (Constrained Application Protocol) is like HTTP over UDP, with:
- Compact binary headers (4 bytes vs HTTP's hundreds)
- Built-in resource discovery (/.well-known/core)
- Observe mode (subscription to resource changes)
- Confirmable/Non-confirmable messages
- DTLS for encryption (CoAPS)

This server listens on UDP port 5683 (CoAP) and optionally 5684 (CoAPS/DTLS).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

try:
    import aiocoap
    from aiocoap import DELETE, GET, POST, PUT, Context, Message
    from aiocoap.numbers.contentformat import ContentFormat

    HAS_AIOCOAP = True
except ImportError:
    HAS_AIOCOAP = False

from adapters.base import InterceptedRequest, ProtocolType
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
            logger.warning("CoAP: aiocoap not installed — pip install aiocoap")
            self._running = False
            return

        cfg = self.config
        self._running = True
        logger.info(
            "CoAP server enabled on %s:%d (DTLS: %s:%d)",
            cfg.host,
            cfg.port,
            cfg.host,
            cfg.dtls_port if cfg.dtls_enabled else 0,
        )

    async def stop(self) -> None:
        if self._context:
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

        body = None
        if request.payload:
            try:
                body = json.loads(request.payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = {"raw": request.payload.hex()}

            # aiocoap returns uri_query as a list of full "k=v" strings, so
            # dict([...]) would raise ValueError (sequence element length).
            query_params = {}
            for item in request.opt.uri_query or []:
                if "=" in item:
                    k, _, v = item.partition("=")
                    query_params[k] = v
                else:
                    query_params[item] = ""
            # Could be absent when the request lacks a remote address
            remote_ip = request.remote.sockname[0] if hasattr(request, "remote") else "unknown"
            intercepted = InterceptedRequest(
                device_id=f"coap-{remote_ip}",
                timestamp=datetime.now(UTC).timestamp(),
                protocol=ProtocolType.COAP,
                method=coap_method,
                path=f"/{path}",
                headers={},
                query_params=query_params,
                body=body,
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

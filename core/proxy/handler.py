"""Decrypted TLS and generic protocol request ingress handlers."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from adapters.base import (
    InterceptedRequest,
    ProtocolAdapter,
    ProtocolAdapterRegistry,
    ProtocolType,
    device_id_from_ip,
    normalize_request_body,
)
from core.proxy.forwarder import _resolve_device_for_ip
from core.upstream_resolver import self_connection_reason

if TYPE_CHECKING:
    from types import ModuleType

    from core.tls_mitm import DecryptedRequest

logger = logging.getLogger(__name__)

_ENRICHMENT_FIELDS: tuple[str, ...] = ("security", "identity", "kind")


def _get_server() -> ModuleType:
    from core import server  # noqa: PLC0415

    return server


def _select_handler_adapter(
    adapter_registry: ProtocolAdapterRegistry | None,
    device_vendor: str,
    resolved_protocol: str,
) -> ProtocolAdapter | None:
    """Pick the protocol adapter for a device request.

    - A registered, vendor-specific adapter always wins (e.g. ``shelly``), so
      devices with a known vendor keep their bespoke handler.
    - Otherwise, fall back to the first adapter that supports the resolved
      protocol.
    """
    if not adapter_registry:
        return None

    vendor_adapter = adapter_registry.get_adapter(device_vendor)
    if vendor_adapter is not None:
        return vendor_adapter

    try:
        resolved = ProtocolType(resolved_protocol)
    except (ValueError, KeyError):
        return None

    matched = adapter_registry.get_adapter_by_protocol(resolved)
    return matched[0] if matched else None


async def handle_tls_decrypted_request(req: DecryptedRequest) -> dict | None:  # noqa: C901
    """Handle a decrypted TLS request — find/create device and run through pipeline."""
    server = _get_server()
    db_manager = getattr(server, "db_manager", None)
    orchestrator = getattr(server, "orchestrator", None)
    adapter_registry = getattr(server, "adapter_registry", None)
    config_mgr = getattr(server, "config_manager", None)
    config = config_mgr.config if config_mgr else None

    if not db_manager or not orchestrator or not config:
        logger.warning("TLS handler: service not ready, dropping request from %s", req.client_ip)
        return None

    self_reason = self_connection_reason(req.client_ip)
    if self_reason:
        logger.debug(
            "TLS: %s client %s is never intercepted, forwarding %s %s",
            self_reason,
            req.client_ip,
            req.method,
            req.path,
        )
        return {"action": "forward", "reason": self_reason}

    recognized = await _resolve_device_for_ip(db_manager, req.client_ip)
    device_id = recognized or device_id_from_ip("ip", req.client_ip)

    try:
        await db_manager.get_or_create_device(
            device_id=device_id,
            vendor="unknown",
        )
        device = await db_manager.get_device(device_id)
        await db_manager.apply_ip_profile(device_id, req.client_ip)

        if await db_manager.is_ips_bypassed(req.client_ip):
            logger.info("TLS: %s is in bypass, forwarding to cloud directly", req.client_ip)
            return {"action": "forward", "reason": "bypass"}

        device_db_dir = Path(config.core.device_db_dir)
        device_db_path = device_db_dir / f"{device_id}.db"
        if not device_db_path.exists():
            try:
                device_db_path.touch()
                logger.info(
                    "TLS handler: created device DB for %s at %s", device_id, device_db_path
                )
            except Exception as e:
                logger.warning("TLS handler: could not create device DB for %s: %s", device_id, e)

        logger.info(
            "TLS: %s %s %s (device=%s, sni=%s, port=%d)",
            req.method,
            req.path,
            req.http_version,
            device_id,
            req.sni,
            req.dst_port,
        )

        device_vendor = getattr(device, "vendor", "unknown") or "unknown"
        resolved_protocol = await db_manager.resolve_device_protocol(
            device_id, ingress_default="https"
        )
        handler_adapter = _select_handler_adapter(
            adapter_registry, device_vendor, resolved_protocol
        )

        intercepted = InterceptedRequest(
            device_id=device_id,
            timestamp=datetime.now(UTC),
            protocol=ProtocolType.HTTPS,
            method=req.method,
            path=req.path,
            headers=req.headers,
            query_params={},
            body=normalize_request_body(req.body),
            body_bytes=req.body,
        )

        if handler_adapter:
            intercepted = await handler_adapter.parse_request(intercepted)

        result = await orchestrator.handle_request(
            device_id=intercepted.device_id or device_id,
            vendor=device_vendor,
            protocol=resolved_protocol,
            method=intercepted.method or req.method,
            path=intercepted.path or req.path,
            headers=dict(intercepted.headers) if intercepted.headers else dict(req.headers),
            body=intercepted.body,
            query_params=intercepted.query_params or {},
        )

        if result.get("action") == "forward" and result.get("correlation_key"):
            result = {
                **result,
                "_learning_context": {
                    "device_id": intercepted.device_id or device_id,
                    "vendor": device_vendor,
                    "protocol": resolved_protocol,
                },
            }
    except Exception as e:
        logger.error("TLS handler: error processing %s: %s", req.client_ip, e, exc_info=True)
        return None
    else:
        if result["action"] == "local_response":
            logger.debug("TLS: local response for %s %s", req.method, req.path)
        else:
            logger.debug("TLS: passthrough for %s %s", req.method, req.path)

        return result


async def handle_tls_upstream_response(handler_result: dict, response: dict) -> dict | None:
    """Correlate a TLS MITM upstream response with its captured request."""
    server = _get_server()
    orchestrator = getattr(server, "orchestrator", None)

    context = handler_result.get("_learning_context")
    if not orchestrator or not context or not handler_result.get("correlation_key"):
        return None

    return await orchestrator.handle_response(
        device_id=context["device_id"],
        vendor=context["vendor"],
        protocol=context["protocol"],
        status_code=response.get("status_code", 200),
        headers=response.get("headers") or {},
        body=normalize_request_body(response.get("body")),
        correlation_key=handler_result.get("correlation_key"),
    )


async def handle_tls_upstream_failure(handler_result: dict) -> bool:
    """Remove a queued learning request when its TLS upstream exchange fails."""
    server = _get_server()
    orchestrator = getattr(server, "orchestrator", None)

    correlation_key = handler_result.get("correlation_key")
    context = handler_result.get("_learning_context")
    if not orchestrator or not context or not correlation_key:
        return False

    return await orchestrator.discard_request(context["device_id"], correlation_key)


async def handle_protocol_request(request: InterceptedRequest) -> dict | None:  # noqa: C901
    """Handle an intercepted request from a protocol server plugin."""
    server = _get_server()
    db_manager = getattr(server, "db_manager", None)
    orchestrator = getattr(server, "orchestrator", None)

    if not db_manager or not orchestrator:
        logger.warning("Protocol handler: service not ready, dropping %s", request.device_id)
        return None

    self_reason = self_connection_reason(getattr(request, "client_ip", None))
    if self_reason:
        logger.debug(
            "Protocol handler: %s client %s is never intercepted, forwarding %s",
            self_reason,
            request.client_ip,
            getattr(request, "path", None) or getattr(request, "topic", None) or "request",
        )
        return {"action": "forward", "reason": self_reason}

    device_id = request.device_id or "unknown"
    device_id = (
        await _resolve_device_for_ip(db_manager, getattr(request, "client_ip", None))
    ) or device_id

    protocol = (
        request.protocol.value
        if hasattr(request.protocol, "value")
        else str(request.protocol or "http")
    )
    method = request.method or ("publish" if request.topic else "GET")
    path = request.path or (f"/{request.topic.lstrip('/')}" if request.topic else "/")

    try:
        await db_manager.get_or_create_device(device_id, "unknown")

        cli_ip = getattr(request, "client_ip", None)
        if cli_ip and await db_manager.is_ips_bypassed(cli_ip):
            logger.info("Protocol handler: %s is in bypass, forwarding to cloud", cli_ip)
            return {"action": "forward", "reason": "bypass"}

        resolved_protocol = await db_manager.resolve_device_protocol(
            device_id, ingress_default=protocol
        )

        enrichment: dict | None = None
        transport = getattr(request, "transport", None)
        if transport is not None or any(
            getattr(request, f, None) is not None for f in _ENRICHMENT_FIELDS
        ):
            enrichment = {}
            if transport is not None:
                if hasattr(transport, "model_dump"):
                    enrichment["transport"] = transport.model_dump()
                elif isinstance(transport, dict):
                    enrichment["transport"] = transport
                else:
                    enrichment["transport"] = str(transport)

            for f in _ENRICHMENT_FIELDS:
                val = getattr(request, f, None)
                if val is not None:
                    enrichment[f] = val.value if hasattr(val, "value") else val

        return await orchestrator.handle_request(
            device_id=device_id,
            vendor="unknown",
            protocol=resolved_protocol,
            method=method,
            path=path,
            headers=request.headers or {},
            body=normalize_request_body(request.body),
            query_params=request.query_params or {},
            enrichment=enrichment,
        )

    except Exception as e:
        logger.error("Protocol handler error for %s: %s", device_id, e, exc_info=True)
        return None

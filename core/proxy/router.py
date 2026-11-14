"""Catch-all reverse proxy router for device vendor traffic."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from adapters.base import InterceptedRequest, ProtocolType, normalize_request_body
from core.modification import BLOCKED_STATUS
from core.proxy.forwarder import (
    _apply_response_modifications,
    _discard_pending_capture,
    _extract_device_id,
    _forward_bypassed_request,
    _get_request_body,
    _is_local_ip,
    _local_http_response,
    _modification_delay,
    _raw_target,
    _resolve_device_for_ip,
    _upstream_response,
)
from core.traffic_selector import TrafficRequestInfo

if TYPE_CHECKING:
    from types import ModuleType

logger = logging.getLogger(__name__)

proxy_router = APIRouter(include_in_schema=False)


def _get_server() -> ModuleType:
    from core import server  # noqa: PLC0415

    return server


def _get_traffic_selector() -> object:
    server = _get_server()
    selector_getter = getattr(server, "get_traffic_selector", None)
    if selector_getter is not None:
        return selector_getter()
    from core.traffic_selector import get_traffic_selector  # noqa: PLC0415

    return get_traffic_selector()


def _get_modification_engine() -> object:
    server = _get_server()
    engine_getter = getattr(server, "get_modification_engine", None)
    if engine_getter is not None:
        return engine_getter()
    from core.modification import get_modification_engine  # noqa: PLC0415

    return get_modification_engine()


def _get_tracer() -> object:
    server = _get_server()
    tracer_getter = getattr(server, "get_tracer", None)
    if tracer_getter is not None:
        return tracer_getter()
    from core.observability import get_tracer  # noqa: PLC0415

    return get_tracer()


def _get_metrics_registry() -> object:
    server = _get_server()
    reg = getattr(server, "metrics_registry", None)
    if reg is not None:
        return reg
    from core.observability import metrics_registry  # noqa: PLC0415

    return metrics_registry


@proxy_router.api_route(
    "/{vendor}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def proxy_vendor_request(  # noqa: C901, PLR0911, PLR0912, PLR0915
    vendor: str, path: str, request: Request
) -> Response:
    """Main proxy endpoint for device API requests."""
    server = _get_server()
    db_manager = getattr(server, "db_manager", None)
    orchestrator = getattr(server, "orchestrator", None)
    adapter_registry = getattr(server, "adapter_registry", None)
    config_mgr = getattr(server, "config_manager", None)
    config = config_mgr.config if config_mgr else None

    if not db_manager or not orchestrator or not adapter_registry or not config:
        return JSONResponse(
            status_code=503,
            content={"error": "Service not ready"},
        )

    adapter = adapter_registry.get_adapter(vendor)
    if not adapter:
        return JSONResponse(
            status_code=404,
            content={
                "error": (
                    f"Protocol '{vendor}' not supported. "
                    f"Supported: {adapter_registry.list_vendors()}"
                )
            },
        )

    metrics_registry = _get_metrics_registry()
    metrics_registry.requests_total.inc()

    forward_bypassed_fn = getattr(server, "_forward_bypassed_request", _forward_bypassed_request)
    get_request_body_fn = getattr(server, "_get_request_body", _get_request_body)
    is_local_ip_fn = getattr(server, "_is_local_ip", _is_local_ip)
    resolve_device_fn = getattr(server, "_resolve_device_for_ip", _resolve_device_for_ip)
    extract_device_fn = getattr(server, "_extract_device_id", _extract_device_id)
    raw_target_fn = getattr(server, "_raw_target", _raw_target)
    modification_delay_fn = getattr(server, "_modification_delay", _modification_delay)
    apply_mods_fn = getattr(server, "_apply_response_modifications", _apply_response_modifications)
    local_http_resp_fn = getattr(server, "_local_http_response", _local_http_response)
    discard_capture_fn = getattr(server, "_discard_pending_capture", _discard_pending_capture)
    upstream_resp_fn = getattr(server, "_upstream_response", _upstream_response)

    raw_body = await request.body()
    body = await get_request_body_fn(request)

    client_ip = request.client.host if request.client else "unknown"
    is_local = is_local_ip_fn(client_ip)

    traffic_selector_fn = _get_traffic_selector
    if traffic_selector := traffic_selector_fn():
        dest_host = request.headers.get("host", f"{vendor}.local")
        request_info = TrafficRequestInfo(
            client_ip=client_ip,
            hostname=dest_host,
            vendor=vendor,
            is_local=is_local,
            url=str(request.url),
            path=f"/{path}",
        )
        decision = traffic_selector.evaluate(request_info)
        if decision.value == "passthrough":
            logger.info(f"Passthrough for traffic from {client_ip} to {vendor}: forwarding")
            return await forward_bypassed_fn(
                adapter, request, path, body, client_ip, raw_body=raw_body
            )

    if await db_manager.is_ips_bypassed(client_ip):
        logger.info(f"Bypass for {client_ip} to {vendor}: forwarding to cloud directly")
        return await forward_bypassed_fn(adapter, request, path, body, client_ip, raw_body=raw_body)

    intercepted = InterceptedRequest(
        device_id="",
        timestamp=datetime.now(UTC),
        protocol=ProtocolType.HTTPS if request.url.scheme == "https" else ProtocolType.HTTP,
        method=request.method,
        path=f"/{path}",
        headers=dict(request.headers),
        query_params=dict(request.query_params),
        body=body,
        body_bytes=raw_body,
        raw_target=raw_target_fn(request),
    )

    device_id = ""
    result: dict | None = None

    try:
        intercepted = await adapter.parse_request(intercepted)
        device_id = (
            await resolve_device_fn(db_manager, client_ip)
            or intercepted.device_id
            or extract_device_fn(request.headers, path)
        )
        intercepted.device_id = device_id

        await db_manager.get_or_create_device(device_id, vendor)

        modification_engine = _get_modification_engine()
        modification_engine.process_request(intercepted)

        if intercepted.path != f"/{path}":
            intercepted.raw_target = None

        if getattr(intercepted, "blocked", False):
            logger.info("Request from %s blocked: %s", device_id, intercepted.block_reason)
            return JSONResponse(
                status_code=BLOCKED_STATUS,
                content={"error": "blocked", "reason": intercepted.block_reason},
            )

        await modification_delay_fn(
            (getattr(intercepted, "metadata", None) or {}).get("artificial_delay_ms")
        )

        tracer = _get_tracer()
        with tracer.start_as_current_span("proxy.handle_request") as span:
            span.set_attribute("http.vendor", vendor)
            span.set_attribute("http.method", intercepted.method or request.method)
            span.set_attribute("http.target", intercepted.path or f"/{path}")
            span.set_attribute("http.device_id", device_id)

            result = await orchestrator.handle_request(
                device_id=device_id,
                vendor=vendor,
                protocol="http",
                method=intercepted.method or request.method,
                path=intercepted.path or f"/{path}",
                headers=dict(intercepted.headers) if intercepted.headers else dict(request.headers),
                body=normalize_request_body(intercepted.body),
                query_params=(
                    dict(intercepted.query_params)
                    if hasattr(intercepted, "query_params") and intercepted.query_params
                    else dict(request.query_params)
                ),
            )
            span.set_attribute("proxy.action", result.get("action", ""))

        if result["action"] == "local_response":
            metrics_registry.local_hits_total.inc()
            out_response = apply_mods_fn(intercepted, result["response"])
            await modification_delay_fn(out_response.pop("delay_ms", None))
            return local_http_resp_fn(out_response)

        if result["action"] == "forward":
            metrics_registry.cloud_misses_total.inc()
            if config.learning.signal_forward_to_cloud:
                await discard_capture_fn(device_id, result, count_as_error=False)
                return JSONResponse(
                    status_code=502,
                    content={"action": "forward", "reason": result.get("reason", "no_match")},
                    headers={"X-Action": "forward", "X-Original-Host": str(request.url)},
                )

            cloud_response = await adapter.forward_to_cloud(intercepted)
            if cloud_response and cloud_response.success:
                resp_body = cloud_response.response or {}
                await orchestrator.handle_response(
                    device_id=device_id,
                    vendor=vendor,
                    protocol="http",
                    status_code=cloud_response.status_code or 200,
                    headers=cloud_response.response_headers or {"content-type": "application/json"},
                    body=resp_body,
                    correlation_key=result.get("correlation_key"),
                )
                return upstream_resp_fn(cloud_response, resp_body)

            await discard_capture_fn(device_id, result)
            return JSONResponse(
                status_code=502,
                content={
                    "error": "Cloud passthrough failed",
                    "detail": str(cloud_response.error) if cloud_response else "No response",
                },
            )

        if result["action"] == "no_fallback":
            return JSONResponse(
                status_code=501,
                content={
                    "error": "Not implemented",
                    "detail": "No local pattern matched and cloud fallback is disabled.",
                    "device_id": device_id,
                    "mode": "production_no_fallback",
                },
            )

        return JSONResponse(
            status_code=500,
            content={"error": f"Unknown action: {result.get('action')}"},
        )

    except Exception as e:
        logger.error(f"Error processing request: {e}", exc_info=True)
        await discard_capture_fn(device_id, result)
        return JSONResponse(
            status_code=500,
            content={"error": "An internal error occurred"},
        )

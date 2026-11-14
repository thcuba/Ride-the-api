"""Unit tests verifying the modularized core.proxy architecture and backward compatibility."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

import core.proxy as proxy_mod
import core.server as server_mod
from adapters.base import ProtocolAdapterRegistry
from core.proxy import (
    _DEVICE_ID_HEADERS,
    _extract_device_id,
    _raw_target,
    _select_handler_adapter,
    proxy_vendor_request,
)
from core.routes.introspection import iter_routes

EXPECTED_STATUS_SERVICE_UNAVAILABLE = 503


def test_proxy_package_exports() -> None:
    """Verify core.proxy exports all expected public and internal interface symbols."""
    expected_symbols = [
        "_BODYLESS_STATUSES",
        "_DEVICE_ID_HEADERS",
        "_ENRICHMENT_FIELDS",
        "_LAST_SEEN_WRITE_INTERVAL_S",
        "_MAX_CLAIMED_DEVICE_ID",
        "_MAX_MODIFICATION_DELAY_S",
        "_RESPONSE_HOP_BY_HOP",
        "_apply_response_modifications",
        "_discard_pending_capture",
        "_extract_device_id",
        "_forward_bypassed_request",
        "_get_request_body",
        "_is_local_ip",
        "_last_seen_is_stale",
        "_local_http_response",
        "_modification_delay",
        "_raw_target",
        "_resolve_device_for_ip",
        "_select_handler_adapter",
        "_upstream_response",
        "handle_device_seen",
        "handle_protocol_request",
        "handle_tls_decrypted_request",
        "handle_tls_upstream_failure",
        "handle_tls_upstream_response",
        "mark_device_untrusted",
        "proxy_router",
        "proxy_vendor_request",
        "record_relayed_connection",
        "resolve_upstream",
        "should_relay_tls",
    ]
    for symbol in expected_symbols:
        assert hasattr(proxy_mod, symbol), f"core.proxy missing symbol {symbol}"
        assert getattr(proxy_mod, symbol) is getattr(server_mod, symbol), (
            f"core.server re-export mismatch for {symbol}"
        )


def test_proxy_router_mounted_in_app() -> None:
    """Verify proxy_router is included in the main FastAPI application."""
    routes = [getattr(r, "path", None) for r in iter_routes(server_mod.app)]
    assert "/{vendor}/{path:path}" in routes


def test_forwarder_raw_target() -> None:
    """Verify URL path extraction preserves raw target path."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/shelly/status",
        "raw_path": b"/shelly/status",
        "query_string": b"query=1",
        "headers": [],
    }
    req = Request(scope)
    assert _raw_target(req) == "/status?query=1"


def test_extract_device_id_from_headers() -> None:
    """Verify device ID extraction from various standard headers."""
    for header in _DEVICE_ID_HEADERS:
        headers = {header: "dev-12345"}
        assert _extract_device_id(headers, "shelly/status") == "dev-12345"


def test_select_handler_adapter_lookup() -> None:
    """Verify adapter selection logic."""
    registry = MagicMock(spec=ProtocolAdapterRegistry)
    adapter = MagicMock()
    registry.get_adapter.return_value = adapter
    assert _select_handler_adapter(registry, "shelly", "http") is adapter
    registry.get_adapter.assert_called_once_with("shelly")


@pytest.mark.asyncio
async def test_proxy_vendor_request_service_not_ready() -> None:
    """Verify 503 response when core services are not initialized."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/shelly/status",
        "headers": [],
    }
    req = Request(scope)
    orig_db = server_mod.db_manager
    try:
        server_mod.db_manager = None
        resp = await proxy_vendor_request("shelly", "status", req)
        assert resp.status_code == EXPECTED_STATUS_SERVICE_UNAVAILABLE
    finally:
        server_mod.db_manager = orig_db

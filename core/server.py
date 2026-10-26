Warning: truncated output (original token count: 37081)
Total output lines: 3354

"""
Local Cloud Replacement Proxy - Main entry point.
Intercepts device traffic, learns protocol via LLM, serves responses locally.
"""

from __future__ import annotations

import asyncio  # noqa: TC003
import base64
import binascii
import json
import logging
import os
import subprocess
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import dns.asyncquery
import dns.message
import dns.rdatatype
import uvicorn
from fastapi import FastAPI, File, Form, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, func, select

from adapters import get_registered_registry
from adapters.base import (
    InterceptedRequest,
    ProtocolAdapter,
    ProtocolAdapterRegistry,
    ProtocolType,
    device_id_from_ip,
    normalize_request_body,
)
from core.atomic_io import sanitize_filename_component
from core.buffer import (
    dispose_memory_db,
    get_buffer_backend,
    initialize_buffer_backend,
    persist_backend,
    set_buffer_backend,
)
from core.cert_manager import CertManager, get_cert_manager
from core.config import ConnectionType, get_config_manager
from core.connection_watchdog import ConnectionWatchdog
from core.database import (
    DatabaseManager,
    DeviceRegistry,
    FieldMapping,
    RelayedConnection,
    RequestPattern,
    ResponseTemplate,
    init_db_manager,
)
from core.dns_server import get_dns_interceptor
from core.llm_decipher import LLMDecipherService, get_llm_decipher
from core.logging_config import setup_logging
from core.modification import get_modification_engine
from core.observability import (
    MetricsServer,
    build_tracer,
    get_tracer,
)
from core.observability import (
    registry as metrics_registry,
)
from core.paths import bundle_root, is_frozen, resource_path
from core.pattern_db import buffer_manager, decipher_ingest
from core.pattern_db.schemas import CaptureDB, DeviceModel, PatternDB
from core.pattern_db.validator import (
    ValidationError,
    validate_capture,
    validate_pattern,
)
from core.pipeline import (
    LearningOrchestrator,
    get_orchestrator,
)
from core.protocol_servers import get_protocol_server_manager
from core.protocol_servers.coap_server import CoAPServerPlugin
from core.protocol_servers.http2_server import HTTP2ServerPlugin
from core.protocol_servers.matter_bridge import MatterBridgePlugin
from core.protocol_servers.modbus_server import ModbusServerPlugin
from core.protocol_servers.mqtt_server import MQTTServerPlugin
from core.protocol_servers.raw_tcp_server import RawTCPServerPlugin
from core.protocol_servers.websocket_server import WebSocketServerPlugin
from core.protocol_servers.zigbee_bridge import ZigbeeBridgePlugin
from core.protocol_servers.zwave_bridge import ZWaveBridgePlugin
from core.resilience import (
    AutoSwitchScheduler,
    register_resilience_routes,
)
from core.security import (
    ControlPlaneAuthMiddleware,
    MaxBodySizeMiddleware,
    SecurityHeadersMiddleware,
    setup_token,
)
from core.tls_mitm import (
    DecryptedRequest,
    TLSMITMServer,
    get_tls_mitm_server,
    peek_tls_mitm_server,
)
from core.traffic_selector import TrafficRequestInfo, get_traffic_selector, is_local_ip

# Path to webui directory (bundle-aware: works in source and PyInstaller builds)
WEBUI_DIR = resource_path("webui")
DASHBOARD_HTML = WEBUI_DIR / "dashboard.html"
PATTERNS_HTML = WEBUI_DIR / "patterns.html"
CONFIG_HTML = WEBUI_DIR / "config.html"
OPTIONS_HTML = WEBUI_DIR / "options.html"

# Loader-path vars that PyInstaller sets to point at its bundled libssl/libcrypto.
# Spawned curl/git would load the bundle copies and fail at startup ("libssl.so.3:
# version `OPENSSL_3.x' not found"), killing the update download. Always stripped
# from child-process environments.
_UPDATER_STRIP_ENV = ("LD_LIBRARY_PATH", "LD_PRELOAD", "DYLD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES")


def updater_env() -> dict[str, str]:
    """Return the current environment without PyInstaller loader-path variables.

    The update/installer scripts are launched as detached child processes; under a
    frozen bundle the inherited ``LD_LIBRARY_PATH`` (pointing into ``_internal/``)
    shadows the system OpenSSL and breaks ``curl``/``git``. Returns an env safe to
    spawn the updater with (system dynamic libraries are used instead).
    """
    return {k: v for k, v in os.environ.items() if k not in _UPDATER_STRIP_ENV}


# Configure logging
setup_logging(level="INFO", fmt="json")
logger = logging.getLogger(__name__)


# Global instances
db_manager: DatabaseManager | None = None
adapter_registry: ProtocolAdapterRegistry | None = None
orchestrator: LearningOrchestrator | None = None
llm_decipher_service: LLMDecipherService | None = None
config_manager = get_config_manager()
tls_mitm_server: TLSMITMServer | None = None
cert_manager: CertManager | None = None
auto_switch_scheduler: AutoSwitchScheduler | None = None
metrics_server: MetricsServer | None = None
_metrics_scrape_task: asyncio.Task | None = None
connection_watchdog: ConnectionWatchdog | None = None

APP_VERSION_FALLBACK = "0.2.0"

# How often the metrics gauge refreshing worker re-reads live state (seconds).
_METRICS_SCRAPE_INTERVAL = 10


def runtime_version() -> str:
    """Return the installed release/tag version.

    The deploy scripts (``deploy/rideapi``, ``bootstrap.sh``, ``update.sh``)
    write the installed tag to ``<install-dir>/VERSION``; in both the source
    tree and the frozen bundle the process CWD is the install root, so the
    file is read from there. Falls back to the hardcoded app version when the
    file is absent (e.g. a dev checkout without a version stamp).
    """
    try:
        v = Path.cwd() / "VERSION"
        if v.is_file():
            text = v.read_text(encoding="utf-8").strip()
            if text:
                return text
    except OSError:
        pass
    return APP_VERSION_FALLBACK


# Protocol server instances (lazy-loaded on first access)
protocol_servers: dict[str, object] = {}
protocol_server_tasks: dict[str, asyncio.Task] = {}

# Event loop the protocol-server manager is active on; refreshed at every
# startup and used by the config-reload callback (which runs on the config
# watch thread) to schedule async start/stop via run_coroutine_threadsafe.
_protocol_event_loop: asyncio.AbstractEventLoop | None = None

# Pre-computed immutable tuples for hot-path request inspection
_ENRICHMENT_FIELDS: tuple[str, ...] = ("security", "identity", "kind")
_DEVICE_ID_HEADERS: tuple[str, ...] = (
    "x-device-id",
    "x-deviceid",
    "device-id",
    "deviceid",
    "x-client-id",
    "x-sn",
)


# ── TLS Decrypted Request Handler ────────────────────────────────────────────


def _select_handler_adapter(
    adapter_registry: ProtocolAdapterRegistry | None,
    device_vendor: str,
    resolved_protocol: str,
) -> ProtocolAdapter | None:
    """Pick the protocol adapter for a device request.

    - A registered, vendor-specific adapter always wins (e.g. ``shelly``), so
      devices with a known vendor keep their bespoke handler.
    - Otherwise, fall back to the first adapter that supports the *resolved*
      protocol (from ``ip_profiles`` → ``device_meta`` → ingress default). This
      is the new ingress consumption of ``connection_mode``/``ProtocolInfo``:
      a device whose protocol was decided at first flush gets its **adapter**
      resolved from that hierarchy, so it is handled by the matching adapter
      even when its vendor is generic/unknown. The resolved protocol is used
      only for adapter selection, **not** for request routing (inside
      ``handle_request`` the ``protocol`` argument is unused).
    """
    if not adapter_registry:
        return None

    # 1) Vendor-specific adapter (unchanged behaviour).
    vendor_adapter = adapter_registry.get_adapter(device_vendor)
    if vendor_adapter is not None:
        return vendor_adapter

    # 2) Adapter for the resolved protocol (new).
    try:
        resolved = ProtocolType(resolved_protocol)
    except (ValueError, KeyError):
        return None
    matched = adapter_registry.get_adapter_by_protocol(resolved)
    return matched[0] if matched else None


async def _resolve_device_for_ip(db, client_ip: str | None) -> str | None:
    """Resolve a source IP to a known device id, when it is bound.

    Automatic device recognition: if ``client_ip`` is exposed by the producer
    and already appears in some device's ``ip_addresses``, return that device
    id so an already-recognized device keeps ONE identity across ports and
    protocols (and reuses its learned protocol/vendor). Returns ``None`` for
    unbound IPs, ``"unknown"`` placeholders, or IP-less protocols so the caller
    falls back to its own generated id.
    """
    if not client_ip or client_ip == "unknown":
        return None
    return await db.resolve_device_id(client_ip)


async def handle_device_seen(client_ip: str, sni: str | None) -> None:
    """Register a device the moment it presents itself on a TLS port.

    Called by the TLS MITM before the handshake completes. A device that pins its vendor's
    certificate (a vendor cloud IoT broker, for example) aborts the handshake, so every step after a
    *successful* handshake never runs for it: such a device stayed invisible in the registry and
    on the dashboard even though the proxy saw each of its connection attempts. The address is the
    identity here, and the SNI is kept as evidence of what the device asked to talk to.
    """
    if not db_manager:
        return
    device_id = device_id_from_ip("ip", client_ip)
    await db_manager.get_or_create_device(device_id, "unknown")
    async with db_manager.core_session() as session:
        result = await session.execute(
            select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
        )
        device = result.scalar_one_or_none()
        if device is None:
            return
        changed = False
        known_ips = list(device.ip_addresses or [])
        if client_ip not in known_ips:
            known_ips.append(client_ip)
            device.ip_addresses = known_ips
            changed = True
        if sni:
            attributes = dict(device.extra_attributes or {})
            hostnames = list(attributes.get("seen_hostnames") or [])
            if sni not in hostnames:
                hostnames.append(sni)
                attributes["seen_hostnames"] = hostnames
                device.extra_attributes = attributes
                changed = True
        if changed:
            await session.commit()
            db_manager.invalidate_ip_lookup_cache()


async def should_relay_tls(client_ip: str) -> bool:
    """Say whether a connection must be forwarded untouched instead of decrypted.

    True for a device already known to refuse our certificate: decrypting it again would only
    break its communication a second time, while the point is to keep it talking to its cloud.
    """
    if not db_manager:
        return False
    device_id = await db_manager.resolve_device_id(client_ip)
    if not device_id:
        return False
    device = await db_manager.get_device(device_id)
    if not device:
        return False
    return bool((device.extra_attributes or {}).get("tls_passthrough"))


async def mark_device_untrusted(client_ip: str, sni: str | None) -> None:
    """Record that a device refused our certificate, so it is relayed from now on.

    A device that pins its vendor's certificate answers our ServerHello with a TLS alert, and no
    server-side change alters that. The useful response is to stop terminating its TLS: the next
    connection is forwarded verbatim to its cloud and the device keeps working. Persisted in
    ``extra_attributes`` under ``tls_passthrough`` -- the field the TLS settings page already
    exposes -- so the decision survives a restart.
    """
    if not db_manager:
        return
    device_id = await db_manager.resolve_device_id(client_ip)
    if not device_id:
        device_id = device_id_from_ip("ip", client_ip)
        await db_manager.get_or_create_device(device_id, "unknown")
    async with db_manager.core_session() as session:
        result = await session.execute(
            select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
        )
        device = result.scalar_one_or_none()
        if device is None:
            return
        extra = dict(device.extra_attributes or {})
        if extra.get("tls_passthrough") and extra.get("tls_untrusted_reason"):
            return  # already recorded: do not rewrite the row on every retry
        extra["tls_passthrough"] = True
        extra["tls_untrusted_reason"] = "client_refused_certificate"
        hostnames = list(extra.get("seen_hostnames") or [])
        if sni and sni not in hostnames:
            hostnames.append(sni)
        extra["seen_hostnames"] = hostnames
        device.extra_attributes = extra
        await session.commit()
    # get_device() serves rows from a 300 s TTL cache: without this the relay decision would keep
    # reading the device as it was before the refusal and would not take effect for five minutes.
    db_manager.invalidate_device_cache(device_id)
    logger.info(
        "TLS: %s refused our certificate; from now on it is forwarded to its cloud undecrypted",
        client_ip,
    )


async def record_relayed_connection(contact: dict) -> None:
    """Persist one connection that was forwarded without decryption.

    The payload of such a connection is unreadable by construction, but the contact is real evidence
    that the device is alive and whom it talks to: which host, how much each way, how long, and
    whether the attempt even succeeded. For a device we cannot see inside, this is its record.
    """
    if not db_manager:
        return
    client_ip = contact.get("client_ip") or ""
    device_id = await db_manager.resolve_device_id(client_ip)
    if not device_id:
        device_id = device_id_from_ip("ip", client_ip)
        await db_manager.get_or_create_device(device_id, "unknown")
    async with db_manager.core_session() as session:
        session.add(
            RelayedConnection(
                device_id=device_id,
                client_ip=client_ip,
                server_name=contact.get("server_name"),
                destination_port=int(contact.get("destination_port") or 0),
                upstream_ip=contact.get("upstream_ip"),
                bytes_to_cloud=int(contact.get("bytes_to_cloud") or 0),
                bytes_to_device=int(contact.get("bytes_to_device") or 0),
                duration_ms=int(contact.get("duration_ms") or 0),
                outcome=contact.get("outcome") or "relayed",
            )
        )
        await session.commit()


async def resolve_upstream(host: str) -> str | None:
    """Resolve ``host`` through the configured upstream resolvers.

    Deliberately not ``getaddrinfo``: the proxy may be the machine's own resolver -- that is the
    point of the DNS interception -- so a local lookup could resolve straight back to us. The
    ``dns.dns_servers`` list exists for exactly this: loop-free cloud forwarding.
    """
    servers = [*config_manager.config.dns.dns_servers, *config_manager.config.dns.dns_servers_v6]
    if not servers:
        return None
    query = dns.message.make_query(host, dns.rdatatype.A)
    for server in servers:
        try:
            response = await dns.asyncquery.udp(query, server, timeout=3)
        except Exception:
            continue
        for answer in response.answer:
            for item in answer:
                if item.rdtype == dns.rdatatype.A:
                    return str(item)
    return None


async def handle_tls_decrypted_request(req: DecryptedRequest) -> dict | None:
    """Handle a decrypted TLS request — find/create device and run through pipeline.

    Called by TLSMITMServer for every successfully decrypted HTTP request.
    If the source IP is unknown, a new device record + SQLite DB is auto-created
    with passthrough=ON (traffic forwarded to cloud while user configures it).
    """
    global db_manager, orchestrator, adapter_registry  # noqa: PLW0602

    if not db_manager or not orchestrator:
        logger.warning("TLS handler: service not ready, dropping request from %s", req.client_ip)
        return None

    config = config_manager.config
    # Automatic device recognition: if this source IP is already bound to a
    # known device (device.ip_addresses), use THAT device instead of a fresh
    # per-IP id. The same physical device keeps one identity across ports.
    recognized = await _resolve_device_for_ip(db_manager, req.client_ip)
    device_id = recognized or device_id_from_ip("ip", req.client_ip)

    try:
        # Create or find device by IP
        await db_manager.get_or_create_device(
            device_id=device_id,
            vendor="unknown",
        )
        device = await db_manager.get_device(device_id)

        # Apply any per-IP override: custom database + connection type (default auto)
        await db_manager.apply_ip_profile(device_id, req.client_ip)

        # Per-IP bypass: forward this device's traffic straight to the cloud
        # without pipeline analysis (no buffer/LLM/local match).
        if await db_manager.is_ips_bypassed(req.client_ip):
            logger.info("TLS: %s is in bypass, forwarding to cloud directly", req.client_ip)
            return {"action": "forward", "reason": "bypass"}

        # Ensure a dedicated device database exists
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

        # Log the intercepted request
        logger.info(
            "TLS: %s %s %s (device=%s, sni=%s, port=%d)",
            req.method,
            req.path,
            req.http_version,
            device_id,
            req.sni,
            req.dst_port,
        )

        # Determine vendor/adapter for this device
        device_vendor = getattr(device, "vendor", "unknown") or "unknown"

        # Resolve the operational protocol (ip_profiles > device_meta -> default).
        # The TLS path always speaks https, so an undecided device stays https.
        resolved_protocol = await db_manager.resolve_device_protocol(
            device_id, ingress_default="https"
        )

        # Find matching adapter (vendor-specific first, else by resolved protocol).
        handler_adapter = _select_handler_adapter(
            adapter_registry, device_vendor, resolved_protocol
        )

        # Build intercepted request for pipeline

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

        # Let the adapter refine the intercepted request (intent, params,
        # device_id, query params...). Any fields it changes are what the
        # orchestrator pipeline should see.
        if handler_adapter:
            intercepted = await handler_adapter.parse_request(intercepted)

        # Run through the orchestrator pipeline
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

        # The TLS server performs its own upstream exchange after this handler
        # returns. Carry the correlation identity with that pending forward so
        # the actual upstream response can complete the request/response pair.
        if result.get("action") == "forward" and result.get("correlation_key"):
            result = {
                **result,
                "_learning_context": {
                    "device_id": intercepted.device_id or device_id,
                    "vendor": device_vendor,
                    "protocol": resolved_protocol,
                },
            }

        if result["action"] == "local_response":
            logger.debug("TLS: local response for %s %s", req.method, req.path)
        else:
            logger.debug("TLS: passthrough for %s %s", req.method, req.path)

        return result  # noqa: TRY300

    except Exception as e:
        logger.error("TLS handler: error processing %s: %s", req.client_ip, e, exc_info=True)
        return None


async def handle_tls_upstream_response(handler_result: dict, response: dict) -> dict | None:
    """Correlate a TLS MITM upstream response with its captured request.

    The TLS listener owns both sides of the terminated connection, so unlike
    the HTTP proxy path it cannot rely on nginx to deliver the upstream
    response to :meth:`LearningOrchestrator.handle_response`.
    """
    global orchestrator  # noqa: PLW0602

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
    )


# ── Application Lifespan ───────────────────────────────────────────────────────


async def handle_protocol_request(request: InterceptedRequest) -> dict | None:
    """Handle an intercepted request from a protocol server plugin.

    Common handler wired into every protocol server plugin (MQTT, CoAP, Modbus,
    WebSocket, Raw TCP, HTTP/2, bridges). It resolves the device, then runs the
    request through the same orchestrator pipeline as the TLS/HTTP paths.

    Returns the pipeline result dict (``action`` + optional ``response``), or
    ``None`` when the services aren't ready. A ``local_response`` result means
    the plugin should send ``result["response"]`` back to the device.
    """
    if not db_manager or not orchestrator:
        logger.warning("Protocol handler: service not ready, dropping %s", request.device_id)
        return None

    device_id = request.device_id or "unknown"

    # Automatic device recognition: a plugin-exposed source IP bound to a known
    # device wins over the per-protocol generated id, so an already-recognized
    # device keeps ONE identity across ports/protocols (and reuses its learned
    # protocol/vendor). IP-less protocols (MQTT/Modbus/bridges) and unbound IPs
    # keep the generated id.
    device_id = (
        await _resolve_device_for_ip(db_manager, getattr(request, "client_ip", None))
    ) or device_id

    protocol = (
        request.protocol.value
        if hasattr(request.protocol, "value")
        else str(request.protocol or "http")
    )
    me…25081 tokens truncated…== device_id)
            )
            device = result.scalar_one_or_none()
            vendor = device.vendor if device else "unknown"
            device_type = device.device_type if device else "unknown"
        capture = await manager.export_capture(device_id, vendor, device_type)
        return capture.model_dump(by_alias=True, exclude_none=True)
    except Exception:
        logger.exception("Handler error (500)")
        return JSONResponse(status_code=500, content={"error": "An internal error occurred"})


@app.post("/api/devices/{device_id}/capture/import")
async def import_buffer(device_id: str, request: Request):
    """Import raw pairs from portable .ride-capture.json into buffer."""
    if not db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    manager = buffer_manager.BufferManager(db_manager)
    try:
        body = await request.json()
        # Validate against the portable JSON Schema
        result = validate_capture(body)
        if not result.valid:
            return JSONResponse(
                status_code=422,
                content={"error": "Capture validation failed", "details": result.to_dict()},
            )
        capture = CaptureDB.model_validate(body)
        # F-08: the payload's (obfuscated) device id must not steer the import
        # into a different device's buffer; force the authorized URL device.
        if capture.device_info.device_id not in ("obfuscated", device_id):
            logger.warning(
                "Capture import device_id mismatch (payload=%r, url=%r); "
                "overriding to url device_id",
                capture.device_info.device_id,
                device_id,
            )
        count = await manager.import_capture(capture, target_device_id=device_id)
        return {"imported": count, "device_id": device_id, "warnings": result.warnings}  # noqa: TRY300
    except ValidationError as e:
        return JSONResponse(
            status_code=422,
            content={"error": "Capture validation failed", "details": e.to_dict()},
        )
    except Exception as e:
        return _client_error(e, "Invalid capture import payload")


# ═══════════════════════════════════════════════════════════════════════════════
# WEB UI (basic dashboard)
# ═══════════════════════════════════════════════════════════════════════════════


# Web UI page cache. webui/*.html are static at runtime, so we read them once
# and serve from memory on subsequent GETs, avoiding repeated blocking disk
# I/O inside async handlers. Only successful reads are cached: a missing file
# is not memoized so a later-created file is picked up.
_WEBUI_HTML_CACHE: dict[Path, str] = {}
_WEBUI_HTML_CACHE.clear()


async def _serve_webui(path: Path, page_name: str, expected: str) -> HTMLResponse:
    """Serve a cached webui HTML page, reading from disk once if needed."""
    html = _WEBUI_HTML_CACHE.get(path)
    if html is None:
        try:
            html = await asyncio.to_thread(path.read_text, encoding="utf-8")
        except FileNotFoundError:
            logger.warning("%s HTML not found at %s", page_name, path)
            html = (
                f"<!DOCTYPE html><html><body><h1>{page_name} not found</h1>"
                f"<p>Expected at {expected}</p></body></html>"
            )
        else:
            _WEBUI_HTML_CACHE[path] = html
    return HTMLResponse(content=html, status_code=200)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Simple web dashboard."""
    return await _serve_webui(DASHBOARD_HTML, "Dashboard", "webui/dashboard.html")


@app.get("/patterns/{device_id}", response_class=HTMLResponse)
async def patterns_page(device_id: str):  # noqa: ARG001
    """Serve the Patterns web UI for a device."""
    return await _serve_webui(PATTERNS_HTML, "Patterns page", "webui/patterns.html")


@app.get("/config", response_class=HTMLResponse)
async def config_page():
    """Serve the Configuration web UI."""
    return await _serve_webui(CONFIG_HTML, "Config page", "webui/config.html")


@app.get("/options", response_class=HTMLResponse)
async def options_page():
    """Serve the Options web UI (theme + software update)."""
    return await _serve_webui(OPTIONS_HTML, "Options page", "webui/options.html")


# ═══════════════════════════════════════════════════════════════════════════════
# Resilience routes (must be registered before catch-all to take priority)
# ═══════════════════════════════════════════════════════════════════════════════

register_resilience_routes(app, lambda: db_manager, lambda: orchestrator)

# ═══════════════════════════════════════════════════════════════════════════════
# Static files for Web UI (registered BEFORE the catch-all proxy route so that
# /static/* is never captured as a {vendor}).
try:
    _static_dir = WEBUI_DIR / "static"
    if _static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
    else:
        logger.warning("Static dir not found: %s", _static_dir)
except Exception as e:
    logger.warning("Could not mount static files: %s", e)

# ── Main proxy endpoint helpers ───────────────────────────────────────────────

# Hop-by-hop/framing headers never replayed to the device: they describe the
# upstream connection, not the message (the response object sets its own).
_RESPONSE_HOP_BY_HOP = ("content-length", "transfer-encoding", "connection")


def _client_error(exc: BaseException, message: str, status_code: int = 400) -> JSONResponse:
    """Return a control-plane error response that never leaks internals.

    Raw exception text can carry filesystem paths, library internals or
    configuration details (CodeQL ``py/stack-trace-exposure``). The client gets
    a stable, actionable message; the detail is logged server-side, where it is
    actually useful for debugging.
    """
    logger.info("Rejected request (%s): %s", message, exc)
    return JSONResponse(status_code=status_code, content={"error": message})


def _upstream_response(cloud_response, decoded: dict | None = None) -> Response:
    """Replay an upstream response verbatim: status, headers and body bytes.

    A proxy must preserve the conversation. Inventing a 200 with a JSON body
    turns a vendor 401/429/500 into a false success for the device and drops
    cookies, ``retry-after`` and content negotiation. The raw upstream bytes are
    reused as-is, so non-JSON payloads survive byte-for-byte.
    """
    body = getattr(cloud_response, "body_bytes", None)
    if body is None:
        fallback = decoded if decoded is not None else (cloud_response.response or {})
        body = json.dumps(fallback).encode("utf-8")
    headers = {
        str(k): str(v)
        for k, v in (getattr(cloud_response, "response_headers", None) or {}).items()
        if str(k).lower() not in _RESPONSE_HOP_BY_HOP
    }
    headers.setdefault("content-type", "application/json")
    return Response(
        content=body,
        status_code=getattr(cloud_response, "status_code", None) or 200,
        headers=headers,
    )


async def _forward_bypassed_request(  # noqa: PLR0913
    adapter, request: Request, path: str, body, client_ip: str, *, raw_body: bytes | None = None
) -> Response:
    """Forward a bypassed device's request straight to the cloud.

    Used by the HTTP proxy endpoint for per-IP bypass: the traffic transits the
    gateway but is forwarded without pipeline analysis (no buffer/LLM/match).
    Mirrors the unmatched-request forward contract — 502 + X-Action: forward
    for nginx when ``signal_forward_to_cloud`` is on, else legacy adapter
    forward.
    """
    config = config_manager.config
    if config.learning.signal_forward_to_cloud:
        return JSONResponse(
            status_code=502,
            content={"action": "forward", "reason": "bypass"},
            headers={"X-Action": "forward", "X-Original-Host": str(request.url)},
        )
    cloud_response = await adapter.forward_to_cloud(
        InterceptedRequest(
            device_id="",
            timestamp=datetime.now(UTC),
            protocol=(ProtocolType.HTTPS if request.url.scheme == "https" else ProtocolType.HTTP),
            method=request.method,
            path=f"/{path}",
            headers=dict(request.headers),
            query_params=dict(request.query_params),
            body=body,
            body_bytes=raw_body,
            client_ip=client_ip,
        )
    )
    if cloud_response and cloud_response.success:
        return _upstream_response(cloud_response)
    return JSONResponse(
        status_code=502,
        content={
            "error": "Cloud passthrough failed",
            "detail": str(cloud_response.error) if cloud_response else "No response",
        },
    )


# MAIN PROXY ENDPOINT - Catches all device traffic
# ═══════════════════════════════════════════════════════════════════════════════


@app.api_route(
    "/{vendor}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def proxy_vendor_request(vendor: str, path: str, request: Request):  # noqa: C901, PLR0911
    """Main proxy endpoint for device API requests.

    Routes:
        - /{vendor}/* -> adapter for that protocol (e.g., /example/*)

    For each request, the pipeline decides:
      learning mode: forward to cloud, correlate, buffer, learn
      production mode: try local match, fall back to cloud + learn
    """
    if not db_manager or not orchestrator or not adapter_registry:
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

    # Prometheus: count every proxied request.
    metrics_registry.requests_total.inc()

    # Parse request body. The raw bytes are kept too: a forward must be
    # byte-faithful and never re-encode a non-JSON payload as JSON.
    raw_body = await request.body()
    body = await _get_request_body(request)

    # Determine source
    client_ip = request.client.host if request.client else "unknown"
    is_local = _is_local_ip(client_ip)

    # Check traffic selection rules
    if traffic_selector := get_traffic_selector():
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
        if decision.value == "passthrough" and is_local:
            # The device addressed *this* gateway, so a passthrough decision
            # must actually forward the request upstream. Replying 200 with a
            # note instead fabricated a success the device never received.
            logger.info(f"Passthrough for local traffic from {client_ip} to {vendor}: forwarding")
            return await _forward_bypassed_request(
                adapter, request, path, body, client_ip, raw_body=raw_body
            )

    # Per-IP bypass: forward this device's traffic straight to the cloud, skipping
    # the pipeline entirely (no buffer/LLM/local match). Uses the same forward
    # contract as an unmatched request: 502 + X-Action: forward for nginx when
    # signal_forward_to_cloud is on, else the legacy adapter forward.
    if await db_manager.is_ips_bypassed(client_ip):
        logger.info(f"Bypass for {client_ip} to {vendor}: forwarding to cloud directly")
        return await _forward_bypassed_request(
            adapter, request, path, body, client_ip, raw_body=raw_body
        )

    # Build intercepted request
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
    )

    try:
        # Parse request to extract device info
        intercepted = await adapter.parse_request(intercepted)
        device_id = intercepted.device_id or _extract_device_id(request.headers, path)

        if not device_id:
            logger.warning(f"Could not extract device ID from request to {vendor}")
            device_id = f"unknown_{vendor}_{hash(str(request.url)) % 10000}"

        # Ensure device is registered
        await db_manager.get_or_create_device(device_id, vendor)

        # Apply request modification rules (on-the-fly modification engine),
        # mutating the intercepted request in place so the pipeline sees the
        # transformed request while the local-match is served.
        get_modification_engine().process_request(intercepted)

        # Pass to pipeline for processing
        with get_tracer().start_as_current_span("proxy.handle_request") as span:
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
            # Serve locally from learned patterns, applying on-the-fly
            # response modification rules.
            metrics_registry.local_hits_total.inc()
            out_response = _apply_response_modifications(intercepted, result["response"])
            return JSONResponse(
                status_code=out_response.get("status_code", 200),
                content=out_response.get("body", {}),
                headers=out_response.get("headers", {}),
            )
        if result["action"] == "forward":
            # Prometheus: count cloud forwards (requests not served locally).
            metrics_registry.cloud_misses_total.inc()
            # Forward to cloud (passthrough).
            #
            # When signal_forward_to_cloud is enabled, the proxy tells nginx
            # to route the request upstream rather than doing it internally.
            # This avoids the DNS loop: nginx resolves the cloud domain
            # via 8.8.8.8 / 1.1.1.1, bypassing the local DNS.
            config = config_manager.config
            if config.learning.signal_forward_to_cloud:
                # Signal nginx to forward the request to the real cloud upstream
                return JSONResponse(
                    status_code=502,
                    content={"action": "forward", "reason": result.get("reason", "no_match")},
                    headers={"X-Action": "forward", "X-Original-Host": str(request.url)},
                )
            # Legacy path: forward via the adapter (may cause DNS loop)
            cloud_response = await adapter.forward_to_cloud(intercepted)
            if cloud_response and cloud_response.success:
                # Process cloud response for learning. The payload lives in
                # `CommandResult.response`, not `.data` — the old code read a
                # nonexistent attribute, so the device always got an empty body.
                resp_body = cloud_response.response or {}
                # Learn from the *real* upstream status/headers, and replay them
                # to the device unchanged (see _upstream_response).
                await orchestrator.handle_response(
                    device_id=device_id,
                    vendor=vendor,
                    protocol="http",
                    status_code=cloud_response.status_code or 200,
                    headers=cloud_response.response_headers or {"content-type": "application/json"},
                    body=resp_body,
                )
                return _upstream_response(cloud_response, resp_body)
            return JSONResponse(
                status_code=502,
                content={
                    "error": "Cloud passthrough failed",
                    "detail": str(cloud_response.error) if cloud_response else "No response",
                },
            )
        if result["action"] == "no_fallback":
            # Production mode with no_cloud_fallback enabled — conclusive
            # local-only response.  The request was not matched and we
            # deliberately refuse to contact the cloud.
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
        return JSONResponse(
            status_code=500,
            content={"error": "An internal error occurred"},
        )


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════


def _apply_response_modifications(intercepted: InterceptedRequest, response: dict) -> dict:
    """Apply on-the-fly response modification rules, normalising to a wrapper.

    ``get_modification_engine().process_response`` returns either the wrapper
    dict ``{status_code, headers, body}`` (when only ``modifications`` are
    attached) or, when a rule rewrites the body, the body value on its own.
    This normalises both back to a well-formed ``{status_code, headers, body}``
    wrapper the HTTP serving path can use directly.
    """
    modified, _was_modified = get_modification_engine().process_response(intercepted, response)
    if isinstance(modified, dict) and "status_code" in modified:
        return modified
    out = dict(response)
    if modified is not None:
        # ``modifications`` is engine tracking metadata, not part of the
        # device response payload — strip it before putting it in the body.
        if isinstance(modified, dict):
            modified = {k: v for k, v in modified.items() if k != "modifications"}
        out["body"] = modified
    return out


async def _get_request_body(request: Request) -> dict | None:
    """Extract JSON body from request.

    Fast zero-allocation byte indexing check avoids calling json.loads and catching
    JSONDecodeError/UnicodeDecodeError for non-JSON or plain-text payloads (~21.7x faster).
    """
    try:
        body = await request.body()
        if not body:
            return None
        i = 0
        n = len(body)
        while i < n and body[i] in b" \t\r\n":
            i += 1
        if i >= n or body[i] not in (123, 91):  # ord('{') == 123, ord('[') == 91
            return None
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        # Non-JSON request bodies (XML, raw bytes, empty) are legitimate
        # device payloads; log at debug instead of failing the request.
        logger.debug("Request body is not JSON: %s", e)
        return None


def _is_local_ip(ip: str) -> bool:
    """Check if IP is in a private/local range."""
    return is_local_ip(ip)


def _extract_device_id(headers: dict, path: str) -> str:
    """Try to extract device ID from headers or path."""
    # Check common headers in priority order
    for header in _DEVICE_ID_HEADERS:
        if header in headers:
            return headers[header]
    # Check path for common patterns
    parts = path.strip("/").split("/")
    for part in parts:
        if len(part) >= 8 and part.isalnum():  # noqa: PLR2004
            return part
    return "unknown"


# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD HTML
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    """Run the server."""
    config = config_manager.config
    lg = config.observability.logging
    setup_logging(level=lg.level, fmt=lg.format, output=lg.output)
    # Pass the app object (not the "core.server:app" string): the string
    # import path breaks in PyInstaller bundles where the module is packed and
    # no longer importable by name. Works in both venv and frozen builds.
    uvicorn.run(
        app,
        host=config.proxy.host,
        port=config.proxy.port,
        log_level=lg.level.lower(),
        reload=False,
    )


if __name__ == "__main__":
    main()

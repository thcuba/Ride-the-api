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

from fastapi import APIRouter, FastAPI, File, Form, Request, Response, UploadFile

from fastapi.middleware.cors import CORSMiddleware

from fastapi.responses import HTMLResponse, JSONResponse

from fastapi.staticfiles import StaticFiles

from pydantic import ValidationError as PydanticValidationError

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
    normalise_backend,
    switch_backend,
)

from core.cert_manager import CertManager, get_cert_manager

from core.config import (
    SECRET_MASK,
    Config,
    ConnectionType,
    get_config_manager,
    mask_secrets,
    restore_masked_secrets,
)

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

from core.dns_discovery import DNSDiscoveryRecorder, get_dns_recorder, set_dns_recorder

from core.dns_server import get_dns_interceptor

from core.http_framing import servable_status, without_framing_headers

from core.llm_decipher import LLMDecipherService, get_llm_decipher

from core.logging_config import get_log_level, setup_logging

from core.modification import BLOCKED_STATUS, get_modification_engine

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
    CrossSiteRequestGuard,
    MaxBodySizeMiddleware,
    SecurityHeadersMiddleware,
    password_problem,
    setup_token,
)

from core.tls_mitm import (
    DecryptedRequest,
    TLSMITMServer,
    get_tls_mitm_server,
    peek_tls_mitm_server,
)

from core.traffic_selector import TrafficRequestInfo, get_traffic_selector, is_local_ip

from core.upstream_resolver import _apply_config, self_connection_reason


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


# A device that reconnects constantly would rewrite its registry row on every

# connection. One write per interval per device keeps the dashboard honest

# without wearing the disk for information nobody can perceive as fresher.

_LAST_SEEN_WRITE_INTERVAL_S = 60


def _last_seen_is_stale(stored: datetime | None, now: datetime) -> bool:
    """Say whether ``last_seen`` is old enough to be worth writing again."""

    if stored is None:
        return True

    # SQLite can hand back a naive datetime; comparing it to an aware "now" raises.

    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=UTC)

    return (now - stored).total_seconds() >= _LAST_SEEN_WRITE_INTERVAL_S


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

    if self_connection_reason(client_ip):
        # The proxy talking to itself is not a device to register.

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

        # When the device was last seen is what the dashboard shows, and it has to

        # survive a restart: the in-memory view in tls_mitm is lost on every

        # service restart, so a device that was obviously alive read as never seen.

        now = datetime.now(UTC)

        if _last_seen_is_stale(device.last_seen, now):
            device.last_seen = now

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



    Every refusal is also **counted** (``tls_refusals``, plus first/last timestamps). Recording only

    the first one could not tell a settled device -- it refuses once and is relayed from then on --

    from one that keeps trying to negotiate, which is the difference between a device the proxy

    carries and one it keeps breaking. The write is not throttled like ``last_seen``: a refusal is a

    rare, deliberate event, and losing counts to save disk would defeat the point of counting.

    """

    if not db_manager:
        return

    if self_connection_reason(client_ip):
        # The proxy talking to itself is never a device, so there is nothing to mark.

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

        now = datetime.now(UTC)

        # Counted on every refusal; the relay decision and the reason stay idempotent so a retry

        # does not rewrite what is already settled. ``extra_attributes`` is free-form JSON written

        # by other code paths, so a corrupted count must not stop the refusal being recorded.

        # Read-then-write, like ``handle_device_seen``: two refusals arriving at the same instant

        # can lose one increment. Under-counting a burst is acceptable, losing the refusal is not.

        previous = extra.get("tls_refusals")

        extra["tls_refusals"] = (previous if isinstance(previous, int) else 0) + 1

        extra["tls_last_refusal_at"] = now.isoformat()

        if not extra.get("tls_first_refusal_at"):
            extra["tls_first_refusal_at"] = now.isoformat()

        extra["tls_passthrough"] = True

        # Falsy rather than "missing": a reason stored as an empty string or null is no reason, and

        # `setdefault` would happily keep it and leave the relay unexplained.

        if not extra.get("tls_untrusted_reason"):
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
        "TLS: %s refused our certificate (%d time(s)); from now on it is forwarded to its cloud "
        "undecrypted",
        client_ip,
        extra["tls_refusals"],
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

    if self_connection_reason(client_ip):
        # A relayed connection from this machine is not evidence about a device.

        return

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

    # Never intercept our own traffic: a request coming from the loopback

    # interface belongs to this machine, not to a device. Forward it untouched

    # (like a bypassed IP) and do not create a device, DB or buffered pair for it.

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
        correlation_key=handler_result.get("correlation_key"),
    )


async def handle_tls_upstream_failure(handler_result: dict) -> bool:
    """Remove a queued learning request when its TLS upstream exchange fails."""

    global orchestrator  # noqa: PLW0602

    correlation_key = handler_result.get("correlation_key")

    context = handler_result.get("_learning_context")

    if not orchestrator or not context or not correlation_key:
        return False

    return await orchestrator.discard_request(context["device_id"], correlation_key)


# ── Application Lifespan ───────────────────────────────────────────────────────


async def handle_protocol_request(request: InterceptedRequest) -> dict | None:  # noqa: C901
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

    # Never intercept our own traffic: a plugin that reports a loopback source IP

    # is looking at this machine talking to itself. Forward it untouched instead

    # of registering a device and buffering a pair for the proxy itself.

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

    method = request.method or ("publish" if request.topic else "GET")

    path = request.path or (f"/{request.topic.lstrip('/')}" if request.topic else "/")

    try:
        await db_manager.get_or_create_device(device_id, "unknown")

        # Per-IP bypass: forward straight to the cloud without pipeline analysis.

        # Only applies when the plugin exposed a source IP (CoAP/TCP/WS/HTTP2);

        # protocols without an IP (MQTT/Modbus/bridges) never match here.

        cli_ip = getattr(request, "client_ip", None)

        if cli_ip and await db_manager.is_ips_bypassed(cli_ip):
            logger.info("Protocol handler: %s is in bypass, forwarding to cloud", cli_ip)

            return {"action": "forward", "reason": "bypass"}

        # Resolve the operational protocol (ip_profiles > device_meta -> default).

        # The plugin protocol is the physical protocol actually spoken, so an

        # undecided device keeps the plugin's own protocol; a device whose first

        # flush decided a different connection_mode wins (config/header override).

        resolved_protocol = await db_manager.resolve_device_protocol(
            device_id, ingress_default=protocol
        )

        # Build the D2 enrichment dict from the InterceptedRequest fields emitted

        # by protocol servers (transport/security/identity/kind). It flows into

        # the correlated pair and the buffer JSON, so the observation/LLM layer

        # can reconstruct contextual metadata without re-deriving it.

        enrichment: dict | None = None

        transport = getattr(request, "transport", None)

        if transport is not None or any(
            getattr(request, f, None) is not None for f in _ENRICHMENT_FIELDS
        ):
            enrichment = {}

            if transport is not None:
                # TransportMeta is a pydantic model; collapse to a plain dict so

                # the buffer JSON serializes cleanly (no str()-of-object).

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


# ── Application Lifespan ───────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(_app: FastAPI):  # noqa: C901, PLR0912, PLR0915
    """Application lifespan handler."""

    global db_manager, adapter_registry  # noqa: PLW0603

    global orchestrator, llm_decipher_service  # noqa: PLW0603

    global tls_mitm_server, cert_manager  # noqa: PLW0603

    global auto_switch_scheduler  # noqa: PLW0603

    logger.info("Starting Local Cloud Replacement Proxy...")

    config = config_manager.config

    # Bootstrap guard: while no control-plane password is set, first-run

    # provisioning is protected by a one-time token. Announce it at startup (and

    # keep it next to the databases) so the operator can complete the setup.

    if config.security.auth_enabled and not config.security.password:
        setup_token(Path(config.core.device_db_dir).parent / "setup-token")

    # Initialize database

    data_dir = Path(config.core.device_db_dir)

    data_dir.mkdir(parents=True, exist_ok=True)

    db_manager = init_db_manager(
        core_db_url=config.core.database_url,
        device_db_dir=data_dir,
        device_db_urls=config.core.device_databases,
        echo=config.observability.logging.level == "DEBUG",
    )

    await db_manager.initialize()

    # Load the persisted buffer backend (disk | memory) so new buffer stores

    # created by the orchestrator and export/import paths honor the UI toggle.

    initialize_buffer_backend()

    logger.info("Buffer backend initialized: %s", get_buffer_backend())

    # Initialize LLM decipher service

    llm_decipher_service = get_llm_decipher()

    profiles = llm_decipher_service.list_profiles()

    logger.info(f"LLM decipher service loaded with profiles: {profiles}")

    # Initialize orchestrator

    orchestrator = get_orchestrator()

    orchestrator.initialize(llm_decipher_service)

    # Register adapters

    adapter_registry = get_registered_registry()

    # Initialize TLS certificate manager

    if config.tls_decrypt.enabled:
        try:
            cert_manager = get_cert_manager()

            cert_path = cert_manager.ensure_ca()

            logger.info("TLS CA certificate ready at %s", cert_path)

        except Exception:
            logger.exception("Failed to initialize TLS cert manager")

    # Start TLS MITM server if enabled

    if config.tls_decrypt.enabled and cert_manager:
        try:
            # get_tls_mitm_server() configures the singleton (ports + cert

            # manager); start() itself takes no arguments. The old code passed

            # cert_manager/ports/request_handler to start(), which accepts no

            # kwargs, so the call raised TypeError that the except swallowed —

            # TLS interception silently never started.

            tls_mitm_server = get_tls_mitm_server(
                cert_manager=cert_manager,
                listen_ports=config.tls_decrypt.listen_ports,
                min_tls_version=config.tls_decrypt.min_tls_version,
                max_tls_version=config.tls_decrypt.max_tls_version,
            )

            tls_mitm_server.request_handler = handle_tls_decrypted_request

            tls_mitm_server.response_handler = handle_tls_upstream_response

            tls_mitm_server.request_failure_handler = handle_tls_upstream_failure

            tls_mitm_server.on_device_seen = handle_device_seen

            tls_mitm_server.should_relay_tls = should_relay_tls

            tls_mitm_server.on_handshake_refused = mark_device_untrusted

            tls_mitm_server.resolve_upstream = resolve_upstream

            tls_mitm_server.on_relayed = record_relayed_connection

            await tls_mitm_server.start()

            logger.info("TLS MITM server listening on ports %s", config.tls_decrypt.listen_ports)

        except Exception:
            logger.exception("Failed to start TLS MITM server, TLS interception disabled")

    else:
        logger.info("TLS decryption is disabled (enable in config.yaml)")

    # Start the DNS interception server (configured names -> this proxy)

    if config.dns.enabled:
        dns_interceptor = get_dns_interceptor(config)

        if dns_interceptor:
            await dns_interceptor.start()

    else:
        logger.info("DNS interception is disabled (set dns.enabled in config.yaml)")

    # DNS discovery: record what every client asks the DNS for, and what the

    # proxy did with it. This is what turns "which domains should we intercept?"

    # from a guess into a list read off real traffic -- and it is the only place

    # the proxy sees a device that it is *not* intercepting yet. Counters are

    # aggregated in memory and written in batches, never per query.

    dns_recorder = DNSDiscoveryRecorder(
        db_manager,
        max_rows=getattr(db_manager, "DNS_QUERY_LOG_MAX_ROWS", 5000),
    )

    replaced_recorder = set_dns_recorder(dns_recorder)

    if replaced_recorder is not None:
        await replaced_recorder.stop()

    await dns_recorder.start()

    dns_interceptor = get_dns_interceptor()

    if dns_interceptor is not None:
        dns_interceptor.query_recorder = dns_recorder

    logger.info("DNS discovery logging is on (batched every %.0fs)", dns_recorder.flush_interval_s)

    # A hand-edited config.yaml must mean what the panel means. Without these two

    # callbacks the interception policy and the upstream resolvers in the file only

    # took effect on the next restart, while the API applied them live.

    config_manager.register_callback(_apply_dns_settings)

    config_manager.register_callback(_apply_config)

    # Start config hot-reload

    config_manager.start_watching()

    # Start protocol servers if configured

    protocol_servers_cfg = getattr(config, "protocol_servers", None)

    if protocol_servers_cfg:
        try:
            proto_mgr = get_protocol_server_manager(protocol_servers_cfg)

            # Register EVERY plugin regardless of `enabled` so the UI can always

            # see and control each protocol server (a server toggled on later is

            # already registered and merely stopped). Constructors are inert —

            # nothing binds a socket until start_all()/start_plugin() is called.

            # Handlers are forwarded to the orchestrator pipeline so intercepted

            # traffic is actually learned.

            proto_mgr.register_plugin(
                MQTTServerPlugin(protocol_servers_cfg.mqtt, handler=handle_protocol_request)
            )
            proto_mgr.register_plugin(
                CoAPServerPlugin(protocol_servers_cfg.coap, handler=handle_protocol_request)
            )
            proto_mgr.register_plugin(
                ModbusServerPlugin(protocol_servers_cfg.modbus, handler=handle_protocol_request)
            )
            proto_mgr.register_plugin(
                WebSocketServerPlugin(
                    protocol_servers_cfg.websocket, handler=handle_protocol_request
                )
            )
            proto_mgr.register_plugin(
                RawTCPServerPlugin(protocol_servers_cfg.raw_tcp, handler=handle_protocol_request)
            )
            proto_mgr.register_plugin(
                HTTP2ServerPlugin(protocol_servers_cfg.http2, handler=handle_protocol_request)
            )
            proto_mgr.register_plugin(
                ZigbeeBridgePlugin(
                    protocol_servers_cfg.zigbee_bridge, handler=handle_protocol_request
                )
            )
            proto_mgr.register_plugin(
                ZWaveBridgePlugin(
                    protocol_servers_cfg.zwave_bridge, handler=handle_protocol_request
                )
            )
            proto_mgr.register_plugin(
                MatterBridgePlugin(
                    protocol_servers_cfg.matter_bridge, handler=handle_protocol_request
                )
            )

            # Auto-start only the enabled servers (bridges included, since they

            # are registered above and start when their config says enabled).

            results = await proto_mgr.start_all()

            for name, status in results.items():
                logger.info("Protocol server %s: %s", name, status)

            # Remember the running event loop so a config hot-reload (fired

            # from the config watch thread) can schedule start/stop onto it.

            _protocol_event_loop = asyncio.get_running_loop()  # noqa: PLW0603

            # Reconcile protocol servers whenever config.yaml changes (e.g. the

            # UI toggles a server on/off). The callback runs on the config

            # watch thread, so async start/stop is scheduled onto the loop.

            def reconcile_protocol_servers(cfg: object) -> None:  # noqa: ANN001
                """Apply the reloaded protocol-server config to the manager."""

                try:
                    loop = _protocol_event_loop

                    new_pcfg = getattr(cfg, "protocol_servers", None)

                    if loop is None or new_pcfg is None or loop.is_closed():
                        return

                    coro = proto_mgr.reconcile_config(new_pcfg)

                    asyncio.run_coroutine_threadsafe(coro, loop)

                except Exception:  # noqa: BLE001
                    logger.exception("Failed to reconcile protocol servers")

            config_manager.register_callback(reconcile_protocol_servers)

        except Exception as e:
            logger.error("Failed to initialize protocol servers: %s", e)  # noqa: TRY400

    # Start auto-switch scheduler

    auto_switch_scheduler = AutoSwitchScheduler(db_manager)

    await auto_switch_scheduler.start()

    logger.info("Auto-switch scheduler started")

    # Start the connection watchdog: detects when the process can no longer

    # reach any outbound HTTP endpoint (a wedged async HTTP stack seen on

    # long-running installs) and restarts the process so the box heals itself.

    global connection_watchdog  # noqa: PLW0603

    watchdog_cfg = config.connection_watchdog

    if watchdog_cfg.enabled:
        try:
            # The cooldown must outlive the restart it triggers.

            connection_watchdog = ConnectionWatchdog(
                watchdog_cfg,
                state_path=Path(config.core.device_db_dir).parent / "watchdog-state.json",
            )

            await connection_watchdog.start()

            logger.info(
                "Connection watchdog enabled: probe %s every %ds, restart after %d failures",
                watchdog_cfg.probe_url,
                watchdog_cfg.interval,
                watchdog_cfg.max_failures,
            )

        except Exception:  # noqa: BLE001
            logger.exception("Failed to start connection watchdog, disabled")

            connection_watchdog = None

    else:
        logger.info("Connection watchdog disabled (config)")

    # Start Prometheus metrics endpoint if enabled (documented in api.md:

    # metrics live on their own port 9090, separate from the proxy's 8911,

    # and expose ride_api_* plus python_* runtime metrics).

    metrics_cfg = config.observability.metrics

    if metrics_cfg.enabled:
        try:

            async def _refresh_gauges() -> None:

                if db_manager is None:
                    return

                try:
                    devices = await db_manager.list_devices()

                except Exception:  # noqa: BLE001
                    logger.exception("Metrics gauge refresh failed")

                    return

                metrics_registry.device_count.set(len(devices))

                metrics_registry.buffer_size_bytes.set(
                    sum(d.get("context_buffer_size", 0) for d in devices)
                )

            async def _scrape_worker() -> None:

                while True:
                    await _refresh_gauges()

                    await asyncio.sleep(_METRICS_SCRAPE_INTERVAL)

            global metrics_server, _metrics_scrape_task  # noqa: PLW0603

            metrics_server = MetricsServer(metrics_cfg.port, metrics_cfg.path)

            metrics_server.start()

            # Refresh gauges periodically so Prometheus scrapes reflect live state.

            _metrics_scrape_task = asyncio.create_task(_scrape_worker())

            logger.info(
                "Prometheus metrics enabled at 0.0.0.0:%s%s",
                metrics_cfg.port,
                metrics_cfg.path,
            )

        except Exception:
            logger.exception("Failed to start metrics server, disabling metrics")

            metrics_server = None

    # Start OpenTelemetry tracing if enabled. The provider is wired so spans

    # emitted by the proxy path are exported via the configured exporter

    # (console default; otlp requires opentelemetry-exporter-otlp).

    tracing_cfg = config.observability.tracing

    if tracing_cfg.enabled:
        try:
            _otel_tracer = build_tracer(
                exporter_name=tracing_cfg.exporter,
                otlp_endpoint=tracing_cfg.otlp_endpoint,
                sample_rate=tracing_cfg.sample_rate,
            )

            logger.info(
                "OpenTelemetry tracing enabled (exporter=%s, sample_rate=%s)",
                tracing_cfg.exporter,
                tracing_cfg.sample_rate,
            )

        except Exception:
            logger.exception("Failed to initialize tracing, disabled")

    logger.info(f"Server started on {config.proxy.host}:{config.proxy.port}")

    logger.info(f"Registered adapters: {adapter_registry.list_vendors()}")

    yield

    # Cleanup

    logger.info("Shutting down...")

    config_manager.stop_watching()

    # Stop auto-switch scheduler

    if auto_switch_scheduler:
        try:
            await auto_switch_scheduler.stop()

            logger.info("Auto-switch scheduler stopped")

        except Exception as e:
            logger.error("Error stopping auto-switch scheduler: %s", e)  # noqa: TRY400

    # Stop the connection watchdog probe loop.

    if connection_watchdog is not None:
        try:
            await connection_watchdog.stop()

            logger.info("Connection watchdog stopped")

        except Exception as e:
            logger.error("Error stopping connection watchdog: %s", e)  # noqa: TRY400

    # Stop the Prometheus metrics server and its gauge-refresh worker.

    if metrics_server:
        try:
            metrics_server.stop()

            logger.info("Metrics server stopped")

        except Exception as e:
            logger.error("Error stopping metrics server: %s", e)  # noqa: TRY400

    if _metrics_scrape_task is not None:
        _metrics_scrape_task.cancel()

        _metrics_scrape_task = None

        # Stop protocol servers

    try:
        proto_mgr = get_protocol_server_manager()

        await proto_mgr.stop_all()

        logger.info("Protocol servers stopped")

    except Exception as e:
        logger.error("Error stopping protocol servers: %s", e)  # noqa: TRY400

    # Stop TLS MITM server

    if tls_mitm_server:
        try:
            await tls_mitm_server.stop()

            logger.info("TLS MITM server stopped")

        except Exception as e:
            logger.error("Error stopping TLS MITM server: %s", e)  # noqa: TRY400

    # Stop the DNS interception server

    dns_interceptor = get_dns_interceptor()

    if dns_interceptor:
        dns_interceptor.query_recorder = None

        try:
            await dns_interceptor.stop()

        except Exception as e:
            logger.error("Error stopping DNS interception server: %s", e)  # noqa: TRY400

    # Write the DNS discovery counters still in memory before the DB goes away.

    dns_recorder = get_dns_recorder()

    if dns_recorder is not None:
        try:
            await dns_recorder.stop()

            set_dns_recorder(None)

        except Exception as e:
            logger.error("Error stopping DNS discovery recorder: %s", e)  # noqa: TRY400

    # Prune stale correlation rows so the training store never grows unbounded.

    # Runs regardless of TLS interception so pruning cannot be skipped.

    if orchestrator:
        # A background flush can sit in an LLM call for minutes: stop it before

        # the LLM client and the database it writes to are closed below.

        try:
            await orchestrator.cancel_background_flushes()

        except Exception as e:
            logger.error("Error cancelling background flushes: %s", e)  # noqa: TRY400

        try:
            await orchestrator.prune_stores()

        except Exception as e:
            logger.error("Error pruning correlation stores: %s", e)  # noqa: TRY400

    if llm_decipher_service:
        await llm_decipher_service.close()

    if db_manager:
        await db_manager.close()

        await dispose_memory_db()

        logger.info("Shutdown complete")


app = FastAPI(
    title="Local Cloud Replacement Proxy",
    description="DNS Interception Proxy that learns device protocols and serves responses locally",
    version="0.2.0",
    lifespan=lifespan,
)

# Restrict CORS to trusted local web UI origins to prevent cross-site request attacks.

_TRUSTED_ORIGINS = ["http://localhost:8911", "http://127.0.0.1:8911"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_TRUSTED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# CORS only governs what a page may read back; a cross-site "simple" POST is

# still delivered. Refuse state-changing API calls a browser makes for another

# site (S-17) -- with auth disabled they were the only barrier.

app.add_middleware(CrossSiteRequestGuard, allowed_origins=_TRUSTED_ORIGINS)

# Control-plane authentication: every /api/* route requires the single

# control-plane password (full access, no read-only distinction). See core/security.py.

app.add_middleware(
    ControlPlaneAuthMiddleware,
    get_security_config=lambda: config_manager.config.security,
)

# Body limits (F-09): /api/* bodies are held to proxy.max_request_size, device

# (data-plane) bodies to the larger proxy.max_device_request_size -- the proxy

# route reads them whole into memory, so they cannot be unbounded either.

app.add_middleware(
    MaxBodySizeMiddleware,
    max_size=config_manager.config.proxy.max_request_size,
    data_plane_max_size=config_manager.config.proxy.max_device_request_size,
)

# Security response headers (defense-in-depth against clickjacking, MIME sniffing, XSS)

app.add_middleware(SecurityHeadersMiddleware)


# FastAPI does not map JSONDecodeError from unguarded

# ``await request.json()`` calls to a client error. Without this handler any

# API route that reads a malformed JSON body would surface an unhandled 500.

# Register a dedicated handler so a malformed body yields a clean 400.


@app.exception_handler(json.JSONDecodeError)
async def _json_decode_error_handler(request: Request, exc: json.JSONDecodeError) -> JSONResponse:
    """Return a client-friendly 400 for malformed JSON payloads."""

    del request, exc

    return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})


# ── TLS API Routes ───────────────────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════════════════════════

# Modular Routers & Re-exports

# ═══════════════════════════════════════════════════════════════════════════════


from core.routes import (
    devices_router,
    dns_router,
    llm_router,
    patterns_router,
    protocols_router,
    system_router,
    tls_router,
    webui_router,
)

from core.routes.common import _client_error

from core.routes.devices import (
    _requested_database_url,
    assign_device_database,
    configure_device_llm,
    delete_buffer_entry,
    delete_device,
    export_buffer,
    get_device,
    get_device_auto_switch,
    get_device_by_ip,
    get_device_connection,
    get_device_context,
    get_device_database,
    get_device_stats,
    get_ip_bypass,
    get_match_rate,
    import_buffer,
    list_buffer_entries,
    list_databases,
    list_devices,
    register_device_ip,
    rename_device_database,
    set_device_auto_switch,
    set_device_context_buffer,
    set_device_mode,
    set_device_threshold,
    set_ip_bypass,
    update_device_connection,
    update_device_context,
)

from core.routes.dns import (
    _apply_dns_settings,
    _clean_dns_hostname,
    _dns_policy_from_body,
    dns_add_intercept,
    dns_clear_discovery,
    dns_discovery,
    dns_get_settings,
    dns_put_settings,
    dns_remove_intercept,
)

from core.routes.llm import (
    create_llm_profile,
    create_user_profile,
    delete_llm_profile,
    delete_user_profile,
    flush_buffer_to_llm,
    get_llm_settings,
    get_user_profile,
    list_llm_profiles,
    list_user_profiles,
    preview_llm_analysis,
    test_llm_profile,
    update_llm_settings,
    update_user_profile,
)

from core.routes.patterns import (
    _resync_device_patterns,
    delete_pattern,
    export_patterns,
    get_device_patterns,
    get_pattern_detail,
    import_patterns,
    patch_pattern,
    put_pattern,
)

from core.routes.protocols import (
    protocol_server_config,
    protocol_server_start,
    protocol_server_stop,
    protocol_servers_status,
)

from core.routes.system import (
    _MIN_PASSWORD_LENGTH,
    _listening_ports,
    _new_password_problem,
    _validation_details,
    get_buffer_backend_setting,
    get_config,
    health_check,
    put_config,
    set_buffer_backend_setting,
    set_control_plane_password,
    system_update,
)

from core.routes.tls import (
    _move_device_db_to_name,
    tls_delete_cert,
    tls_device_ports,
    tls_download_ca,
    tls_download_root_ca,
    tls_frida_script,
    tls_get_cert_info,
    tls_list_certs,
    tls_list_ports,
    tls_relayed,
    tls_rotate_cert,
    tls_stats,
    tls_unidentified,
    tls_update_device_config,
    tls_upload_cert,
    tls_upload_cert_json,
)

from core.routes.webui import (
    _serve_webui,
    config_page,
    dashboard,
    options_page,
    patterns_index_page,
    patterns_page,
)


def _register_router(target_app: FastAPI, router: APIRouter) -> None:

    target_app.include_router(router)

    for r in router.routes:
        if r not in target_app.routes:
            target_app.routes.append(r)


# Register all modular routers

_register_router(app, tls_router)

_register_router(app, dns_router)

_register_router(app, protocols_router)

_register_router(app, devices_router)

_register_router(app, patterns_router)

_register_router(app, llm_router)

_register_router(app, system_router)

_register_router(app, webui_router)


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


# Statuses that must not carry a body.

_BODYLESS_STATUSES = frozenset({204, 304})


def _local_http_response(response: dict) -> Response:
    """Serve a locally built ``{status_code, headers, body}`` response.



    Learned template headers used to be passed through as-is: the vendor's

    stale Content-Length won over the re-rendered body.

    """

    status = servable_status(response.get("status_code"))

    headers = without_framing_headers(response.get("headers"))

    if status in _BODYLESS_STATUSES:
        return Response(status_code=status, headers=headers)

    return JSONResponse(status_code=status, content=response.get("body", {}), headers=headers)


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


def _raw_target(request: Request) -> str | None:
    """``/{path}?{query}`` exactly as the client sent it, minus the vendor prefix.



    The route's ``path`` is decoded ("%3F" becomes "?", "%20" a space) and

    ``request.query_params`` folds repeated keys, so neither can rebuild what

    the device asked for.

    """

    raw_path = request.scope.get("raw_path")

    if not raw_path:
        return None

    parts = raw_path.decode("latin-1").split("/", 2)

    target = "/" + (parts[2] if len(parts) > 2 else "")  # noqa: PLR2004 - "", vendor, rest

    query = request.scope.get("query_string") or b""

    return f"{target}?{query.decode('latin-1')}" if query else target


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
            raw_target=_raw_target(request),
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
async def proxy_vendor_request(vendor: str, path: str, request: Request):  # noqa: C901, PLR0911, PLR0912, PLR0915
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

        if decision.value == "passthrough":
            # The client addressed *this* gateway, so a passthrough decision

            # must actually forward the request upstream. Replying 200 with a

            # note instead fabricated a success the device never received.

            # (Honoured for every client: an intercepted request is forwarded

            # the same way, only after the pipeline has seen it.)

            logger.info(f"Passthrough for traffic from {client_ip} to {vendor}: forwarding")

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
        raw_target=_raw_target(request),
    )

    # Bound before the try so the error path can drop a capture registered by

    # handle_request: an orphan would otherwise be paired with a later response.

    device_id = ""

    result: dict | None = None

    try:
        # Parse request to extract device info

        intercepted = await adapter.parse_request(intercepted)

        # A source IP bound to a known device decides who is talking, as on the

        # TLS and protocol paths: the claimed id (a header or a path segment)

        # is the client's word, and any client could speak for any device.

        device_id = (
            await _resolve_device_for_ip(db_manager, client_ip)
            or intercepted.device_id
            or _extract_device_id(request.headers, path)
        )

        intercepted.device_id = device_id

        # Ensure device is registered

        await db_manager.get_or_create_device(device_id, vendor)

        # Apply request modification rules (on-the-fly modification engine),

        # mutating the intercepted request in place so the pipeline sees the

        # transformed request while the local-match is served.

        get_modification_engine().process_request(intercepted)

        if intercepted.path != f"/{path}":
            # The adapter or a REDIRECT rule chose another path: forward that.

            intercepted.raw_target = None

        if getattr(intercepted, "blocked", False):
            # A block rule: the request goes neither upstream nor to the

            # pipeline (BLOCK used to be recorded and then ignored).

            logger.info("Request from %s blocked: %s", device_id, intercepted.block_reason)

            return JSONResponse(
                status_code=BLOCKED_STATUS,
                content={"error": "blocked", "reason": intercepted.block_reason},
            )

        await _modification_delay(
            (getattr(intercepted, "metadata", None) or {}).get("artificial_delay_ms")
        )

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

            await _modification_delay(out_response.pop("delay_ms", None))

            return _local_http_response(out_response)

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
                # nginx performs this forward and the response never comes back

                # here, so the captured request can never be paired: drop it now

                # (not an error — the handoff itself succeeded).

                await _discard_pending_capture(device_id, result, count_as_error=False)

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
                    correlation_key=result.get("correlation_key"),
                )

                return _upstream_response(cloud_response, resp_body)

            await _discard_pending_capture(device_id, result)

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

        await _discard_pending_capture(device_id, result)

        return JSONResponse(
            status_code=500,
            content={"error": "An internal error occurred"},
        )


# ═══════════════════════════════════════════════════════════════════════════════

# HELPERS

# ═══════════════════════════════════════════════════════════════════════════════


async def _discard_pending_capture(
    device_id: str, result: dict | None, *, count_as_error: bool = True
) -> None:
    """Drop the request captured for ``result`` if its response will never be paired.



    Best effort: a cleanup failure is logged and never changes the reply.

    """

    key = (result or {}).get("correlation_key")

    if not key or not device_id or not orchestrator:
        return

    try:
        await orchestrator.discard_request(device_id, key, count_as_error=count_as_error)

    except Exception:  # noqa: BLE001 - cleanup must not mask the real reply
        logger.warning("Could not discard pending capture %s", key, exc_info=True)


# A DELAY rule may not hold a device connection longer than this.

_MAX_MODIFICATION_DELAY_S = 30.0


async def _modification_delay(delay_ms: object) -> None:
    """Honour a DELAY modification rule (it used to be recorded and ignored)."""

    try:
        seconds = float(delay_ms) / 1000

    except (TypeError, ValueError):
        return

    if seconds > 0:
        await asyncio.sleep(min(seconds, _MAX_MODIFICATION_DELAY_S))


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


# Longest device id taken from a request: a claimed id becomes a registry row

# and a database file name.

_MAX_CLAIMED_DEVICE_ID = 128


def _extract_device_id(headers: dict, path: str) -> str:
    """Try to extract device ID from headers or path (never empty)."""

    # Check common headers in priority order

    for header in _DEVICE_ID_HEADERS:
        value = (headers.get(header) or "").strip()

        if 0 < len(value) <= _MAX_CLAIMED_DEVICE_ID:
            return value

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


_UVICORN_LOG_LEVELS = frozenset({"critical", "error", "warning", "info", "debug", "trace"})


def main():
    """Run the server."""

    config = config_manager.config

    lg = config.observability.logging

    setup_logging(level=lg.level, fmt=lg.format, output=lg.output)

    # uvicorn needs one of its own level names; use the level setup_logging

    # actually applied (case-normalised, INFO for an unknown value).

    uvicorn_level = get_log_level().lower()

    if uvicorn_level not in _UVICORN_LOG_LEVELS:
        uvicorn_level = "info"

    # Pass the app object (not the "core.server:app" string): the string

    # import path breaks in PyInstaller bundles where the module is packed and

    # no longer importable by name. Works in both venv and frozen builds.

    uvicorn.run(
        app,
        host=config.proxy.host,
        port=config.proxy.port,
        log_level=uvicorn_level,
        reload=False,
    )


if __name__ == "__main__":
    main()

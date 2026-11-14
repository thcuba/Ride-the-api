"""

Local Cloud Replacement Proxy - Main entry point.

Intercepts device traffic, learns protocol via LLM, serves responses locally.

"""

from __future__ import annotations

import asyncio  # noqa: TC003
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from adapters import get_registered_registry
from adapters.base import (
    InterceptedRequest,
    ProtocolAdapterRegistry,
    ProtocolType,
)
from core.buffer import (
    dispose_memory_db,
    get_buffer_backend,
    initialize_buffer_backend,
)
from core.cert_manager import CertManager, get_cert_manager
from core.config import (
    get_config_manager,
)
from core.connection_watchdog import ConnectionWatchdog
from core.database import (
    DatabaseManager,
    init_db_manager,
)
from core.dns_discovery import DNSDiscoveryRecorder, get_dns_recorder, set_dns_recorder
from core.dns_server import get_dns_interceptor
from core.llm_decipher import LLMDecipherService, get_llm_decipher
from core.logging_config import get_log_level, setup_logging
from core.modification import get_modification_engine
from core.observability import (
    MetricsServer,
    build_tracer,
    get_tracer,
)
from core.observability import (
    registry as metrics_registry,
)
from core.paths import resource_path
from core.pipeline import (
    LearningOrchestrator,
    get_orchestrator,
)
from core.protocol_servers import (
    discover_server_plugin_entry_points,
    get_protocol_server_manager,
    register_standard_plugins,
)
from core.proxy import (
    _BODYLESS_STATUSES,
    _DEVICE_ID_HEADERS,
    _ENRICHMENT_FIELDS,
    _LAST_SEEN_WRITE_INTERVAL_S,
    _MAX_CLAIMED_DEVICE_ID,
    _MAX_MODIFICATION_DELAY_S,
    _RESPONSE_HOP_BY_HOP,
    _apply_response_modifications,
    _discard_pending_capture,
    _extract_device_id,
    _forward_bypassed_request,
    _get_request_body,
    _is_local_ip,
    _last_seen_is_stale,
    _local_http_response,
    _modification_delay,
    _raw_target,
    _resolve_device_for_ip,
    _select_handler_adapter,
    _upstream_response,
    handle_device_seen,
    handle_protocol_request,
    handle_tls_decrypted_request,
    handle_tls_upstream_failure,
    handle_tls_upstream_response,
    mark_device_untrusted,
    proxy_router,
    proxy_vendor_request,
    record_relayed_connection,
    resolve_upstream,
    should_relay_tls,
)
from core.resilience import (
    AutoSwitchScheduler,
    register_resilience_routes,
)
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
from core.routes.dns import _apply_dns_settings
from core.security import (
    ControlPlaneAuthMiddleware,
    CrossSiteRequestGuard,
    MaxBodySizeMiddleware,
    SecurityHeadersMiddleware,
    setup_token,
)
from core.tls_mitm import (
    TLSMITMServer,
    get_tls_mitm_server,
)
from core.traffic_selector import get_traffic_selector
from core.upstream_resolver import _apply_config, self_connection_reason

if TYPE_CHECKING:
    from types import ModuleType

__all__ = [
    # Re-exported from core.proxy for backward compatibility
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
    "self_connection_reason",
    "should_relay_tls",
    # Re-exported types and getters for backward compatibility
    "InterceptedRequest",
    "ProtocolType",
    "get_modification_engine",
    "get_tracer",
    "get_traffic_selector",
    # Application & lifecycle symbols
    "APP_VERSION_FALLBACK",
    "_UPDATER_STRIP_ENV",
    "adapter_registry",
    "app",
    "auto_switch_scheduler",
    "config_manager",
    "connection_watchdog",
    "db_manager",
    "llm_decipher_service",
    "main",
    "metrics_server",
    "orchestrator",
    "runtime_version",
    "tls_mitm_server",
    "updater_env",
]

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

            register_standard_plugins(
                proto_mgr, protocol_servers_cfg, handler=handle_protocol_request
            )
            discover_server_plugin_entry_points(
                proto_mgr, protocol_servers_cfg, handler=handle_protocol_request
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

# Modular Routers

# ═══════════════════════════════════════════════════════════════════════════════


# Register all modular routers

app.include_router(tls_router)

app.include_router(dns_router)

app.include_router(protocols_router)

app.include_router(devices_router)

app.include_router(patterns_router)

app.include_router(llm_router)

app.include_router(system_router)

app.include_router(webui_router)

# The resilience endpoints (/api/independence/...) register themselves straight onto
# the app instead of living in a router, so they need an explicit call. Without it the
# paths fall through to the proxy catch-all and answer 503 "Service not ready".
register_resilience_routes(app, lambda: db_manager, lambda: orchestrator)


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


# Include catch-all reverse proxy router for device vendor traffic
app.include_router(proxy_router)


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


_CANONICAL_MODULE = "core.server"


def publish_entrypoint_module(executing: ModuleType | None = None) -> None:
    """Publish the ``python -m core.server`` instance under its canonical name.

    ``python -m`` executes this file as ``__main__``, so ``sys.modules`` holds no
    ``core.server`` entry. The route modules in ``core/routes/`` reach the live
    runtime with a lazy ``from core import server`` (deferred to break the import
    cycle); without the entry the import machinery executes this file a SECOND
    time and hands the routers that fresh copy, whose globals are all ``None``.
    The visible symptom is a service that logs a clean startup and then answers
    ``503 {"error": "Service not ready"}`` on everything routed that way
    (``/api/devices``, ``/api/llm/*``, ``/api/tls/certs``), i.e. the dashboard
    showing no devices and no LLM profiles.

    Alias the executing module so the process keeps exactly one runtime state.
    This is a no-op when the module was imported normally (tests, PyInstaller
    bundle, ``from core.server import main``).
    """
    module = executing if executing is not None else sys.modules.get(__name__)
    if module is None or sys.modules.get(_CANONICAL_MODULE) is not None:
        return
    sys.modules[_CANONICAL_MODULE] = module
    package = sys.modules.get("core")
    if package is not None and getattr(package, "server", None) is None:
        package.server = module


if __name__ == "__main__":
    publish_entrypoint_module()
    main()

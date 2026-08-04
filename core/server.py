"""
Local Cloud Replacement Proxy - Main entry point.
Intercepts device traffic, learns protocol via LLM, serves responses locally.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import signal
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

# Path to webui directory (relative to project root)
WEBUI_DIR = Path(__file__).resolve().parent.parent / "webui"
DASHBOARD_HTML = WEBUI_DIR / "dashboard.html"
PATTERNS_HTML = WEBUI_DIR / "patterns.html"

import uvicorn

from fastapi import FastAPI, Request, Response, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from core.config import get_config_manager
from core.database import DatabaseManager, Base, init_db_manager, get_db_manager, DeviceRegistry
from core.llm_decipher import get_llm_decipher, LLMDecipherService
from core.pipeline import (
    LearningOrchestrator, get_orchestrator, ContextBuffer,
    PatternMatcher, MatchRateTracker, PipelineMode,
)
from core.resilience import CloudIndependenceVerifier, register_resilience_routes, AutoSwitchScheduler
from core.traffic_selector import get_traffic_selector, TrafficSelector, TrafficRequestInfo
from core.cert_manager import get_cert_manager, CertManager
from core.tls_mitm import (
    get_tls_mitm_server, TLSMITMServer,
    DecryptedRequest,
)
from adapters import get_registered_registry
from adapters.base import (
    ProtocolAdapterRegistry, InterceptedRequest, ProtocolType,
    Command, CommandType,
)
from sqlalchemy import select

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
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

# Protocol server instances (lazy-loaded on first access)
protocol_servers: dict[str, object] = {}
protocol_server_tasks: dict[str, asyncio.Task] = {}


# ── TLS Decrypted Request Handler ────────────────────────────────────────────


async def handle_tls_decrypted_request(req: DecryptedRequest) -> dict | None:
    """Handle a decrypted TLS request — find/create device and run through pipeline.

    Called by TLSMITMServer for every successfully decrypted HTTP request.
    If the source IP is unknown, a new device record + SQLite DB is auto-created
    with passthrough=ON (traffic forwarded to cloud while user configures it).
    """
    global db_manager, orchestrator, adapter_registry

    if not db_manager or not orchestrator:
        logger.warning("TLS handler: service not ready, dropping request from %s", req.client_ip)
        return None

    config = config_manager.config
    device_id = f"ip-{req.client_ip.replace('.', '-')}"

    try:
        # Create or find device by IP
        device = await db_manager.get_or_create_device(
            device_id=device_id,
            vendor="unknown",
        )

        # Ensure a dedicated device database exists
        device_db_dir = Path(config.core.device_db_dir)
        device_db_path = device_db_dir / f"{device_id}.db"
        if not device_db_path.exists():
            try:
                device_db_path.touch()
                logger.info("TLS handler: created device DB for %s at %s", device_id, device_db_path)
            except Exception as e:
                logger.warning("TLS handler: could not create device DB for %s: %s", device_id, e)

        # Log the intercepted request
        logger.info(
            "TLS: %s %s %s (device=%s, sni=%s, port=%d)",
            req.method, req.path, req.http_version,
            device_id, req.sni, req.dst_port,
        )

        # Determine vendor/adapter for this device
        device_vendor = getattr(device, "vendor", "unknown") or "unknown"

        # Find matching adapter if available
        handler_adapter = None
        if adapter_registry and device_vendor in adapter_registry._adapters:
            handler_adapter = adapter_registry._adapters[device_vendor]

        # Build intercepted request for pipeline
        from adapters.base import InterceptedRequest as AdapterInterceptedRequest, ProtocolType

        intercepted = AdapterInterceptedRequest(
            device_id=device_id,
            timestamp=datetime.now(timezone.utc),
            protocol=ProtocolType.HTTPS,
            method=req.method,
            path=req.path,
            headers=req.headers,
            query_params={},
            body=req.body,
        )

        if handler_adapter:
            intercepted = await handler_adapter.parse_request(intercepted)

        # Run through the orchestrator pipeline
        result = await orchestrator.handle_request(
            device_id=device_id,
            vendor=device_vendor,
            protocol="https",
            method=req.method,
            path=req.path,
            headers=req.headers,
            body=req.body,
            query_params={},
        )

        if result["action"] == "local_response":
            logger.debug("TLS: local response for %s %s", req.method, req.path)
        else:
            logger.debug("TLS: passthrough for %s %s", req.method, req.path)

        return result

    except Exception as e:
        logger.error("TLS handler: error processing %s: %s", req.client_ip, e, exc_info=True)
        return None


# ── Application Lifespan ───────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global db_manager, adapter_registry, orchestrator, llm_decipher_service, tls_mitm_server, cert_manager, auto_switch_scheduler

    logger.info("Starting Local Cloud Replacement Proxy...")

    config = config_manager.config

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
        except Exception as e:
            logger.error("Failed to initialize TLS cert manager: %s", e)

    # Start TLS MITM server if enabled
    if config.tls_decrypt.enabled and cert_manager:
        try:
            tls_mitm_server = get_tls_mitm_server()
            await tls_mitm_server.start(
                cert_manager=cert_manager,
                ports=config.tls_decrypt.listen_ports,
                request_handler=handle_tls_decrypted_request,
            )
            logger.info("TLS MITM server listening on ports %s", config.tls_decrypt.listen_ports)
        except Exception as e:
            logger.error("Failed to start TLS MITM server: %s, TLS interception disabled", e)
    else:
        logger.info("TLS decryption is disabled (enable in config.yaml)")

    # Start config hot-reload
    config_manager.start_watching()

    # Start protocol servers if configured
    # Start protocol servers if configured
    protocol_servers_cfg = getattr(config, "protocol_servers", None)
    if protocol_servers_cfg:
        try:
            from core.protocol_servers import get_protocol_server_manager
            proto_mgr = get_protocol_server_manager(protocol_servers_cfg)
            from core.protocol_servers.mqtt_server import MQTTServerPlugin
            from core.protocol_servers.coap_server import CoAPServerPlugin
            from core.protocol_servers.modbus_server import ModbusServerPlugin
            from core.protocol_servers.websocket_server import WebSocketServerPlugin
            from core.protocol_servers.raw_tcp_server import RawTCPServerPlugin
            from core.protocol_servers.http2_server import HTTP2ServerPlugin

            # Register plugins based on config
            if getattr(protocol_servers_cfg.mqtt, "enabled", False):
                proto_mgr.register_plugin(MQTTServerPlugin(protocol_servers_cfg.mqtt))
            if getattr(protocol_servers_cfg.coap, "enabled", False):
                proto_mgr.register_plugin(CoAPServerPlugin(protocol_servers_cfg.coap))
            if getattr(protocol_servers_cfg.modbus, "enabled", False):
                proto_mgr.register_plugin(ModbusServerPlugin(protocol_servers_cfg.modbus))
            if getattr(protocol_servers_cfg.websocket, "enabled", False):
                proto_mgr.register_plugin(WebSocketServerPlugin(protocol_servers_cfg.websocket))
            if getattr(protocol_servers_cfg.raw_tcp, "enabled", False):
                proto_mgr.register_plugin(RawTCPServerPlugin(protocol_servers_cfg.raw_tcp))
            if getattr(protocol_servers_cfg.http2, "enabled", False):
                proto_mgr.register_plugin(HTTP2ServerPlugin(protocol_servers_cfg.http2))

            # Auto-start enabled servers
            results = await proto_mgr.start_all()
            for name, status in results.items():
                logger.info("Protocol server %s: %s", name, status)

            # Register bridges if enabled
            if getattr(protocol_servers_cfg.zigbee_bridge, "enabled", False):
                from core.protocol_servers.zigbee_bridge import ZigbeeBridgePlugin
                proto_mgr.register_plugin(ZigbeeBridgePlugin(protocol_servers_cfg.zigbee_bridge))
                await proto_mgr.start_plugin("zigbee_bridge")

            if getattr(protocol_servers_cfg.zwave_bridge, "enabled", False):
                from core.protocol_servers.zwave_bridge import ZWaveBridgePlugin
                proto_mgr.register_plugin(ZWaveBridgePlugin(protocol_servers_cfg.zwave_bridge))
                await proto_mgr.start_plugin("zwave_bridge")

            if getattr(protocol_servers_cfg.matter_bridge, "enabled", False):
                from core.protocol_servers.matter_bridge import MatterBridgePlugin
                proto_mgr.register_plugin(MatterBridgePlugin(protocol_servers_cfg.matter_bridge))
                await proto_mgr.start_plugin("matter_bridge")
        except Exception as e:
            logger.error("Failed to initialize protocol servers: %s", e)

    # Mount static files for Web UI
    try:
        static_dir = Path(__file__).resolve().parent.parent / "webui" / "static"
        if static_dir.exists():
            app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    except Exception as e:
        logger.warning("Could not mount static files: %s", e)

    # Start auto-switch scheduler
    auto_switch_scheduler = AutoSwitchScheduler(db_manager)
    await auto_switch_scheduler.start()
    logger.info("Auto-switch scheduler started")

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
            logger.error("Error stopping auto-switch scheduler: %s", e)

        # Stop protocol servers
    try:
        from core.protocol_servers import get_protocol_server_manager
        proto_mgr = get_protocol_server_manager()
        await proto_mgr.stop_all()
        logger.info("Protocol servers stopped")
    except Exception as e:
        logger.error("Error stopping protocol servers: %s", e)

    # Stop TLS MITM server
    if tls_mitm_server:
        try:
            await tls_mitm_server.stop()
            logger.info("TLS MITM server stopped")
        except Exception as e:
            logger.error("Error stopping TLS MITM server: %s", e)
    if llm_decipher_service:
        await llm_decipher_service.close()
    if db_manager:
        await db_manager.close()
    logger.info("Shutdown complete")


app = FastAPI(
    title="Local Cloud Replacement Proxy",
    description="DNS Interception Proxy that learns device protocols and serves responses locally",
    version="0.2.0",
    lifespan=lifespan,
)
# CORS for local dashboard access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── TLS API Routes ───────────────────────────────────────────────────────────


@app.get("/api/tls/ca-cert")
async def tls_download_ca():
    """Download the CA certificate (PEM) for installation on devices."""
    if not cert_manager:
        return JSONResponse(status_code=503, content={"error": "Cert manager not ready"})
    try:
        ca_pem = cert_manager.ca_cert_pem()
        return Response(
            content=ca_pem,
            media_type="application/x-pem-file",
            headers={"Content-Disposition": "attachment; filename=ride-the-api-ca.pem"},
        )
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/tls/stats")
async def tls_stats():
    """Return TLS/MITM statistics."""
    stats = {"cert_manager": {}, "mitm_server": False, "device_ports": []}
    if cert_manager:
        stats["cert_manager"] = cert_manager.stats()
    if tls_mitm_server:
        stats["mitm_server"] = True
        stats["listen_ports"] = tls_mitm_server.listen_ports.copy()
        stats["device_ports"] = [
            {
                "ip": dp.ip,
                "port": dp.port,
                "device_id": dp.device_id,
                "first_seen": dp.first_seen.isoformat(),
                "last_seen": dp.last_seen.isoformat(),
            }
            for dp in tls_mitm_server.device_ports.values()
        ]
    return stats


@app.get("/api/tls/device-ports")
async def tls_device_ports():
    """Return IP-to-device mapping for all connected devices."""
    if not tls_mitm_server:
        return {"devices": []}
    devices = []
    for ip, info in tls_mitm_server.device_ports.items():
        devices.append({
            "ip": info.ip,
            "port": info.port,
            "device_id": info.device_id,
            "first_seen": info.first_seen.isoformat(),
            "last_seen": info.last_seen.isoformat(),
        })
    return {"devices": devices}


@app.get("/api/tls/unidentified")
async def tls_unidentified():
    """List device IDs starting with 'ip-' (auto-created by TLS handler)."""
    if not db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    devices = await db_manager.list_devices()
    unidentified = [d for d in devices if d.get("device_id", "").startswith("ip-")]
    return {"unidentified": unidentified}


@app.post("/api/tls/ports")
async def tls_add_port(request: Request):
    """Dynamically add a TLS listen port."""
    if not tls_mitm_server:
        return JSONResponse(status_code=503, content={"error": "TLS MITM not running"})
    try:
        body = await request.json()
        port = int(body.get("port", 0))
        if port < 1 or port > 65535:
            return JSONResponse(status_code=400, content={"error": "Invalid port number"})
        success = await tls_mitm_server.add_port(port)
        if success:
            # Persist to config
            config = config_manager.config
            if port not in config.tls_decrypt.listen_ports:
                config.tls_decrypt.listen_ports.append(port)
            return {"status": "ok", "port": port, "listen_ports": tls_mitm_server.listen_ports.copy()}
        return JSONResponse(status_code=500, content={"error": "Failed to add port"})
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.delete("/api/tls/ports/{port}")
async def tls_remove_port(port: int):
    """Dynamically remove a TLS listen port."""
    if not tls_mitm_server:
        return JSONResponse(status_code=503, content={"error": "TLS MITM not running"})
    success = await tls_mitm_server.remove_port(port)
    if success:
        # Update config
        config = config_manager.config
        if port in config.tls_decrypt.listen_ports:
            config.tls_decrypt.listen_ports.remove(port)
        return {"status": "ok", "port": port, "listen_ports": tls_mitm_server.listen_ports.copy()}
    return JSONResponse(status_code=404, content={"error": "Port not found"})


@app.put("/api/devices/{device_id}/tls-config")
async def tls_update_device_config(device_id: str, request: Request):
    """Update TLS config for a specific device (name, vendor, passthrough, pinning_bypass)."""
    if not db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    try:
        body = await request.json()
        async with db_manager.core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            if not device:
                return JSONResponse(status_code=404, content={"error": "Device not found"})

            # Update editable fields
            if "name" in body and body["name"]:
                device.name = body["name"]
            if "vendor" in body and body["vendor"]:
                device.vendor = body["vendor"]
            # passthrough and pinning_bypass stored in extra_attributes
            extra = dict(device.extra_attributes or {})
            if "passthrough" in body:
                extra["tls_passthrough"] = body["passthrough"]
            if "pinning_bypass" in body:
                extra["tls_pinning_bypass"] = body["pinning_bypass"]
            device.extra_attributes = extra

            session.add(device)
            await session.commit()
            return {"status": "ok", "device_id": device_id}

    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.get("/api/tls/ports")
async def tls_list_ports():
    """List all currently active TLS listen ports."""
    if not tls_mitm_server:
        return {"ports": [], "enabled": False}
    return {"ports": tls_mitm_server.listen_ports.copy(), "enabled": True}


@app.get("/api/tls/frida/script.js")
async def tls_frida_script():
    """Return a Frida script for bypassing certificate pinning."""
    script = r"""// Ride the API — Certificate Pinning Bypass (Frida)
// Usage: frida -U -l script.js <app>
// Requires: Frida installed on device, CA cert installed

Java.perform(function() {
    // Common SSL pinning bypass targets
    var X509TrustManager = Java.use('javax.net.ssl.X509TrustManager');
    var SSLContext = Java.use('javax.net.ssl.SSLContext');

    // Bypass checkClientTrusted / checkServerTrusted
    X509TrustManager.checkClientTrusted.implementation = function(chain, authType) {
        console.log('[RideTheAPI] Bypassing checkClientTrusted');
    };
    X509TrustManager.checkServerTrusted.implementation = function(chain, authType) {
        console.log('[RideTheAPI] Bypassing checkServerTrusted');
    };
    X509TrustManager.getAcceptedIssuers.implementation = function() {
        console.log('[RideTheAPI] Returning empty accepted issuers');
        return [];
    };

    // Hook OkHttp HostnameVerifier
    try {
        var HostnameVerifier = Java.use('javax.net.ssl.HostnameVerifier');
        HostnameVerifier.verify.implementation = function(hostname, session) {
            console.log('[RideTheAPI] Bypassing hostname verification for: ' + hostname);
            return true;
        };
    } catch(e) { console.log('[RideTheAPI] HostnameVerifier not found'); }

    // Hook OkHttp CertificatePinner
    try {
        var CertificatePinner = Java.use('okhttp3.CertificatePinner');
        CertificatePinner.check.implementation = function(pin) {
            console.log('[RideTheAPI] Bypassing certificate pin for: ' + pin);
        };
    } catch(e) { console.log("[RideTheAPI] CertificatePinner not found"); }

    console.log("[RideTheAPI] Pinning bypass injected successfully");
});
"""
    return Response(content=script, media_type="application/javascript")


    # ═══════════════════════════════════════════════════════════════════════════════
    # TLS CERTIFICATE MANAGEMENT API
    # ═══════════════════════════════════════════════════════════════════════════════


    @app.get("/api/tls/certs")
    async def tls_list_certs():
        """List all imported and auto-generated certificates."""
        if not cert_manager:
            return JSONResponse(status_code=503, content={"error": "Cert manager not ready"})
        try:
            imported = cert_manager.list_imported_certs()
            return {"certs": imported}
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})


    @app.get("/api/tls/certs/{hostname}")
    async def tls_get_cert_info(hostname: str):
        """Get detailed info about a specific certificate."""
        if not cert_manager:
            return JSONResponse(status_code=503, content={"error": "Cert manager not ready"})
        try:
            info = cert_manager.get_cert_info(hostname)
            if not info:
                return JSONResponse(status_code=404, content={"error": f"No certificate found for '{hostname}'"})
            return {"cert": info}
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})


    @app.post("/api/tls/certs/upload")
    async def tls_upload_cert(
        hostname: str = Form(...),
        cert: UploadFile = File(...),
        key: UploadFile = File(...),
    ):
        """Upload a certificate + private key (PEM format) for a specific hostname."""
        if not cert_manager:
            return JSONResponse(status_code=503, content={"error": "Cert manager not ready"})
        try:
            cert_pem = await cert.read()
            key_pem = await key.read()
            cert_manager.import_cert(hostname, cert_pem.decode("utf-8"), key_pem.decode("utf-8"))
            return {"status": "ok", "hostname": hostname, "message": f"Certificate imported for '{hostname}'"}
        except Exception as e:
            return JSONResponse(status_code=400, content={"error": str(e)})


    @app.post("/api/tls/certs/upload-json")
    async def tls_upload_cert_json(request: Request):
        """Upload a certificate + private key as JSON (base64-encoded PEM)."""
        if not cert_manager:
            return JSONResponse(status_code=503, content={"error": "Cert manager not ready"})
        try:
            body = await request.json()
            hostname = body.get("hostname")
            cert_b64 = body.get("cert_base64")
            key_b64 = body.get("key_base64")
            if not all([hostname, cert_b64, key_b64]):
                return JSONResponse(
                    status_code=400,
                    content={"error": "Missing 'hostname', 'cert_base64', or 'key_base64'"},
                )
            cert_pem = base64.b64decode(cert_b64).decode("utf-8")
            key_pem = base64.b64decode(key_b64).decode("utf-8")
            cert_manager.import_cert(hostname, cert_pem, key_pem)
            return {"status": "ok", "hostname": hostname}
        except Exception as e:
            return JSONResponse(status_code=400, content={"error": str(e)})


    @app.delete("/api/tls/certs/{hostname}")
    async def tls_delete_cert(hostname: str):
        """Delete an imported certificate, reverting to auto-generated."""
        if not cert_manager:
            return JSONResponse(status_code=503, content={"error": "Cert manager not ready"})
        try:
            cert_manager.delete_cert(hostname)
            return {"status": "ok", "hostname": hostname, "message": f"Certificate deleted for '{hostname}'"}
        except Exception as e:
            return JSONResponse(status_code=400, content={"error": str(e)})


    @app.post("/api/tls/certs/{hostname}/rotate")
    async def tls_rotate_cert(hostname: str, request: Request):
        """Replace an existing certificate without downtime."""
        if not cert_manager:
            return JSONResponse(status_code=503, content={"error": "Cert manager not ready"})
        try:
            body = await request.json()
            cert_b64 = body.get("cert_base64")
            key_b64 = body.get("key_base64")
            if not all([cert_b64, key_b64]):
                return JSONResponse(status_code=400, content={"error": "Missing 'cert_base64' or 'key_base64'"})
            cert_pem = base64.b64decode(cert_b64).decode("utf-8")
            key_pem = base64.b64decode(key_b64).decode("utf-8")
            cert_manager.import_cert(hostname, cert_pem, key_pem)
            return {"status": "ok", "hostname": hostname, "message": f"Certificate rotated for '{hostname}'"}
        except Exception as e:
            return JSONResponse(status_code=400, content={"error": str(e)})


    @app.post("/api/tls/root-ca/download")
    async def tls_download_root_ca():
        """Download the root CA certificate for manual installation on devices."""
        if not cert_manager:
            return JSONResponse(status_code=503, content={"error": "Cert manager not ready"})
        try:
            ca_pem = cert_manager.ca_cert_pem()
            return Response(
                content=ca_pem,
                media_type="application/x-pem-file",
                headers={"Content-Disposition": "attachment; filename=ride-the-api-ca.pem"},
            )
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})


    # ═══════════════════════════════════════════════════════════════════════════════
    # PROTOCOL SERVER MANAGEMENT API
    # ═══════════════════════════════════════════════════════════════════════════════


    @app.get("/api/protocol-servers")
    async def protocol_servers_status():
        """List all protocol servers and their status."""
        from core.protocol_servers import get_protocol_server_manager
        manager = get_protocol_server_manager()
        status_list = await manager.get_all_status()
        return {"servers": status_list}


    @app.post("/api/protocol-servers/{name}/start")
    async def protocol_server_start(name: str):
        """Start a specific protocol server."""
        from core.protocol_servers import get_protocol_server_manager
        manager = get_protocol_server_manager()
        success = await manager.start_plugin(name)
        if not success:
            plugin = manager.get_plugin(name)
            if plugin is None:
                return JSONResponse(status_code=404, content={"error": f"Server '{name}' not found"})
            return JSONResponse(status_code=500, content={"error": f"Failed to start '{name}'"})
        return {"status": "ok", "server": name, "running": True}


    @app.post("/api/protocol-servers/{name}/stop")
    async def protocol_server_stop(name: str):
        """Stop a specific protocol server."""
        from core.protocol_servers import get_protocol_server_manager
        manager = get_protocol_server_manager()
        success = await manager.stop_plugin(name)
        if not success:
            plugin = manager.get_plugin(name)
            if plugin is None:
                return JSONResponse(status_code=404, content={"error": f"Server '{name}' not found"})
            return JSONResponse(status_code=500, content={"error": f"Failed to stop '{name}'"})
        return {"status": "ok", "server": name, "running": False}


    @app.get("/api/protocol-servers/{name}/config")
    async def protocol_server_config(name: str):
        """Get configuration for a specific protocol server."""
        from core.protocol_servers import get_protocol_server_manager
        manager = get_protocol_server_manager()
        plugin = manager.get_plugin(name)
        if not plugin:
            return JSONResponse(status_code=404, content={"error": f"Server '{name}' not found"})
        cfg = plugin.config
        config_dict = cfg.model_dump() if hasattr(cfg, "model_dump") else vars(cfg)
        return {"name": name, "config": config_dict}


    # ═══════════════════════════════════════════════════════════════════════════════
    # HEALTH & METRICS
    # ═══════════════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "local-cloud-replacement-proxy",
        "version": "0.2.0",
        "adapters": adapter_registry.list_vendors() if adapter_registry else [],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# DEVICE MANAGEMENT API
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/devices")
async def list_devices():
    """List all registered devices."""
    if not db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    devices = await db_manager.list_devices()
    return {"devices": devices}


@app.get("/api/devices/{device_id}")
async def get_device(device_id: str):
    """Get device details and stats."""
    if not db_manager or not orchestrator:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    async with db_manager.core_session() as session:
        result = await session.execute(
            select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
        )
        device = result.scalar_one_or_none()
        if not device:
            return JSONResponse(status_code=404, content={"error": "Device not found"})

    stats = await orchestrator.get_device_stats(device_id)
    return {"device": stats}


@app.get("/api/devices/{device_id}/stats")
async def get_device_stats(device_id: str):
    """Get real-time match statistics for a device."""
    if not orchestrator:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    stats = await orchestrator.get_device_stats(device_id)
    return {"stats": stats}


@app.get("/api/devices/{device_id}/match-rate")
async def get_match_rate(device_id: str):
    """Get current match rate percentage."""
    if not orchestrator:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    stats = await orchestrator.get_device_stats(device_id)
    return {
        "device_id": device_id,
        "match_rate_pct": stats.get("match_rate_pct", 0),
        "total_requests": stats.get("total_requests", 0),
        "local_hits": stats.get("local_hits", 0),
        "cloud_misses": stats.get("cloud_misses", 0),
    }


@app.post("/api/devices/{device_id}/mode")
async def set_device_mode(device_id: str, request: Request):
    """Switch device between learning and production mode."""
    if not db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    body = await request.json()
    mode = body.get("mode", "learning")
    if mode not in ("learning", "production", "hybrid"):
        return JSONResponse(status_code=400, content={"error": "Invalid mode. Use 'learning', 'production', or 'hybrid'"})
    success = await db_manager.update_device_mode(device_id, mode)
    if not success:
        return JSONResponse(status_code=404, content={"error": "Device not found"})
    return {"device_id": device_id, "mode": mode}


    @app.get("/api/devices/{device_id}/auto-switch")
    async def get_device_auto_switch(device_id: str):
        """Get auto-switch status for a device."""
        if not db_manager:
            return JSONResponse(status_code=503, content={"error": "Service not ready"})
        devices = await db_manager.list_devices()
        for d in devices:
            if d["device_id"] == device_id:
                return {"device_id": device_id, "auto_switch_enabled": d.get("auto_switch_enabled", False)}
        return JSONResponse(status_code=404, content={"error": "Device not found"})


@app.put("/api/devices/{device_id}/auto-switch")
async def set_device_auto_switch(device_id: str, request: Request):
    """Enable or disable auto-switch to production for a device."""
    if not db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    body = await request.json()
    enabled = body.get("enabled", False)
    if not isinstance(enabled, bool):
        return JSONResponse(status_code=400, content={"error": "'enabled' must be a boolean"})
    success = await db_manager.update_device_auto_switch(device_id, enabled)
    if not success:
        return JSONResponse(status_code=404, content={"error": "Device not found"})
    return {"device_id": device_id, "auto_switch_enabled": enabled}


@app.put("/api/devices/{device_id}/llm")
async def configure_device_llm(device_id: str, request: Request):
    """Configure LLM settings for a device."""
    if not db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    body = await request.json()
    success = await db_manager.update_device_llm_config(
        device_id,
        base_url=body.get("base_url"),
        model_id=body.get("model_id"),
        profile_name=body.get("profile_name"),
    )
    if not success:
        return JSONResponse(status_code=404, content={"error": "Device not found"})
    return {"device_id": device_id, "status": "updated"}


# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE ASSIGNMENT API
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/devices/{device_id}/database")
async def assign_device_database(device_id: str, request: Request):
    """Assign a database (URL or name) to a device. Creates a new DB if only name given."""
    if not db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    body = await request.json()
    database_url = body.get("database_url")
    database_name = body.get("database_name")

    # If only a name is given, create a new SQLite database for it
    if not database_url and database_name:
        db_path = db_manager.device_db_dir / f"{database_name}.db"
        database_url = f"sqlite+aiosqlite:///{db_path}"
    elif not database_url and not database_name:
        return JSONResponse(
            status_code=400,
            content={"error": "Provide 'database_url' or 'database_name'"},
        )

    success = await db_manager.assign_device_database(
        device_id, database_url=database_url, database_name=database_name,
    )
    if not success:
        return JSONResponse(status_code=404, content={"error": "Device not found"})
    return {
        "device_id": device_id,
        "database_url": database_url,
        "database_name": database_name,
    }


@app.get("/api/databases")
async def list_databases():
    """List all active device databases."""
    if not db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    databases = await db_manager.list_databases()
    return {"databases": databases}


@app.get("/api/devices/{device_id}/database")
async def get_device_database(device_id: str):
    """Get the database assignment for a device."""
    if not db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    async with db_manager.core_session() as session:
        from sqlalchemy import select
        from core.database import DeviceRegistry
        result = await session.execute(
            select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
        )
        device = result.scalar_one_or_none()
        if not device:
            return JSONResponse(status_code=404, content={"error": "Device not found"})
        return {
            "device_id": device.device_id,
            "database_url": device.database_url,
            "database_name": device.database_name,
            "ip_addresses": device.ip_addresses,
        }


@app.get("/api/devices/by-ip/{ip_address}")
async def get_device_by_ip(ip_address: str):
    """Look up a device by its IP address."""
    if not db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    device_id = await db_manager.resolve_device_id(ip_address)
    if not device_id:
        return JSONResponse(status_code=404, content={"error": "Device not found for this IP"})
    return {"device_id": device_id, "ip_address": ip_address}


@app.post("/api/devices/{device_id}/ip")
async def register_device_ip(device_id: str, request: Request):
    """Register an IP address for a device."""
    if not db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    body = await request.json()
    ip_address = body.get("ip_address")
    if not ip_address:
        return JSONResponse(status_code=400, content={"error": "Provide 'ip_address'"})
    async with db_manager.core_session() as session:
        from sqlalchemy import select
        from core.database import DeviceRegistry
        result = await session.execute(
            select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
        )
        device = result.scalar_one_or_none()
        if not device:
            return JSONResponse(status_code=404, content={"error": "Device not found"})
        current_ips = list(device.ip_addresses or [])
        if ip_address not in current_ips:
            current_ips.append(ip_address)
            device.ip_addresses = current_ips
            await session.commit()
        return {"device_id": device_id, "ip_addresses": current_ips}


@app.get("/api/devices/{device_id}/patterns")
async def get_device_patterns(device_id: str):
    """Get learned patterns for a device."""
    if not db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    from core.database import RequestPattern, ResponseTemplate, FieldMapping
    from sqlalchemy import select
    async with db_manager.device_session(device_id) as session:
        patterns = await session.execute(
            select(RequestPattern).order_by(RequestPattern.confidence.desc())
        )
        patterns_list = []
        for p in patterns.scalars().all():
            templates = await session.execute(
                select(ResponseTemplate).where(ResponseTemplate.pattern_id == p.pattern_id)
            )
            tpl = templates.scalar_one_or_none()
            patterns_list.append({
                "pattern_id": p.pattern_id,
                "method": p.method,
                "path": p.path_pattern,
                "path_pattern": p.path_pattern,
                "protocol": p.protocol,
                "intent": p.intent,
                "confidence": p.confidence,
                "hit_count": p.hit_count,
                "required_headers": p.required_headers,
                "body_schema": p.body_schema,
                "query_param_keys": p.query_param_keys,
                "response_template": {
                    "status_code": tpl.status_code,
                    "body_template": tpl.body_template,
                    "headers_template": tpl.headers_template,
                    "field_mappings": tpl.field_mappings,
                } if tpl else None,
            })
        return {"device_id": device_id, "patterns": patterns_list}


@app.get("/api/devices/{device_id}/patterns/{pattern_id}")
async def get_pattern_detail(device_id: str, pattern_id: str):
    """Get detailed pattern info including field mappings."""
    if not db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    from core.database import RequestPattern, ResponseTemplate, FieldMapping
    from sqlalchemy import select
    async with db_manager.device_session(device_id) as session:
        result = await session.execute(
            select(RequestPattern).where(RequestPattern.pattern_id == pattern_id)
        )
        pattern = result.scalar_one_or_none()
        if not pattern:
            return JSONResponse(status_code=404, content={"error": "Pattern not found"})

        tpl_result = await session.execute(
            select(ResponseTemplate).where(ResponseTemplate.pattern_id == pattern_id)
        )
        template = tpl_result.scalar_one_or_none()

        mappings_result = await session.execute(
            select(FieldMapping).where(FieldMapping.intent == pattern.intent)
        )
        mappings = [{"request_field": m.request_field, "response_field": m.response_field,
                      "transform": m.transform, "confidence": m.confidence}
                    for m in mappings_result.scalars().all()]

        return {
            "pattern": {
                "pattern_id": pattern.pattern_id,
                "method": pattern.method,
                "path_pattern": pattern.path_pattern,
                "protocol": pattern.protocol,
                "required_headers": pattern.required_headers,
                "body_schema": pattern.body_schema,
                "intent": pattern.intent,
                "confidence": pattern.confidence,
                "hit_count": pattern.hit_count,
            },
            "response_template": {
                "status_code": template.status_code,
                "headers_template": template.headers_template,
                "body_template": template.body_template,
                "field_mappings": template.field_mappings,
                "expected_variables": template.expected_variables,
            } if template else None,
            "field_mappings": mappings,
        }


# ── Pattern CRUD: Update, Patch, Delete ───────────────────────────────────────

@app.put("/api/devices/{device_id}/patterns/{pattern_id}")
async def put_pattern(device_id: str, pattern_id: str, request: Request):
    """Full update of a request pattern, its response template, and field mappings."""
    if not db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    from core.database import RequestPattern, ResponseTemplate, FieldMapping
    from sqlalchemy import select, delete
    try:
        body = await request.json()
        async with db_manager.device_session(device_id) as session:
            result = await session.execute(select(RequestPattern).where(RequestPattern.pattern_id == pattern_id))
            pattern = result.scalar_one_or_none()
            if not pattern:
                # Upsert: create the pattern if it doesn't exist
                pattern = RequestPattern(
                    pattern_id=pattern_id,
                    method=body.get("method", "GET"),
                    path_pattern=body.get("path", body.get("path_pattern", "")),
                    protocol=body.get("protocol", "http"),
                    required_headers=body.get("required_headers", []),
                    body_schema=body.get("body_schema", {}),
                    query_param_keys=body.get("query_param_keys", []),
                    intent=body.get("intent", ""),
                    confidence=body.get("confidence", 0.0),
                )

            # Replace pattern fields
            for f in ("method","path_pattern","protocol","required_headers","body_schema","query_param_keys","intent","confidence"):
                if f in body:
                    setattr(pattern, f, body[f])
            # Accept "path" as alias for "path_pattern"
            if "path" in body and "path_pattern" not in body:
                pattern.path_pattern = body["path"]
            session.add(pattern)

            # Response template
            tpl_data = body.get("response_template") or {}
            tpl_result = await session.execute(select(ResponseTemplate).where(ResponseTemplate.pattern_id == pattern_id))
            tpl = tpl_result.scalar_one_or_none()
            if not tpl:
                tpl = ResponseTemplate(
                    template_id=(str(uuid.uuid4())[:16]),
                    pattern_id=pattern_id,
                    status_code=tpl_data.get("status_code", 200),
                    headers_template=tpl_data.get("headers_template", {}),
                    body_template=tpl_data.get("body_template", {}),
                    field_mappings=tpl_data.get("field_mappings", {}),
                    expected_variables=tpl_data.get("expected_variables", []),
                )
            else:
                tpl.status_code = tpl_data.get("status_code", tpl.status_code)
                tpl.headers_template = tpl_data.get("headers_template", tpl.headers_template)
                tpl.body_template = tpl_data.get("body_template", tpl.body_template)
                tpl.field_mappings = tpl_data.get("field_mappings", tpl.field_mappings)
                tpl.expected_variables = tpl_data.get("expected_variables", tpl.expected_variables)
            session.add(tpl)

            # Field mappings: replace all mappings for this intent
            fm_list = body.get("field_mappings")
            if fm_list is not None:
                await session.execute(delete(FieldMapping).where(FieldMapping.intent == pattern.intent))
                for m in fm_list:
                    mapping_id = m.get("mapping_id") or str(uuid.uuid4())[:16]
                    fm = FieldMapping(
                        mapping_id=mapping_id,
                        request_field=m.get("request_field", ""),
                        request_type=m.get("request_type", ""),
                        response_field=m.get("response_field", ""),
                        response_type=m.get("response_type", ""),
                        transform=m.get("transform"),
                        enum_values=m.get("enum_values"),
                        intent=pattern.intent,
                        confidence=m.get("confidence", 0.5),
                    )
                    session.add(fm)

            await session.commit()
            return {"status": "ok", "pattern_id": pattern_id}
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.patch("/api/devices/{device_id}/patterns/{pattern_id}")
async def patch_pattern(device_id: str, pattern_id: str, request: Request):
    """Partial update for a pattern."""
    if not db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    from core.database import RequestPattern, ResponseTemplate, FieldMapping
    from sqlalchemy import select, delete
    try:
        body = await request.json()
        async with db_manager.device_session(device_id) as session:
            result = await session.execute(select(RequestPattern).where(RequestPattern.pattern_id == pattern_id))
            pattern = result.scalar_one_or_none()
            if not pattern:
                return JSONResponse(status_code=404, content={"error": "Pattern not found"})

            # Update only provided fields
            for f in ("method","path_pattern","protocol","required_headers","body_schema","query_param_keys","intent","confidence"):
                if f in body:
                    setattr(pattern, f, body[f])
            session.add(pattern)

            tpl_data = body.get("response_template")
            if tpl_data is not None:
                tpl_result = await session.execute(select(ResponseTemplate).where(ResponseTemplate.pattern_id == pattern_id))
                tpl = tpl_result.scalar_one_or_none()
                if not tpl:
                    tpl = ResponseTemplate(
                        template_id=(str(uuid.uuid4())[:16]),
                        pattern_id=pattern_id,
                        status_code=tpl_data.get("status_code", 200),
                        headers_template=tpl_data.get("headers_template", {}),
                        body_template=tpl_data.get("body_template", {}),
                        field_mappings=tpl_data.get("field_mappings", {}),
                        expected_variables=tpl_data.get("expected_variables", []),
                    )
                else:
                    if "status_code" in tpl_data: tpl.status_code = tpl_data["status_code"]
                    if "headers_template" in tpl_data: tpl.headers_template = tpl_data["headers_template"]
                    if "body_template" in tpl_data: tpl.body_template = tpl_data["body_template"]
                    if "field_mappings" in tpl_data: tpl.field_mappings = tpl_data["field_mappings"]
                    if "expected_variables" in tpl_data: tpl.expected_variables = tpl_data["expected_variables"]
                session.add(tpl)

            fm_list = body.get("field_mappings")
            if fm_list is not None:
                # Replace per-intent mappings
                await session.execute(delete(FieldMapping).where(FieldMapping.intent == pattern.intent))
                for m in fm_list:
                    mapping_id = m.get("mapping_id") or str(uuid.uuid4())[:16]
                    fm = FieldMapping(
                        mapping_id=mapping_id,
                        request_field=m.get("request_field", ""),
                        request_type=m.get("request_type", ""),
                        response_field=m.get("response_field", ""),
                        response_type=m.get("response_type", ""),
                        transform=m.get("transform"),
                        enum_values=m.get("enum_values"),
                        intent=pattern.intent,
                        confidence=m.get("confidence", 0.5),
                    )
                    session.add(fm)

            await session.commit()
            return {"status": "ok", "pattern_id": pattern_id}
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.delete("/api/devices/{device_id}/patterns/{pattern_id}")
async def delete_pattern(device_id: str, pattern_id: str):
    """Delete a pattern and its associated response template and field mappings."""
    if not db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    from core.database import RequestPattern, ResponseTemplate, FieldMapping
    from sqlalchemy import select, delete
    try:
        async with db_manager.device_session(device_id) as session:
            result = await session.execute(select(RequestPattern).where(RequestPattern.pattern_id == pattern_id))
            pattern = result.scalar_one_or_none()
            if not pattern:
                return JSONResponse(status_code=404, content={"error": "Pattern not found"})

            # Delete response template
            await session.execute(delete(ResponseTemplate).where(ResponseTemplate.pattern_id == pattern_id))
            # Delete request pattern
            await session.execute(delete(RequestPattern).where(RequestPattern.pattern_id == pattern_id))
            # Delete field mappings for this intent
            await session.execute(delete(FieldMapping).where(FieldMapping.intent == pattern.intent))
            await session.commit()
            return {"status": "deleted", "pattern_id": pattern_id}
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.get("/api/llm/profiles")
async def list_llm_profiles():
    """List available LLM profiles."""
    if not llm_decipher_service:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    profiles = llm_decipher_service.list_profiles()
    return {"profiles": profiles}


# ═══════════════════════════════════════════════════════════════════════════════
# PORTABLE PATTERN DB ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/api/devices/{device_id}/patterns/export")
async def export_patterns(device_id: str):
    """Export deciphered patterns to portable .ride-pattern.json format."""
    if not db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    from core.pattern_db import decipher_ingest
    ingester = decipher_ingest.DecipherIngest(db_manager)
    try:
        async with db_manager.device_session(device_id) as session:
            from core.database import DeviceRegistry
            from sqlalchemy import select as sel
            result = await session.execute(sel(DeviceRegistry).where(DeviceRegistry.device_id == device_id))
            device = result.scalar_one_or_none()
            vendor = device.vendor if device else "unknown"
            device_type = device.device_type if device else "unknown"
        pattern_db = await ingester.export_patterns(device_id, vendor, device_type)
        return pattern_db.model_dump(by_alias=True, exclude_none=True)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/devices/{device_id}/patterns/import")
async def import_patterns(device_id: str, request: Request):
    """Import patterns from portable .ride-pattern.json format."""
    if not db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    from core.pattern_db.schemas import PatternDB
    from core.pattern_db import decipher_ingest
    from core.pattern_db.validator import validate_pattern, ValidationError
    try:
        body = await request.json()
        # Validate against the portable JSON Schema
        result = validate_pattern(body)
        if not result.valid:
            return JSONResponse(
                status_code=422,
                content={"error": "Pattern validation failed", "details": result.to_dict()},
            )
        pattern_db = PatternDB.model_validate(body)
        ingester = decipher_ingest.DecipherIngest(db_manager)
        count = await ingester.import_patterns(device_id, pattern_db)
        return {"imported": count, "device_id": device_id, "warnings": result.warnings}
    except ValidationError as e:
        return JSONResponse(
            status_code=422,
            content={"error": "Pattern validation failed", "details": e.to_dict()},
        )
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.get("/api/devices/{device_id}/capture/export")
async def export_buffer(device_id: str):
    """Export raw buffer to portable .ride-capture.json format."""
    if not db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    from core.pattern_db import buffer_manager
    manager = buffer_manager.BufferManager(db_manager)
    try:
        async with db_manager.device_session(device_id) as session:
            from core.database import DeviceRegistry
            from sqlalchemy import select as sel
            result = await session.execute(sel(DeviceRegistry).where(DeviceRegistry.device_id == device_id))
            device = result.scalar_one_or_none()
            vendor = device.vendor if device else "unknown"
            device_type = device.device_type if device else "unknown"
        capture = await manager.export_capture(device_id, vendor, device_type)
        return capture.model_dump(by_alias=True, exclude_none=True)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/devices/{device_id}/capture/import")
async def import_buffer(device_id: str, request: Request):
    """Import raw pairs from portable .ride-capture.json into buffer."""
    if not db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    from core.pattern_db.schemas import CaptureDB
    from core.pattern_db import buffer_manager
    from core.pattern_db.validator import validate_capture, ValidationError
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
        count = await manager.import_capture(capture)
        return {"imported": count, "device_id": device_id, "warnings": result.warnings}
    except ValidationError as e:
        return JSONResponse(
            status_code=422,
            content={"error": "Capture validation failed", "details": e.to_dict()},
        )
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


# ═══════════════════════════════════════════════════════════════════════════════
# WEB UI (basic dashboard)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Simple web dashboard."""
    try:
        html = DASHBOARD_HTML.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("Dashboard HTML not found at %s", DASHBOARD_HTML)
        html = "<!DOCTYPE html><html><body><h1>Dashboard not found</h1><p>Expected at webui/dashboard.html</p></body></html>"
    return HTMLResponse(content=html, status_code=200)


@app.get("/patterns/{device_id}", response_class=HTMLResponse)
async def patterns_page(device_id: str):
    """Serve the Patterns web UI for a device."""
    try:
        html = PATTERNS_HTML.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("Patterns HTML not found at %s", PATTERNS_HTML)
        html = "<!DOCTYPE html><html><body><h1>Patterns page not found</h1><p>Expected at webui/patterns.html</p></body></html>"
    return HTMLResponse(content=html, status_code=200)


# ═══════════════════════════════════════════════════════════════════════════════
# Resilience routes (must be registered before catch-all to take priority)
# ═══════════════════════════════════════════════════════════════════════════════

register_resilience_routes(app, lambda: db_manager, lambda: orchestrator)

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN PROXY ENDPOINT - Catches all device traffic
# ═══════════════════════════════════════════════════════════════════════════════

@app.api_route("/{vendor}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy_vendor_request(vendor: str, path: str, request: Request):
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
            content={"error": f"Protocol '{vendor}' not supported. Supported: {adapter_registry.list_vendors()}"},
        )

    # Parse request body
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
            logger.info(f"Passthrough for local traffic from {client_ip} to {vendor}")
            return JSONResponse(
                status_code=200,
                content={"status": "passthrough", "message": "Traffic forwarded directly"},
            )

    # Build intercepted request
    intercepted = InterceptedRequest(
        device_id="",
            timestamp=datetime.now(timezone.utc),
        protocol=ProtocolType.HTTPS if request.url.scheme == "https" else ProtocolType.HTTP,
        method=request.method,
        path=f"/{path}",
        headers=dict(request.headers),
        query_params=dict(request.query_params),
        body=body,
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

        # Pass to pipeline for processing
        result = await orchestrator.handle_request(
            device_id=device_id,
            vendor=vendor,
            protocol="http",
            method=request.method,
            path=f"/{path}",
            headers=dict(request.headers),
            body=body,
            query_params=dict(request.query_params),
        )

        if result["action"] == "local_response":
            # Serve locally from learned patterns
            response = result["response"]
            return JSONResponse(
                status_code=response.get("status_code", 200),
                content=response.get("body", {}),
                headers=response.get("headers", {}),
            )
        else:
            # Forward to cloud (passthrough)
            # In production, this would forward to the actual cloud endpoint
            # For now, return a simulated response that the adapter provides
            cloud_response = await adapter.forward_to_cloud(intercepted)
            if cloud_response and cloud_response.success:
                # Process cloud response for learning
                resp_body = cloud_response.data if hasattr(cloud_response, 'data') else {}
                await orchestrator.handle_response(
                    device_id=device_id,
                    vendor=vendor,
                    protocol="http",
                    status_code=200,
                    headers={"content-type": "application/json"},
                    body=resp_body,
                )
                return JSONResponse(content=resp_body)
            else:
                return JSONResponse(
                    status_code=502,
                    content={"error": "Cloud passthrough failed", "detail": str(cloud_response.error) if cloud_response else "No response"},
                )

    except Exception as e:
        logger.error(f"Error processing request: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": f"Internal error: {str(e)}"},
        )


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def _get_request_body(request: Request) -> dict | None:
    """Extract JSON body from request."""
    try:
        body = await request.body()
        if body:
            return json.loads(body)
    except Exception:
        pass
    return None


def _is_local_ip(ip: str) -> bool:
    """Check if IP is in a private/local range."""
    try:
        import ipaddress
        ip_obj = ipaddress.ip_address(ip)
        return ip_obj.is_private or ip_obj.is_loopback
    except ValueError:
        return False


def _extract_device_id(headers: dict, path: str) -> str:
    """Try to extract device ID from headers or path."""
    # Check common headers
    for header in ["x-device-id", "x-deviceid", "device-id", "deviceid", "x-client-id", "x-sn"]:
        if header in headers:
            return headers[header]
    # Check path for common patterns
    parts = path.strip("/").split("/")
    for part in parts:
        if len(part) >= 8 and part.isalnum():
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
    uvicorn.run(
        "core.server:app",
        host=config.proxy.host,
        port=config.proxy.port,
        log_level=config.observability.logging.level.lower(),
        reload=False,
    )


if __name__ == "__main__":
    main()
"""
Local Cloud Replacement Proxy - Main entry point.
Intercepts device traffic, learns protocol via LLM, serves responses locally.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse

from core.config import get_config_manager
from core.database import DatabaseManager, Base, init_db_manager, get_db_manager, DeviceRegistry
from core.llm_decipher import get_llm_decipher, LLMDecipherService
from core.pipeline import (
    LearningOrchestrator, get_orchestrator, ContextBuffer,
    PatternMatcher, MatchRateTracker, PipelineMode,
)
from core.resilience import CloudIndependenceVerifier, register_resilience_routes
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
    global db_manager, adapter_registry, orchestrator, llm_decipher_service, tls_mitm_server, cert_manager

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

    logger.info(f"Server started on {config.proxy.host}:{config.proxy.port}")
    logger.info(f"Registered adapters: {adapter_registry.list_vendors()}")

    yield

    # Cleanup
    logger.info("Shutting down...")
    config_manager.stop_watching()

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
                "intent": p.intent,
                "confidence": p.confidence,
                "hit_count": p.hit_count,
                "response_template": {
                    "status_code": tpl.status_code,
                    "body_template": tpl.body_template,
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
    try:
        body = await request.json()
        pattern_db = PatternDB.model_validate(body)
        ingester = decipher_ingest.DecipherIngest(db_manager)
        count = await ingester.import_patterns(device_id, pattern_db)
        return {"imported": count, "device_id": device_id}
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
    manager = buffer_manager.BufferManager(db_manager)
    try:
        body = await request.json()
        capture = CaptureDB.model_validate(body)
        count = await manager.import_capture(capture)
        return {"imported": count, "device_id": device_id}
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


# ═══════════════════════════════════════════════════════════════════════════════
# WEB UI (basic dashboard)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Simple web dashboard."""
    return HTMLResponse(content=HTML_DASHBOARD, status_code=200)


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

HTML_DASHBOARD = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ride the API — Local Cloud Replacement</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0b0e14; --surface: #141820; --surface2: #1a1f2b; --border: #232a36;
    --text: #e1e6ed; --text2: #8a94a6; --accent: #6c8cff; --accent2: #5b7ad0;
    --green: #34d399; --yellow: #fbbf24; --red: #f87171; --blue: #60a5fa;
    --radius: 12px; --shadow: 0 8px 32px rgba(0,0,0,.3);
  }
  body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
    background: var(--bg); color: var(--text); padding: 24px; min-height: 100vh;
    background-image: radial-gradient(ellipse at 20% 10%, rgba(108,140,255,.08) 0%, transparent 50%),
                      radial-gradient(ellipse at 80% 90%, rgba(52,211,153,.06) 0%, transparent 50%);
  }
  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: var(--surface); }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  /* ── Header ────────────────────────────────────────────────────────────── */
  .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 28px; flex-wrap: wrap; gap: 12px; }
  .header-left { display: flex; flex-direction: column; }
  .header-left h1 { font-size: 28px; font-weight: 800; background: linear-gradient(135deg, var(--accent), var(--green)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; letter-spacing: -.5px; }
  .header-left .subtitle { font-size: 14px; color: var(--text2); margin-top: 2px; }
  .header-right { display: flex; gap: 10px; align-items: center; }
  .btn {
    padding: 8px 18px; border: none; border-radius: 8px; font-family: inherit; font-size: 13px;
    font-weight: 600; cursor: pointer; transition: all .2s; display: inline-flex; align-items: center; gap: 6px;
  }
  .btn-primary { background: linear-gradient(135deg, var(--accent), var(--accent2)); color: #fff; box-shadow: 0 4px 14px rgba(108,140,255,.25); }
  .btn-primary:hover { transform: translateY(-1px); box-shadow: 0 6px 20px rgba(108,140,255,.35); }
  .btn-primary:active { transform: translateY(0); }
  .btn-ghost { background: var(--surface2); color: var(--text); border: 1px solid var(--border); }
  .btn-ghost:hover { border-color: var(--accent); background: rgba(108,140,255,.1); }

  /* ── Global Stats Bar ──────────────────────────────────────────────────── */
  .global-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 24px; }
  .global-stat {
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px;
    position: relative; overflow: hidden;
  }
  .global-stat::before { content: ''; position: absolute; inset: 0; opacity: .06; }
  .global-stat .gs-icon { font-size: 20px; margin-bottom: 6px; }
  .global-stat .gs-num { font-size: 26px; font-weight: 700; }
  .global-stat .gs-lbl { font-size: 12px; color: var(--text2); margin-top: 2px; text-transform: uppercase; letter-spacing: .5px; }
  .global-stat.gs-blue .gs-num { color: var(--blue); }
  .global-stat.gs-green .gs-num { color: var(--green); }
  .global-stat.gs-yellow .gs-num { color: var(--yellow); }
  .global-stat.gs-red .gs-num { color: var(--red); }

  /* ── Grid / Cards ──────────────────────────────────────────────────────── */
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }
  .device-card {
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 18px;
    cursor: pointer; transition: all .25s; position: relative; overflow: hidden;
  }
  .device-card::after { content: ''; position: absolute; inset: 0; border-radius: var(--radius); opacity: 0; transition: opacity .25s; background: linear-gradient(135deg, rgba(108,140,255,.05), transparent); }
  .device-card:hover { border-color: var(--accent); transform: translateY(-2px); box-shadow: 0 8px 24px rgba(0,0,0,.35); }
  .device-card:hover::after { opacity: 1; }
  .device-card .dc-top { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 10px; position: relative; z-index: 1; }
  .device-card .dc-name { font-size: 15px; font-weight: 700; }
  .device-card .dc-id { font-size: 11px; color: var(--text2); margin-top: 2px; }
  .device-card .dc-meta { font-size: 12px; color: var(--text2); margin-bottom: 8px; position: relative; z-index: 1; }
  .device-card .dc-stats { display: flex; flex-wrap: wrap; gap: 6px 12px; position: relative; z-index: 1; }
  .device-card .dc-stat { font-size: 12px; display: flex; align-items: center; gap: 4px; }
  .device-card .dc-stat .dc-label { color: var(--text2); }
  .device-card .dc-stat .dc-value { font-weight: 600; }

  /* ── Detail Panel ──────────────────────────────────────────────────────── */
  #details { margin-bottom: 24px; }
  .detail-card {
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px;
    box-shadow: var(--shadow); max-width: 800px; margin: 0 auto;
  }
  .detail-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
  .detail-header h2 { font-size: 18px; font-weight: 700; background: linear-gradient(135deg, var(--accent), var(--green)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }
  .close-btn { background: none; border: 1px solid var(--border); color: var(--text2); font-size: 20px; cursor: pointer; padding: 2px 12px; border-radius: 6px; transition: all .2s; }
  .close-btn:hover { color: var(--red); border-color: var(--red); background: rgba(248,113,113,.1); }

  /* ── SVG Ring ──────────────────────────────────────────────────────────── */
  .ring-container { display: flex; justify-content: center; align-items: center; margin: 16px 0; }
  .ring-svg { width: 120px; height: 120px; transform: rotate(-90deg); }
  .ring-bg { fill: none; stroke: var(--border); stroke-width: 8; }
  .ring-fg { fill: none; stroke-width: 8; stroke-linecap: round; transition: stroke-dashoffset .6s ease; }
  .ring-label { position: absolute; text-align: center; font-weight: 700; }
  .ring-label .pct { font-size: 28px; }
  .ring-label .pct-label { font-size: 11px; color: var(--text2); display: block; margin-top: 2px; font-weight: 500; }
  .ring-wrap { position: relative; display: inline-flex; align-items: center; justify-content: center; }

  /* ── Stats Grid ────────────────────────────────────────────────────────── */
  .stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin: 16px 0; }
  .stat-box { text-align: center; padding: 12px 6px; background: var(--surface2); border-radius: 8px; }
  .stat-box .num { font-size: 22px; font-weight: 700; }
  .stat-box .lbl { font-size: 11px; color: var(--text2); margin-top: 2px; text-transform: uppercase; letter-spacing: .3px; }
  .stat-box .num.green { color: var(--green); }
  .stat-box .num.blue { color: var(--blue); }
  .stat-box .num.red { color: var(--red); }
  .stat-box .num.yellow { color: var(--yellow); }

  /* ── Detail Rows ───────────────────────────────────────────────────────── */
  .detail-rows { margin: 12px 0; }
  .detail-row { display: flex; justify-content: space-between; padding: 6px 0; font-size: 13px; border-bottom: 1px solid rgba(35,42,54,.5); }
  .detail-row:last-child { border-bottom: none; }
  .detail-row .dr-label { color: var(--text2); }
  .detail-row .dr-value { font-weight: 600; }

  /* ── Sparkline ─────────────────────────────────────────────────────────── */
  .sparkline-wrap { margin: 14px 0; background: var(--surface2); border-radius: 8px; padding: 12px; }
  .sparkline-label { font-size: 12px; color: var(--text2); margin-bottom: 8px; text-transform: uppercase; letter-spacing: .5px; }
  .chart { display: flex; gap: 2px; align-items: flex-end; height: 40px; }
  .chart .bar { width: 6px; border-radius: 2px 2px 0 0; transition: height .3s; flex-shrink: 0; }
  .bar.hit { background: var(--green); }
  .bar.miss { background: var(--blue); }
  .bar.error { background: var(--red); }
  .legend { display: flex; gap: 14px; margin-top: 8px; font-size: 11px; color: var(--text2); }
  .legend-item { display: flex; align-items: center; gap: 4px; }
  .legend-dot { width: 8px; height: 8px; border-radius: 2px; }

  /* ── Mode Switch ───────────────────────────────────────────────────────── */
  .mode-area { display: flex; gap: 10px; align-items: center; margin-top: 16px; flex-wrap: wrap; }
  .mode-area label { font-size: 13px; color: var(--text2); }
  .mode-area select {
    background: var(--surface2); color: var(--text); border: 1px solid var(--border); padding: 6px 12px;
    border-radius: 6px; font-family: inherit; font-size: 13px; cursor: pointer; transition: border-color .2s;
  }
  .mode-area select:focus { outline: none; border-color: var(--accent); }

  /* ── Badges ────────────────────────────────────────────────────────────── */
  .badge { display: inline-block; padding: 2px 10px; border-radius: 10px; font-size: 11px; font-weight: 600; white-space: nowrap; }
  .badge-learning { background: rgba(96,165,250,.12); color: var(--blue); border: 1px solid rgba(96,165,250,.3); }
  .badge-production { background: rgba(52,211,153,.12); color: var(--green); border: 1px solid rgba(52,211,153,.3); }
  .badge-hybrid { background: rgba(251,191,36,.12); color: var(--yellow); border: 1px solid rgba(251,191,36,.3); }

  /* ── Toast ─────────────────────────────────────────────────────────────── */
  .toast-container { position: fixed; top: 20px; right: 20px; z-index: 1000; display: flex; flex-direction: column; gap: 8px; }
  .toast {
    padding: 12px 20px; border-radius: 10px; background: var(--surface); border: 1px solid var(--border);
    box-shadow: 0 8px 24px rgba(0,0,0,.4); font-size: 13px; font-weight: 500;
    animation: slideIn .3s ease; display: flex; align-items: center; gap: 8px; max-width: 360px;
  }
  .toast.success { border-color: var(--green); }
  .toast.error { border-color: var(--red); }
  .toast.info { border-color: var(--blue); }
  @keyframes slideIn { from { opacity: 0; transform: translateX(40px); } to { opacity: 1; transform: translateX(0); } }

  /* ── Misc ──────────────────────────────────────────────────────────────── */
  .empty { color: var(--text2); text-align: center; padding: 60px 20px; grid-column: 1 / -1; }
  .empty-icon { font-size: 48px; margin-bottom: 12px; opacity: .3; }
  .empty-text { font-size: 16px; font-weight: 500; margin-bottom: 4px; }
  .empty-sub { font-size: 13px; color: var(--text2); }
  .invisible { display: none !important; }

    /* ── TLS: Monitored Ports Bar ──────────────────────────────────────────── */
    .tls-bar { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 12px 18px; margin-bottom: 20px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
    .tls-bar .tb-label { font-size: 12px; color: var(--text2); text-transform: uppercase; letter-spacing: .5px; font-weight: 600; }
    .tls-bar .tb-badge { display: inline-flex; align-items: center; gap: 4px; background: var(--surface2); border: 1px solid var(--border); border-radius: 6px; padding: 4px 10px; font-size: 13px; font-weight: 600; }
    .tls-bar .tb-badge.tb-active { border-color: var(--green); background: rgba(52,211,153,.08); color: var(--green); }
    .tls-bar .tb-badge.tb-inactive { border-color: var(--border); color: var(--text2); }
    .tls-bar .tb-btn { background: none; border: none; color: var(--text2); cursor: pointer; font-size: 14px; padding: 0 2px; transition: color .2s; }
    .tls-bar .tb-btn:hover { color: var(--red); }
    .tls-bar .tb-add input { background: var(--surface2); border: 1px solid var(--border); border-radius: 6px; padding: 4px 8px; color: var(--text); font-size: 13px; width: 70px; font-family: inherit; }
    .tls-bar .tb-add input:focus { outline: none; border-color: var(--accent); }
    .tls-bar .tb-status { font-size: 12px; color: var(--text2); margin-left: auto; }

    /* ── TLS: Unidentified Device Card ─────────────────────────────────────── */
    .device-card.unidentified { border-color: var(--yellow); background: rgba(251,191,36,.04); }
    .device-card.unidentified .dc-name { color: var(--yellow); }
    .device-card.unidentified::after { background: linear-gradient(135deg, rgba(251,191,36,.08), transparent); }

    /* ── TLS Config Panel ──────────────────────────────────────────────────── */
    .tls-config { margin-top: 16px; padding: 14px; background: var(--surface2); border-radius: 8px; border: 1px solid var(--border); }
    .tls-config h3 { font-size: 14px; font-weight: 700; color: var(--accent); margin-bottom: 12px; display: flex; align-items: center; gap: 6px; }
    .tls-config .tc-row { display: flex; justify-content: space-between; align-items: center; padding: 6px 0; font-size: 13px; border-bottom: 1px solid rgba(35,42,54,.5); }
    .tls-config .tc-row:last-child { border-bottom: none; }
    .tls-config .tc-label { color: var(--text2); }
    .tls-config .tc-value { font-weight: 600; }
    .tls-config input, .tls-config select { background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 4px 8px; color: var(--text); font-size: 12px; font-family: inherit; max-width: 180px; }
    .tls-config input:focus, .tls-config select:focus { outline: none; border-color: var(--accent); }
    .tls-config .tc-toggle { position: relative; display: inline-block; width: 40px; height: 22px; }
    .tls-config .tc-toggle input { opacity: 0; width: 0; height: 0; }
    .tls-config .tc-slider { position: absolute; cursor: pointer; inset: 0; background: var(--border); border-radius: 22px; transition: .3s; }
    .tls-config .tc-slider::before { content: ''; position: absolute; height: 16px; width: 16px; left: 3px; bottom: 3px; background: var(--text); border-radius: 50%; transition: .3s; }
    .tls-config .tc-toggle input:checked + .tc-slider { background: var(--green); }
    .tls-config .tc-toggle input:checked + .tc-slider::before { transform: translateX(18px); }
  </style>
</head>
<body>

<div class="toast-container" id="toasts"></div>

<div class="header">
  <div class="header-left">
    <h1>Ride the API</h1>
    <span class="subtitle">Local Cloud Replacement — Device Protocol Learning &amp; Response Dashboard</span>
  </div>
  <div class="header-right">
    <button class="btn btn-ghost" onclick="loadDevices()">⟳ Refresh</button>
  </div>
</div>

<!-- Monitored Ports (TLS) -->
<div class="tls-bar" id="tlsBar">
  <span class="tb-label">🔐 Monitored Ports</span>
  <span id="tlsBadges"></span>
  <span class="tb-add">
    <input type="number" id="newPortInput" placeholder="Port" min="1" max="65535" style="width:80px">
    <button class="btn btn-ghost" onclick="addTlsPort()" style="padding:4px 10px;font-size:12px;">+ Add</button>
  </span>
  <span class="tb-status" id="tlsStatus">loading…</span>
</div>

<!-- Global stats bar -->
<div class="global-stats" id="globalStats">
  <div class="global-stat gs-blue">
    <div class="gs-icon">⟐</div>
    <div class="gs-num">0</div>
    <div class="gs-lbl">Total Devices</div>
  </div>
  <div class="global-stat gs-green">
    <div class="gs-icon">⊙</div>
    <div class="gs-num">0</div>
    <div class="gs-lbl">Learning</div>
  </div>
  <div class="global-stat gs-yellow">
    <div class="gs-icon">⊕</div>
    <div class="gs-num">0</div>
    <div class="gs-lbl">Production</div>
  </div>
  <div class="global-stat gs-red">
    <div class="gs-icon">⊞</div>
    <div class="gs-num">0</div>
    <div class="gs-lbl">Hybrid</div>
  </div>
</div>

<div id="devices" class="grid">
  <div class="empty">
    <div class="empty-icon">⟳</div>
    <div class="empty-text">Loading devices...</div>
  </div>
</div>

<div id="details" class="invisible"></div>

<script>
// ── Helpers ──────────────────────────────────────────────────────────────────
function rateClass(pct) { return pct >= 80 ? 'good' : pct >= 50 ? 'warning' : 'danger'; }
function rateColor(pct) { return pct >= 80 ? '#34d399' : pct >= 50 ? '#fbbf24' : '#f87171'; }
function modeBadge(mode) {
  const cls = mode === 'production' ? 'badge-production' : mode === 'hybrid' ? 'badge-hybrid' : 'badge-learning';
  return `<span class="badge ${cls}">${mode}</span>`;
}
function shortId(id) { return id.length > 30 ? id.slice(0, 12) + '…' : id; }

// ── Toast ────────────────────────────────────────────────────────────────────
function showToast(msg, type) {
  const c = document.getElementById('toasts');
  const t = document.createElement('div');
  t.className = 'toast ' + (type || 'info');
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity .3s'; setTimeout(() => t.remove(), 300); }, 3000);
}

// ── Load device list ─────────────────────────────────────────────────────────
async function loadDevices() {
  try {
    const res = await fetch('/api/devices');
    const data = await res.json();
    const container = document.getElementById('devices'), devices = data.devices || [];

    // Global stats
    const total = devices.length;
    const learn = devices.filter(d => d.mode === 'learning').length;
    const prod = devices.filter(d => d.mode === 'production').length;
    const hybrid = devices.filter(d => d.mode === 'hybrid').length;
    const gs = document.getElementById('globalStats');
    gs.querySelectorAll('.gs-num')[0].textContent = total;
    gs.querySelectorAll('.gs-num')[1].textContent = learn;
    gs.querySelectorAll('.gs-num')[2].textContent = prod;
    gs.querySelectorAll('.gs-num')[3].textContent = hybrid;

    if (devices.length === 0) {
      container.innerHTML = '<div class="empty"><div class="empty-icon">⟐</div><div class="empty-text">No devices yet</div><div class="empty-sub">Route a device through the proxy to begin learning.</div></div>';
      return;
    }
    container.innerHTML = devices.map(d => {
      const rate = d.match_rate_pct !== undefined ? d.match_rate_pct : null;
          const isUnknown = d.device_id && d.device_id.startsWith('ip-');
          const unkClass = isUnknown ? ' unidentified' : '';
          const unkBadge = isUnknown ? '<span class="badge badge-hybrid">⚠ Unidentified</span>' : modeBadge(d.mode);
          return `<div class="device-card${unkClass}" onclick="loadDeviceStats('${d.device_id}')">
            <div class="dc-top">
              <div><div class="dc-name">${d.name || shortId(d.device_id)}</div><div class="dc-id">${shortId(d.device_id)}</div></div>
              ${unkBadge}
            </div>
            <div class="dc-meta">${d.vendor || 'unknown'} · ${d.device_type || 'generic'}</div>
            <div class="dc-stats">
              ${rate !== null ? `<span class="dc-stat"><span class="dc-label">Match:</span><span class="dc-value" style="color:${rateColor(rate)}">${rate}%</span></span>` : ''}
              <span class="dc-stat"><span class="dc-label">Last:</span><span class="dc-value">${d.last_seen ? new Date(d.last_seen).toLocaleString() : 'Never'}</span></span>
            </div>
          </div>`;
        }).join('');
  } catch(e) {
    document.getElementById('devices').innerHTML = '<div class="empty"><div class="empty-icon">⚠</div><div class="empty-text">Error loading devices</div><div class="empty-sub">Is the server running?</div></div>';
    showToast('Failed to load devices', 'error');
  }
}

// ── Load device detail ───────────────────────────────────────────────────────
async function loadDeviceStats(deviceId) {
  try {
    const res = await fetch(`/api/devices/${deviceId}/stats`);
    const stats = await res.json();
    const s = stats.stats, pct = s.match_rate_pct || 0, rc = rateClass(pct);
    const detail = document.getElementById('details');
    detail.classList.remove('invisible');

    // SVG ring
    const circum = 2 * Math.PI * 44;
    const offset = circum - (pct / 100) * circum;
    const ringColor = rateColor(pct);
    const ring = `<div class="ring-wrap"><svg class="ring-svg" viewBox="0 0 100 100">
      <circle class="ring-bg" cx="50" cy="50" r="44"/>
      <circle class="ring-fg" cx="50" cy="50" r="44" stroke="${ringColor}" stroke-dasharray="${circum}" stroke-dashoffset="${offset}"/>
    </svg><div class="ring-label"><span class="pct" style="color:${ringColor}">${pct}%</span><span class="pct-label">Match Rate</span></div></div>`;

    // Sparkline
    const recent = (s.recent_results || []).slice(-80);
    let spark = '';
    if (recent.length > 0) {
      spark = '<div class="sparkline-wrap"><div class="sparkline-label">Recent Activity</div><div class="chart">';
      recent.forEach(r => {
        const cls = r.result === 'local_hit' ? 'hit' : r.result === 'cloud_miss' ? 'miss' : 'error';
        spark += `<div class="bar ${cls}" style="height:40px"></div>`;
      });
      const hits = recent.filter(r => r.result === 'local_hit').length;
      const misses = recent.filter(r => r.result === 'cloud_miss').length;
      const errs = recent.filter(r => r.result !== 'local_hit' && r.result !== 'cloud_miss').length;
      spark += '</div><div class="legend">';
      if (hits) spark += `<span class="legend-item"><span class="legend-dot" style="background:var(--green)"></span>${hits} hits</span>`;
      if (misses) spark += `<span class="legend-item"><span class="legend-dot" style="background:var(--blue)"></span>${misses} misses</span>`;
      if (errs) spark += `<span class="legend-item"><span class="legend-dot" style="background:var(--red)"></span>${errs} errors</span>`;
      spark += '</div></div>';
    }

    detail.innerHTML = `<div class="detail-card">
      <div class="detail-header">
        <h2>${s.name || deviceId} — Details</h2>
        <button class="close-btn" onclick="closeDetails()">&times;</button>
      </div>
      ${ring}
      <div class="stats-grid">
        <div class="stat-box"><div class="num green">${s.local_hits}</div><div class="lbl">Local Hits</div></div>
        <div class="stat-box"><div class="num blue">${s.cloud_misses}</div><div class="lbl">Cloud Misses</div></div>
        <div class="stat-box"><div class="num red">${s.errors}</div><div class="lbl">Errors</div></div>
        <div class="stat-box"><div class="num yellow">${s.patterns_learned}</div><div class="lbl">Patterns</div></div>
      </div>
      <div class="detail-rows">
        <div class="detail-row"><span class="dr-label">Total Requests</span><span class="dr-value">${s.total_requests}</span></div>
        <div class="detail-row"><span class="dr-label">Mode</span>${modeBadge(s.mode)}</div>
        <div class="detail-row"><span class="dr-label">Threshold</span><span class="dr-value">${(s.match_threshold * 100).toFixed(0)}%</span></div>
        <div class="detail-row"><span class="dr-label">Buffer</span><span class="dr-value">${(s.current_buffer_size_bytes / 1024).toFixed(1)} KB / ${(s.context_buffer_size / 1024).toFixed(0)} KB</span></div>
        <div class="detail-row"><span class="dr-label">Flushes</span><span class="dr-value">${s.buffer_flushes}</span></div>
        <div class="detail-row"><span class="dr-label">Templates</span><span class="dr-value">${s.templates_created}</span></div>
      </div>
      ${spark}
      <div class="mode-area">
        <label>Mode:</label>
        <select id="mode-select-${deviceId}">
          <option value="learning" ${s.mode === 'learning' ? 'selected' : ''}>Cloud — Learn All</option>
          <option value="production" ${s.mode === 'production' ? 'selected' : ''}>Local — Serve All</option>
          <option value="hybrid" ${s.mode === 'hybrid' ? 'selected' : ''}>Hybrid — Local then Cloud</option>
        </select>
        <button class="btn btn-primary" onclick="switchMode('${deviceId}')">Apply</button>
      </div>

            <!-- TLS Config Panel (for all devices) -->
            <div class="tls-config">
              <h3>🔐 TLS Interception</h3>
              <div class="tc-row"><span class="tc-label">Device ID</span><span class="tc-value">${shortId(deviceId)}</span></div>
              <div class="tc-row"><span class="tc-label">Name</span><input type="text" id="tc-name-${deviceId}" value="${s.name || deviceId}"></div>
              <div class="tc-row"><span class="tc-label">Vendor / Adapter</span>
                <select id="tc-vendor-${deviceId}">
                  <option value="unknown" ${(s.vendor || 'unknown') === 'unknown' ? 'selected' : ''}>unknown</option>
                </select>
              </div>
              <div class="tc-row"><span class="tc-label">Passthrough to Cloud</span>
                <label class="tc-toggle">
                  <input type="checkbox" id="tc-passthrough-${deviceId}" checked>
                  <span class="tc-slider"></span>
                </label>
              </div>
              <div class="tc-row"><span class="tc-label">Pinning Bypass</span>
                <select id="tc-pinning-${deviceId}">
                  <option value="mitm_proxy">mitm_proxy (default)</option>
                  <option value="frida">frida (script)</option>
                  <option value="disable_pin_check">disable_pin_check</option>
                </select>
              </div>
              <div class="tc-row">
                <span class="tc-label">⬇️ CA Certificate</span>
                <button class="btn btn-ghost" onclick="downloadCaCert()" style="padding:4px 10px;font-size:12px;">Download</button>
              </div>
              <div style="text-align:right;margin-top:8px;">
                <button class="btn btn-primary" onclick="saveTlsConfig('${deviceId}')" style="padding:6px 16px;font-size:12px;">Save TLS Config</button>
              </div>
            </div>
          </div>`;
  } catch(e) { showToast('Failed to load device details', 'error'); }
}

function closeDetails() { document.getElementById('details').classList.add('invisible'); }

async function switchMode(deviceId) {
  const select = document.getElementById('mode-select-' + deviceId);
  const mode = select.value;
  try {
    await fetch(`/api/devices/${deviceId}/mode`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({mode}),
    });
    showToast('Mode switched to ' + mode, 'success');
    loadDeviceStats(deviceId);
    loadDevices();
  } catch(e) { showToast('Failed to switch mode', 'error'); }
}

loadDevices();
loadTlsBar();
loadVendors();
setInterval(loadDevices, 8000);
setInterval(loadTlsBar, 15000);

// ── Load vendors list for TLS config dropdown ─────────────────────────────
async function loadVendors() {
  try {
    // Get the list from the first device's detail or from a known endpoint
    // For now, populate from /health which lists adapters
    const res = await fetch('/health');
    if (!res.ok) return;
    const h = await res.json();
    const adapters = h.adapters || [];
    if (adapters.length === 0) return;
    // Update any vendor select dropdown on the page
    document.querySelectorAll('[id^="tc-vendor-"]').forEach(sel => {
      adapters.forEach(v => {
        if (!sel.querySelector(`option[value="${v}"]`)) {
          const opt = document.createElement('option');
          opt.value = v; opt.textContent = v;
          sel.appendChild(opt);
        }
      });
    });
  } catch(e) { /* ignore */ }
}

// ── TLS: Load monitored ports bar ──────────────────────────────────────────
async function loadTlsBar() {
  try {
    const res = await fetch('/api/tls/ports');
    const data = await res.json();
    const container = document.getElementById('tlsBadges');
    const status = document.getElementById('tlsStatus');
    if (!data.enabled) {
      container.innerHTML = '<span class="tb-badge tb-inactive">disabled</span>';
      status.textContent = 'TLS MITM off';
      return;
    }
    const ports = data.ports || [];
    container.innerHTML = ports.map(p => `<span class="tb-badge tb-active">${p} <button class="tb-btn" onclick="removeTlsPort(${p})">&times;</button></span>`).join('');
    status.textContent = `${ports.length} port${ports.length !== 1 ? 's' : ''}`;
  } catch(e) {
    document.getElementById('tlsBadges').innerHTML = '<span class="tb-badge tb-inactive">error</span>';
  }
}

async function addTlsPort() {
  const input = document.getElementById('newPortInput');
  const port = parseInt(input.value);
  if (!port || port < 1 || port > 65535) { showToast('Invalid port', 'error'); return; }
  try {
    const res = await fetch('/api/tls/ports', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({port}),
    });
    if (!res.ok) { showToast('Failed to add port', 'error'); return; }
    showToast('Added port ' + port, 'success');
    input.value = '';
    loadTlsBar();
  } catch(e) { showToast('Failed to add port', 'error'); }
}

async function removeTlsPort(port) {
  try {
    const res = await fetch(`/api/tls/ports/${port}`, { method: 'DELETE' });
    if (!res.ok) { showToast('Failed to remove port', 'error'); return; }
    showToast('Removed port ' + port, 'success');
    loadTlsBar();
  } catch(e) { showToast('Failed to remove port', 'error'); }
}

function downloadCaCert() {
  window.open('/api/tls/ca-cert', '_blank');
}

async function saveTlsConfig(deviceId) {
  const name = document.getElementById('tc-name-' + deviceId).value;
  const vendor = document.getElementById('tc-vendor-' + deviceId).value;
  const passthrough = document.getElementById('tc-passthrough-' + deviceId).checked;
  const pinning = document.getElementById('tc-pinning-' + deviceId).value;
  try {
    const res = await fetch(`/api/devices/${deviceId}/tls-config`, {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, vendor, passthrough, pinning_bypass: pinning}),
    });
    if (!res.ok) { showToast('Failed to save config', 'error'); return; }
    showToast('TLS config saved', 'success');
    loadDeviceStats(deviceId);
    loadDevices();
  } catch(e) { showToast('Failed to save config', 'error'); }
}
</script>
</body>
</html>"""


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
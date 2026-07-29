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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global db_manager, adapter_registry, orchestrator, llm_decipher_service

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

    # Start config hot-reload
    config_manager.start_watching()

    logger.info(f"Server started on {config.proxy.host}:{config.proxy.port}")
    logger.info(f"Registered adapters: {adapter_registry.list_vendors()}")

    yield

    # Cleanup
    logger.info("Shutting down...")
    config_manager.stop_watching()
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
      return `<div class="device-card" onclick="loadDeviceStats('${d.device_id}')">
        <div class="dc-top">
          <div><div class="dc-name">${d.name || shortId(d.device_id)}</div><div class="dc-id">${shortId(d.device_id)}</div></div>
          ${modeBadge(d.mode)}
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
setInterval(loadDevices, 8000);
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
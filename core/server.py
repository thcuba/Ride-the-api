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
from datetime import datetime
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
        timestamp=asyncio.get_running_loop().time(),
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
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }
  h1 { color: #58a6ff; margin-bottom: 4px; }
  .subtitle { color: #8b949e; margin-bottom: 24px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; margin-bottom: 16px; }
  .card h2 { color: #58a6ff; font-size: 16px; margin-bottom: 12px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; }
  .device-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; cursor: pointer; transition: border-color 0.2s; }
  .device-card:hover { border-color: #58a6ff; }
  .device-name { font-size: 16px; font-weight: 600; color: #c9d1d9; }
  .device-id { font-size: 12px; color: #8b949e; margin-bottom: 8px; }
  .stat-row { display: flex; justify-content: space-between; padding: 4px 0; font-size: 13px; }
  .stat-label { color: #8b949e; }
  .stat-value { color: #c9d1d9; font-weight: 500; }
  .match-rate-big { font-size: 32px; font-weight: 700; text-align: center; padding: 12px; }
  .match-rate-big.good { color: #3fb950; }
  .match-rate-big.warning { color: #d29922; }
  .match-rate-big.danger { color: #f85149; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; white-space: nowrap; }
  .badge-learning { background: #1f6feb22; color: #58a6ff; border: 1px solid #1f6feb; }
  .badge-production { background: #3fb95022; color: #3fb950; border: 1px solid #3fb950; }
  .badge-hybrid { background: #d2992222; color: #d29922; border: 1px solid #d29922; }
  .badge-local { background: #3fb95022; color: #3fb950; border: 1px solid #3fb950; }
  .badge-cloud { background: #1f6feb22; color: #58a6ff; border: 1px solid #1f6feb; }
  button { background: #238636; color: #fff; border: none; padding: 6px 16px; border-radius: 6px; cursor: pointer; font-size: 13px; }
  button:hover { background: #2ea043; }
  button.danger { background: #da3633; }
  button.danger:hover { background: #f85149; }
  select { background: #0d1117; color: #c9d1d9; border: 1px solid #30363d; padding: 4px 8px; border-radius: 4px; font-size: 13px; }
  #refresh { position: fixed; top: 20px; right: 20px; }
  .empty { color: #8b949e; text-align: center; padding: 40px; font-style: italic; }
  .mode-switch { display: flex; gap: 8px; align-items: center; margin-top: 12px; flex-wrap: wrap; }
  .progress-bar { background: #21262d; border-radius: 8px; height: 12px; overflow: hidden; margin: 8px 0; }
  .progress-fill { height: 100%; border-radius: 8px; transition: width 0.5s; }
  .progress-fill.good { background: #3fb950; }
  .progress-fill.warning { background: #d29922; }
  .progress-fill.danger { background: #f85149; }
  .mini-chart { display: flex; gap: 2px; align-items: flex-end; height: 40px; margin: 8px 0; }
  .mini-chart .bar { width: 6px; border-radius: 2px 2px 0 0; flex-shrink: 0; }
  .bar.hit { background: #3fb950; }
  .bar.miss { background: #58a6ff; }
  .bar.error { background: #f85149; }
  .stats-summary { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 12px 0; }
  .stat-tile { text-align: center; padding: 8px; background: #0d1117; border-radius: 6px; }
  .stat-tile .num { font-size: 20px; font-weight: 700; }
  .stat-tile .lbl { font-size: 11px; color: #8b949e; }
  .stat-tile .num.green { color: #3fb950; }
  .stat-tile .num.blue { color: #58a6ff; }
  .stat-tile .num.red { color: #f85149; }
  .stat-tile .num.orange { color: #d29922; }
  .chart-container { width: 100%; overflow-x: auto; }
  .invisible { display: none; }
  .detail-header { display: flex; justify-content: space-between; align-items: center; }
  .close-btn { background: none; border: 1px solid #30363d; color: #8b949e; font-size: 18px; cursor: pointer; padding: 2px 10px; border-radius: 4px; }
  .close-btn:hover { color: #f85149; border-color: #f85149; }
</style>
</head>
<body>
<h1>Ride the API</h1>
<p class="subtitle">Local Cloud Replacement — Device Protocol Learning &amp; Response Dashboard</p>
<button id="refresh" onclick="loadDevices()">Refresh</button>
<div id="devices" class="grid"><div class="empty">Loading devices...</div></div>
<div id="details" class="invisible"></div>

<script>
// ── Helpers ──────────────────────────────────────────────────────────────────
function rateClass(pct) {
  if (pct >= 80) return 'good';
  if (pct >= 50) return 'warning';
  return 'danger';
}
function modeBadge(mode) {
  const cls = mode === 'production' ? 'badge-production' : mode === 'hybrid' ? 'badge-hybrid' : 'badge-learning';
  return `<span class="badge ${cls}">${mode}</span>`;
}
function shortId(id) { return id.length > 30 ? id.slice(0, 14) + '…' : id; }

// ── Load device list ─────────────────────────────────────────────────────────
async function loadDevices() {
  try {
    const res = await fetch('/api/devices');
    const data = await res.json();
    const container = document.getElementById('devices');
    if (!data.devices || data.devices.length === 0) {
      container.innerHTML = '<div class="empty">No devices registered yet. Route a device through the proxy to begin.</div>';
      return;
    }
    container.innerHTML = data.devices.map(d => {
      const rate = d.match_rate_pct !== undefined ? d.match_rate_pct : null;
      return `<div class="device-card" onclick="loadDeviceStats('${d.device_id}')">
        <div class="device-name">${d.name || shortId(d.device_id)}</div>
        <div class="device-id">${d.device_id} · ${d.vendor} · ${d.device_type}</div>
        <div class="stat-row"><span class="stat-label">Mode</span>${modeBadge(d.mode)}</div>
        ${rate !== null ? `<div class="stat-row"><span class="stat-label">Match Rate</span><span class="stat-value ${rateClass(rate)}">${rate}%</span></div>` : ''}
        <div class="stat-row"><span class="stat-label">Last Seen</span><span class="stat-value">${d.last_seen ? new Date(d.last_seen).toLocaleString() : 'Never'}</span></div>
      </div>`;
    }).join('');
  } catch(e) {
    document.getElementById('devices').innerHTML = '<div class="empty">Error loading devices. Is the server running?</div>';
  }
}

// ── Load device detail ───────────────────────────────────────────────────────
async function loadDeviceStats(deviceId) {
  try {
    const res = await fetch(`/api/devices/${deviceId}/stats`);
    const stats = await res.json();
    const s = stats.stats;
    const rc = rateClass(s.match_rate_pct);
    const detail = document.getElementById('details');
    detail.classList.remove('invisible');

    // Build mini sparkline from recent_results
    let sparkline = '';
    const recent = (s.recent_results || []).slice(-80);
    if (recent.length > 0) {
      sparkline = '<div class="chart-container"><div class="mini-chart">';
      const maxVal = Math.max(...recent.map(r => 1));
      recent.forEach(r => {
        const cls = r.result === 'local_hit' ? 'hit' : r.result === 'cloud_miss' ? 'miss' : 'error';
        sparkline += `<div class="bar ${cls}" style="height:${Math.max(4, 40 * (1 / maxVal))}px"></div>`;
      });
      sparkline += '</div></div>';
    }

    // Progress bar
    const pct = s.match_rate_pct || 0;
    const progressColor = rc;

    detail.innerHTML = `<div class="card">
      <div class="detail-header">
        <h2>${s.name || deviceId} — Details</h2>
        <button class="close-btn" onclick="closeDetails()">&times;</button>
      </div>
      <div class="match-rate-big ${rc}">${pct}% <span style="font-size:14px;font-weight:400;">Match Rate</span></div>
      <div class="progress-bar"><div class="progress-fill ${progressColor}" style="width:${pct}%"></div></div>

      <div class="stats-summary">
        <div class="stat-tile"><div class="num green">${s.local_hits}</div><div class="lbl">Local Hits</div></div>
        <div class="stat-tile"><div class="num blue">${s.cloud_misses}</div><div class="lbl">Cloud Misses</div></div>
        <div class="stat-tile"><div class="num red">${s.errors}</div><div class="lbl">Errors</div></div>
        <div class="stat-tile"><div class="num orange">${s.patterns_learned}</div><div class="lbl">Patterns</div></div>
      </div>

      <div class="stat-row"><span class="stat-label">Total Requests</span><span class="stat-value">${s.total_requests}</span></div>
      <div class="stat-row"><span class="stat-label">Mode</span>${modeBadge(s.mode)}</div>
      <div class="stat-row"><span class="stat-label">Match Threshold</span><span class="stat-value">${(s.match_threshold * 100).toFixed(0)}%</span></div>
      <div class="stat-row"><span class="stat-label">Buffer</span><span class="stat-value">${(s.current_buffer_size_bytes / 1024).toFixed(1)} KB / ${(s.context_buffer_size / 1024).toFixed(0)} KB</span></div>
      <div class="stat-row"><span class="stat-label">Buffer Flushes</span><span class="stat-value">${s.buffer_flushes}</span></div>
      <div class="stat-row"><span class="stat-label">Templates</span><span class="stat-value">${s.templates_created}</span></div>

      ${sparkline}

      <div class="mode-switch">
        <label style="font-size:13px;color:#8b949e;">Mode:</label>
        <select id="mode-select-${deviceId}">
          <option value="learning" ${s.mode === 'learning' ? 'selected' : ''}>Cloud — Learn All</option>
          <option value="production" ${s.mode === 'production' ? 'selected' : ''}>Local — Serve All</option>
          <option value="hybrid" ${s.mode === 'hybrid' ? 'selected' : ''}>Hybrid — Local then Cloud</option>
        </select>
        <button onclick="switchMode('${deviceId}')">Apply</button>
      </div>
    </div>`;
  } catch(e) { /* ignore */ }
}

function closeDetails() {
  document.getElementById('details').classList.add('invisible');
}

async function switchMode(deviceId) {
  const select = document.getElementById('mode-select-' + deviceId);
  const mode = select.value;
  await fetch(`/api/devices/${deviceId}/mode`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({mode}),
  });
  loadDeviceStats(deviceId);
  loadDevices();
}

// ── Auto-refresh ─────────────────────────────────────────────────────────────
loadDevices();
setInterval(loadDevices, 5000);
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
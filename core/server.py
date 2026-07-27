"""
Edge HVAC Proxy Server - Main entry point.
DNS Interception Proxy for multi-vendor HVAC devices.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from core.config import get_config_manager
from core.database import DatabaseManager, Base
from core.safety import SafetyEngine, DEFAULT_SAFETY_CONFIG
from core.traffic_analysis import (
    TrafficAnalyzer, RequestContext, ResponseRecord, 
    TrafficSource, ProcessingMode, ResponseMatchType
)
from core.traffic_selector import get_traffic_selector, TrafficSelector, TrafficRule
from core.correlation import get_correlation_engine, RequestResponsePair
from core.llm_decipher import get_llm_decipher, DecipherResult
from core.modification import get_modification_engine, ModificationRule, ModificationType
from adapters import get_registered_registry
from adapters.base import (
    ProtocolAdapterRegistry, InterceptedRequest, ProtocolType, 
    Command, CommandType, VendorProtocolAdapter
)
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# Global instances
db_manager: DatabaseManager | None = None
adapter_registry: ProtocolAdapterRegistry | None = None
safety_engine: SafetyEngine | None = None
traffic_analyzer: TrafficAnalyzer | None = None
traffic_selector = None
correlation_engine = None
llm_decipher_service = None
modification_engine = None
traffic_selector: TrafficSelector | None = None
modification_engine = None
llm_decipher_service = None
config_manager = get_config_manager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global db_manager, adapter_registry, safety_engine, traffic_analyzer
    global traffic_selector, modification_engine, llm_decipher_service
    
    logger.info("Starting Edge HVAC Proxy Server...")
    
    # Load configuration
    config = config_manager.config
    
    # Initialize database
    data_dir = Path(config.core.vendor_db_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    
    db_manager = DatabaseManager(
        core_db_url=config.core.database_url,
        vendor_db_dir=data_dir,
        vendor_db_urls=config.core.vendor_databases,
        echo=config.observability.logging.level == "DEBUG",
    )
    await db_manager.initialize()
    
    # Initialize safety engine
    safety_engine = SafetyEngine(config.control.safety.__dict__ if hasattr(config.control, 'safety') else DEFAULT_SAFETY_CONFIG)
    safety_engine.initialize()
    
    # Initialize traffic analyzer
    traffic_analyzer = TrafficAnalyzer(db_manager=db_manager)
    await traffic_analyzer.start()
    
    # Initialize traffic selector
    traffic_selector = get_traffic_selector()
    logger.info(f"Traffic selector loaded with {len(traffic_selector.get_rules())} rules")
    
    # Initialize modification engine
    from core.modification import get_modification_engine
    modification_engine = get_modification_engine()
    logger.info(f"Modification engine loaded with {len(modification_engine.get_rules())} rules")
    
    # Initialize LLM decipher service
    from core.llm_decipher import get_llm_decipher
    llm_decipher_service = get_llm_decipher()
    if llm_decipher_service:
        profiles = llm_decipher_service.list_profiles()
        logger.info(f"LLM decipher service loaded with profiles: {profiles}")
    
    # Initialize global services
        from core.traffic_selector import get_traffic_selector
        from core.correlation import get_correlation_engine
        from core.llm_decipher import get_llm_decipher
        from core.modification import get_modification_engine
    
        global traffic_selector, correlation_engine, llm_decipher_service, modification_engine
    
        # Initialize traffic selector
        traffic_selector = get_traffic_selector()
        logger.info(f"Traffic selector loaded with {len(traffic_selector.get_rules())} rules")
    
        # Initialize correlation engine
        correlation_engine = get_correlation_engine()
        logger.info("Correlation engine initialized")
    
        # Initialize modification engine
        modification_engine = get_modification_engine()
        logger.info(f"Modification engine loaded with {len(modification_engine.get_rules())} rules")
    
        # Initialize LLM decipher service
        llm_decipher_service = get_llm_decipher()
        if llm_decipher_service:
            profiles = llm_decipher_service.list_profiles()
            logger.info(f"LLM decipher service loaded with profiles: {profiles}")
    
        # Register adapters
        adapter_registry = get_registered_registry()
    
        # Start config hot-reload
        config_manager.start_watching()
    
        logger.info(f"Server started on {config.proxy.host}:{config.proxy.port}")
        logger.info(f"Registered vendors: {adapter_registry.list_vendors()}")
        logger.info(f"Safety rules loaded: {len(safety_engine.rules)} global + vendor-specific")
    
        yield
    
        # Cleanup
        logger.info("Shutting down...")
        config_manager.stop_watching()
        if traffic_analyzer:
            await traffic_analyzer.stop()
        if llm_decipher_service:
            await llm_decipher_service.close()
        if db_manager:
            await db_manager.close()
        logger.info("Shutdown complete")


app = FastAPI(
    title="Edge HVAC Proxy",
    description="DNS Interception Proxy for Multi-Vendor HVAC Edge AI",
    version="0.1.0",
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
        "service": "edge-hvac-proxy",
        "version": "0.1.0",
        "vendors": adapter_registry.list_vendors() if adapter_registry else [],
    }


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    # TODO: Implement Prometheus metrics
    return Response(content="# Metrics not yet implemented", media_type="text/plain")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN PROXY ENDPOINT - Catches all vendor requests
# ═══════════════════════════════════════════════════════════════════════════════

from datetime import datetime

@app.api_route("/{vendor}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy_vendor_request(vendor: str, path: str, request: Request):
    """
    Main proxy endpoint for vendor API requests.
    
    Routes:
    - /ty/* -> TY (Tuya) adapter
    - /tl/* -> TL (TP-Link) adapter
    - /zh/* -> ZH (Zehnder) adapter
    - /hr/* -> HR (Haier) adapter
    """
    if not adapter_registry:
        return JSONResponse(
            status_code=503,
            content={"error": "Service not ready"},
        )
    
    adapter = adapter_registry.get_adapter(vendor)
    if not adapter:
        return JSONResponse(
            status_code=404,
            content={"error": f"Vendor '{vendor}' not supported"},
        )
    
    # Build intercepted request
    body = await _get_request_body(request)
    
    # Determine traffic source (local vs internet)
    client_ip = request.client.host if request.client else "unknown"
    # Simple heuristic: local if private IP range
    is_local = _is_local_ip(client_ip)
    source = TrafficSource.LOCAL_NETWORK if is_local else TrafficSource.INTERNET
    
    intercepted = InterceptedRequest(
        device_id="",  # Will be extracted by adapter
        timestamp=asyncio.get_event_loop().time(),
        protocol=ProtocolType.HTTPS if request.url.scheme == "https" else ProtocolType.HTTP,
        method=request.method,
        path=f"/{path}",
        headers=dict(request.headers),
        query_params=dict(request.query_params),
        body=body,
    )
    
    try:
        # Parse request to extract intent
        intercepted = await adapter.parse_request(intercepted)
        
        # Log intercepted request to vendor DB
        await _log_intercepted_request(vendor, intercepted)
        
        # Get device state and info for safety checks
        device_state = await adapter.get_device_state(intercepted.device_id) if intercepted.device_id else None
        device_info = await adapter.get_device_info(intercepted.device_id) if intercepted.device_id else None
        
        # Prepare context for safety engine
        safety_context = {
            "source": source.value,
            "client_ip": client_ip,
        }
        
        # Handle locally (edge inference) - but first check safety if it's a command
        if intercepted.parsed_intent in (
            "set_temperature", "set_mode", "set_fan_speed", "set_swing",
            "turn_on", "turn_off", "set_schedule"
        ):
            from adapters.base import Command, CommandType
            
            # Map parsed intent to CommandType
            intent_map = {
                "set_temperature": CommandType.SET_TEMPERATURE,
                "set_mode": CommandType.SET_MODE,
                "set_fan_speed": CommandType.SET_FAN_SPEED,
                "set_swing": CommandType.SET_SWING,
                "turn_on": CommandType.TURN_ON,
                "turn_off": CommandType.TURN_OFF,
                "set_schedule": CommandType.SET_SCHEDULE,
            }
            
            cmd_type = intent_map.get(intercepted.parsed_intent, CommandType.UNKNOWN)
            if cmd_type != CommandType.UNKNOWN:
                command = Command(
                    device_id=intercepted.device_id,
                    command_type=cmd_type,
                    params=intercepted.parsed_params,
                    source="edge_auto",
                )
                
                # Run safety checks
                safety_result = await safety_engine.check_command(
                    command, device_state, device_info, safety_context
                )
                
                if not safety_result.allowed:
                    # Blocked by safety
                    return JSONResponse(
                        status_code=403,
                        content={
                            "error": "Command blocked by safety rules",
                            "violations": [
                                {"type": v.violation_type.value, "message": v.message, "action": v.action.value}
                                for v in safety_result.violations
                            ],
                        },
                    )
                
                # Use modified command if safety modified it
                if safety_result.modified_command:
                    command = safety_result.modified_command
                
                # Execute command through adapter
                result = await adapter.send_command(intercepted.device_id, command)
                
                # Record command for rate limiting/conflict detection
                safety_engine.record_command(intercepted.device_id, command.command_type)
                
                # Track compliance if we have actual response
                if device_state:
                    await traffic_analyzer.track_device_command(
                        device_id=intercepted.device_id,
                        vendor=vendor,
                        command_sent=command.params,
                        expected_state=_command_to_expected_state(command),
                        actual_response=result.response,
                        latency_ms=result.response.get("latency_ms", 0) if result.response else 0,
                    )
                
                response_data = await adapter.build_response(intercepted, result)
                
                # Traffic analysis: compare with cloud if available
                if config_manager.config.proxy.fallback.enabled and result.success:
                    cloud_result = await adapter.forward_to_cloud(intercepted)
                    if cloud_result.success:
                        edge_response = ResponseRecord(
                            source="edge",
                            status_code=200,
                            headers={},
                            body=response_data,
                            latency_ms=0,
                            timestamp=datetime.utcnow(),
                        )
                        cloud_response = ResponseRecord(
                            source="cloud",
                            status_code=200,
                            headers={},
                            body=cloud_result.response,
                            latency_ms=0,
                            timestamp=datetime.utcnow(),
                        )
                        
                        request_context = RequestContext(
                            request_id=intercepted.device_id,
                            device_id=intercepted.device_id,
                            vendor=vendor,
                            device_type=device_info.device_type if device_info else "unknown",
                            source=source,
                            timestamp=datetime.utcnow(),
                            protocol=intercepted.protocol.value,
                            method=intercepted.method,
                            path=intercepted.path,
                            headers=intercepted.headers,
                            body=intercepted.body,
                            query_params=intercepted.query_params,
                        )
                        
                        await traffic_analyzer.analyze_request(
                            request_context, edge_response, cloud_response
                        )
                
                return JSONResponse(content=response_data)
        
        # For non-command requests (GET_STATE, etc.), handle normally
        result = await adapter.handle_request(intercepted)
        
        # Build vendor-compatible response
        response_data = await adapter.build_response(intercepted, result)
        
        return JSONResponse(content=response_data)
        
    except Exception as e:
        logger.exception(f"Error processing request for {vendor}: {e}")
        
        # Fallback to cloud
        if config_manager.config.proxy.fallback.enabled:
            try:
                result = await adapter.forward_to_cloud(intercepted)
                response_data = await adapter.build_response(intercepted, result)
                return JSONResponse(content=response_data)
            except Exception as fallback_error:
                logger.exception(f"Fallback to cloud failed: {fallback_error}")
        
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error", "detail": str(e)},
        )


    async def _forward_to_cloud(vendor: str, path: str, request: Request) -> JSONResponse:
        """Forward request to real cloud API."""
        adapter = adapter_registry.get_adapter(vendor)
        if not adapter:
            return JSONResponse(status_code=404, content={"error": f"Vendor '{vendor}' not supported"})
    
        body = await _get_request_body(request)
    
        intercepted = InterceptedRequest(
            device_id="",
            timestamp=asyncio.get_event_loop().time(),
            protocol=ProtocolType.HTTPS if request.url.scheme == "https" else ProtocolType.HTTP,
            method=request.method,
            path=f"/{path}",
            headers=dict(request.headers),
            query_params=dict(request.query_params),
            body=body,
        )
    
        intercepted = await adapter.parse_request(intercepted)
        result = await adapter.forward_to_cloud(intercepted)
        response_data = await adapter.build_response(intercepted, result)
    
        return JSONResponse(content=response_data, status_code=result.response.get("status_code", 200) if result.response else 200)


    async def _process_llm_deciphering(pair, vendor: str):
        """Background task to process LLM deciphering for a request/response pair."""
        try:
            if not llm_decipher_service or not pair:
                return
        
            # Get database schema for vendor
            from core.llm_decipher import DecipherRequest
        
            db_schema = llm_decipher_service._get_db_schema(vendor)
        
            # Get recent patterns for context
            recent_patterns = llm_decipher_service._get_recent_patterns(vendor, None)
        
            # Build decipher request
            decipher_request = DecipherRequest(
                vendor=vendor,
                device_type=pair.request.metadata.get('device_type', 'unknown'),
                pairs=[{
                    'request': {
                        'method': pair.request.method,
                        'path': pair.request.path,
                        'headers': pair.request.headers,
                        'body': pair.request.body,
                        'query_params': pair.request.query_params,
                    },
                    'response': {
                        'status_code': 200,
                        'headers': pair.response.headers if pair.response else {},
                        'body': pair.response.body if pair.response else {},
                    }
                }],
                db_schema=db_schema,
                recent_patterns=recent_patterns,
            )
        
            # Decipher
            result = await llm_decipher_service.decipher(decipher_request)
        
            # Store result back in pair
            if pair.llm_analysis is None:
                pair.llm_analysis = {}
            pair.llm_analysis.update({
                'intent': result.intent,
                'fields': result.fields,
                'confidence': result.confidence,
                'suggested_dp_codes': result.suggested_dp_codes,
                'protocol_notes': result.protocol_notes,
            })
        
            logger.info(f"LLM deciphered pair {pair.pair_id}: intent={result.intent}, confidence={result.confidence:.2f}")
        
        except Exception as e:
            logger.error(f"LLM deciphering failed for pair {pair.pair_id if pair else 'unknown'}: {e}")


    def _is_local_ip(ip: str) -> bool:
        """Check if IP is in private/local range."""
        import ipaddress
        try:
            addr = ipaddress.ip_address(ip)
            return addr.is_private or addr.is_loopback
        except Exception:
            return False


def _command_to_expected_state(command: Command) -> dict:
    """Convert command to expected device state."""
    expected = {}
    if command.command_type == CommandType.SET_TEMPERATURE:
        expected["temp_target"] = command.params.get("temperature")
    elif command.command_type == CommandType.SET_MODE:
        expected["mode"] = command.params.get("mode")
    elif command.command_type == CommandType.SET_FAN_SPEED:
        expected["fan_speed"] = command.params.get("fan_speed")
    elif command.command_type == CommandType.SET_SWING:
        expected["swing"] = command.params.get("swing")
    elif command.command_type == CommandType.TURN_ON:
        expected["on_off"] = True
    elif command.command_type == CommandType.TURN_OFF:
        expected["on_off"] = False
    return expected


async def _get_request_body(request: Request) -> dict | bytes | None:
    """Extract request body."""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        return await request.json()
    elif "application/x-www-form-urlencoded" in content_type:
        return dict(await request.form())
    elif content_type:
        return await request.body()
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# MQTT PROXY ENDPOINT (for MQTT-based vendors like Tuya)
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/mqtt/{vendor}/publish")
async def mqtt_proxy_publish(vendor: str, request: Request):
    """Proxy MQTT publish from device."""
    if not adapter_registry:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    
    adapter = adapter_registry.get_adapter(vendor)
    if not adapter or ProtocolType.MQTT not in adapter.supported_protocols:
        return JSONResponse(status_code=404, content={"error": "MQTT not supported for vendor"})
    
    body = await request.json()
    topic = body.get("topic", "")
    payload = body.get("payload", {})
    qos = body.get("qos", 0)
    retain = body.get("retain", False)
    
    intercepted = InterceptedRequest(
        device_id="",
        timestamp=asyncio.get_event_loop().time(),
        protocol=ProtocolType.MQTT,
        topic=topic,
        qos=qos,
        retain=retain,
        body=payload,
    )
    
    intercepted = await adapter.parse_request(intercepted)
    await _log_intercepted_request(vendor, intercepted)
    result = await adapter.handle_request(intercepted)
    response_data = await adapter.build_response(intercepted, result)
    
    return JSONResponse(content=response_data)


# ═══════════════════════════════════════════════════════════════════════════════
# DEVICE MANAGEMENT API (for dashboard/control)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/devices")
async def list_devices():
    """List all registered devices."""
    if not db_manager:
        return JSONResponse(status_code=503, content={"error": "DB not ready"})
    
    async with db_manager.core_session() as session:
        from sqlalchemy import select
        from core.database import DeviceRegistry
        
        result = await session.execute(select(DeviceRegistry))
        devices = result.scalars().all()
        
        return [
            {
                "device_id": d.device_id,
                "vendor": d.vendor,
                "device_type": d.device_type,
                "name": d.name,
                "location": d.location,
                "status": d.status,
                "last_seen": d.last_seen.isoformat() if d.last_seen else None,
                "capabilities": d.capabilities,
            }
            for d in devices
        ]


@app.get("/api/devices/{device_id}")
async def get_device(device_id: str):
    """Get device details."""
    if not db_manager:
        return JSONResponse(status_code=503, content={"error": "DB not ready"})
    
    async with db_manager.core_session() as session:
        from sqlalchemy import select
        from core.database import DeviceRegistry
        
        result = await session.execute(
            select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
        )
        device = result.scalar_one_or_none()
        
        if not device:
            return JSONResponse(status_code=404, content={"error": "Device not found"})
        
        # Get recent readings from vendor DB
        vendor_db = device.vendor_db_name
        readings = []
        if vendor_db and db_manager:
            try:
                async with db_manager.vendor_session(vendor_db) as v_session:
                    from sqlalchemy import select, desc
                    from core.database import VendorReading
                    
                    result = await v_session.execute(
                        select(VendorReading)
                        .where(VendorReading.device_id == device_id)
                        .order_by(desc(VendorReading.timestamp))
                        .limit(50)
                    )
                    readings = [
                        {
                            "timestamp": r.timestamp.isoformat(),
                            "temp_target": r.temp_target,
                            "temp_actual": r.temp_actual,
                            "humidity": r.humidity,
                            "power_watts": r.power_watts,
                            "mode": r.mode,
                            "fan_speed": r.fan_speed,
                        }
                        for r in result.scalars().all()
                    ]
            except Exception as e:
                logger.warning(f"Failed to fetch readings: {e}")
        
        return {
            "device_id": device.device_id,
            "vendor": device.vendor,
            "device_type": device.device_type,
            "name": device.name,
            "location": device.location,
            "capabilities": device.capabilities,
            "config": device.config,
            "status": device.status,
            "last_seen": device.last_seen.isoformat() if device.last_seen else None,
            "recent_readings": readings,
        }


@app.post("/api/devices/{device_id}/command")
async def send_device_command(device_id: str, request: Request):
    """Send command to device."""
    if not db_manager or not adapter_registry:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    
    # Get device info
    async with db_manager.core_session() as session:
        from sqlalchemy import select
        from core.database import DeviceRegistry
        
        result = await session.execute(
            select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
        )
        device = result.scalar_one_or_none()
        
        if not device:
            return JSONResponse(status_code=404, content={"error": "Device not found"})
        
        adapter = adapter_registry.get_adapter(device.vendor)
        if not adapter:
            return JSONResponse(status_code=400, content={"error": f"No adapter for vendor {device.vendor}"})
        
        body = await request.json()
        command_type = body.get("command")
        params = body.get("params", {})
        
        from adapters.base import Command, CommandType
        
        try:
            cmd_type = CommandType(command_type)
        except ValueError:
            return JSONResponse(status_code=400, content={"error": f"Unknown command: {command_type}"})
        
        command = Command(
            device_id=device_id,
            command_type=cmd_type,
            params=params,
            source="edge_manual",
        )
        
        result = await adapter.send_command(device_id, command)
        
        return JSONResponse(content={
            "success": result.success,
            "response": result.response,
            "error": result.error,
            "forwarded": result.forwarded,
        })


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

async def _log_intercepted_request(vendor: str, request: InterceptedRequest):
    """Log intercepted request to vendor database."""
    if not db_manager:
        return
    
    try:
        from core.database import VendorInterceptedRequest
        
        async with db_manager.vendor_session(vendor) as session:
            log_entry = VendorInterceptedRequest(
                device_id=request.device_id or "unknown",
                protocol=request.protocol.value,
                method=request.method,
                path=request.path,
                headers=request.headers,
                body=request.body,
                query_params=request.query_params,
                topic=request.topic,
                qos=request.qos,
                retain=request.retain,
                parsed_intent=request.parsed_intent.value if request.parsed_intent else "unknown",
                parsed_params=request.parsed_params,
            )
            session.add(log_entry)
            await session.commit()
    except Exception as e:
        logger.warning(f"Failed to log intercepted request: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    """Main entry point."""
    config = config_manager.config
    
    # Setup signal handlers for graceful shutdown
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    def signal_handler(sig, frame):
        logger.info(f"Received signal {sig}, shutting down...")
        loop.stop()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Run server
    uvicorn.run(
        "server:app",
        host=config.proxy.host,
        port=config.proxy.port,
        log_level=config.observability.logging.level.lower(),
        reload=False,  # Disable reload in production
    )


if __name__ == "__main__":
    main()
"""Protocol servers management routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core.protocol_servers import get_protocol_server_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["protocols"])


def _server():
    from core import server  # noqa: PLC0415 - lazy import avoids a cycle with core.server

    return server


@router.get("/api/protocol-servers")
async def protocol_servers_status():
    """List all protocol servers and their status."""
    manager = get_protocol_server_manager()
    status_list = await manager.get_all_status()
    return {"servers": status_list}


@router.post("/api/protocol-servers/{name}/start")
async def protocol_server_start(name: str):
    """Start a specific protocol server and persist enabled=true in config."""
    manager = get_protocol_server_manager()
    success = await manager.start_plugin(name)
    if not success:
        plugin = manager.get_plugin(name)
        if plugin is None:
            return JSONResponse(status_code=404, content={"error": f"Server '{name}' not found"})
        return JSONResponse(status_code=500, content={"error": f"Failed to start '{name}'"})
    plugin = manager.get_plugin(name)
    if plugin is not None:
        setattr(plugin.config, "enabled", True)
    persisted = _server().config_manager.set_protocol_server_enabled(name, True)
    if not persisted:
        logger.warning("Protocol server %s started but enabling it could not be persisted", name)
    return {"status": "ok", "server": name, "running": True, "persisted": persisted}


@router.post("/api/protocol-servers/{name}/stop")
async def protocol_server_stop(name: str):
    """Stop a specific protocol server and persist enabled=false in config."""
    manager = get_protocol_server_manager()
    success = await manager.stop_plugin(name)
    if not success:
        plugin = manager.get_plugin(name)
        if plugin is None:
            return JSONResponse(status_code=404, content={"error": f"Server '{name}' not found"})
        return JSONResponse(status_code=500, content={"error": f"Failed to stop '{name}'"})
    plugin = manager.get_plugin(name)
    if plugin is not None:
        setattr(plugin.config, "enabled", False)
    persisted = _server().config_manager.set_protocol_server_enabled(name, False)
    if not persisted:
        logger.warning("Protocol server %s stopped but disabling it could not be persisted", name)
    return {"status": "ok", "server": name, "running": False, "persisted": persisted}


@router.get("/api/protocol-servers/{name}/config")
async def protocol_server_config(name: str):
    """Get configuration for a specific protocol server."""
    manager = get_protocol_server_manager()
    plugin = manager.get_plugin(name)
    if not plugin:
        return JSONResponse(status_code=404, content={"error": f"Server '{name}' not found"})
    cfg = plugin.config
    config_dict = cfg.model_dump() if hasattr(cfg, "model_dump") else vars(cfg)
    return {"name": name, "config": config_dict}

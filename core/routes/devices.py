"""Device management, configuration, database binding, and buffer routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import select

from core.atomic_io import sanitize_filename_component
from core.config import ConnectionType
from core.database import DeviceRegistry
from core.pattern_db import buffer_manager
from core.pattern_db.schemas import CaptureDB
from core.pattern_db.validator import validate_capture
from core.routes.common import _client_error

logger = logging.getLogger(__name__)


router = APIRouter(tags=["devices"])


def _server():

    from core import server  # noqa: PLC0415 - lazy import avoids a cycle with core.server

    return server


@router.get("/api/devices")
async def list_devices():
    """List all registered devices."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    devices = await _server().db_manager.list_devices()

    return {"devices": devices}


@router.get("/api/devices/{device_id}")
async def get_device(device_id: str):
    """Get device details and stats."""

    if not _server().db_manager or not _server().orchestrator:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    async with _server().db_manager.core_session() as session:
        result = await session.execute(
            select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
        )

        device = result.scalar_one_or_none()

        if not device:
            return JSONResponse(status_code=404, content={"error": "Device not found"})

    stats = await _server().orchestrator.get_device_stats(device_id)

    return {"device": stats}


@router.get("/api/devices/{device_id}/stats")
async def get_device_stats(device_id: str):
    """Get real-time match statistics for a device."""

    if not _server().orchestrator:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    stats = await _server().orchestrator.get_device_stats(device_id)

    return {"stats": stats}


@router.delete("/api/devices/{device_id}")
async def delete_device(device_id: str):
    """Delete a device and its data (registry row, per-device DB, pattern file)."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    try:
        safe_id = sanitize_filename_component(device_id)

    except ValueError as e:
        return _client_error(e, "Invalid device_id")

    success = await _server().db_manager.delete_device(safe_id)

    if not success:
        return JSONResponse(status_code=404, content={"error": "Device not found"})

    return {"device_id": safe_id, "status": "deleted"}


@router.get("/api/devices/{device_id}/match-rate")
async def get_match_rate(device_id: str):
    """Get current match rate percentage."""

    if not _server().orchestrator:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    stats = await _server().orchestrator.get_device_stats(device_id)

    return {
        "device_id": device_id,
        "match_rate_pct": stats.get("match_rate_pct", 0),
        "total_requests": stats.get("total_requests", 0),
        "local_hits": stats.get("local_hits", 0),
        "cloud_misses": stats.get("cloud_misses", 0),
    }


@router.post("/api/devices/{device_id}/mode")
async def set_device_mode(device_id: str, request: Request):
    """Switch device between learning and production mode."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    body = await request.json()

    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400, content={"error": "Invalid JSON body: expected an object"}
        )

    mode = body.get("mode", "learning")

    if mode not in ("learning", "production", "hybrid", "passthrough"):
        return JSONResponse(
            status_code=400,
            content={
                "error": "Invalid mode. Use 'learning', 'production', 'hybrid', or 'passthrough'"
            },
        )

    success = await _server().db_manager.update_device_mode(device_id, mode)

    if not success:
        return JSONResponse(status_code=404, content={"error": "Device not found"})

    return {"device_id": device_id, "mode": mode}


@router.put("/api/devices/{device_id}/threshold")
async def set_device_threshold(device_id: str, request: Request):  # noqa: PLR0911
    """Set the per-device match confidence threshold (0.0 to 1.0)."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    body = await request.json()

    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400, content={"error": "Invalid JSON body: expected an object"}
        )

    raw = body.get("match_threshold")

    if raw is None:
        return JSONResponse(status_code=400, content={"error": "Provide 'match_threshold'"})

    try:
        threshold = float(raw)

    except (TypeError, ValueError):
        return JSONResponse(
            status_code=400, content={"error": "'match_threshold' must be a number"}
        )

    if not 0.0 <= threshold <= 1.0:
        return JSONResponse(
            status_code=400, content={"error": "'match_threshold' must be between 0.0 and 1.0"}
        )

    success = await _server().db_manager.update_device_match_threshold(device_id, threshold)

    if not success:
        return JSONResponse(status_code=404, content={"error": "Device not found"})

    return {"device_id": device_id, "match_threshold": threshold}


@router.put("/api/devices/{device_id}/context-buffer")
async def set_device_context_buffer(device_id: str, request: Request):  # noqa: PLR0911
    """Set the per-device context buffer size in bytes."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    body = await request.json()

    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400, content={"error": "Invalid JSON body: expected an object"}
        )

    raw = body.get("context_buffer_size")

    if raw is None:
        return JSONResponse(status_code=400, content={"error": "Provide 'context_buffer_size'"})

    try:
        size = int(raw)

    except (TypeError, ValueError):
        return JSONResponse(
            status_code=400, content={"error": "'context_buffer_size' must be an integer"}
        )

    if size <= 0:
        return JSONResponse(
            status_code=400, content={"error": "'context_buffer_size' must be positive"}
        )

    success = await _server().db_manager.update_device_context_buffer_size(device_id, size)

    if not success:
        return JSONResponse(status_code=404, content={"error": "Device not found"})

    return {"device_id": device_id, "context_buffer_size": size}


@router.get("/api/devices/{device_id}/auto-switch")
async def get_device_auto_switch(device_id: str):
    """Get auto-switch status for a device."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    devices = await _server().db_manager.list_devices()

    for d in devices:
        if d["device_id"] == device_id:
            return {
                "device_id": device_id,
                "auto_switch_enabled": d.get("auto_switch_enabled", False),
            }

    return JSONResponse(status_code=404, content={"error": "Device not found"})


@router.put("/api/devices/{device_id}/auto-switch")
async def set_device_auto_switch(device_id: str, request: Request):
    """Enable or disable auto-switch to production for a device."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    body = await request.json()

    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400, content={"error": "Invalid JSON body: expected an object"}
        )

    enabled = body.get("enabled", False)

    if not isinstance(enabled, bool):
        return JSONResponse(status_code=400, content={"error": "'enabled' must be a boolean"})

    success = await _server().db_manager.update_device_auto_switch(device_id, enabled)

    if not success:
        return JSONResponse(status_code=404, content={"error": "Device not found"})

    return {"device_id": device_id, "auto_switch_enabled": enabled}


@router.put("/api/devices/{device_id}/llm")
async def configure_device_llm(device_id: str, request: Request):
    """Configure LLM settings for a device."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    body = await request.json()

    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400, content={"error": "Invalid JSON body: expected an object"}
        )

    success = await _server().db_manager.update_device_llm_config(
        device_id,
        base_url=body.get("base_url"),
        model_id=body.get("model_id"),
        profile_name=body.get("profile_name"),
    )

    if not success:
        return JSONResponse(status_code=404, content={"error": "Device not found"})

    return {"device_id": device_id, "status": "updated"}


def _requested_database_url(body: dict) -> tuple[str | None, str | None]:
    """The device database URL a request asks for, or why it cannot have it."""

    database_url = body.get("database_url")

    database_name = body.get("database_name")

    if database_url:
        try:
            return (_server().db_manager.checked_device_db_url(str(database_url)), None)

        except ValueError as e:
            return (None, str(e))

    if database_name:
        try:
            safe_name = sanitize_filename_component(str(database_name))

        except ValueError as e:
            return (None, f"Invalid database_name: {e}")

        return (
            f"sqlite+aiosqlite:///{_server().db_manager.device_db_dir / f'{safe_name}.db'}",
            None,
        )

    return (None, "Provide 'database_url' or 'database_name'")


@router.post("/api/devices/{device_id}/database")
async def assign_device_database(device_id: str, request: Request):
    """Assign a database (URL or name) to a device. Creates a new DB if only name given."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    body = await request.json()

    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400, content={"error": "Invalid JSON body: expected an object"}
        )

    database_url, error = _requested_database_url(body)

    if error:
        return JSONResponse(status_code=400, content={"error": error})

    database_name = body.get("database_name")

    success = await _server().db_manager.assign_device_database(
        device_id, database_url=database_url, database_name=database_name
    )

    if not success:
        return JSONResponse(status_code=404, content={"error": "Device not found"})

    return {"device_id": device_id, "database_url": database_url, "database_name": database_name}


@router.post("/api/devices/{device_id}/database/rename")
async def rename_device_database(device_id: str, request: Request):
    """Rename a device's database file.







    The new name is sanitized into a filename segment and the ``.db`` suffix is



    added when missing. The device keeps working: its engine is closed, the file



    (with its SQLite sidecars) is moved, and the device is repointed at it.



    """

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    body = await request.json()

    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400, content={"error": "Invalid JSON body: expected an object"}
        )

    new_name = body.get("name") or body.get("database_name")

    if not new_name or not isinstance(new_name, str):
        return JSONResponse(status_code=400, content={"error": "Provide 'name'"})

    try:
        database_url = await _server().db_manager.rename_device_database(device_id, new_name)

    except ValueError as e:
        return JSONResponse(status_code=409, content={"error": str(e)})

    if database_url is None:
        return JSONResponse(status_code=404, content={"error": "Device not found"})

    return {"device_id": device_id, "name": new_name, "database_url": database_url}


@router.get("/api/databases")
async def list_databases():
    """List all active device databases."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    databases = await _server().db_manager.list_databases()

    return {"databases": databases}


@router.get("/api/devices/{device_id}/database")
async def get_device_database(device_id: str):
    """Get the database assignment for a device."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    async with _server().db_manager.core_session() as session:
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


@router.get("/api/devices/by-ip/{ip_address}")
async def get_device_by_ip(ip_address: str):
    """Look up a device by its IP address."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    if not _server().db_manager._is_ip(ip_address):
        return JSONResponse(status_code=400, content={"error": "Invalid IP address format"})

    device_id = await _server().db_manager.resolve_device_id(ip_address)

    if not device_id:
        return JSONResponse(status_code=404, content={"error": "Device not found for this IP"})

    return {"device_id": device_id, "ip_address": ip_address}


@router.get("/api/devices/{device_id}/connection")
async def get_device_connection(device_id: str):
    """Get the connection type for a device (default 'auto')."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    async with _server().db_manager.core_session() as session:
        result = await session.execute(
            select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
        )

        device = result.scalar_one_or_none()

        if not device:
            return JSONResponse(status_code=404, content={"error": "Device not found"})

        return {
            "device_id": device.device_id,
            "connection": (device.extra_attributes or {}).get("connection", "auto"),
        }


@router.put("/api/devices/{device_id}/connection")
async def update_device_connection(device_id: str, request: Request):
    """Set the connection type for a device (auto | tls | http | mqtt | coap | modbus)."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    body = await request.json()

    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400, content={"error": "Invalid JSON body: expected an object"}
        )

    raw = str(body.get("connection", "auto")).lower()

    try:
        connection = ConnectionType(raw)

    except ValueError:
        return JSONResponse(status_code=400, content={"error": f"Invalid connection: {raw}"})

    async with _server().db_manager.core_session() as session:
        result = await session.execute(
            select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
        )

        device = result.scalar_one_or_none()

        if not device:
            return JSONResponse(status_code=404, content={"error": "Device not found"})

        extra = dict(device.extra_attributes or {})

        extra["connection"] = connection.value

        device.extra_attributes = extra

        session.add(device)

        await session.commit()

        _server().db_manager.invalidate_device_cache(device_id)

    return {"device_id": device_id, "connection": connection.value}


@router.get("/api/ip-profiles/{ip_address}/bypass")
async def get_ip_bypass(ip_address: str):
    """Get the bypass mode for an IP (from ip_profiles config, default False)."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    if not _server().db_manager._is_ip(ip_address):
        return JSONResponse(status_code=400, content={"error": "Invalid IP address format"})

    return {
        "ip_address": ip_address,
        "bypass": await _server().db_manager.is_ips_bypassed(ip_address),
    }


@router.put("/api/ip-profiles/{ip_address}/bypass")
async def set_ip_bypass(ip_address: str, request: Request):
    """Set/clear bypass for an IP, persisting to the config file."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    if not _server().db_manager._is_ip(ip_address):
        return JSONResponse(status_code=400, content={"error": "Invalid IP address format"})

    body = await request.json()

    if not isinstance(body, dict) or "bypass" not in body:
        return JSONResponse(
            status_code=400, content={"error": "Expected JSON body with a 'bypass' boolean"}
        )

    bypass = bool(body["bypass"])

    ok = _server().config_manager.set_ip_profile_bypass(ip_address, bypass)

    if not ok:
        return JSONResponse(status_code=500, content={"error": "Failed to persist config"})

    return {"ip_address": ip_address, "bypass": bypass}


@router.post("/api/devices/{device_id}/ip")
async def register_device_ip(device_id: str, request: Request):  # noqa: PLR0911
    """Register an IP address for a device."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    body = await request.json()

    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400, content={"error": "Invalid JSON body: expected an object"}
        )

    ip_address = body.get("ip_address")

    if not ip_address:
        return JSONResponse(status_code=400, content={"error": "Provide 'ip_address'"})

    if not isinstance(ip_address, str) or not _server().db_manager._is_ip(ip_address):
        return JSONResponse(status_code=400, content={"error": "Invalid IP address format"})

    if _server().self_connection_reason(ip_address):
        return JSONResponse(
            status_code=400,
            content={
                "error": "Addresses of this machine cannot be bound to a device: "
                "the proxy never intercepts its own traffic"
            },
        )

    current_ips = await _server().db_manager.bind_ip(device_id, ip_address)

    if current_ips is None:
        return JSONResponse(status_code=404, content={"error": "Device not found"})

    return {"device_id": device_id, "ip_addresses": current_ips}


@router.get("/api/devices/{device_id}/buffer")
async def list_buffer_entries(device_id: str):
    """List unflushed buffer entries for a device."""

    if not _server().db_manager or not _server().orchestrator:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    try:
        buf = _server().orchestrator.buffer.get(device_id)

        if not buf:
            buf = await _server().orchestrator.ensure_buffer(device_id)

        pairs = await buf.get_buffer_pairs(device_id)

        total_size = sum(p["size"] for p in pairs)

        return {
            "device_id": device_id,
            "entries": pairs,
            "total_entries": len(pairs),
            "total_size_bytes": total_size,
        }

    except Exception:
        logger.exception("Handler error (500)")

        return JSONResponse(status_code=500, content={"error": "An internal error occurred"})


@router.delete("/api/devices/{device_id}/buffer/{entry_id}")
async def delete_buffer_entry(device_id: str, entry_id: int):
    """Delete a single buffer entry."""

    if not _server().db_manager or not _server().orchestrator:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    try:
        buf = _server().orchestrator.buffer.get(device_id)

        if not buf:
            return JSONResponse(status_code=404, content={"error": "Buffer not found"})

        success = await buf.delete_entry(device_id, entry_id)

        if not success:
            return JSONResponse(status_code=404, content={"error": "Entry not found"})

        return {  # noqa: TRY300 - the return is the handler's success path
            "device_id": device_id,
            "entry_id": entry_id,
            "status": "deleted",
        }

    except Exception:
        logger.exception("Handler error (500)")

        return JSONResponse(status_code=500, content={"error": "An internal error occurred"})


@router.get("/api/devices/{device_id}/context")
async def get_device_context(device_id: str):
    """Get custom context notes for a device."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    notes = await _server().db_manager.get_device_context_notes(device_id)

    return {"device_id": device_id, "context_notes": notes or ""}


@router.put("/api/devices/{device_id}/context")
async def update_device_context(device_id: str, request: Request):
    """Update custom context notes for a device."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    body = await request.json()

    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400, content={"error": "Invalid JSON body: expected an object"}
        )

    notes = body.get("context_notes", "")

    success = await _server().db_manager.update_device_context_notes(device_id, notes)

    if not success:
        return JSONResponse(status_code=404, content={"error": "Device not found"})

    return {"device_id": device_id, "context_notes": notes, "status": "updated"}


@router.get("/api/devices/{device_id}/capture/export")
async def export_buffer(device_id: str):
    """Export raw buffer to portable .ride-capture.json format."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    manager = buffer_manager.BufferManager(_server().db_manager)

    try:
        async with _server().db_manager.core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )

            device = result.scalar_one_or_none()

            vendor = device.vendor if device else "unknown"

            device_type = device.device_type if device else "unknown"

        capture = await manager.export_capture(device_id, vendor, device_type)

        return capture.model_dump(by_alias=True, exclude_none=True)

    except Exception:
        logger.exception("Handler error (500)")

        return JSONResponse(status_code=500, content={"error": "An internal error occurred"})


@router.post("/api/devices/{device_id}/capture/import")
async def import_buffer(device_id: str, request: Request):
    """Import raw pairs from portable .ride-capture.json into buffer."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    manager = buffer_manager.BufferManager(_server().db_manager)

    try:
        body = await request.json()

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

        return {  # noqa: TRY300 - the return is the handler's success path
            "imported": count,
            "device_id": device_id,
            "warnings": result.warnings,
        }

    except ValidationError as e:
        return JSONResponse(
            status_code=422, content={"error": "Capture validation failed", "details": e.to_dict()}
        )

    except Exception as e:
        return _client_error(e, "Invalid capture import payload")

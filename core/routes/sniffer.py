"""
Sniffer routes — import HAR, PCAP, and PCAPNG captures into device buffers.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse

from core.atomic_io import sanitize_filename_component
from core.sniffer.service import SnifferService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sniffer"])


def _server() -> Any:
    return sys.modules.get("core.server")


async def _extract_content(
    request: Request,
    file: UploadFile | None,
) -> tuple[bytes, str | None]:
    """Extract content bytes and filename from upload file or request body."""
    if file is not None and file.filename:
        file_name = sanitize_filename_component(file.filename)
        content = await file.read()
        return content, file_name

    content = await request.body()
    body_filename: str | None = None
    content_disp = request.headers.get("content-disposition", "")
    if "filename=" in content_disp:
        parts = content_disp.split("filename=")
        if len(parts) > 1:
            raw_filename = parts[1].split(";")[0].strip("\"'; ")
            if raw_filename:
                body_filename = sanitize_filename_component(raw_filename)
    return content, body_filename


@router.post("/api/devices/{device_id}/sniffer/import")
async def import_sniffer_capture(  # noqa: C901
    device_id: str,
    request: Request,
    file: UploadFile | None = File(None),
) -> Any:  # noqa: ANN401
    """Import an external sniffer capture (HAR, PCAP, PCAPNG, .ride-capture.json)."""
    server_mod = _server()
    if not server_mod or not getattr(server_mod, "db_manager", None):
        return JSONResponse(status_code=503, content={"error": "Database service not ready"})

    safe_device_id = sanitize_filename_component(device_id)
    content, filename = await _extract_content(request, file)

    if not content:
        return JSONResponse(
            status_code=400,
            content={"error": "No capture file or body provided for import"},
        )

    service = SnifferService(server_mod.db_manager)
    try:
        result = await service.import_to_device(
            device_id=safe_device_id,
            content=content,
            filename=filename,
        )
    except ValueError as e:
        logger.warning("Sniffer parse error for device %s: %s", safe_device_id, e)
        return JSONResponse(
            status_code=422,
            content={
                "error": (
                    "Unable to parse capture file. "
                    "Expected .har, .pcap, .pcapng, or .ride-capture.json"
                )
            },
        )
    except Exception:
        logger.exception("Unexpected error importing sniffer capture for %s", safe_device_id)
        return JSONResponse(status_code=500, content={"error": "Failed to import capture"})
    else:
        if getattr(server_mod, "orchestrator", None):
            buf = server_mod.orchestrator.buffer.get(safe_device_id)
            if buf and hasattr(buf, "invalidate_cache"):
                buf.invalidate_cache()
        return result

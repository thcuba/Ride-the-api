"""Resilience and cloud-independence API route definitions."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from starlette.requests import Request  # noqa: TC002
from starlette.responses import JSONResponse, Response

from core.resilience import CloudIndependenceVerifier

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def _service_not_ready() -> JSONResponse:
    return JSONResponse(status_code=503, content={"error": "Service not ready"})


def register_resilience_routes(  # noqa: C901
    app: FastAPI,
    get_db: Callable[[], Any],
    get_orch: Callable[[], Any],
) -> None:
    """Register resilience-related API routes."""

    @app.get("/api/independence/{device_id}")
    async def check_device_independence(device_id: str) -> Response | dict[str, Any]:
        """Check if a device can operate without vendor cloud."""
        db = get_db()
        if not db:
            return _service_not_ready()
        verifier = CloudIndependenceVerifier(db)
        status = await verifier.check_cloud_independence(device_id)
        return {"device_id": device_id, "independence": status}

    # The trailing-slash form used to fall through to the vendor proxy route.
    @app.get("/api/independence/", include_in_schema=False)
    @app.get("/api/independence")
    async def check_all_independence() -> Response | dict[str, Any]:
        """Check independence for all devices."""
        db = get_db()
        if not db:
            return _service_not_ready()
        verifier = CloudIndependenceVerifier(db)
        results = await verifier.verify_all_devices()
        return {"devices": results}

    @app.post("/api/independence/{device_id}/auto-switch")
    async def auto_switch_device(device_id: str) -> Response | dict[str, Any]:
        """Auto-switch device to production if conditions are met."""
        db = get_db()
        if not db:
            return _service_not_ready()
        verifier = CloudIndependenceVerifier(db)
        switched = await verifier.auto_switch_to_production(device_id)
        return {"device_id": device_id, "switched": switched}

    @app.get("/api/independence/{device_id}/export")
    async def export_device_patterns(device_id: str) -> Response | dict[str, Any]:
        """Export learned patterns for backup/sharing."""
        db = get_db()
        if not db:
            return _service_not_ready()
        verifier = CloudIndependenceVerifier(db)
        return await verifier.export_device_patterns(device_id)

    @app.post("/api/independence/{device_id}/import")
    async def import_device_patterns(device_id: str, request: Request) -> Response | dict[str, Any]:
        """Import previously exported patterns."""
        db = get_db()
        if not db:
            return _service_not_ready()
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "Body is not JSON"})
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"error": "Expected a JSON object"})
        verifier = CloudIndependenceVerifier(db)
        try:
            count = await verifier.import_device_patterns(device_id, body)
        except Exception:
            logger.exception("Pattern import for %s failed", device_id)
            return JSONResponse(status_code=400, content={"error": "Invalid pattern import"})
        # The engine serves its in-memory set when it has one: refresh it.
        orchestrator = get_orch()
        if orchestrator is not None:
            try:
                await orchestrator.sync_engine(device_id)
            except Exception:
                logger.exception("Could not refresh the served patterns for %s", device_id)
        return {"device_id": device_id, "patterns_imported": count}


__all__ = ["register_resilience_routes"]

"""System, Health, Config, and Control Plane routes."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from jsonschema.exceptions import ValidationError
from pydantic import ValidationError as PydanticValidationError

from core.buffer import get_buffer_backend, normalise_backend, switch_backend
from core.config import mask_secrets, restore_masked_secrets
from core.dns_server import get_dns_interceptor
from core.paths import bundle_root, is_frozen
from core.security import password_problem
from core.tls_mitm import peek_tls_mitm_server

logger = logging.getLogger(__name__)


router = APIRouter(tags=["system"])


_MIN_PASSWORD_LENGTH = 8


def _server():
    from core import server  # noqa: PLC0415 - lazy import avoids a cycle with core.server

    return server


def _listening_ports() -> dict[str, object]:
    """What is actually bound — the first question when a device "does not work".



    Failures here are quiet by nature: a port below 1024 without CAP_NET_BIND_SERVICE, or a

    device aimed at a listener that never came up. Reporting it costs nothing and turns a

    guess into a diagnosis. Never creates anything (a health check must not change state),

    never raises.

    """

    report: dict[str, object] = {}

    try:
        report["proxy"] = {"port": _server().config_manager.config.proxy.port, "listening": True}

    except Exception:
        report["proxy"] = {"listening": True}

    try:
        dns = get_dns_interceptor()

        report["dns"] = (
            {"enabled": False, "listening": False}
            if dns is None
            else {"enabled": dns.enabled, "host": dns.host, "port": dns.port, **dns.listening}
        )

    except Exception:
        report["dns"] = {"enabled": None}

    try:
        tls = peek_tls_mitm_server()

        report["tls_mitm"] = (
            {"requested": [], "bound": [], "missing": []}
            if tls is None
            else {
                "requested": list(tls.listen_ports),
                "bound": list(tls.bound_ports),
                "missing": [p for p in tls.listen_ports if p not in tls.bound_ports],
            }
        )

    except Exception:
        report["tls_mitm"] = {"requested": None}

    return report


@router.get("/health")
async def health_check():
    """Health check endpoint: also reports what is actually bound."""

    return {
        "status": "healthy",
        "service": "local-cloud-replacement-proxy",
        "version": _server().runtime_version(),
        "adapters": _server().adapter_registry.list_vendors() if _server().adapter_registry else [],
        "ports": _listening_ports(),
    }


@router.post("/api/system/update")
async def system_update():
    """Update the software to the latest state and restart the service.



    Detects the install type and runs the bundled ``deploy/update.sh``

    **detached** so the HTTP response is delivered before the service

    restarts. The update log is written to ``<install-dir>/update.log``.

    """

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    install_dir = Path.cwd()

    server_root = Path(_server().__file__).resolve().parent.parent

    updater_base = (bundle_root() if is_frozen() else server_root) or server_root

    script = updater_base / "deploy" / "update.sh"

    if not script.exists():
        script = updater_base / "deploy" / "rideapi"

    if not script.exists():
        return JSONResponse(
            status_code=500, content={"error": "Updater script not found in this install"}
        )

    try:
        subprocess.Popen(
            ["bash", str(script), "--dir", str(install_dir), "--restart", "systemd"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(install_dir),
            env=_server().updater_env(),
        )

    except Exception:
        logger.exception("Failed to launch updater")

        return JSONResponse(
            status_code=500,
            content={"error": "Failed to start update. Check server logs for details."},
        )

    return {
        "status": "started",
        "message": "Update started in the background. The service will restart — "
        "wait a moment and reload.",
        "log": str(install_dir / "update.log"),
    }


@router.get("/api/settings/buffer-backend")
async def get_buffer_backend_setting():
    """Return the currently active buffer backend (``disk`` = SQLite, or ``memory``)."""

    return {"backend": get_buffer_backend()}


@router.put("/api/settings/buffer-backend")
async def set_buffer_backend_setting(request: Request):
    """Switch the buffer backend, carrying the pairs in flight, and persist the choice.



    ``disk`` (alias: ``sqlite``) is the SQLite backend: buffered pairs live in the device

    database and survive a restart. ``memory`` (alias: ``ram``) keeps them in a

    process-shared in-memory engine.



    The switch is lossless in both directions: whatever has not been flushed yet is copied

    into the new store before the old one is marked flushed, and the live buffers are

    repointed at it, so no capture is lost and none is buffered twice.

    """

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    try:
        body = await request.json()

    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400, content={"error": "Invalid JSON body: expected an object"}
        )

    backend = normalise_backend(body.get("backend"))

    if backend is None:
        return JSONResponse(
            status_code=400,
            content={
                "error": "backend must be 'disk' (alias: sqlite) or 'memory' (alias: ram)",
                "got": body.get("backend"),
            },
        )

    summary = await switch_backend(
        _server().db_manager,
        backend,
        rebind=_server().orchestrator.rebind_buffer_store if _server().orchestrator else None,
    )

    logger.info("Buffer backend switched to %s via settings API", backend)

    return summary


@router.get("/api/config")
async def get_config():
    """Return the full current configuration (validated YAML model_dump).



    Secrets (control-plane password, API keys, bridge passwords) are masked;

    sending the mask back unchanged on PUT keeps the stored value.

    """

    return {"config": mask_secrets(_server().config_manager.config.model_dump(mode="json"))}


@router.put("/api/config")
async def put_config(request: Request):
    """Validate and persist a full replacement of the configuration."""

    try:
        body = await request.json()

    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    if not isinstance(body, dict) or "config" not in body:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid JSON body: expected an object with a 'config' key"},
        )

    payload = restore_masked_secrets(
        body["config"], _server().config_manager.config.model_dump(mode="json")
    )

    if isinstance(payload, dict) and _server().llm_decipher_service:
        payload = {
            **payload,
            "llm_decipher": _server().llm_decipher_service.get_settings_for_persistence(),
        }

    try:
        ok = _server().config_manager.update_config(payload)

    except (ValidationError, PydanticValidationError) as ve:
        return JSONResponse(
            status_code=400,
            content={"error": "Config validation failed", "details": _validation_details(ve)},
        )

    except Exception:
        logger.exception("Failed to update config")

        return JSONResponse(status_code=500, content={"error": "Failed to update config"})

    if not ok:
        return JSONResponse(status_code=500, content={"error": "Failed to persist config"})

    logger.info("Configuration updated via /api/config API")

    return {
        "config": mask_secrets(_server().config_manager.config.model_dump(mode="json")),
        "status": "saved",
    }


def _validation_details(error: Exception) -> list:
    """Field locations and messages of a validation error, without input values.



    Pydantic's string form echoes submitted values (which may be secrets), so

    only ``loc``/``msg`` pairs are returned to the client.

    """

    if isinstance(error, PydanticValidationError):
        return [
            {"loc": [str(part) for part in err.get("loc", ())], "msg": err.get("msg", "")}
            for err in error.errors()
        ]

    return [{"loc": [], "msg": str(error)}]


def _new_password_problem(password: object) -> str | None:
    """Why ``password`` cannot become the control-plane password, if it cannot."""

    if not password or not isinstance(password, str):
        return "password must be non-empty"

    if len(password) < _MIN_PASSWORD_LENGTH:
        return f"password must be at least {_MIN_PASSWORD_LENGTH} characters long"

    return password_problem(password)


@router.post("/api/auth/password")
async def set_control_plane_password(request: Request):
    """Set the control-plane ``security.password`` on first run.



    Reachable without credentials only while the current password is empty

    (the middleware lets ``SETUP_PATH`` through in that state). Once a

    password is configured, this endpoint is protected like any other

    ``/api/*`` route.

    """

    if _server().config_manager.config.security.password:
        return JSONResponse(status_code=403, content={"error": "A password is already set"})

    try:
        body = await request.json()

    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    password = body.get("password") or "" if isinstance(body, dict) else ""

    if problem := _new_password_problem(password):
        return JSONResponse(status_code=400, content={"error": problem})

    ok = _server().config_manager.set_security_password(password)

    if not ok:
        return JSONResponse(status_code=500, content={"error": "Failed to persist password"})

    logger.info("Control-plane password set via /api/auth/password (first run)")

    return {"status": "ok", "config_updated": True}

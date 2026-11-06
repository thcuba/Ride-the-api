"""LLM settings, profiles, testing, and extraction routes."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.config import SECRET_MASK

logger = logging.getLogger(__name__)

router = APIRouter(tags=["llm"])


def _server():
    from core import server

    return server


@router.get("/api/llm/profiles")
async def list_llm_profiles():
    """List available LLM profiles (system-level)."""
    if not _server().llm_decipher_service:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    profiles = _server().llm_decipher_service.list_profiles()
    return {"profiles": profiles}


@router.get("/api/llm/settings")
async def get_llm_settings():
    """Return the effective LLM settings (enabled, default profile, profiles)."""
    if not _server().llm_decipher_service:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    return _server().llm_decipher_service.get_settings()


@router.put("/api/llm/settings")
async def update_llm_settings(request: Request):
    """Update the LLM settings (enabled, default profile, profiles) and persist."""
    if not _server().llm_decipher_service:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})
    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400, content={"error": "Invalid JSON body: expected an object"}
        )
    try:
        settings = _server().llm_decipher_service.update_settings(body)
    except ValueError as e:
        logger.warning("Invalid LLM settings update: %s", e)
        return JSONResponse(status_code=400, content={"error": "Invalid LLM settings"})
    except Exception:
        logger.exception("Failed to update LLM settings")
        return JSONResponse(status_code=500, content={"error": "Failed to update LLM settings"})
    return settings


@router.post("/api/llm/profiles/test")
async def test_llm_profile(request: Request):
    """Send a "hello" completion to a provider and report whether it answers.

    The dashboard's per-profile Test button calls this with the current form
    values (which need not be saved yet). Returns 200 with ``{"success": true}``
    when the provider responds, or 502 with the provider error otherwise.
    """
    if not _server().llm_decipher_service:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})
    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400, content={"error": "Invalid JSON body: expected an object"}
        )
    base_url = str(body.get("base_url", "")).strip()
    model_id = str(body.get("model_id", "")).strip()
    if not base_url or not model_id:
        return JSONResponse(
            status_code=400,
            content={"error": "Both 'base_url' and 'model_id' are required for the test"},
        )
    api_key = str(body.get("api_key", ""))
    if api_key == SECRET_MASK:
        api_key = _server().llm_decipher_service.stored_api_key(str(body.get("name") or ""))
    timeout = int(body.get("timeout", 30) or 30)
    max_retries = int(body.get("max_retries", 2) or 2)
    result = await _server().llm_decipher_service.test_connection(
        base_url=base_url,
        model_id=model_id,
        api_key=api_key,
        timeout=timeout,
        max_retries=max_retries,
    )
    if not result.get("success"):
        return JSONResponse(status_code=502, content=result)
    return result


@router.post("/api/llm/settings/profiles")
async def create_llm_profile(request: Request):
    """Create or update a single LLM profile and persist the settings."""
    if not _server().llm_decipher_service:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})
    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400, content={"error": "Invalid JSON body: expected an object"}
        )
    name = body.get("name")
    if not name:
        return JSONResponse(status_code=400, content={"error": "name is required"})
    settings = _server().llm_decipher_service.get_settings()
    profiles = settings["profiles"]
    profiles[name] = {
        "base_url": body.get("base_url", "https://api.openai.com/v1"),
        "api_key": body.get("api_key", ""),
        "model_id": body.get("model_id", "gpt-4o-mini"),
        "prompt_template": body.get("prompt_template", ""),
        "enabled": body.get("enabled", True),
        "timeout": body.get("timeout", 30),
        "max_retries": body.get("max_retries", 2),
    }
    try:
        updated = _server().llm_decipher_service.update_settings(settings)
    except ValueError as e:
        logger.warning("Invalid LLM profile: %s", e)
        return JSONResponse(status_code=400, content={"error": "Invalid LLM profile"})
    except Exception:
        logger.exception("Failed to create LLM profile")
        return JSONResponse(status_code=500, content={"error": "Failed to create LLM profile"})
    return {"name": name, "status": "saved", "settings": updated}


@router.delete("/api/llm/settings/profiles/{name}")
async def delete_llm_profile(name: str):
    """Delete an LLM profile and persist the settings."""
    if not _server().llm_decipher_service:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    settings = _server().llm_decipher_service.get_settings()
    profiles = settings["profiles"]
    if name not in profiles:
        return JSONResponse(status_code=404, content={"error": "Profile not found"})
    del profiles[name]
    if settings["default_profile"] == name:
        settings["default_profile"] = next(iter(profiles), "")
    try:
        updated = _server().llm_decipher_service.update_settings(settings)
    except ValueError as e:
        logger.warning("Invalid LLM settings on delete: %s", e)
        return JSONResponse(status_code=400, content={"error": "Invalid LLM settings"})
    except Exception:
        logger.exception("Failed to delete LLM profile")
        return JSONResponse(status_code=500, content={"error": "Failed to delete LLM profile"})
    return {"name": name, "status": "deleted", "settings": updated}


@router.post("/api/devices/{device_id}/llm/flush")
async def flush_buffer_to_llm(device_id: str, request: Request):
    """Flush selected buffer entries to LLM with optional context notes."""
    if not _server().db_manager or not _server().orchestrator:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400, content={"error": "Invalid JSON body: expected an object"}
        )
    pair_ids = body.get("pair_ids")
    context_notes = body.get("context_notes")
    result = await _server().orchestrator.flush_and_learn(
        device_id, pair_ids=pair_ids, context_notes=context_notes
    )
    if not result.get("success"):
        status = 500 if "failed" in result.get("error", "") else 400
        return JSONResponse(status_code=status, content=result)
    return result


@router.post("/api/devices/{device_id}/llm/preview")
async def preview_llm_analysis(device_id: str, request: Request):
    """Run LLM analysis without saving patterns. Returns analysis for review."""
    if not _server().db_manager or not _server().orchestrator:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400, content={"error": "Invalid JSON body: expected an object"}
        )
    pair_ids = body.get("pair_ids")
    context_notes = body.get("context_notes")
    result = await _server().orchestrator.preview_analysis(
        device_id, pair_ids=pair_ids, context_notes=context_notes
    )
    if not result.get("success"):
        status = 500 if "failed" in result.get("error", "") else 400
        return JSONResponse(status_code=status, content=result)
    return result


@router.get("/api/llm/user-profiles")
async def list_user_profiles():
    """List all user-saved LLM profile templates."""
    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    profiles = await _server().db_manager.list_llm_profiles()
    return {"profiles": profiles}


@router.post("/api/llm/user-profiles")
async def create_user_profile(request: Request):
    """Create a new user LLM profile template."""
    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400, content={"error": "Invalid JSON body: expected an object"}
        )
    if not body.get("name") or not body.get("prompt_template"):
        return JSONResponse(
            status_code=400, content={"error": "name and prompt_template are required"}
        )
    success = await _server().db_manager.create_llm_profile(body)
    if not success:
        return JSONResponse(
            status_code=409, content={"error": f"Profile '{body['name']}' already exists"}
        )
    return {"name": body["name"], "status": "created"}


@router.get("/api/llm/user-profiles/{name}")
async def get_user_profile(name: str):
    """Get a single user LLM profile."""
    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    profile = await _server().db_manager.get_llm_profile(name)
    if not profile:
        return JSONResponse(status_code=404, content={"error": "Profile not found"})
    return profile


@router.put("/api/llm/user-profiles/{name}")
async def update_user_profile(name: str, request: Request):
    """Update an existing user LLM profile."""
    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400, content={"error": "Invalid JSON body: expected an object"}
        )
    success = await _server().db_manager.update_llm_profile(name, body)
    if not success:
        return JSONResponse(status_code=404, content={"error": "Profile not found"})
    return {"name": name, "status": "updated"}


@router.delete("/api/llm/user-profiles/{name}")
async def delete_user_profile(name: str):
    """Delete a user LLM profile."""
    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    success = await _server().db_manager.delete_llm_profile(name)
    if not success:
        return JSONResponse(status_code=404, content={"error": "Profile not found"})
    return {"name": name, "status": "deleted"}

"""Web UI routes serving dashboard, patterns, config, and options HTML."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)

_WEBUI_HTML_CACHE: dict[Path, str] = {}


def _server():
    from core import server

    return server


async def _serve_webui(path: Path, page_name: str, expected: str) -> HTMLResponse:
    """Serve a cached webui HTML page, reading from disk once if needed."""
    html = _WEBUI_HTML_CACHE.get(path)
    if html is None:
        try:
            html = await asyncio.to_thread(path.read_text, encoding="utf-8")
        except FileNotFoundError:
            logger.warning("%s HTML not found at %s", page_name, path)
            html = f"<!DOCTYPE html><html><body><h1>{page_name} not found</h1><p>Expected at {expected}</p></body></html>"
        else:
            _WEBUI_HTML_CACHE[path] = html
    return HTMLResponse(content=html, status_code=200)


@router.get("/", response_class=HTMLResponse)
async def dashboard():
    """Simple web dashboard."""
    return await _serve_webui(_server().DASHBOARD_HTML, "Dashboard", "webui/dashboard.html")


@router.get("/patterns", response_class=HTMLResponse)
async def patterns_index_page():
    """Serve the Patterns web UI with no device selected.

    The dashboard navigates here. Without this route the path is picked up by the device-proxy
    catch-all below, which reads it as a vendor name and answers "Protocol 'patterns' not
    supported" - a page request mistaken for intercepted traffic.
    """
    return await _serve_webui(_server().PATTERNS_HTML, "Patterns page", "webui/patterns.html")


@router.get("/patterns/{device_id}", response_class=HTMLResponse)
async def patterns_page(device_id: str):
    """Serve the Patterns web UI for a device."""
    return await _serve_webui(_server().PATTERNS_HTML, "Patterns page", "webui/patterns.html")


@router.get("/config", response_class=HTMLResponse)
async def config_page():
    """Serve the Configuration web UI."""
    return await _serve_webui(_server().CONFIG_HTML, "Config page", "webui/config.html")


@router.get("/options", response_class=HTMLResponse)
async def options_page():
    """Serve the Options web UI (theme + software update)."""
    return await _serve_webui(_server().OPTIONS_HTML, "Options page", "webui/options.html")

"""Modular API and Web UI route definitions for Ride-the-API."""

from __future__ import annotations

from core.routes.devices import router as devices_router
from core.routes.dns import router as dns_router
from core.routes.llm import router as llm_router
from core.routes.patterns import router as patterns_router
from core.routes.protocols import router as protocols_router
from core.routes.system import router as system_router
from core.routes.tls import router as tls_router
from core.routes.webui import router as webui_router

__all__ = [
    "devices_router",
    "dns_router",
    "llm_router",
    "patterns_router",
    "protocols_router",
    "system_router",
    "tls_router",
    "webui_router",
]

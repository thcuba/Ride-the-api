"""CoAP network server plugin re-export for the unified CoAP protocol bundle."""

from __future__ import annotations

try:
    from core.protocol_servers.coap_server import CoAPServerPlugin
except ImportError:  # pragma: no cover
    CoAPServerPlugin = None  # type: ignore[assignment, misc]

__all__ = ["CoAPServerPlugin"]

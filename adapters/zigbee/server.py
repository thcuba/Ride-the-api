"""Zigbee network bridge plugin re-export for the unified Zigbee protocol bundle."""

from __future__ import annotations

try:
    from core.protocol_servers.zigbee_bridge import ZigbeeBridgePlugin
except ImportError:  # pragma: no cover
    ZigbeeBridgePlugin = None  # type: ignore[assignment, misc]

__all__ = ["ZigbeeBridgePlugin"]

"""Z-Wave Server Plugin re-export."""

from __future__ import annotations

try:
    from core.protocol_servers.zwave_bridge import ZWaveBridgePlugin
except ImportError:  # pragma: no cover
    ZWaveBridgePlugin = None  # type: ignore[assignment, misc]

__all__ = ["ZWaveBridgePlugin"]

"""Thread / Matter Server Plugin re-export."""

from __future__ import annotations

try:
    from core.protocol_servers.matter_bridge import MatterBridgePlugin
except ImportError:  # pragma: no cover
    MatterBridgePlugin = None  # type: ignore[assignment, misc]

__all__ = ["MatterBridgePlugin"]

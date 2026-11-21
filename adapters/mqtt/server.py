"""MQTT network server plugin re-export for the unified MQTT protocol bundle."""

from __future__ import annotations

try:
    from core.protocol_servers.mqtt_server import MQTTServerPlugin
except ImportError:  # pragma: no cover
    MQTTServerPlugin = None  # type: ignore[assignment, misc]

__all__ = ["MQTTServerPlugin"]

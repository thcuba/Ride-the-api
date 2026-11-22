"""
MQTT Protocol Adapter — generic, vendor-neutral MQTT message handling.

MQTT is a lightweight pub/sub messaging protocol widely used in IoT.
This adapter translates standard MQTT topics and payloads into
standardized CommandTypes without vendor-specific logic.
"""

from __future__ import annotations

from adapters.mqtt.adapter import (
    PAYLOAD_INTENT_MAP,
    TOPIC_INTENT_MAP,
    MQTTProtocolAdapter,
)
from adapters.mqtt.definition import PROTOCOL_DEFINITION
from adapters.mqtt.server import MQTTServerPlugin

__all__ = [
    "MQTTProtocolAdapter",
    "MQTTServerPlugin",
    "PAYLOAD_INTENT_MAP",
    "PROTOCOL_DEFINITION",
    "TOPIC_INTENT_MAP",
]

"""
CoAP Protocol Adapter Example — for CoAP-enabled IoT devices (constrained sensors/actuators).

CoAP (Constrained Application Protocol) is a REST-like protocol over UDP for IoT devices.
This adapter translates CoAP resource interactions to standardized CommandTypes.
"""

from __future__ import annotations

from adapters.coap.adapter import (
    ACTUATOR_RESOURCES,
    COAP_RESOURCE_MAP,
    CoAPProtocolAdapter,
)
from adapters.coap.definition import PROTOCOL_DEFINITION
from adapters.coap.server import CoAPServerPlugin

__all__ = [
    "ACTUATOR_RESOURCES",
    "COAP_RESOURCE_MAP",
    "CoAPProtocolAdapter",
    "CoAPServerPlugin",
    "PROTOCOL_DEFINITION",
]

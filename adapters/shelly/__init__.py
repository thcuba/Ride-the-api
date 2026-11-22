"""
Shelly Protocol Adapter package.

Exports ShellyProtocolAdapter and PROTOCOL_DEFINITION.
"""

from __future__ import annotations

from adapters.shelly.adapter import (
    SHELLY_GEN1_KEYS,
    SHELLY_GEN2_RPC,
    ShellyProtocolAdapter,
)
from adapters.shelly.definition import PROTOCOL_DEFINITION

__all__ = [
    "PROTOCOL_DEFINITION",
    "SHELLY_GEN1_KEYS",
    "SHELLY_GEN2_RPC",
    "ShellyProtocolAdapter",
]

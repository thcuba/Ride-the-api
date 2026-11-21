"""
Base Protocol Adapter Interface.
All vendor adapters must implement this interface.
"""

from __future__ import annotations

from adapters.base.adapter import ProtocolAdapter
from adapters.base.definition import ProtocolDefinition
from adapters.base.helpers import (
    _strip_port,
    device_id_from_ip,
    host_from_request,
    normalize_request_body,
)
from adapters.base.models import (
    Command,
    CommandResult,
    CommandType,
    DeviceCapability,
    DeviceInfo,
    DeviceState,
    InterceptedRequest,
    ProtocolType,
)
from adapters.base.registry import (
    ProtocolAdapterRegistry,
    get_adapter_registry,
)

__all__ = [
    "Command",
    "CommandResult",
    "CommandType",
    "DeviceCapability",
    "DeviceInfo",
    "DeviceState",
    "InterceptedRequest",
    "ProtocolAdapter",
    "ProtocolAdapterRegistry",
    "ProtocolDefinition",
    "ProtocolType",
    "_strip_port",
    "device_id_from_ip",
    "get_adapter_registry",
    "host_from_request",
    "normalize_request_body",
]

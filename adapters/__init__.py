"""
Adapters package - Register protocol adapters.
Supports built-in adapters and community plugins via entry points.
"""

from __future__ import annotations

import logging

from adapters.base import ProtocolAdapterRegistry, get_adapter_registry

logger = logging.getLogger(__name__)


def register_all_adapters() -> ProtocolAdapterRegistry:
    """Register all available protocol adapters (built-in and entry-point discovered)."""
    registry = get_adapter_registry()

    # Import and register the example reference adapter
    from adapters.example import ExampleProtocolAdapter  # noqa: PLC0415

    example_adapter = ExampleProtocolAdapter(
        "example",
        {
            "region": "eu",
            "api_version": "v1.0",
        },
    )
    registry.register(example_adapter)

    # CoAP adapter example
    from adapters.coap import CoAPProtocolAdapter  # noqa: PLC0415

    registry.register(CoAPProtocolAdapter("coap_example", {}))

    # Modbus adapter example
    from adapters.modbus import ModbusProtocolAdapter  # noqa: PLC0415

    registry.register(ModbusProtocolAdapter("modbus_example", {}))

    # Shelly adapter
    from adapters.shelly import ShellyProtocolAdapter  # noqa: PLC0415

    registry.register(ShellyProtocolAdapter("shelly", {}))

    # Zigbee (open protocol)
    from adapters.zigbee import ZigbeeProtocolAdapter  # noqa: PLC0415

    registry.register(ZigbeeProtocolAdapter("zigbee", {}))

    # Z-Wave (open protocol)
    from adapters.zwave import ZWaveProtocolAdapter  # noqa: PLC0415

    registry.register(ZWaveProtocolAdapter("zwave", {}))

    # Thread / Matter (open protocol)
    from adapters.thread_matter import MatterProtocolAdapter  # noqa: PLC0415

    registry.register(MatterProtocolAdapter("matter", {}))

    # Discover and register third-party / community adapter plugins
    try:
        from importlib.metadata import entry_points  # noqa: PLC0415

        eps = entry_points(group="ride_the_api.adapters")
        for ep in eps:
            try:
                if not registry.get_adapter(ep.name):
                    adapter_cls = ep.load()
                    registry.register(adapter_cls(ep.name, {}))
                    logger.info("Loaded adapter plugin from entry point: %s", ep.name)
            except Exception:
                logger.exception("Failed to load adapter entry point %s", ep.name)
    except Exception:
        pass

    return registry


# Auto-register on import
_adapter_registry = None


def get_registered_registry() -> ProtocolAdapterRegistry:
    """Get registry with all adapters registered."""
    global _adapter_registry  # noqa: PLW0603
    if _adapter_registry is None:
        _adapter_registry = register_all_adapters()
    return _adapter_registry

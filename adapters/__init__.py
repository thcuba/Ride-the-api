"""
Adapters package - Register protocol adapters.
Users/community add their own adapter implementations here.
"""

from adapters.base import ProtocolAdapterRegistry, get_adapter_registry


def register_all_adapters() -> ProtocolAdapterRegistry:
    """Register all available protocol adapters."""
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

    # Users/community: add your own adapter registrations here, e.g.:
    # from adapters.my_protocol import MyProtocolAdapter  # noqa: ERA001
    # my_adapter = MyProtocolAdapter("my_protocol", {})  # noqa: ERA001
    # registry.register(my_adapter)  # noqa: ERA001

    return registry


# Auto-register on import
_adapter_registry = None


def get_registered_registry() -> ProtocolAdapterRegistry:
    """Get registry with all adapters registered."""
    global _adapter_registry  # noqa: PLW0603
    if _adapter_registry is None:
        _adapter_registry = register_all_adapters()
    return _adapter_registry

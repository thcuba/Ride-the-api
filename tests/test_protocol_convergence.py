"""
Unit tests for protocol architecture convergence and modularization.

Verifies:
- Uniform package structure for all protocol adapters.
- ProtocolDefinition unified contract bridging adapters and protocol server plugins.
- STANDARD_PROTOCOL_DEFINITIONS registry integration in adapters/__init__.py.
- Unified registration in core/protocol_servers/__init__.py.
- 100% backward compatibility of imports.
- Dynamic discovery via entry points for protocols, adapters, and server plugins.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

import pytest

from adapters import (
    STANDARD_PROTOCOL_DEFINITIONS,
    discover_adapter_entry_points,
    register_all_adapters,
)
from adapters.base import (
    Command,
    CommandResult,
    CommandType,
    DeviceCapability,
    DeviceInfo,
    DeviceState,
    InterceptedRequest,
    ProtocolAdapter,
    ProtocolAdapterRegistry,
    ProtocolDefinition,
    ProtocolType,
    device_id_from_ip,
    get_adapter_registry,
    host_from_request,
    normalize_request_body,
)
from adapters.coap.server import CoAPServerPlugin as CoAPFromAdapter
from adapters.modbus.server import ModbusServerPlugin as ModbusFromAdapter
from adapters.mqtt import PROTOCOL_DEFINITION as MQTT_DEF
from adapters.mqtt.server import MQTTServerPlugin as MQTTFromAdapter
from adapters.thread_matter.server import MatterBridgePlugin as MatterFromAdapter
from adapters.zigbee.server import ZigbeeBridgePlugin as ZigbeeFromAdapter
from adapters.zwave.server import ZWaveBridgePlugin as ZWaveFromAdapter
from core.config import ProtocolServersConfig
from core.protocol_servers import (
    ProtocolServerManager,
    ProtocolServerPlugin,
    discover_server_plugin_entry_points,
    register_standard_plugins,
)
from core.protocol_servers.coap_server import CoAPServerPlugin
from core.protocol_servers.matter_bridge import MatterBridgePlugin
from core.protocol_servers.modbus_server import ModbusServerPlugin
from core.protocol_servers.mqtt_server import MQTTServerPlugin
from core.protocol_servers.zigbee_bridge import ZigbeeBridgePlugin
from core.protocol_servers.zwave_bridge import ZWaveBridgePlugin

EXPECTED_BUILTIN_COUNT = 8


def test_standard_protocol_definitions_list():
    """STANDARD_PROTOCOL_DEFINITIONS contains all 8 built-in protocol definitions."""
    names = [d.name for d in STANDARD_PROTOCOL_DEFINITIONS]
    expected = ["example", "coap", "modbus", "shelly", "zigbee", "zwave", "matter", "mqtt"]
    for exp in expected:
        assert exp in names
    assert len(STANDARD_PROTOCOL_DEFINITIONS) == EXPECTED_BUILTIN_COUNT


@pytest.mark.parametrize(
    ("module_name", "has_server"),
    [
        ("adapters.mqtt", True),
        ("adapters.coap", True),
        ("adapters.modbus", True),
        ("adapters.zigbee", True),
        ("adapters.zwave", True),
        ("adapters.thread_matter", True),
        ("adapters.shelly", False),
        ("adapters.example", False),
    ],
)
def test_uniform_protocol_package_structure(module_name: str, has_server: bool):
    """Every protocol package provides adapter.py, definition.py, and server.py where needed."""
    pkg = importlib.import_module(module_name)
    assert hasattr(pkg, "PROTOCOL_DEFINITION")
    assert isinstance(pkg.PROTOCOL_DEFINITION, ProtocolDefinition)

    adapter_mod = importlib.import_module(f"{module_name}.adapter")
    assert adapter_mod is not None

    def_mod = importlib.import_module(f"{module_name}.definition")
    assert hasattr(def_mod, "PROTOCOL_DEFINITION")

    if has_server:
        server_mod = importlib.import_module(f"{module_name}.server")
        assert server_mod is not None
        assert pkg.PROTOCOL_DEFINITION.server_plugin_class is not None


def test_protocol_definition_factory_methods():
    """ProtocolDefinition.create_adapter and create_server_plugin instantiate correctly."""
    adapter = MQTT_DEF.create_adapter()
    assert adapter is not None
    assert isinstance(adapter, ProtocolAdapter)
    assert adapter.vendor == "mqtt"

    cfg = ProtocolServersConfig()
    server_plugin = MQTT_DEF.create_server_plugin(cfg)
    assert server_plugin is not None
    assert isinstance(server_plugin, ProtocolServerPlugin)
    assert server_plugin.name == "mqtt"


def test_register_all_adapters_populates_registry():
    """register_all_adapters registers all built-in adapters."""
    registry = register_all_adapters()
    assert isinstance(registry, ProtocolAdapterRegistry)

    expected_vendors = [
        "example",
        "coap_example",
        "modbus_example",
        "shelly",
        "zigbee",
        "zwave",
        "matter",
        "mqtt",
    ]
    vendors = registry.list_vendors()
    for v in expected_vendors:
        assert v in vendors, f"Vendor {v} missing from registry"


def test_register_standard_plugins_convergence():
    """register_standard_plugins registers all unified and transport plugins."""
    manager = ProtocolServerManager()
    config = ProtocolServersConfig()
    handler = MagicMock()

    register_standard_plugins(manager, config, handler=handler)

    expected_plugins = [
        "mqtt",
        "coap",
        "modbus",
        "websocket",
        "raw_tcp",
        "http2",
        "zigbee_bridge",
        "zwave_bridge",
        "matter_bridge",
    ]
    for p_name in expected_plugins:
        plugin = manager.get_plugin(p_name)
        assert plugin is not None, f"Plugin {p_name} not found"
        assert manager.get_handler(p_name) is handler


def test_backward_compatibility_base_imports():
    """All base models, adapters, and helpers remain importable from adapters.base."""
    assert Command is not None
    assert CommandResult is not None
    assert CommandType is not None
    assert DeviceCapability is not None
    assert DeviceInfo is not None
    assert DeviceState is not None
    assert InterceptedRequest is not None
    assert ProtocolAdapter is not None
    assert ProtocolAdapterRegistry is not None
    assert ProtocolDefinition is not None
    assert ProtocolType is not None
    assert callable(device_id_from_ip)
    assert callable(get_adapter_registry)
    assert callable(host_from_request)
    assert callable(normalize_request_body)


def test_backward_compatibility_protocol_server_imports():
    """Server plugins can be imported from adapters.<proto>.server and core.protocol_servers."""
    assert MQTTFromAdapter is MQTTServerPlugin
    assert CoAPFromAdapter is CoAPServerPlugin
    assert ModbusFromAdapter is ModbusServerPlugin
    assert ZigbeeFromAdapter is ZigbeeBridgePlugin
    assert ZWaveFromAdapter is ZWaveBridgePlugin
    assert MatterFromAdapter is MatterBridgePlugin


class CustomDiscoveryAdapter(ProtocolAdapter):
    """Adapter for entry point discovery testing."""

    @property
    def supported_protocols(self) -> list[ProtocolType]:
        return [ProtocolType.HTTP]

    @property
    def vendor_hostnames(self) -> list[str]:
        return ["custom.vendor.local"]

    async def parse_request(self, request: InterceptedRequest) -> InterceptedRequest:
        return request

    async def handle_request(self, _request: InterceptedRequest) -> CommandResult:
        return CommandResult(success=True)

    async def build_response(self, _request: InterceptedRequest, _result: CommandResult):
        return {}

    async def forward_to_cloud(self, _request: InterceptedRequest):
        return None

    async def get_device_info(self, _device_id: str):
        return None

    async def get_device_state(self, _device_id: str):
        return None

    async def send_command(self, _device_id: str, _command: Command) -> CommandResult:
        return CommandResult(success=True)


class CustomDiscoveryServerPlugin(ProtocolServerPlugin):
    """Server plugin for entry point discovery testing."""

    name = "custom_server"


class MockEntryPoint:
    """Mock EntryPoint object conforming to importlib.metadata.EntryPoint."""

    def __init__(self, name: str, obj: object) -> None:
        self.name = name
        self.obj = obj

    def load(self) -> object:
        return self.obj


def test_dynamic_discovery_entry_points():
    """Entry point discovery works seamlessly for third-party protocol bundles."""
    custom_def = ProtocolDefinition(
        name="custom_proto",
        adapter_class=CustomDiscoveryAdapter,
        server_plugin_class=CustomDiscoveryServerPlugin,
    )

    ep = MockEntryPoint("custom_proto", custom_def)

    def mock_eps(group: str):
        if group == "ride_the_api.protocols":
            return [ep]
        return []

    # Test adapter discovery
    registry = ProtocolAdapterRegistry()
    with patch("importlib.metadata.entry_points", side_effect=mock_eps):
        discover_adapter_entry_points(registry)
    assert registry.get_adapter("custom_proto") is not None

    # Test server plugin discovery
    manager = ProtocolServerManager()
    with patch("importlib.metadata.entry_points", side_effect=mock_eps):
        discover_server_plugin_entry_points(manager)
    assert manager.get_plugin("custom_server") is not None

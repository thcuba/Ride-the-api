"""
Tests for Protocol Plugin Architecture and Entry Point Discovery.

Verifies:
- ProtocolDefinition unified descriptor
- ProtocolAdapterRegistry.register_definition
- ProtocolServerManager.register_from_definition
- Dynamic entry points discovery for:
  * ride_the_api.protocols (unified bundles)
  * ride_the_api.adapters (payload normalizers)
  * ride_the_api.protocol_servers (network listeners)
- Standard built-in protocol package definitions
- Third-party / community package simulation and fault tolerance
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from adapters import discover_adapter_entry_points
from adapters.base import (
    Command,
    CommandResult,
    InterceptedRequest,
    ProtocolAdapter,
    ProtocolAdapterRegistry,
    ProtocolDefinition,
    ProtocolType,
)
from adapters.coap import PROTOCOL_DEFINITION as COAP_DEF
from adapters.example import PROTOCOL_DEFINITION as EXAMPLE_DEF
from adapters.modbus import PROTOCOL_DEFINITION as MODBUS_DEF
from adapters.mqtt import PROTOCOL_DEFINITION as MQTT_DEF
from adapters.shelly import PROTOCOL_DEFINITION as SHELLY_DEF
from adapters.thread_matter import PROTOCOL_DEFINITION as MATTER_DEF
from adapters.zigbee import PROTOCOL_DEFINITION as ZIGBEE_DEF
from adapters.zwave import PROTOCOL_DEFINITION as ZWAVE_DEF
from core.config import ProtocolServersConfig
from core.protocol_servers import (
    ProtocolServerManager,
    ProtocolServerPlugin,
    discover_server_plugin_entry_points,
    register_standard_plugins,
)


class DummyMockAdapter(ProtocolAdapter):
    """Dummy adapter for testing."""

    @property
    def supported_protocols(self) -> list[ProtocolType]:
        return [ProtocolType.HTTP]

    @property
    def vendor_hostnames(self) -> list[str]:
        return ["dummy.test.local"]

    async def parse_request(self, request: InterceptedRequest) -> InterceptedRequest:
        return request

    async def handle_request(self, _request: InterceptedRequest) -> CommandResult:
        return CommandResult(success=True)

    async def build_response(self, _request: InterceptedRequest, _result: CommandResult) -> object:
        return {"status": "ok"}

    async def forward_to_cloud(self, _request: InterceptedRequest) -> object:
        return None

    async def get_device_info(self, _device_id: str):
        return None

    async def get_device_state(self, _device_id: str):
        return None

    async def send_command(self, _device_id: str, _command: Command) -> CommandResult:
        return CommandResult(success=True)


class DummyMockServerPlugin(ProtocolServerPlugin):
    """Dummy server plugin for testing."""

    name = "dummy_server"

    def __init__(self, config: object, handler: object = None) -> None:
        super().__init__(config)
        self.handler = handler


class DummyEntryPoint:
    """Mock EntryPoint object conforming to importlib.metadata.EntryPoint."""

    def __init__(self, name: str, value: object, group: str = "ride_the_api.protocols") -> None:
        self.name = name
        self.value = value
        self.group = group

    def load(self) -> object:
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def test_protocol_definition_creation_and_instantiation():
    """ProtocolDefinition correctly instantiates adapters and server plugins."""
    definition = ProtocolDefinition(
        name="dummy_bundle",
        adapter_class=DummyMockAdapter,
        server_plugin_class=DummyMockServerPlugin,
        config_attr="dummy_config",
        adapter_config={"option": 42},
        description="Dummy test protocol",
    )

    # Instantiate adapter
    adapter = definition.create_adapter()
    assert isinstance(adapter, DummyMockAdapter)
    assert adapter.vendor == "dummy_bundle"
    assert adapter.config == {"option": 42}

    # Instantiate server plugin with matching config attr
    class DummyConfigContainer:
        dummy_config = {"listen": "0.0.0.0"}

    plugin = definition.create_server_plugin(DummyConfigContainer(), handler=lambda req: req)
    assert isinstance(plugin, DummyMockServerPlugin)
    assert plugin.config == {"listen": "0.0.0.0"}
    assert plugin.handler is not None


def test_protocol_definition_defaults():
    """ProtocolDefinition handles None adapter or plugin gracefully."""
    empty_def = ProtocolDefinition(name="empty")
    assert empty_def.create_adapter() is None
    assert empty_def.create_server_plugin({}) is None


def test_protocol_definition_vendor_override():
    """ProtocolDefinition respects adapter_vendor if provided."""
    definition = ProtocolDefinition(
        name="coap",
        adapter_class=DummyMockAdapter,
        adapter_vendor="coap_custom_vendor",
    )
    adapter = definition.create_adapter()
    assert isinstance(adapter, DummyMockAdapter)
    assert adapter.vendor == "coap_custom_vendor"


def test_adapter_registry_register_definition():
    """ProtocolAdapterRegistry registers adapter from ProtocolDefinition."""
    registry = ProtocolAdapterRegistry()
    definition = ProtocolDefinition(
        name="custom_proto",
        adapter_class=DummyMockAdapter,
        adapter_config={"key": "val"},
    )
    registry.register_definition(definition)
    adapter = registry.get_adapter("custom_proto")
    assert adapter is not None
    assert isinstance(adapter, DummyMockAdapter)
    assert adapter.config == {"key": "val"}

    # Repeated registration is a no-op
    registry.register_definition(definition)
    assert len(registry.list_vendors()) == 1


def test_protocol_server_manager_register_from_definition():
    """ProtocolServerManager registers server plugin from ProtocolDefinition."""
    manager = ProtocolServerManager()
    definition = ProtocolDefinition(
        name="dummy_server",
        server_plugin_class=DummyMockServerPlugin,
    )
    handler = MagicMock()
    success = manager.register_from_definition(definition, handler=handler)
    assert success is True
    plugin = manager.get_plugin("dummy_server")
    assert plugin is not None
    assert isinstance(plugin, DummyMockServerPlugin)
    assert manager.get_handler("dummy_server") is handler


def test_protocol_server_manager_register_definition_without_plugin():
    """ProtocolServerManager returns False when definition lacks server plugin."""
    manager = ProtocolServerManager()
    definition = ProtocolDefinition(name="no_server", adapter_class=DummyMockAdapter)
    assert manager.register_from_definition(definition) is False
    assert manager.get_plugin("no_server") is None


def test_built_in_protocol_definitions_present():
    """All built-in protocol adapter packages export PROTOCOL_DEFINITION."""
    defs = [
        COAP_DEF,
        EXAMPLE_DEF,
        MODBUS_DEF,
        MQTT_DEF,
        SHELLY_DEF,
        MATTER_DEF,
        ZIGBEE_DEF,
        ZWAVE_DEF,
    ]
    for d in defs:
        assert isinstance(d, ProtocolDefinition)
        assert d.name
        assert d.adapter_class is not None


def test_discover_adapter_entry_points():
    """discover_adapter_entry_points loads both unified protocols and standalone adapters."""
    proto_bundle = ProtocolDefinition(
        name="community_bundle",
        adapter_class=DummyMockAdapter,
    )
    ep_protocol = DummyEntryPoint("community_bundle", proto_bundle, "ride_the_api.protocols")
    ep_adapter = DummyEntryPoint("community_adapter", DummyMockAdapter, "ride_the_api.adapters")
    ep_error = DummyEntryPoint("broken", RuntimeError("Explosion"), "ride_the_api.protocols")

    def mock_entry_points(group: str):
        if group == "ride_the_api.protocols":
            return [ep_protocol, ep_error]
        if group == "ride_the_api.adapters":
            return [ep_adapter]
        return []

    registry = ProtocolAdapterRegistry()
    with patch("importlib.metadata.entry_points", side_effect=mock_entry_points):
        discover_adapter_entry_points(registry)

    assert registry.get_adapter("community_bundle") is not None
    assert registry.get_adapter("community_adapter") is not None


def test_discover_server_plugin_entry_points():
    """discover_server_plugin_entry_points loads both unified protocols and standalone plugins."""
    proto_bundle = ProtocolDefinition(
        name="community_server_bundle",
        server_plugin_class=DummyMockServerPlugin,
    )
    ep_protocol = DummyEntryPoint("community_server_bundle", proto_bundle, "ride_the_api.protocols")

    class StandaloneServerPlugin(ProtocolServerPlugin):
        name = "standalone_server"

    ep_server = DummyEntryPoint(
        "standalone_server", StandaloneServerPlugin, "ride_the_api.protocol_servers"
    )
    ep_error = DummyEntryPoint(
        "broken_server", RuntimeError("Failed load"), "ride_the_api.protocol_servers"
    )

    def mock_entry_points(group: str):
        if group == "ride_the_api.protocols":
            return [ep_protocol]
        if group == "ride_the_api.protocol_servers":
            return [ep_server, ep_error]
        return []

    manager = ProtocolServerManager()
    with patch("importlib.metadata.entry_points", side_effect=mock_entry_points):
        discover_server_plugin_entry_points(manager)

    assert manager.get_plugin("dummy_server") is not None
    assert manager.get_plugin("standalone_server") is not None


def test_register_standard_plugins_binds_correct_bridge_config_fields():
    """register_standard_plugins uses zigbee_bridge, zwave_bridge, matter_bridge without error."""
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
    for name in expected_plugins:
        plugin = manager.get_plugin(name)
        assert plugin is not None, f"Plugin {name} not registered"
        assert manager.get_handler(name) is handler

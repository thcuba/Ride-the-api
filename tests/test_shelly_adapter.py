"""
Tests for Shelly Protocol Adapter — Gen1, Gen2, Gen3 intent parsing and lifecycle.
"""
from datetime import datetime

import pytest

from adapters.base import (
    Command,
    CommandResult,
    CommandType,
    DeviceCapability,
    DeviceInfo,
    DeviceState,
    InterceptedRequest,
    ProtocolType,
)
from adapters.shelly import ShellyProtocolAdapter


# ---------------------------------------------------------------------------
#  Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def adapter():
    return ShellyProtocolAdapter(vendor="shelly", config={})

@pytest.fixture
def http_request():
    return InterceptedRequest(
        device_id="shelly-1-abc123",
        timestamp=datetime.utcnow(),
        protocol=ProtocolType.HTTP,
        method="GET",
        path="/status",
        headers={"Host": "shelly-abc123.local"},
    )


# ---------------------------------------------------------------------------
#  Protocol metadata
# ---------------------------------------------------------------------------

class TestAdapterMetadata:
    def test_vendor_code(self):
        assert ShellyProtocolAdapter.VENDOR_CODE == "shelly"

    def test_vendor_hostnames(self):
        assert "shelly-*.local" in ShellyProtocolAdapter.VENDOR_HOSTNAMES
        assert "shelly-*" in ShellyProtocolAdapter.VENDOR_HOSTNAMES

    def test_supported_protocols(self, adapter):
        protocols = adapter.supported_protocols
        assert ProtocolType.HTTP in protocols
        assert ProtocolType.HTTPS in protocols
        assert ProtocolType.COAP in protocols
        assert ProtocolType.MQTT in protocols
        assert ProtocolType.WEBSOCKET in protocols

    def test_vendor_hostnames_property(self, adapter):
        assert adapter.vendor_hostnames == ShellyProtocolAdapter.VENDOR_HOSTNAMES


# ---------------------------------------------------------------------------
#  Gen1 — HTTP intent parsing
# ---------------------------------------------------------------------------

class TestGen1HTTP:
    @pytest.mark.asyncio
    async def test_status_endpoint(self, adapter, http_request):
        result = await adapter._parse_http(http_request)
        assert result.parsed_intent == CommandType.GET_STATE

    @pytest.mark.asyncio
    async def test_turn_on(self, adapter):
        req = InterceptedRequest(
            device_id="shelly-1", timestamp=datetime.utcnow(),
            protocol=ProtocolType.HTTP, method="GET", path="/relay/0?turn=on",
        )
        result = await adapter._parse_http(req)
        assert result.parsed_intent == CommandType.TURN_ON

    @pytest.mark.asyncio
    async def test_turn_off(self, adapter):
        req = InterceptedRequest(
            device_id="shelly-1", timestamp=datetime.utcnow(),
            protocol=ProtocolType.HTTP, method="GET", path="/relay/0?turn=off",
        )
        result = await adapter._parse_http(req)
        assert result.parsed_intent == CommandType.TURN_OFF

    @pytest.mark.asyncio
    async def test_settings_endpoint(self, adapter):
        req = InterceptedRequest(
            device_id="shelly-1", timestamp=datetime.utcnow(),
            protocol=ProtocolType.HTTP, method="GET", path="/settings",
        )
        result = await adapter._parse_http(req)
        assert result.parsed_intent == CommandType.GET_STATE

    @pytest.mark.asyncio
    async def test_unknown_path_unchanged(self, adapter):
        req = InterceptedRequest(
            device_id="shelly-1", timestamp=datetime.utcnow(),
            protocol=ProtocolType.HTTP, method="GET", path="/some/unknown",
            parsed_intent=CommandType.UNKNOWN,
        )
        result = await adapter._parse_http(req)
        assert result.parsed_intent == CommandType.UNKNOWN


# ---------------------------------------------------------------------------
#  Gen2 / Gen3 — HTTP RPC intent parsing
# ---------------------------------------------------------------------------

class TestGen2HTTP:
    @pytest.mark.asyncio
    async def test_rpc_get_status(self, adapter):
        req = InterceptedRequest(
            device_id="shelly-plus-1", timestamp=datetime.utcnow(),
            protocol=ProtocolType.HTTP, method="GET", path="/rpc/Shelly.GetStatus",
        )
        result = await adapter._parse_http(req)
        assert result.parsed_intent == CommandType.GET_STATE

    @pytest.mark.asyncio
    async def test_rpc_switch_set(self, adapter):
        req = InterceptedRequest(
            device_id="shelly-plus-1", timestamp=datetime.utcnow(),
            protocol=ProtocolType.HTTP, method="POST", path="/rpc/Switch.Set",
        )
        result = await adapter._parse_http(req)
        assert result.parsed_intent == CommandType.TURN_ON

    @pytest.mark.asyncio
    async def test_rpc_light_set(self, adapter):
        req = InterceptedRequest(
            device_id="shelly-plus-1", timestamp=datetime.utcnow(),
            protocol=ProtocolType.HTTP, method="POST", path="/rpc/Light.Set",
        )
        result = await adapter._parse_http(req)
        assert result.parsed_intent == CommandType.TURN_ON

    @pytest.mark.asyncio
    async def test_rpc_get_config(self, adapter):
        req = InterceptedRequest(
            device_id="shelly-plus-1", timestamp=datetime.utcnow(),
            protocol=ProtocolType.HTTP, method="GET", path="/rpc/Shelly.GetConfig",
        )
        result = await adapter._parse_http(req)
        assert result.parsed_intent == CommandType.GET_STATE

    @pytest.mark.asyncio
    async def test_rpc_unknown_fallsback(self, adapter):
        req = InterceptedRequest(
            device_id="shelly-plus-1", timestamp=datetime.utcnow(),
            protocol=ProtocolType.HTTP, method="POST", path="/rpc/Unknown.Method",
        )
        result = await adapter._parse_http(req)
        assert result.parsed_intent == CommandType.UNKNOWN

    @pytest.mark.asyncio
    async def test_rpc_toggle(self, adapter):
        req = InterceptedRequest(
            device_id="shelly-plus-1", timestamp=datetime.utcnow(),
            protocol=ProtocolType.HTTP, method="POST", path="/rpc/Switch.Toggle",
        )
        result = await adapter._parse_http(req)
        assert result.parsed_intent == CommandType.UNKNOWN  # explicitly mapped


# ---------------------------------------------------------------------------
#  CoAP parsing
# ---------------------------------------------------------------------------

class TestCoAP:
    @pytest.mark.asyncio
    async def test_shelly_coap_path(self, adapter):
        req = InterceptedRequest(
            device_id="shelly-1", timestamp=datetime.utcnow(),
            protocol=ProtocolType.COAP, path="/shelly/status",
        )
        result = await adapter._parse_coap(req)
        assert result.parsed_intent == CommandType.GET_STATE

    @pytest.mark.asyncio
    async def test_non_shelly_coap_path(self, adapter):
        req = InterceptedRequest(
            device_id="shelly-1", timestamp=datetime.utcnow(),
            protocol=ProtocolType.COAP, path="/other/resource",
            parsed_intent=CommandType.UNKNOWN,
        )
        result = await adapter._parse_coap(req)
        assert result.parsed_intent == CommandType.UNKNOWN

    @pytest.mark.asyncio
    async def test_parse_request_delegates_coap(self, adapter):
        req = InterceptedRequest(
            device_id="shelly-1", timestamp=datetime.utcnow(),
            protocol=ProtocolType.COAP, path="/shelly/status",
        )
        result = await adapter.parse_request(req)
        assert result.parsed_intent == CommandType.GET_STATE


# ---------------------------------------------------------------------------
#  MQTT / WebSocket parsing
# ---------------------------------------------------------------------------

class TestMQTT:
    @pytest.mark.asyncio
    async def test_status_topic(self, adapter):
        req = InterceptedRequest(
            device_id="shelly-plus-1", timestamp=datetime.utcnow(),
            protocol=ProtocolType.MQTT, topic="shelly/device-1/status",
        )
        result = await adapter._parse_mqtt_ws(req)
        assert result.parsed_intent == CommandType.GET_STATE

    @pytest.mark.asyncio
    async def test_status_subtopic(self, adapter):
        req = InterceptedRequest(
            device_id="shelly-plus-1", timestamp=datetime.utcnow(),
            protocol=ProtocolType.MQTT, topic="shelly/device-1/status/power",
        )
        result = await adapter._parse_mqtt_ws(req)
        assert result.parsed_intent == CommandType.GET_STATE

    @pytest.mark.asyncio
    async def test_rpc_response_with_src(self, adapter):
        req = InterceptedRequest(
            device_id="shelly-plus-1", timestamp=datetime.utcnow(),
            protocol=ProtocolType.MQTT,
            topic="shelly/device-1/rpc",
            body={"src": "shelly", "method": "Switch.Set"},
        )
        result = await adapter._parse_mqtt_ws(req)
        assert result.parsed_intent == CommandType.TURN_ON

    @pytest.mark.asyncio
    async def test_rpc_response_no_src_unchanged(self, adapter):
        req = InterceptedRequest(
            device_id="shelly-plus-1", timestamp=datetime.utcnow(),
            protocol=ProtocolType.MQTT,
            topic="shelly/device-1/cmd",
            body={"method": "Switch.Set"},
            parsed_intent=CommandType.UNKNOWN,
        )
        result = await adapter._parse_mqtt_ws(req)
        assert result.parsed_intent == CommandType.UNKNOWN

    @pytest.mark.asyncio
    async def test_websocket_status(self, adapter):
        req = InterceptedRequest(
            device_id="shelly-plus-1", timestamp=datetime.utcnow(),
            protocol=ProtocolType.WEBSOCKET, path="/ws/device-1/status",
        )
        result = await adapter._parse_mqtt_ws(req)
        assert result.parsed_intent == CommandType.GET_STATE

    @pytest.mark.asyncio
    async def test_parse_request_delegates_mqtt(self, adapter, http_request):
        req = InterceptedRequest(
            device_id="shelly-plus-1", timestamp=datetime.utcnow(),
            protocol=ProtocolType.MQTT, topic="shelly/device-1/status",
        )
        result = await adapter.parse_request(req)
        assert result.parsed_intent == CommandType.GET_STATE

    @pytest.mark.asyncio
    async def test_parse_request_delegates_ws(self, adapter):
        req = InterceptedRequest(
            device_id="shelly-plus-1", timestamp=datetime.utcnow(),
            protocol=ProtocolType.WEBSOCKET, path="/ws/device-1/status",
        )
        result = await adapter.parse_request(req)
        assert result.parsed_intent == CommandType.GET_STATE


# ---------------------------------------------------------------------------
#  Handle request
# ---------------------------------------------------------------------------

class TestHandleRequest:
    @pytest.mark.asyncio
    async def test_get_state_returns_power(self, adapter):
        device_id = "shelly-1-abc123"
        await adapter.on_device_connect(device_id, {
            "model": "shelly1pm", "fw": "20230913-123456",
            "power": 15.2, "temperature": 22.5, "humidity": 60.0,
        })

        req = InterceptedRequest(
            device_id=device_id, timestamp=datetime.utcnow(),
            protocol=ProtocolType.HTTP, method="GET", path="/status",
        )
        await adapter.parse_request(req)
        result = await adapter.handle_request(req)

        assert result.success is True
        assert result.response["power"] == 15.2
        assert result.response["temperature"] == 22.5
        assert result.response["humidity"] == 60.0

    @pytest.mark.asyncio
    async def test_get_state_no_device_forwards(self, adapter):
        req = InterceptedRequest(
            device_id="unknown", timestamp=datetime.utcnow(),
            protocol=ProtocolType.HTTP, method="GET", path="/status",
        )
        await adapter.parse_request(req)
        result = await adapter.handle_request(req)
        assert result.success is False
        assert result.forwarded is True

    @pytest.mark.asyncio
    async def test_non_get_state_forwards(self, adapter):
        device_id = "shelly-1-abc123"
        await adapter.on_device_connect(device_id, {"model": "shelly1pm"})
        req = InterceptedRequest(
            device_id=device_id, timestamp=datetime.utcnow(),
            protocol=ProtocolType.HTTP, method="GET", path="/relay/0?turn=on",
        )
        await adapter.parse_request(req)
        result = await adapter.handle_request(req)
        assert result.success is False
        assert result.forwarded is True


# ---------------------------------------------------------------------------
#  Forward / Build response
# ---------------------------------------------------------------------------

class TestForwardAndBuild:
    @pytest.mark.asyncio
    async def test_forward_to_cloud(self, adapter):
        req = InterceptedRequest(
            device_id="test", timestamp=datetime.utcnow(),
            protocol=ProtocolType.HTTP,
        )
        result = await adapter.forward_to_cloud(req)
        assert result.success is False
        assert result.forwarded is True
        assert "Cloud forward not implemented" in (result.error or "")

    @pytest.mark.asyncio
    async def test_build_response_success(self, adapter):
        req = InterceptedRequest(
            device_id="test", timestamp=datetime.utcnow(),
            protocol=ProtocolType.HTTP,
        )
        result = CommandResult(success=True, response={"power": 100})
        response = await adapter.build_response(req, result)
        assert response["power"] == 100

    @pytest.mark.asyncio
    async def test_build_response_failure(self, adapter):
        req = InterceptedRequest(
            device_id="test", timestamp=datetime.utcnow(),
            protocol=ProtocolType.HTTP,
        )
        result = CommandResult(success=False, error="fail")
        response = await adapter.build_response(req, result)
        assert response["success"] is False


# ---------------------------------------------------------------------------
#  Device lifecycle
# ---------------------------------------------------------------------------

class TestDeviceLifecycle:
    @pytest.mark.asyncio
    async def test_on_device_connect(self, adapter):
        await adapter.on_device_connect("shelly-1", {"model": "shelly1pm", "fw": "v1.0"})
        info = await adapter.get_device_info("shelly-1")
        assert info is not None
        assert info.device_id == "shelly-1"
        assert info.model == "shelly1pm"
        assert info.firmware_version == "v1.0"
        assert DeviceCapability.POWER_MONITORING in info.capabilities
        assert DeviceCapability.INDOOR_TEMP_SENSOR in info.capabilities

    @pytest.mark.asyncio
    async def test_on_device_disconnect(self, adapter):
        await adapter.on_device_connect("shelly-1", {"model": "shelly1pm"})
        await adapter.on_device_disconnect("shelly-1")
        info = await adapter.get_device_info("shelly-1")
        assert info is None

    @pytest.mark.asyncio
    async def test_get_device_state(self, adapter):
        await adapter.on_device_connect("shelly-1", {
            "power": 12.3, "temperature": 25.0, "humidity": 55.0,
        })
        state = await adapter.get_device_state("shelly-1")
        assert state is not None
        assert state.power_watts == 12.3
        assert state.temp_actual == 25.0
        assert state.humidity == 55.0
        assert state.source == "device"
        assert state.quality == "good"

    @pytest.mark.asyncio
    async def test_get_device_state_not_found(self, adapter):
        state = await adapter.get_device_state("nonexistent")
        assert state is None

    @pytest.mark.asyncio
    async def test_send_command_not_implemented(self, adapter):
        cmd = Command(device_id="shelly-1", command_type=CommandType.GET_STATE)
        result = await adapter.send_command("shelly-1", cmd)
        assert result.success is False
        assert "not implemented" in (result.error or "")

    @pytest.mark.asyncio
    async def test_on_device_disconnect_unknown(self, adapter):
        """Disconnecting an untracked device should be safe."""
        await adapter.on_device_disconnect("unknown-device")
        # No exception is the test


# ---------------------------------------------------------------------------
#  Parse request routing
# ---------------------------------------------------------------------------

class TestParseRequestRouting:
    @pytest.mark.asyncio
    async def test_http_routed_to_parse_http(self, adapter):
        req = InterceptedRequest(
            device_id="shelly-1", timestamp=datetime.utcnow(),
            protocol=ProtocolType.HTTPS, method="GET", path="/status",
        )
        result = await adapter.parse_request(req)
        assert result.parsed_intent == CommandType.GET_STATE

    @pytest.mark.asyncio
    async def test_unhandled_protocol_passthrough(self, adapter):
        req = InterceptedRequest(
            device_id="shelly-1", timestamp=datetime.utcnow(),
            protocol=ProtocolType.MODBUS,  # not in Shelly's list
            parsed_intent=CommandType.UNKNOWN,
        )
        result = await adapter.parse_request(req)
        assert result.parsed_intent == CommandType.UNKNOWN
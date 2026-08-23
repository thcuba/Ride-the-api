"""
Tests for the MQTT Protocol Adapter (generic, vendor-neutral).
"""

from datetime import UTC, datetime

import pytest

from adapters.base import (
    Command,
    CommandResult,
    CommandType,
    DeviceCapability,
    InterceptedRequest,
    ProtocolType,
)
from adapters.mqtt import MQTTProtocolAdapter

# ---------------------------------------------------------------------------
#  Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def adapter():
    return MQTTProtocolAdapter(vendor="mqtt", config={})


# ---------------------------------------------------------------------------
#  Metadata
# ---------------------------------------------------------------------------


class TestMetadata:
    def test_vendor_code(self):
        assert MQTTProtocolAdapter.VENDOR_CODE == "mqtt"

    def test_supported_protocols(self, adapter):
        protocols = adapter.supported_protocols
        assert ProtocolType.MQTT in protocols
        assert ProtocolType.MQTTS in protocols
        assert ProtocolType.HTTP not in protocols

    def test_vendor_hostnames_empty(self, adapter):
        assert adapter.vendor_hostnames == []


# ---------------------------------------------------------------------------
#  Topic → intent (via _resolve_intent)
# ---------------------------------------------------------------------------


class TestTopicIntent:
    def test_status_topic(self, adapter):
        intent = adapter._resolve_intent("device-1/status", {})
        assert intent == CommandType.GET_STATE

    def test_status_subtopic(self, adapter):
        intent = adapter._resolve_intent("device-1/status/power", {})
        assert intent == CommandType.GET_STATE

    def test_telemetry_topic(self, adapter):
        intent = adapter._resolve_intent("device-1/telemetry", {})
        assert intent == CommandType.GET_STATE

    def test_health_topic(self, adapter):
        intent = adapter._resolve_intent("device-1/health", {})
        assert intent == CommandType.GET_STATE

    def test_config_topic(self, adapter):
        intent = adapter._resolve_intent("device-1/config", {})
        assert intent == CommandType.GET_STATE

    def test_turn_on_topic(self, adapter):
        intent = adapter._resolve_intent("device-1/turn_on", {})
        assert intent == CommandType.TURN_ON

    def test_turn_off_topic(self, adapter):
        intent = adapter._resolve_intent("device-1/turn_off", {})
        assert intent == CommandType.TURN_OFF

    def test_reboot_topic(self, adapter):
        intent = adapter._resolve_intent("device-1/reboot", {})
        assert intent == CommandType.REBOOT

    def test_reset_topic(self, adapter):
        intent = adapter._resolve_intent("device-1/reset", {})
        assert intent == CommandType.FACTORY_RESET

    def test_firmware_topic(self, adapter):
        intent = adapter._resolve_intent("device-1/firmware", {})
        assert intent == CommandType.FIRMWARE_CHECK

    def test_ota_topic(self, adapter):
        intent = adapter._resolve_intent("device-1/ota", {})
        assert intent == CommandType.FIRMWARE_UPDATE

    def test_power_topic(self, adapter):
        intent = adapter._resolve_intent("device-1/power", {})
        assert intent == CommandType.GET_STATE

    def test_will_topic(self, adapter):
        intent = adapter._resolve_intent("device-1/will", {})
        assert intent == CommandType.UNKNOWN

    def test_unknown_topic(self, adapter):
        intent = adapter._resolve_intent("device-1/custom/thing", {})
        assert intent == CommandType.UNKNOWN

    def test_empty_topic(self, adapter):
        intent = adapter._resolve_intent("", {})
        assert intent == CommandType.UNKNOWN


# ---------------------------------------------------------------------------
#  set/cmd/command topic patterns
# ---------------------------------------------------------------------------


class TestSetCommandTopics:
    def test_set_on(self, adapter):
        intent = adapter._resolve_intent("device-1/set/on", {})
        assert intent == CommandType.TURN_ON

    def test_set_off(self, adapter):
        intent = adapter._resolve_intent("device-1/set/off", {})
        assert intent == CommandType.TURN_OFF

    def test_set_temperature(self, adapter):
        intent = adapter._resolve_intent("device-1/set/temperature", {})
        assert intent == CommandType.SET_TEMPERATURE

    def test_set_mode(self, adapter):
        intent = adapter._resolve_intent("device-1/set/mode", {})
        assert intent == CommandType.SET_MODE

    def test_set_unknown(self, adapter):
        # set with no known action → falls through to payload
        intent = adapter._resolve_intent("device-1/set", {})
        assert intent == CommandType.UNKNOWN

    def test_cmd_on(self, adapter):
        intent = adapter._resolve_intent("device-1/cmd/on", {})
        assert intent == CommandType.TURN_ON

    def test_cmd_reboot(self, adapter):
        intent = adapter._resolve_intent("device-1/cmd/reboot", {})
        assert intent == CommandType.REBOOT

    def test_command_ota(self, adapter):
        intent = adapter._resolve_intent("device-1/command/ota", {})
        assert intent == CommandType.FIRMWARE_UPDATE


# ---------------------------------------------------------------------------
#  Payload-based intent resolution
# ---------------------------------------------------------------------------


class TestPayloadIntent:
    def test_action_on(self, adapter):
        intent = adapter._payload_intent({"action": "on"})
        assert intent == CommandType.TURN_ON

    def test_command_off(self, adapter):
        intent = adapter._payload_intent({"command": "off"})
        assert intent == CommandType.TURN_OFF

    def test_value_true(self, adapter):
        intent = adapter._payload_intent({"value": "true"})
        assert intent == CommandType.TURN_ON

    def test_state_false(self, adapter):
        intent = adapter._payload_intent({"state": "false"})
        assert intent == CommandType.TURN_OFF

    def test_cmd_reboot(self, adapter):
        intent = adapter._payload_intent({"cmd": "reboot"})
        assert intent == CommandType.REBOOT

    def test_cmd_reset(self, adapter):
        intent = adapter._payload_intent({"cmd": "reset"})
        assert intent == CommandType.FACTORY_RESET

    def test_action_cool(self, adapter):
        intent = adapter._payload_intent({"action": "cool"})
        assert intent == CommandType.SET_MODE

    def test_action_heat(self, adapter):
        intent = adapter._payload_intent({"action": "heat"})
        assert intent == CommandType.SET_MODE

    def test_boolean_on_true(self, adapter):
        intent = adapter._payload_intent({"on": True})
        assert intent == CommandType.TURN_ON

    def test_boolean_on_false(self, adapter):
        intent = adapter._payload_intent({"on": False})
        assert intent == CommandType.TURN_OFF

    def test_temperature_in_payload(self, adapter):
        intent = adapter._payload_intent({"temperature": 22.5})
        assert intent == CommandType.SET_TEMPERATURE

    def test_temp_alias(self, adapter):
        intent = adapter._payload_intent({"temp": 25})
        assert intent == CommandType.SET_TEMPERATURE

    def test_empty_dict(self, adapter):
        intent = adapter._payload_intent({})
        assert intent == CommandType.UNKNOWN

    def test_set_topic_falls_to_payload(self, adapter):
        """Topic 'device-1/set' with a known payload action."""
        intent = adapter._resolve_intent("device-1/set", {"action": "on"})
        assert intent == CommandType.TURN_ON


# ---------------------------------------------------------------------------
#  parse_request
# ---------------------------------------------------------------------------


class TestParseRequest:
    @pytest.mark.asyncio
    async def test_parse_request_sets_intent_and_params(self, adapter):
        req = InterceptedRequest(
            device_id="test-device",
            timestamp=datetime.now(UTC),
            protocol=ProtocolType.MQTT,
            topic="device-1/status",
            qos=1,
            retain=False,
        )
        result = await adapter.parse_request(req)
        assert result.parsed_intent == CommandType.GET_STATE
        assert result.parsed_params["topic"] == "device-1/status"
        assert result.parsed_params["qos"] == 1
        assert result.parsed_params["retain"] is False

    @pytest.mark.asyncio
    async def test_parse_request_uses_path_when_no_topic(self, adapter):
        req = InterceptedRequest(
            device_id="test-device",
            timestamp=datetime.now(UTC),
            protocol=ProtocolType.MQTTS,
            path="/device-1/reboot",
        )
        result = await adapter.parse_request(req)
        assert result.parsed_intent == CommandType.REBOOT

    @pytest.mark.asyncio
    async def test_parse_request_unknown_topic(self, adapter):
        req = InterceptedRequest(
            device_id="test-device",
            timestamp=datetime.now(UTC),
            protocol=ProtocolType.MQTT,
            topic="device-1/custom/x/y",
        )
        result = await adapter.parse_request(req)
        assert result.parsed_intent == CommandType.UNKNOWN


# ---------------------------------------------------------------------------
#  handle_request / forward / response
# ---------------------------------------------------------------------------


class TestHandleRequest:
    @pytest.mark.asyncio
    async def test_get_state_returns_sensor_data(self, adapter):
        device_id = "sensor-1"
        await adapter.on_device_connect(
            device_id,
            {
                "model": "temp-sensor",
                "temperature_actual": 23.0,
                "humidity": 65.0,
                "power_consumption": 0.5,
            },
        )
        req = InterceptedRequest(
            device_id=device_id,
            timestamp=datetime.now(UTC),
            protocol=ProtocolType.MQTT,
            topic="sensor-1/status",
        )
        await adapter.parse_request(req)
        result = await adapter.handle_request(req)
        assert result.success is True
        assert result.response["temperature_actual"] == 23.0  # noqa: PLR2004
        assert result.response["humidity"] == 65.0  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_get_state_no_device_forwards(self, adapter):
        req = InterceptedRequest(
            device_id="unknown",
            timestamp=datetime.now(UTC),
            protocol=ProtocolType.MQTT,
            topic="unknown/status",
        )
        await adapter.parse_request(req)
        result = await adapter.handle_request(req)
        assert result.success is False
        assert result.forwarded is True

    @pytest.mark.asyncio
    async def test_non_get_state_forwards(self, adapter):
        device_id = "device-1"
        await adapter.on_device_connect(device_id, {"model": "test"})
        req = InterceptedRequest(
            device_id=device_id,
            timestamp=datetime.now(UTC),
            protocol=ProtocolType.MQTT,
            topic="device-1/turn_on",
        )
        await adapter.parse_request(req)
        result = await adapter.handle_request(req)
        assert result.success is False
        assert result.forwarded is True

    @pytest.mark.asyncio
    async def test_forward_to_cloud(self, adapter):
        req = InterceptedRequest(
            device_id="test",
            timestamp=datetime.now(UTC),
            protocol=ProtocolType.MQTT,
        )
        result = await adapter.forward_to_cloud(req)
        assert result.success is False
        assert result.forwarded is True

    @pytest.mark.asyncio
    async def test_build_response_success(self, adapter):
        req = InterceptedRequest(
            device_id="test",
            timestamp=datetime.now(UTC),
            protocol=ProtocolType.MQTT,
        )
        result = CommandResult(success=True, response={"temp": 25})
        resp = await adapter.build_response(req, result)
        assert resp["temp"] == 25  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_build_response_error(self, adapter):
        req = InterceptedRequest(
            device_id="test",
            timestamp=datetime.now(UTC),
            protocol=ProtocolType.MQTT,
        )
        result = CommandResult(success=False, error="fail")
        resp = await adapter.build_response(req, result)
        assert "error" in resp


# ---------------------------------------------------------------------------
#  Device lifecycle
# ---------------------------------------------------------------------------


class TestDeviceLifecycle:
    @pytest.mark.asyncio
    async def test_on_device_connect(self, adapter):
        await adapter.on_device_connect("mqtt-dev-1", {"model": "v1"})
        info = await adapter.get_device_info("mqtt-dev-1")
        assert info is not None
        assert info.device_id == "mqtt-dev-1"
        assert info.model == "v1"
        assert DeviceCapability.POWER_MONITORING in info.capabilities

    @pytest.mark.asyncio
    async def test_on_device_disconnect(self, adapter):
        await adapter.on_device_connect("mqtt-dev-1", {"model": "v1"})
        await adapter.on_device_disconnect("mqtt-dev-1")
        info = await adapter.get_device_info("mqtt-dev-1")
        assert info is None

    @pytest.mark.asyncio
    async def test_device_state(self, adapter):
        await adapter.on_device_connect(
            "sensor-1",
            {
                "temperature_setpoint": 22.0,
                "temperature_actual": 22.5,
                "on_off": True,
            },
        )
        state = await adapter.get_device_state("sensor-1")
        assert state is not None
        assert state.temp_target == 22.0  # noqa: PLR2004
        assert state.temp_actual == 22.5  # noqa: PLR2004
        assert state.on_off is True

    @pytest.mark.asyncio
    async def test_device_state_not_found(self, adapter):
        state = await adapter.get_device_state("nonexistent")
        assert state is None

    @pytest.mark.asyncio
    async def test_send_command_not_implemented(self, adapter):
        cmd = Command(device_id="test", command_type=CommandType.GET_STATE)
        result = await adapter.send_command("test", cmd)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_disconnect_unknown_is_safe(self, adapter):
        await adapter.on_device_disconnect("unknown-device")
        # No exception is the test

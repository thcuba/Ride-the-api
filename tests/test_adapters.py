"""
Tests for protocol adapters.
"""

import pytest
from datetime import datetime

from adapters.base import (
    ProtocolType,
    CommandType,
    DeviceCapability,
    DeviceInfo,
    DeviceState,
    Command,
    CommandResult,
    InterceptedRequest,
    ProtocolAdapter,
    ProtocolAdapterRegistry,
)
from adapters.example import ExampleProtocolAdapter


class TestProtocolAdapterBase:
    """Test base adapter functionality."""
    
    def test_intercepted_request_creation(self):
        """Test creating intercepted request."""
        request = InterceptedRequest(
            device_id="test_device",
            timestamp=datetime.utcnow(),
            protocol=ProtocolType.HTTPS,
            method="POST",
            path="/v1.0/devices/test_device/commands",
            headers={"Content-Type": "application/json"},
            body={"commands": [{"code": "temp_set", "value": 240}]},
        )
        
        assert request.device_id == "test_device"
        assert request.protocol == ProtocolType.HTTPS
        assert request.parsed_intent == CommandType.UNKNOWN
    
    def test_device_state_creation(self):
        """Test creating device state."""
        state = DeviceState(
            device_id="test_device",
            timestamp=datetime.utcnow(),
            temp_target=24.0,
            temp_actual=23.5,
            mode="cool",
            fan_speed="auto",
        )
        
        assert state.temp_target == 24.0
        assert state.mode == "cool"
    
    def test_command_creation(self):
        """Test creating command."""
        command = Command(
            device_id="test_device",
            command_type=CommandType.SET_TEMPERATURE,
            params={"temperature": 25.0},
            source="edge_auto",
            confidence=0.9,
        )
        
        assert command.command_type == CommandType.SET_TEMPERATURE
        assert command.params["temperature"] == 25.0
        assert command.confidence == 0.9
    
    def test_command_result(self):
        """Test command result."""
        result = CommandResult(
            success=True,
            response={"status": "ok"},
            forwarded=False,
        )
        
        assert result.success is True
        assert result.forwarded is False


class TestExampleProtocolAdapter:
    """Test Example protocol adapter."""
    
    @pytest.fixture
        def example_adapter(self):
            """Create example adapter instance."""
            return ExampleProtocolAdapter("example", {
            "region": "eu",
            "api_version": "v1.0",
        })
    
        def test_supported_protocols(self, example_adapter):
        """Test supported protocols."""
            protocols = example_adapter.supported_protocols
        assert ProtocolType.MQTT in protocols
        assert ProtocolType.MQTTS in protocols
        assert ProtocolType.HTTPS in protocols
        assert ProtocolType.HTTP in protocols
    
        def test_vendor_hostnames(self, example_adapter):
        """Test vendor hostnames."""
            hostnames = example_adapter.vendor_hostnames
            assert "mqtt.example.com" in hostnames
            assert "api.example.com" in hostnames
            assert "openapi.example.com" in hostnames
    
        def test_dp_codes_mapping(self, example_adapter):
        """Test DP code mappings."""
        # Standard codes
            assert example_adapter.DP_CODES["power"] == "1"
            assert example_adapter.DP_CODES["temp_set"] == "3"
            assert example_adapter.DP_CODES["mode"] == "2"
        
        # Reverse mapping
            assert example_adapter.DP_CODES_REV["1"] == "power"
            assert example_adapter.DP_CODES_REV["3"] == "temp_set"
    
        def test_mode_mapping(self, example_adapter):
            """Test mode mapping vendor <-> Standard."""
            # Vendor to standard
            assert example_adapter.MODE_VENDOR_TO_STD["cold"] == "cool"
            assert example_adapter.MODE_VENDOR_TO_STD["hot"] == "heat"
            assert example_adapter.MODE_VENDOR_TO_STD["wet"] == "dry"
            assert example_adapter.MODE_VENDOR_TO_STD["wind"] == "fan"
            assert example_adapter.MODE_VENDOR_TO_STD["auto"] == "auto"
        
            # Standard to vendor
            assert example_adapter.MODE_STD_TO_VENDOR["cool"] == "cold"
            assert example_adapter.MODE_STD_TO_VENDOR["heat"] == "hot"

        def test_fan_mapping(self, example_adapter):
        """Test fan speed mapping."""
            assert example_adapter.FAN_VENDOR_TO_STD["low"] == "low"
            assert example_adapter.FAN_VENDOR_TO_STD["medium"] == "medium"
            assert example_adapter.FAN_VENDOR_TO_STD["high"] == "high"
            assert example_adapter.FAN_VENDOR_TO_STD["auto"] == "auto"

        def test_dp_params_to_intent(self, example_adapter):
        """Test mapping DP params to command intent."""
        # Power on
            assert example_adapter._dp_params_to_intent({"power": True}) == CommandType.TURN_ON
            assert example_adapter._dp_params_to_intent({"power": False}) == CommandType.TURN_OFF

        # Temperature
            assert example_adapter._dp_params_to_intent({"temp_set": 240}) == CommandType.SET_TEMPERATURE

        # Mode
            assert example_adapter._dp_params_to_intent({"mode": "cold"}) == CommandType.SET_MODE

        # Fan
            assert example_adapter._dp_params_to_intent({"fan_speed": "high"}) == CommandType.SET_FAN_SPEED

        # Swing
            assert example_adapter._dp_params_to_intent({"swing": True}) == CommandType.SET_SWING

        # Unknown
            assert example_adapter._dp_params_to_intent({"unknown": "value"}) == CommandType.UNKNOWN

        def test_command_to_dps(self, example_adapter):
            """Test converting standard command to DPs."""
        # Turn on
        cmd = Command(
            device_id="test",
            command_type=CommandType.TURN_ON,
            params={},
        )
            dps = example_adapter._command_to_dps(cmd)
        assert len(dps) == 1
        assert dps[0]["code"] == "1"
        assert dps[0]["value"] is True

        # Turn off
        cmd = Command(
            device_id="test",
            command_type=CommandType.TURN_OFF,
            params={},
        )
            dps = example_adapter._command_to_dps(cmd)
        assert dps[0]["code"] == "1"
        assert dps[0]["value"] is False

        # Set temperature
        cmd = Command(
            device_id="test",
            command_type=CommandType.SET_TEMPERATURE,
            params={"temperature": 25.0},
        )
            dps = example_adapter._command_to_dps(cmd)
        assert dps[0]["code"] == "3"
        assert dps[0]["value"] == 250  # 25.0 * 10

        # Set mode
        cmd = Command(
            device_id="test",
            command_type=CommandType.SET_MODE,
            params={"mode": "cool"},
        )
            dps = example_adapter._command_to_dps(cmd)
        assert dps[0]["code"] == "2"
        assert dps[0]["value"] == "cold"

        # Set fan
        cmd = Command(
            device_id="test",
            command_type=CommandType.SET_FAN_SPEED,
            params={"fan_speed": "high"},
        )
            dps = example_adapter._command_to_dps(cmd)
        assert dps[0]["code"] == "5"
        assert dps[0]["value"] == "high"
    
    @pytest.mark.asyncio
        async def test_parse_mqtt_request(self, example_adapter):
            """Test parsing MQTT request."""
        request = InterceptedRequest(
            device_id="",
            timestamp=datetime.utcnow(),
            protocol=ProtocolType.MQTT,
            topic="thing/command/device_123",
            body={
                "data": {
                    "1": True,   # power on
                    "3": 240,    # temp 24.0
                }
            },
        )

            parsed = await example_adapter.parse_request(request)

        assert parsed.device_id == "device_123"
        assert parsed.parsed_intent == CommandType.TURN_ON
        assert parsed.parsed_params["power"] is True
        assert parsed.parsed_params["temp_set"] == 240

    @pytest.mark.asyncio
        async def test_parse_http_request(self, example_adapter):
            """Test parsing HTTP request."""
        request = InterceptedRequest(
            device_id="",
            timestamp=datetime.utcnow(),
            protocol=ProtocolType.HTTPS,
            method="POST",
            path="/v1.0/devices/device_456/commands",
            body={
                "commands": [
                    {"code": "mode", "value": "cold"},
                    {"code": "temp_set", "value": 260},
                ]
            },
        )

            parsed = await example_adapter.parse_request(request)

        assert parsed.device_id == "device_456"
        assert parsed.parsed_intent == CommandType.SET_MODE
        assert parsed.parsed_params["mode"] == "cold"
        assert parsed.parsed_params["temp_set"] == 260

    @pytest.mark.asyncio
        async def test_build_mqtt_response(self, example_adapter):
        """Test building MQTT response."""
        request = InterceptedRequest(
            device_id="device_123",
            timestamp=datetime.utcnow(),
            protocol=ProtocolType.MQTT,
            parsed_intent=CommandType.GET_STATE,
        )

        result = CommandResult(
            success=True,
            response={
                "power": True,
                "temp_set": 240,
                "mode": "cool",
            },
        )

            response = await example_adapter.build_response(request, result)
        
        assert response["type"] == "thing.status"
        assert response["bid"] == "device_123"
        assert "data" in response
        assert "dps" in response["data"]
        assert response["data"]["dps"]["1"] is True
        assert response["data"]["dps"]["3"] == 240

        def test_is_firmware_request(self, example_adapter):
        """Test firmware request detection."""
        # HTTP firmware paths
        request = InterceptedRequest(
            device_id="test",
            timestamp=datetime.utcnow(),
            protocol=ProtocolType.HTTPS,
            path="/v1.0/devices/test/firmware/upgrade",
        )
            assert example_adapter.is_firmware_request(request) is True

        request.path = "/v1.0/devices/test/ota"
            assert example_adapter.is_firmware_request(request) is True

        # MQTT firmware topics
        request = InterceptedRequest(
            device_id="test",
            timestamp=datetime.utcnow(),
            protocol=ProtocolType.MQTT,
            topic="thing/fota/device_123",
        )
            assert example_adapter.is_firmware_request(request) is True

        # Non-firmware
        request = InterceptedRequest(
            device_id="test",
            timestamp=datetime.utcnow(),
            protocol=ProtocolType.HTTPS,
            path="/v1.0/devices/test/status",
        )
            assert example_adapter.is_firmware_request(request) is False

        def test_is_auth_request(self, example_adapter):
        """Test auth request detection."""
        request = InterceptedRequest(
            device_id="test",
            timestamp=datetime.utcnow(),
            protocol=ProtocolType.HTTPS,
            path="/v1.0/token",
        )
            assert example_adapter.is_auth_request(request) is True

        request.path = "/v1.0/login"
            assert example_adapter.is_auth_request(request) is True

        request.path = "/v1.0/devices/test/status"
            assert example_adapter.is_auth_request(request) is False


class TestProtocolAdapterRegistry:
    """Test adapter registry."""
    
    def test_registry_registration(self):
        """Test registering adapters."""
        registry = ProtocolAdapterRegistry()
        
        # Create mock adapter
        class MockAdapter(ProtocolAdapter):
            @property
            def supported_protocols(self):
                return [ProtocolType.HTTP]
            
            @property
            def vendor_hostnames(self):
                return ["mock.example.com"]
            
            async def parse_request(self, request):
                return request
            
            async def handle_request(self, request):
                return CommandResult(success=True)
            
            async def forward_to_cloud(self, request):
                return CommandResult(success=True)
            
            async def build_response(self, request, result):
                return {}
            
            async def get_device_info(self, device_id):
                return None
            
            async def get_device_state(self, device_id):
                return None
            
            async def send_command(self, device_id, command):
                return CommandResult(success=True)
        
        adapter = MockAdapter("mock", {})
        registry.register(adapter)
        
        assert registry.get_adapter("mock") == adapter
        assert registry.get_adapter_by_hostname("mock.example.com") == adapter
        assert "mock" in registry.list_vendors()
    
    def test_get_adapter_by_protocol(self):
        """Test getting adapters by protocol."""
        registry = ProtocolAdapterRegistry()
        
        class MockAdapter(ProtocolAdapter):
            @property
            def supported_protocols(self):
                return [ProtocolType.MQTT, ProtocolType.HTTP]
            
            @property
            def vendor_hostnames(self):
                return []
            
            async def parse_request(self, request):
                return request
            
            async def handle_request(self, request):
                return CommandResult(success=True)
            
            async def forward_to_cloud(self, request):
                return CommandResult(success=True)
            
            async def build_response(self, request, result):
                return {}
            
            async def get_device_info(self, device_id):
                return None
            
            async def get_device_state(self, device_id):
                return None
            
            async def send_command(self, device_id, command):
                return CommandResult(success=True)
        
        adapter = MockAdapter("mock", {})
        registry.register(adapter)
        
        mqtt_adapters = registry.get_adapter_by_protocol(ProtocolType.MQTT)
        assert adapter in mqtt_adapters
        
        http_adapters = registry.get_adapter_by_protocol(ProtocolType.HTTP)
        assert adapter in http_adapters
        
        https_adapters = registry.get_adapter_by_protocol(ProtocolType.HTTPS)
        assert adapter not in https_adapters


# ═══════════════════════════════════════════════════════════════════════════════
# RUN TESTS
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
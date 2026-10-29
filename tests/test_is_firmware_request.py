"""Tests for `is_firmware_request` and `is_auth_request` in `adapters.base`."""

from datetime import UTC, datetime
from typing import Any

import pytest

from adapters.base import (
    Command,
    CommandResult,
    DeviceInfo,
    DeviceState,
    InterceptedRequest,
    ProtocolAdapter,
    ProtocolType,
)


class DummyAdapter(ProtocolAdapter):
    """Dummy concrete adapter subclassing ProtocolAdapter for testing default methods."""

    @property
    def supported_protocols(self) -> list[ProtocolType]:
        return [ProtocolType.HTTP, ProtocolType.MQTT]

    @property
    def vendor_hostnames(self) -> list[str]:
        return ["dummy.local"]

    async def parse_request(self, request: InterceptedRequest) -> InterceptedRequest:
        return request

    async def handle_request(self, request: InterceptedRequest) -> CommandResult:
        _ = request
        return CommandResult(success=True)

    async def forward_to_cloud(self, request: InterceptedRequest) -> CommandResult:
        _ = request
        return CommandResult(success=True)

    async def build_response(
        self, request: InterceptedRequest, result: CommandResult
    ) -> dict[str, Any]:
        _ = (request, result)
        return {}

    async def get_device_info(self, device_id: str) -> DeviceInfo | None:
        _ = device_id
        return None

    async def get_device_state(self, device_id: str) -> DeviceState | None:
        _ = device_id
        return None

    async def send_command(self, device_id: str, command: Command) -> CommandResult:
        _ = (device_id, command)
        return CommandResult(success=True)


@pytest.fixture
def adapter() -> DummyAdapter:
    return DummyAdapter(vendor="dummy", config={})


def _req(path: str | None = None, topic: str | None = None) -> InterceptedRequest:
    return InterceptedRequest(
        device_id="dev-123",
        timestamp=datetime.now(UTC),
        protocol=ProtocolType.HTTP if path else ProtocolType.MQTT,
        path=path,
        topic=topic,
    )


class TestIsFirmwareRequest:
    """Test suite for ProtocolAdapter.is_firmware_request."""

    @pytest.mark.parametrize(
        "path",
        [
            "/fota",
            "/fota/download",
            "/firmware",
            "/firmware/v2/check",
            "/ota",
            "/ota/start",
            "/update",
            "/system/update",
            "/upgrade",
            "/device/upgrade/status",
        ],
    )
    def test_firmware_paths_positive(self, adapter: DummyAdapter, path: str):
        req = _req(path=path)
        assert adapter.is_firmware_request(req) is True

    @pytest.mark.parametrize(
        "path",
        [
            "/FOTA",
            "/Firmware/Check",
            "/OTA/Download",
            "/UPDATE",
            "/UpGrade",
        ],
    )
    def test_firmware_paths_case_insensitive(self, adapter: DummyAdapter, path: str):
        req = _req(path=path)
        assert adapter.is_firmware_request(req) is True

    @pytest.mark.parametrize(
        "topic",
        [
            "fota",
            "device/fota/request",
            "firmware",
            "v1/firmware/update",
            "ota",
            "ota/check",
        ],
    )
    def test_firmware_topics_positive(self, adapter: DummyAdapter, topic: str):
        req = _req(topic=topic)
        assert adapter.is_firmware_request(req) is True

    @pytest.mark.parametrize(
        "topic",
        [
            "FOTA",
            "Device/FIRMWARE/Status",
            "OTA/Check",
        ],
    )
    def test_firmware_topics_case_insensitive(self, adapter: DummyAdapter, topic: str):
        req = _req(topic=topic)
        assert adapter.is_firmware_request(req) is True

    @pytest.mark.parametrize(
        "path",
        [
            "/v1/status",
            "/device/info",
            "/api/control",
            "/telemetry",
            "/healthz",
        ],
    )
    def test_non_firmware_paths_negative(self, adapter: DummyAdapter, path: str):
        req = _req(path=path)
        assert adapter.is_firmware_request(req) is False

    @pytest.mark.parametrize(
        "topic",
        [
            "telemetry",
            "sensors/temperature",
            "status/power",
            "commands/set_state",
        ],
    )
    def test_non_firmware_topics_negative(self, adapter: DummyAdapter, topic: str):
        req = _req(topic=topic)
        assert adapter.is_firmware_request(req) is False

    def test_edge_cases(self, adapter: DummyAdapter):
        # Empty/None fields
        assert adapter.is_firmware_request(_req(path=None, topic=None)) is False
        assert adapter.is_firmware_request(_req(path="", topic="")) is False

        # Request matching path but not topic
        assert adapter.is_firmware_request(_req(path="/fota", topic="status")) is True

        # Request matching topic but not path
        assert adapter.is_firmware_request(_req(path="/status", topic="fota")) is True

        # Request matching both
        assert adapter.is_firmware_request(_req(path="/ota", topic="firmware")) is True


class TestIsAuthRequest:
    """Test suite for ProtocolAdapter.is_auth_request."""

    @pytest.mark.parametrize(
        "path",
        [
            "/auth",
            "/auth/login",
            "/login",
            "/token",
            "/api/v1/token",
            "/oauth",
            "/oauth/token",
            "/session",
            "/session/create",
        ],
    )
    def test_auth_paths_positive(self, adapter: DummyAdapter, path: str):
        req = _req(path=path)
        assert adapter.is_auth_request(req) is True

    @pytest.mark.parametrize(
        "path",
        [
            "/AUTH",
            "/Login",
            "/TOKEN",
            "/OAuth/Authorize",
            "/SESSION",
        ],
    )
    def test_auth_paths_case_insensitive(self, adapter: DummyAdapter, path: str):
        req = _req(path=path)
        assert adapter.is_auth_request(req) is True

    @pytest.mark.parametrize(
        "path",
        [
            "/v1/status",
            "/device/control",
            "/telemetry",
        ],
    )
    def test_non_auth_paths_negative(self, adapter: DummyAdapter, path: str):
        req = _req(path=path)
        assert adapter.is_auth_request(req) is False

    def test_edge_cases(self, adapter: DummyAdapter):
        assert adapter.is_auth_request(_req(path=None)) is False
        assert adapter.is_auth_request(_req(path="")) is False


class CustomHeartbeatAdapter(DummyAdapter):
    """Subclass overriding on_heartbeat to verify custom behavior."""

    def __init__(self, vendor: str, config: dict[str, Any]) -> None:
        super().__init__(vendor, config)
        self.heartbeats: list[str] = []

    async def on_heartbeat(self, device_id: str) -> None:
        self.heartbeats.append(device_id)


class TestProtocolAdapterLifecycle:
    """Test suite for ProtocolAdapter lifecycle methods, including on_heartbeat."""

    @pytest.mark.asyncio
    async def test_on_heartbeat_default_noop(self, adapter: DummyAdapter):
        """Default on_heartbeat implementation completes without error."""
        res = await adapter.on_heartbeat("dev-123")
        assert res is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("device_id", ["", "dev-456", "device@home:123", "a" * 256])
    async def test_on_heartbeat_various_device_ids(self, adapter: DummyAdapter, device_id: str):
        """on_heartbeat handles different device ID formats gracefully."""
        res = await adapter.on_heartbeat(device_id)
        assert res is None

    @pytest.mark.asyncio
    async def test_on_device_connect_disconnect_default_noop(self, adapter: DummyAdapter):
        """Default on_device_connect and on_device_disconnect complete without error."""
        res_connect = await adapter.on_device_connect("dev-123", {"fw": "1.0"})
        assert res_connect is None

        res_disconnect = await adapter.on_device_disconnect("dev-123")
        assert res_disconnect is None

    @pytest.mark.asyncio
    async def test_on_heartbeat_subclass_override(self):
        """Subclasses can override on_heartbeat to track keepalive signals."""
        custom_adapter = CustomHeartbeatAdapter(vendor="custom", config={})
        await custom_adapter.on_heartbeat("dev-001")
        await custom_adapter.on_heartbeat("dev-002")

        assert custom_adapter.heartbeats == ["dev-001", "dev-002"]

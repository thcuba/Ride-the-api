"""Tests for base protocol adapter in `adapters.base`."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

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


class MockProtocolAdapter(ProtocolAdapter):
    """Minimal concrete subclass of ProtocolAdapter for testing base class behaviors."""

    @property
    def supported_protocols(self) -> list[ProtocolType]:
        return [ProtocolType.HTTP]

    @property
    def vendor_hostnames(self) -> list[str]:
        return ["base.local"]

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


class HeartbeatTrackingAdapter(MockProtocolAdapter):
    """Adapter that overrides on_heartbeat to update device session state."""

    async def on_heartbeat(self, device_id: str) -> None:
        session = self._device_sessions.setdefault(device_id, {})
        session["last_heartbeat"] = 123456789.0
        session["heartbeat_count"] = session.get("heartbeat_count", 0) + 1


@pytest.fixture
def base_adapter() -> MockProtocolAdapter:
    return MockProtocolAdapter(vendor="base_test", config={"key": "value"})


class TestProtocolAdapterHeartbeat:
    """Dedicated test suite for on_heartbeat in adapters/base/__init__.py."""

    @pytest.mark.asyncio
    async def test_on_heartbeat_default_returns_none(self, base_adapter: MockProtocolAdapter):
        """Default base implementation of on_heartbeat is a coroutine returning None."""
        result = await base_adapter.on_heartbeat("device-1")
        assert result is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "device_id",
        [
            "device-001",
            "shelly-123",
            "192.168.1.100",
            "",
            "dev:with:colons",
            "dev/with/slashes",
            "unicode-⚡-device",
        ],
    )
    async def test_on_heartbeat_device_id_inputs(
        self, base_adapter: MockProtocolAdapter, device_id: str
    ):
        """on_heartbeat accepts arbitrary valid and edge-case device IDs without error."""
        result = await base_adapter.on_heartbeat(device_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_on_heartbeat_with_device_session(self):
        """Test on_heartbeat interacting with _device_sessions when overridden."""
        expected_heartbeat_time = 123456789.0
        adapter = HeartbeatTrackingAdapter(vendor="tracking", config={})
        adapter._device_sessions["dev-1"] = {"connected": True}

        await adapter.on_heartbeat("dev-1")
        session = adapter._device_sessions["dev-1"]
        assert session["connected"] is True
        assert session["last_heartbeat"] == expected_heartbeat_time
        assert session["heartbeat_count"] == 1

        await adapter.on_heartbeat("dev-1")
        expected_count = 2
        assert adapter._device_sessions["dev-1"]["heartbeat_count"] == expected_count

    @pytest.mark.asyncio
    async def test_on_heartbeat_mocked_invocation(self, base_adapter: MockProtocolAdapter):
        """Verify on_heartbeat can be spied or mocked using unittest.mock.AsyncMock."""
        base_adapter.on_heartbeat = AsyncMock(wraps=base_adapter.on_heartbeat)
        await base_adapter.on_heartbeat("mock-device")
        base_adapter.on_heartbeat.assert_awaited_once_with("mock-device")

"""Tests for base protocol adapter in `adapters.base`."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

import adapters.base
from adapters import register_all_adapters
from adapters.base import (
    Command,
    CommandResult,
    DeviceInfo,
    DeviceState,
    InterceptedRequest,
    ProtocolAdapter,
    ProtocolAdapterRegistry,
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

    @pytest.mark.asyncio
    async def test_on_heartbeat_concurrent_calls(self):
        """Test concurrent execution of on_heartbeat across multiple devices."""
        adapter = HeartbeatTrackingAdapter(vendor="tracking", config={})
        num_devices = 10
        device_ids = [f"device-{i}" for i in range(num_devices)]

        results = await asyncio.gather(*(adapter.on_heartbeat(dev_id) for dev_id in device_ids))

        assert all(res is None for res in results)
        assert len(adapter._device_sessions) == num_devices
        for dev_id in device_ids:
            assert adapter._device_sessions[dev_id]["heartbeat_count"] == 1

    @pytest.mark.asyncio
    async def test_on_heartbeat_subclass_exception_propagation(self):
        """Exceptions raised in subclass on_heartbeat implementations propagate up."""

        class FailingHeartbeatAdapter(MockProtocolAdapter):
            async def on_heartbeat(self, device_id: str) -> None:
                raise RuntimeError(f"Heartbeat failed for {device_id}")

        adapter = FailingHeartbeatAdapter(vendor="failing", config={})
        with pytest.raises(RuntimeError, match="Heartbeat failed for dev-err"):
            await adapter.on_heartbeat("dev-err")

    @pytest.mark.asyncio
    async def test_on_heartbeat_uninitialized_session_handling(self):
        """Test on_heartbeat handles devices without pre-existing sessions."""
        expected_heartbeat_time = 123456789.0
        expected_second_count = 2
        adapter = HeartbeatTrackingAdapter(vendor="tracking", config={})
        device_id = "uninitialized-device"

        assert device_id not in adapter._device_sessions
        await adapter.on_heartbeat(device_id)
        assert device_id in adapter._device_sessions
        assert adapter._device_sessions[device_id]["heartbeat_count"] == 1
        assert adapter._device_sessions[device_id]["last_heartbeat"] == expected_heartbeat_time

        # Successive heartbeat increments counter
        await adapter.on_heartbeat(device_id)
        assert adapter._device_sessions[device_id]["heartbeat_count"] == expected_second_count


class TestProtocolAdapterRegistryListVendors:
    """Dedicated test suite for ProtocolAdapterRegistry.list_vendors."""

    def test_list_vendors_empty_registry(self):
        """An empty registry returns an empty list."""
        registry = ProtocolAdapterRegistry()
        assert registry.list_vendors() == []

    def test_list_vendors_single_adapter(self):
        """Registering a single adapter returns its vendor name."""
        registry = ProtocolAdapterRegistry()
        adapter = MockProtocolAdapter(vendor="vendor-alpha", config={})
        registry.register(adapter)

        assert registry.list_vendors() == ["vendor-alpha"]

    def test_list_vendors_multiple_adapters(self):
        """Registering multiple distinct adapters returns all vendor names in insertion order."""
        registry = ProtocolAdapterRegistry()
        adapter1 = MockProtocolAdapter(vendor="vendor-1", config={})
        adapter2 = MockProtocolAdapter(vendor="vendor-2", config={})
        adapter3 = MockProtocolAdapter(vendor="vendor-3", config={})

        registry.register(adapter1)
        registry.register(adapter2)
        registry.register(adapter3)

        assert registry.list_vendors() == ["vendor-1", "vendor-2", "vendor-3"]

    def test_list_vendors_reregister_existing_vendor(self):
        """Re-registering an adapter with the same vendor replaces the entry without duplicates."""
        registry = ProtocolAdapterRegistry()
        adapter1 = MockProtocolAdapter(vendor="vendor-dup", config={"v": 1})
        adapter2 = MockProtocolAdapter(vendor="vendor-dup", config={"v": 2})

        registry.register(adapter1)
        assert registry.list_vendors() == ["vendor-dup"]

        registry.register(adapter2)
        assert registry.list_vendors() == ["vendor-dup"]
        assert registry.get_adapter("vendor-dup") is adapter2

    def test_list_vendors_returns_copy_isolation(self):
        """list_vendors() returns a shallow copy that does not mutate internal state."""
        registry = ProtocolAdapterRegistry()
        adapter = MockProtocolAdapter(vendor="vendor-immutable", config={})
        registry.register(adapter)

        vendors = registry.list_vendors()
        vendors.append("injected-vendor")
        vendors[0] = "mutated"

        assert registry.list_vendors() == ["vendor-immutable"]

    def test_list_vendors_registered_all_adapters_integration(self):
        """register_all_adapters() includes standard built-in vendors."""
        registry = register_all_adapters()
        vendors = registry.list_vendors()

        expected_vendors = [
            "example",
            "coap_example",
            "modbus_example",
            "shelly",
            "zigbee",
            "zwave",
            "matter",
        ]
        for vendor in expected_vendors:
            assert vendor in vendors


class AuthHeaderExtendingAdapter(MockProtocolAdapter):
    """Subclass overriding is_auth_request to check for custom auth headers or paths."""

    def is_auth_request(self, request: InterceptedRequest) -> bool:
        # Check custom vendor header or fallback to base path check
        if request.headers.get("authorization") or request.headers.get("x-auth-token"):
            return True
        return super().is_auth_request(request)


def _make_request(
    path: str | None = None,
    protocol: ProtocolType = ProtocolType.HTTP,
    headers: dict[str, str] | None = None,
) -> InterceptedRequest:
    return InterceptedRequest(
        device_id="dev-auth-test",
        timestamp=123456789.0,
        protocol=protocol,
        path=path,
        headers=headers or {},
    )


class TestProtocolAdapterIsAuthRequest:
    """Dedicated test suite for is_auth_request in adapters/base/__init__.py."""

    @pytest.mark.parametrize(
        "path",
        [
            "/auth",
            "/auth/v1/login",
            "/api/auth",
            "/login",
            "/device/login",
            "/token",
            "/oauth/token",
            "/api/token/refresh",
            "/oauth",
            "/oauth2/authorize",
            "/session",
            "/api/session/new",
            "/user/session/terminate",
        ],
    )
    def test_auth_paths_detected(self, base_adapter: MockProtocolAdapter, path: str):
        """All canonical auth paths and subpaths are recognized as auth requests."""
        req = _make_request(path=path)
        assert base_adapter.is_auth_request(req) is True

    @pytest.mark.parametrize(
        "path",
        [
            "/AUTH",
            "/Auth/Login",
            "/LOGIN",
            "/device/LogIn",
            "/TOKEN",
            "/api/Token",
            "/OAuth",
            "/OAUTH2/token",
            "/SESSION",
            "/Session/Status",
        ],
    )
    def test_auth_paths_case_insensitive(self, base_adapter: MockProtocolAdapter, path: str):
        """Path matching is case-insensitive for auth keywords."""
        req = _make_request(path=path)
        assert base_adapter.is_auth_request(req) is True

    @pytest.mark.parametrize(
        "path",
        [
            "/api/status",
            "/device/info",
            "/telemetry/temperature",
            "/sensors/all",
            "/v1/control/power",
            "/health",
            "/metrics",
            "/ping",
            "/firmware/check",
            "/ota/update",
        ],
    )
    def test_non_auth_paths_rejected(self, base_adapter: MockProtocolAdapter, path: str):
        """Non-auth paths correctly evaluate to False."""
        req = _make_request(path=path)
        assert base_adapter.is_auth_request(req) is False

    @pytest.mark.parametrize(
        "protocol",
        [
            ProtocolType.HTTP,
            ProtocolType.HTTPS,
            ProtocolType.WEBSOCKET,
            ProtocolType.COAP,
            ProtocolType.MQTT,
        ],
    )
    def test_is_auth_request_across_protocols(
        self, base_adapter: MockProtocolAdapter, protocol: ProtocolType
    ):
        """is_auth_request behaves consistently across different protocol types."""
        auth_req = _make_request(path="/api/token", protocol=protocol)
        non_auth_req = _make_request(path="/api/telemetry", protocol=protocol)

        assert base_adapter.is_auth_request(auth_req) is True
        assert base_adapter.is_auth_request(non_auth_req) is False

    def test_edge_cases_empty_or_none_path(self, base_adapter: MockProtocolAdapter):
        """Requests with None or empty string paths return False safely."""
        req_none = _make_request(path=None)
        req_empty = _make_request(path="")

        assert base_adapter.is_auth_request(req_none) is False
        assert base_adapter.is_auth_request(req_empty) is False

    def test_path_with_query_string(self, base_adapter: MockProtocolAdapter):
        """Auth keyword present in path with query parameters is detected."""
        req = _make_request(path="/oauth?client_id=123&redirect_uri=callback")
        assert base_adapter.is_auth_request(req) is True

    def test_subclass_override_extending_behavior(self):
        """Subclasses can override is_auth_request and leverage super().is_auth_request."""
        custom_adapter = AuthHeaderExtendingAdapter(vendor="custom", config={})

        # Path-based auth detected via super()
        req_path = _make_request(path="/auth/renew")
        assert custom_adapter.is_auth_request(req_path) is True

        # Header-based auth detected via override
        req_header = _make_request(path="/data", headers={"authorization": "Bearer xyz"})
        assert custom_adapter.is_auth_request(req_header) is True

        req_custom_header = _make_request(path="/data", headers={"x-auth-token": "secret"})
        assert custom_adapter.is_auth_request(req_custom_header) is True

        # Neither path nor header is auth
        req_plain = _make_request(path="/data", headers={"content-type": "application/json"})
        assert custom_adapter.is_auth_request(req_plain) is False


class TestBaseAdapterReExports:
    """Verify exported symbols in adapters.base."""

    def test_basemodel_not_exported(self):
        """BaseModel is not imported or re-exported in adapters.base."""

        assert not hasattr(adapters.base, "BaseModel")

    def test_field_not_exported(self):
        """Field is not imported or re-exported in adapters.base."""

        assert not hasattr(adapters.base, "Field")

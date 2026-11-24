"""Tests for protocol adapters' forward_to_cloud implementations."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from adapters.base import CommandResult, InterceptedRequest, ProtocolType
from adapters.coap.adapter import CoAPProtocolAdapter
from adapters.modbus.adapter import ModbusProtocolAdapter
from adapters.mqtt.adapter import MQTTProtocolAdapter
from adapters.thread_matter.adapter import MatterProtocolAdapter
from adapters.zigbee.adapter import ZigbeeProtocolAdapter
from adapters.zwave.adapter import ZWaveProtocolAdapter


def _dummy_request(protocol: ProtocolType, headers: dict | None = None) -> InterceptedRequest:
    return InterceptedRequest(
        device_id="test-device-01",
        timestamp=time.time(),
        protocol=protocol,
        client_ip="192.168.1.50",
        method="POST",
        path="/api/v1/update",
        headers=headers or {},
        body={"status": "ok"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_cls", "protocol"),
    [
        (CoAPProtocolAdapter, ProtocolType.COAP),
        (MQTTProtocolAdapter, ProtocolType.MQTT),
        (ModbusProtocolAdapter, ProtocolType.MODBUS),
        (ZigbeeProtocolAdapter, ProtocolType.ZIGBEE),
        (ZWaveProtocolAdapter, ProtocolType.ZWAVE),
        (MatterProtocolAdapter, ProtocolType.MATTER),
    ],
)
async def test_adapter_forward_to_cloud_with_config(adapter_cls, protocol) -> None:  # noqa: ANN001
    """When cloud endpoint is configured, forward_intercepted is called with correct parameters."""
    config = {
        "cloud": {
            "hostname": "cloud.vendor-platform.com",
            "port": 8443,
            "tls": True,
        }
    }
    adapter = adapter_cls("test_vendor", config)
    req = _dummy_request(protocol)

    mock_res = CommandResult(
        success=True,
        forwarded=True,
        response={"cloud_ack": True},
        status_code=200,
    )

    patch_target = f"{adapter_cls.__module__}.forward_intercepted"
    with patch(patch_target, new_callable=AsyncMock) as mock_forward:
        mock_forward.return_value = mock_res
        res = await adapter.forward_to_cloud(req)

        assert res.success is True
        assert res.forwarded is True
        assert res.response == {"cloud_ack": True}
        mock_forward.assert_called_once_with(
            req,
            hostname="cloud.vendor-platform.com",
            port=8443,
            use_tls=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_cls", "protocol"),
    [
        (CoAPProtocolAdapter, ProtocolType.COAP),
        (MQTTProtocolAdapter, ProtocolType.MQTT),
        (ModbusProtocolAdapter, ProtocolType.MODBUS),
        (ZigbeeProtocolAdapter, ProtocolType.ZIGBEE),
        (ZWaveProtocolAdapter, ProtocolType.ZWAVE),
        (MatterProtocolAdapter, ProtocolType.MATTER),
    ],
)
async def test_adapter_forward_to_cloud_no_host(adapter_cls, protocol) -> None:  # noqa: ANN001
    """When no cloud host is configured or in request headers, forward_intercepted returns error."""
    adapter = adapter_cls("test_vendor", {})
    req = _dummy_request(protocol, headers={})

    res = await adapter.forward_to_cloud(req)
    assert res.success is False
    assert res.forwarded is False
    assert "no upstream host configured" in (res.error or "")

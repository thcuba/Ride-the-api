"""
Tests for the CoAP protocol adapter (intent parsing).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from adapters.base import CommandType, InterceptedRequest, ProtocolType
from adapters.coap import CoAPProtocolAdapter


@pytest.fixture
def adapter() -> CoAPProtocolAdapter:
    return CoAPProtocolAdapter(vendor="coap_example", config={})


def _req(path: str, method: str = "GET", body: dict | None = None) -> InterceptedRequest:
    return InterceptedRequest(
        device_id="dev-coap",
        timestamp=datetime.now(),
        protocol=ProtocolType.COAP,
        method=method,
        path=path,
        body=body,
    )


async def test_write_relay_on_is_turn_on(adapter):
    """A PUT with value 'on' on an actuator resolves to TURN_ON."""
    req = _req("/relay", method="PUT", body={"value": "on"})
    result = await adapter.parse_request(req)
    assert result.parsed_intent == CommandType.TURN_ON


async def test_write_relay_off_is_turn_off(adapter):
    """A PUT with value 'off' on an actuator must NOT resolve to TURN_ON."""
    req = _req("/relay", method="PUT", body={"value": "off"})
    result = await adapter.parse_request(req)
    assert result.parsed_intent == CommandType.TURN_OFF


async def test_read_actuator_is_get_state(adapter):
    """A GET on an actuator reports state, not a turn action."""
    req = _req("/relay", method="GET")
    result = await adapter.parse_request(req)
    assert result.parsed_intent == CommandType.GET_STATE

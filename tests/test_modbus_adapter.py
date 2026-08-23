"""
Tests for the Modbus protocol adapter (intent parsing on reads).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from adapters.base import CommandType, InterceptedRequest, ProtocolType
from adapters.modbus import ModbusProtocolAdapter


@pytest.fixture
def adapter() -> ModbusProtocolAdapter:
    return ModbusProtocolAdapter(vendor="modbus_example", config={})


def _req(function_code: int, register: int) -> InterceptedRequest:
    return InterceptedRequest(
        device_id="dev-modbus",
        timestamp=datetime.now(),
        protocol=ProtocolType.MODBUS,
        body={"function_code": function_code, "register": register},
    )


async def test_write_register_keeps_set_intent(adapter):
    """A write (0x06) to the mode register keeps a write intent."""
    req = _req(function_code=0x06, register=0x0002)
    result = await adapter.parse_request(req)
    assert result.parsed_intent == CommandType.SET_MODE


async def test_read_register_coerces_write_intent_to_get_state(adapter):
    """A read (0x03) of a writable register must report state, not SET_MODE."""
    req = _req(function_code=0x03, register=0x0002)
    result = await adapter.parse_request(req)
    assert result.parsed_intent == CommandType.GET_STATE


async def test_read_temperature_setpoint_is_get_state(adapter):
    """A read of the temperature-setpoint register (a write intent) is a read."""
    req = _req(function_code=0x03, register=0x0000)
    result = await adapter.parse_request(req)
    assert result.parsed_intent == CommandType.GET_STATE


async def test_read_on_off_is_get_state(adapter):
    """A read of the on_off register (TURN_ON) reports state, not a write."""
    req = _req(function_code=0x01, register=0x0004)
    result = await adapter.parse_request(req)
    assert result.parsed_intent == CommandType.GET_STATE

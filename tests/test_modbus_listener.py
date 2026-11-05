"""The Modbus listener serves registers and mirrors every access (C-21)."""

from __future__ import annotations

import asyncio
import socket

import pytest
from pymodbus.client import AsyncModbusTcpClient

from core.config import ModbusServerConfig
from core.protocol_servers.modbus_server import ModbusServerPlugin

WRITE_SINGLE_REGISTER, READ_HOLDING_REGISTERS = 6, 3
VALUE = 42


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.asyncio
async def test_register_access_is_served_and_mirrored():
    seen: list = []

    async def handler(request) -> None:  # noqa: ANN001
        seen.append(request)

    port = _free_port()
    plugin = ModbusServerPlugin(
        ModbusServerConfig(host="127.0.0.1", port=port, enabled=True), handler=handler
    )
    await plugin.start()
    client = AsyncModbusTcpClient("127.0.0.1", port=port)
    try:
        assert await client.connect()
        written = await client.write_register(address=1, value=VALUE, device_id=1)
        assert not written.isError()
        read = await client.read_holding_registers(address=1, count=1, device_id=1)
        assert not read.isError()
        assert read.registers == [VALUE]
        await asyncio.sleep(0.1)  # mirrored to the pipeline in the background
    finally:
        client.close()
        await plugin.stop()

    frames = [(r.body["func_code"], r.body["operation"]) for r in seen]
    assert (WRITE_SINGLE_REGISTER, "write") in frames
    assert (READ_HOLDING_REGISTERS, "read") in frames
    write = next(r for r in seen if r.body["operation"] == "write")
    assert write.body["values"] == [VALUE]
    assert write.path == f"/modbus/{WRITE_SINGLE_REGISTER}/1"


@pytest.mark.asyncio
async def test_a_taken_port_fails_the_start():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        plugin = ModbusServerPlugin(
            ModbusServerConfig(host="127.0.0.1", port=busy.getsockname()[1], enabled=True)
        )
        with pytest.raises(Exception):  # noqa: B017, PT011 - pymodbus raises RuntimeError
            await plugin.start()
    assert plugin.running is False

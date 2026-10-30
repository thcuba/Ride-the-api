"""A relayed session must be visible while it is open, not only once it is over.

A device that pins its vendor's certificate is forwarded without decryption, and its contact is
recorded **when the connection ends**. With a device that keeps one session open for ~4 hours --
measured live -- the contact list therefore says "nothing" for exactly as long as the device is
busy talking to its cloud, and ``http.api/tls/relayed`` had no way to tell a working device from
an absent one.

These tests pin the behaviour that closes that gap: the live view appears as soon as the forwarding
starts, its byte counters move with the streams, and it disappears in the same breath as the
contact record takes over.
"""

from __future__ import annotations

import asyncio
import json
from http import HTTPStatus
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import core.server as server_mod
from core.tls_mitm import TLSMITMServer

if TYPE_CHECKING:
    from collections.abc import Callable

CLIENT_IP = "192.168.1.77"
SNI = "ats.iot.vendor-cloud.example"
UPSTREAM_IP = "203.0.113.9"
DST_PORT = 443
CLIENT_HELLO = b"\x16\x03\x01\x00\x2f" + b"hello-from-the-device"
CLOUD_ANSWER = b"answer-from-the-cloud"
#: Stand-in for a real session's byte counts, taken from a measured relayed contact.
LIVE_BYTES_TO_CLOUD = 32626
LIVE_BYTES_TO_DEVICE = 22014
API_PASSWORD = "testpass"
#: How long to wait for an event that is set by another coroutine in the same loop.
_LOOP_TIMEOUT_S = 2


class _FakeWriter:
    """The writer surface the relay touches, plus what it wrote."""

    def __init__(self) -> None:
        self.written = bytearray()
        self.closed = False

    def write(self, data) -> None:
        self.written += bytes(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _GatedCloudReader:
    """Return the cloud's answer once, then block until the test lets the session end."""

    def __init__(self, chunks: list[bytes], gate: asyncio.Event) -> None:
        self._chunks = list(chunks)
        self._gate = gate

    async def read(self, _size: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        await self._gate.wait()
        return b""


def _mitm(
    gate: asyncio.Event, relayed: AsyncMock
) -> tuple[TLSMITMServer, _FakeWriter, asyncio.StreamReader, Callable]:
    """Build a MITM wired for one relayed session against a fake cloud."""
    server = TLSMITMServer(listen_ports=[])
    server.resolve_upstream = AsyncMock(return_value=UPSTREAM_IP)
    server.on_relayed = relayed

    device_reader = asyncio.StreamReader()
    device_reader.feed_data(b"first-payload")
    device_reader.feed_eof()
    device_writer = _FakeWriter()
    cloud_reader = _GatedCloudReader([CLOUD_ANSWER], gate)

    async def _open_connection(*_args, **_kwargs):
        return cloud_reader, _FakeWriter()

    return server, device_writer, device_reader, _open_connection


async def _run_relay(server, device_reader, device_writer, open_connection, monkeypatch):
    """Start one relayed connection and return the task driving it."""
    monkeypatch.setattr("core.tls_mitm.asyncio.open_connection", open_connection)
    return asyncio.create_task(
        server._relay_to_upstream(
            device_reader, device_writer, CLIENT_HELLO, SNI, CLIENT_IP, DST_PORT
        )
    )


async def _wait_until(predicate, timeout: float = _LOOP_TIMEOUT_S) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


class TestLiveRelayRegistry:
    @pytest.mark.asyncio
    async def test_a_session_is_visible_while_it_is_open(self, monkeypatch):
        """The whole point: a 4-hour session must not look like silence."""
        gate = asyncio.Event()
        relayed = AsyncMock()
        server, device_writer, device_reader, open_connection = _mitm(gate, relayed)

        task = await _run_relay(server, device_reader, device_writer, open_connection, monkeypatch)
        assert await _wait_until(lambda: bool(server.get_live_relays())), (
            "la sessione relay non e' comparsa mentre e' aperta"
        )

        live = server.get_live_relays()
        assert len(live) == 1
        assert live[0]["client_ip"] == CLIENT_IP
        assert live[0]["server_name"] == SNI
        assert live[0]["upstream_ip"] == UPSTREAM_IP
        assert live[0]["destination_port"] == DST_PORT
        assert live[0]["in_progress"] is True
        assert live[0]["started_at"]
        # The ClientHello is already on the wire, so the counter is not zero even at the start.
        assert live[0]["bytes_to_cloud"] >= len(CLIENT_HELLO)

        gate.set()
        await asyncio.wait_for(task, timeout=_LOOP_TIMEOUT_S)

        assert server.get_live_relays() == [], "la sessione viva deve sparire alla chiusura"
        relayed.assert_awaited_once()
        assert relayed.await_args is not None
        contact = relayed.await_args.args[0]
        assert contact["outcome"] == "relayed"
        assert contact["bytes_to_cloud"] == len(CLIENT_HELLO) + len(b"first-payload")
        assert contact["bytes_to_device"] == len(CLOUD_ANSWER)

    @pytest.mark.asyncio
    async def test_byte_counts_move_with_the_streams(self, monkeypatch):
        """The live view must report what has moved so far, not a frozen zero."""
        gate = asyncio.Event()
        server, device_writer, device_reader, open_connection = _mitm(gate, AsyncMock())

        task = await _run_relay(server, device_reader, device_writer, open_connection, monkeypatch)
        assert await _wait_until(lambda: bool(server.get_live_relays()))
        assert await _wait_until(
            lambda: server.get_live_relays()[0]["bytes_to_device"] == len(CLOUD_ANSWER)
        ), "i byte verso il dispositivo non sono stati aggiornati nella vista viva"

        gate.set()
        await asyncio.wait_for(task, timeout=_LOOP_TIMEOUT_S)

    @pytest.mark.asyncio
    async def test_a_failed_relay_leaves_nothing_live(self, monkeypatch):
        """A session that never starts must not leave a phantom in the live view."""

        async def _unreachable(*_args, **_kwargs):
            raise OSError("no route to host")

        server = TLSMITMServer(listen_ports=[])
        server.resolve_upstream = AsyncMock(return_value=UPSTREAM_IP)
        server.on_relayed = AsyncMock()
        device_reader = asyncio.StreamReader()
        device_reader.feed_eof()
        monkeypatch.setattr("core.tls_mitm.asyncio.open_connection", _unreachable)

        await server._relay_to_upstream(
            device_reader, _FakeWriter(), CLIENT_HELLO, SNI, CLIENT_IP, DST_PORT
        )

        assert server.get_live_relays() == []
        assert server.on_relayed.await_args is not None
        assert server.on_relayed.await_args.args[0]["outcome"] == "unreachable"


class TestRelayedEndpointReportsOpenSessions:
    @pytest.mark.asyncio
    async def test_endpoint_exposes_the_open_session_and_its_byte_counts(self, monkeypatch):
        """One call must answer both "did it ever connect" and "is it connected now"."""
        server = TLSMITMServer(listen_ports=[])
        await server._register_live_relay(
            "key-1",
            {
                "client_ip": CLIENT_IP,
                "server_name": SNI,
                "destination_port": DST_PORT,
                "upstream_ip": UPSTREAM_IP,
                "to_cloud": {"bytes": LIVE_BYTES_TO_CLOUD},
                "to_device": {"bytes": LIVE_BYTES_TO_DEVICE},
            },
        )
        monkeypatch.setattr(server_mod, "tls_mitm_server", server)
        monkeypatch.setattr(server_mod, "db_manager", None)
        monkeypatch.setattr(server_mod.config_manager.config.security, "password", API_PASSWORD)
        # No lifespan: entering the client would start every protocol server for a read-only check.
        client = TestClient(server_mod.app)

        response = client.get("/api/tls/relayed", headers={"X-API-Key": API_PASSWORD})
        payload = response.json()

        assert response.status_code == HTTPStatus.OK
        assert payload["relayed"] == []
        assert payload["open"] == 1
        assert payload["in_progress"][0]["client_ip"] == CLIENT_IP
        assert payload["in_progress"][0]["bytes_to_cloud"] == LIVE_BYTES_TO_CLOUD
        assert payload["in_progress"][0]["bytes_to_device"] == LIVE_BYTES_TO_DEVICE
        # JSON-serialisable as it leaves the process, not only inside Python.
        assert json.loads(json.dumps(payload))["in_progress"][0]["in_progress"] is True

    @pytest.mark.asyncio
    async def test_tls_stats_counts_the_open_sessions(self, monkeypatch):
        """The aggregate view has to agree with the per-session list."""
        server = TLSMITMServer(listen_ports=[])
        await server._register_live_relay(
            "key-1",
            {
                "client_ip": CLIENT_IP,
                "server_name": SNI,
                "destination_port": DST_PORT,
                "upstream_ip": UPSTREAM_IP,
                "to_cloud": {"bytes": 1},
                "to_device": {"bytes": 2},
            },
        )
        monkeypatch.setattr(server_mod, "tls_mitm_server", server)
        monkeypatch.setattr(server_mod.config_manager.config.security, "password", API_PASSWORD)
        client = TestClient(server_mod.app)

        response = client.get("/api/tls/stats", headers={"X-API-Key": API_PASSWORD})

        assert response.status_code == HTTPStatus.OK
        assert response.json()["open_relays"] == 1

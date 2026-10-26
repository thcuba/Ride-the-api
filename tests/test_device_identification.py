"""Identify a device that refuses our certificate, then forward it to its cloud.

Two behaviours, both seen live on the Raspberry Pi with a Philips air purifier talking to AWS IoT:

A -- such a device pins its vendor's certificate and answers our ServerHello with a TLS alert (7
     bytes on the wire: ``bad certificate``). Registration used to happen only *after* a successful
     handshake, so the device was never registered at all: the proxy saw every attempt and the
     registry stayed empty. The refusal is now recorded, and it survives a restart.
B -- from then on its connections are not decrypted: the ClientHello is replayed to the real host
     and the two streams are piped verbatim. The device keeps working with its cloud -- "do not stop
     the communication at the proxy, let it reach the cloud".

Also covered: identification happens before the handshake, on the ClientHello alone.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import core.server as server_mod
from core.database import DatabaseManager, DeviceRegistry
from core.tls_mitm import TLSMITMServer

CLIENT_IP = "192.168.178.77"  # the purifier, as seen on the Pi
SNI = "ats.prod.eu-da.iot.versuni.com"  # the hostname it asks for, roughly once a minute
EXPECTED_DEVICE_ID = "ip-192-168-178-77"


@pytest.fixture
async def db_manager(tmp_path):
    dm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "device_dbs",
    )
    await dm.initialize()
    yield dm
    await dm.close()


async def _device(db_manager, device_id):
    async with db_manager.core_session() as session:
        result = await session.execute(
            select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
        )
        return result.scalar_one_or_none()


def _real_client_hello(hostname: str) -> bytes:
    """Return a genuine TLS ClientHello for ``hostname``, built by the ssl module.

    The handshake cannot complete (there is no peer), but the first flight is exactly what a device
    sends, SNI included — which is the whole point of the test.
    """
    incoming = ssl.MemoryBIO()
    outgoing = ssl.MemoryBIO()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # Deliberately no verification: this context exists only to *emit* a ClientHello locally, there
    # is no peer to authenticate and nothing is trusted or used as a trust decision.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    obj = ctx.wrap_bio(incoming, outgoing, server_side=False, server_hostname=hostname)
    with contextlib.suppress(ssl.SSLWantReadError):
        obj.do_handshake()
    return outgoing.read()


class _FakeWriter:
    """The bare minimum the MITM touches on a writer, plus what it wrote back."""

    def __init__(self, peer: tuple[str, int], local: tuple[str, int] = ("127.0.0.1", 0)) -> None:
        self._peer = peer
        self._local = local
        self.written = bytearray()
        self.closed = False

    def get_extra_info(self, name, default=None):
        if name == "peername":
            return self._peer
        if name == "sockname":
            return self._local  # the port the device reached: where a relay must connect
        return default

    def write(self, data) -> None:
        self.written += bytes(data)

    async def drain(self) -> None:
        return None

    def write_eof(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _mitm(**hooks) -> TLSMITMServer:
    """Build a MITM with the handlers server.py would wire, for a single connection."""
    server = TLSMITMServer(listen_ports=[])
    server.on_device_seen = hooks.get("on_device_seen")
    server.should_relay_tls = hooks.get("should_relay_tls")
    server.on_handshake_refused = hooks.get("on_handshake_refused")
    server.resolve_upstream = hooks.get("resolve_upstream")
    return server


class TestDeviceSeenOnFirstContact:
    @pytest.mark.asyncio
    async def test_mitm_reports_the_device_before_the_handshake_completes(self):
        """The callback must fire even though the handshake then fails.

        This is the regression guard: the previous code registered a device only after a successful
        handshake, which never happens for a pinning device.
        """
        seen = AsyncMock()
        server = _mitm(on_device_seen=seen)

        reader = asyncio.StreamReader()
        reader.feed_data(_real_client_hello(SNI))
        reader.feed_eof()
        writer = _FakeWriter((CLIENT_IP, 52431))

        await server._handle_connection(reader, writer)

        assert seen.await_count == 1, "il contatto deve essere segnalato prima dell'handshake"
        assert seen.await_args is not None
        client_ip, sni = seen.await_args.args[:2]
        assert client_ip == CLIENT_IP
        assert sni == SNI

    @pytest.mark.asyncio
    async def test_registers_device_with_ip_and_hostname(self, db_manager, monkeypatch):
        monkeypatch.setattr(server_mod, "db_manager", db_manager)

        await server_mod.handle_device_seen(CLIENT_IP, SNI)

        device = await _device(db_manager, EXPECTED_DEVICE_ID)
        assert device is not None, "il device deve esistere anche se l'handshake non si completa"
        assert device.ip_addresses == [CLIENT_IP]
        assert device.extra_attributes.get("seen_hostnames") == [SNI]

    @pytest.mark.asyncio
    async def test_repeated_contact_is_idempotent(self, db_manager, monkeypatch):
        """The device retries every minute: no duplicated addresses or hostnames."""
        monkeypatch.setattr(server_mod, "db_manager", db_manager)
        for _ in range(3):
            await server_mod.handle_device_seen(CLIENT_IP, SNI)

        device = await _device(db_manager, EXPECTED_DEVICE_ID)
        assert device.ip_addresses == [CLIENT_IP]
        assert device.extra_attributes.get("seen_hostnames") == [SNI]

    @pytest.mark.asyncio
    async def test_second_hostname_from_the_same_device_is_kept(self, db_manager, monkeypatch):
        monkeypatch.setattr(server_mod, "db_manager", db_manager)
        await server_mod.handle_device_seen(CLIENT_IP, SNI)
        await server_mod.handle_device_seen(CLIENT_IP, "device-metrics.versuni.com")

        device = await _device(db_manager, EXPECTED_DEVICE_ID)
        assert device.extra_attributes.get("seen_hostnames") == [
            SNI,
            "device-metrics.versuni.com",
        ]

    @pytest.mark.asyncio
    async def test_works_without_sni(self, db_manager, monkeypatch):
        """A missing SNI means less evidence, not a failure."""
        monkeypatch.setattr(server_mod, "db_manager", db_manager)

        await server_mod.handle_device_seen("192.168.178.90", None)

        device = await _device(db_manager, "ip-192-168-178-90")
        assert device is not None
        assert device.ip_addresses == ["192.168.178.90"]
        assert not (device.extra_attributes or {}).get("seen_hostnames")


class TestRefusalIsRecorded:
    """A: a handshake that dies is information, not just an error."""

    @pytest.mark.asyncio
    async def test_refused_handshake_is_reported(self):
        refused = AsyncMock()
        server = _mitm(on_device_seen=AsyncMock(), on_handshake_refused=refused)

        reader = asyncio.StreamReader()
        reader.feed_data(_real_client_hello(SNI))
        reader.feed_eof()  # the device says nothing more, as when it closes the connection
        writer = _FakeWriter((CLIENT_IP, 52431))

        await server._handle_connection(reader, writer)

        assert refused.await_count == 1, "l'handshake fallito deve essere registrato"
        assert refused.await_args is not None
        assert refused.await_args.args[:2] == (CLIENT_IP, SNI)

    @pytest.mark.asyncio
    async def test_marks_the_device_as_untrusted(self, db_manager, monkeypatch):
        monkeypatch.setattr(server_mod, "db_manager", db_manager)

        await server_mod.mark_device_untrusted(CLIENT_IP, SNI)

        device = await _device(db_manager, EXPECTED_DEVICE_ID)
        assert device is not None
        assert device.extra_attributes.get("tls_passthrough") is True
        assert device.extra_attributes.get("tls_untrusted_reason") == "client_refused_certificate"
        assert device.extra_attributes.get("seen_hostnames") == [SNI]

    @pytest.mark.asyncio
    async def test_recording_twice_changes_nothing(self, db_manager, monkeypatch):
        """The device retries every minute; the row must not be rewritten each time."""
        monkeypatch.setattr(server_mod, "db_manager", db_manager)
        await server_mod.mark_device_untrusted(CLIENT_IP, SNI)
        first = (await _device(db_manager, EXPECTED_DEVICE_ID)).extra_attributes

        await server_mod.mark_device_untrusted(CLIENT_IP, SNI)

        assert (await _device(db_manager, EXPECTED_DEVICE_ID)).extra_attributes == first

    @pytest.mark.asyncio
    async def test_should_relay_only_after_the_refusal(self, db_manager, monkeypatch):
        monkeypatch.setattr(server_mod, "db_manager", db_manager)
        await server_mod.handle_device_seen(CLIENT_IP, SNI)

        assert await server_mod.should_relay_tls(CLIENT_IP) is False, (
            "finche' il device non ha rifiutato, il traffico va decifrato e imparato"
        )

        await server_mod.mark_device_untrusted(CLIENT_IP, SNI)

        assert await server_mod.should_relay_tls(CLIENT_IP) is True

    @pytest.mark.asyncio
    async def test_should_relay_is_false_for_an_unknown_address(self, db_manager, monkeypatch):
        monkeypatch.setattr(server_mod, "db_manager", db_manager)

        assert await server_mod.should_relay_tls("10.0.0.5") is False


class TestRelayDoesNotStopTheCommunication:
    """B: the connection reaches the real cloud instead of dying at the proxy."""

    @pytest.mark.asyncio
    async def test_client_hello_is_replayed_and_the_answer_comes_back(self):
        client_hello = _real_client_hello(SNI)
        upstream_saw: list[bytes] = []

        async def handle_upstream(up_reader, up_writer):
            upstream_saw.append(await up_reader.read(4096))
            up_writer.write(b"REAL-SERVER-HELLO")  # stands in for the vendor's server
            await up_writer.drain()
            up_writer.close()

        upstream = await asyncio.start_server(handle_upstream, "127.0.0.1", 0)
        up_port = upstream.sockets[0].getsockname()[1]

        try:
            server = _mitm(
                on_device_seen=AsyncMock(),
                should_relay_tls=AsyncMock(return_value=True),
                resolve_upstream=AsyncMock(return_value="127.0.0.1"),
            )
            reader = asyncio.StreamReader()
            reader.feed_data(client_hello)
            reader.feed_eof()  # the device sends nothing else in this test
            # the device reached the upstream's port: that is where the relay must connect
            writer = _FakeWriter((CLIENT_IP, 52431), local=("192.168.178.200", up_port))

            await asyncio.wait_for(server._handle_connection(reader, writer), timeout=10)
        finally:
            upstream.close()

        assert upstream_saw == [client_hello], "il ClientHello deve arrivare intatto al cloud"
        assert b"REAL-SERVER-HELLO" in bytes(writer.written), (
            "la risposta del cloud deve tornare al device: la comunicazione non si ferma"
        )
        assert not bytes(writer.written).startswith(b"\x16\x03"), (
            "nessun ServerHello nostro: il TLS non deve essere terminato"
        )

    @pytest.mark.asyncio
    async def test_no_relay_when_the_device_has_not_refused_anything(self):
        """Interceptable devices keep being decrypted and learned: the normal path is untouched."""
        server = _mitm(on_device_seen=AsyncMock(), should_relay_tls=AsyncMock(return_value=False))

        reader = asyncio.StreamReader()
        reader.feed_data(_real_client_hello(SNI))
        reader.feed_eof()
        writer = _FakeWriter((CLIENT_IP, 52431))

        await server._handle_connection(reader, writer)

        assert bytes(writer.written).startswith(b"\x16\x03"), (
            "senza pinning il MITM presenta il proprio certificato, come prima"
        )

    @pytest.mark.asyncio
    async def test_relay_is_skipped_when_the_host_does_not_resolve(self):
        server = _mitm(
            on_device_seen=AsyncMock(),
            should_relay_tls=AsyncMock(return_value=True),
            resolve_upstream=AsyncMock(return_value=None),
        )

        reader = asyncio.StreamReader()
        reader.feed_data(_real_client_hello(SNI))
        reader.feed_eof()
        writer = _FakeWriter((CLIENT_IP, 52431))

        await asyncio.wait_for(server._handle_connection(reader, writer), timeout=10)

        assert writer.closed, "senza risoluzione la connessione va chiusa, non lasciata appesa"

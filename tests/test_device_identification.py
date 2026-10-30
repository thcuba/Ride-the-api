"""Identify a device that refuses our certificate, then forward it to its cloud.

Two behaviours, both observed live with a consumer IoT device that talks to its vendor cloud and
pins the vendor certificate, so it refuses the one we generate:

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

CLIENT_IP = "192.168.1.77"  # the device, as seen on the proxy
SNI = "ats.iot.vendor-cloud.example"  # the hostname it asks for, roughly once a minute
EXPECTED_DEVICE_ID = "ip-192-168-1-77"
BYTES_TO_CLOUD = 1234  # what the device sent, standing in for a real payload
BYTES_TO_DEVICE = 5678  # what the cloud answered
DURATION_MS = 910
RELAYED_COUNT = 3  # how many contacts the retry loop produces
REPEATED_CONTACTS = 2
REFUSAL_ATTEMPTS = 3  # how many times the device tries to negotiate


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


class _ChunkedReader:
    """Return prescribed TCP fragments to exercise ClientHello reassembly."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = iter(chunks)

    async def read(self, _size: int) -> bytes:
        return next(self._chunks, b"")


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
    async def test_sni_survives_client_hello_split_across_tcp_reads(self):
        """TCP segmentation must not make the MITM choose a generic certificate."""
        hello = _real_client_hello(SNI)
        seen = AsyncMock()
        server = _mitm(
            on_device_seen=seen,
            should_relay_tls=AsyncMock(return_value=True),
            resolve_upstream=AsyncMock(return_value=None),
        )
        reader = _ChunkedReader([hello[:3], hello[3:9], hello[9:]])
        writer = _FakeWriter((CLIENT_IP, 52431))

        await server._handle_connection(reader, writer)

        assert seen.await_count == 1
        assert seen.await_args is not None
        assert seen.await_args.args[:2] == (CLIENT_IP, SNI)

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
        await server_mod.handle_device_seen(CLIENT_IP, "metrics.vendor-cloud.example")

        device = await _device(db_manager, EXPECTED_DEVICE_ID)
        assert device.extra_attributes.get("seen_hostnames") == [
            SNI,
            "metrics.vendor-cloud.example",
        ]

    @pytest.mark.asyncio
    async def test_works_without_sni(self, db_manager, monkeypatch):
        """A missing SNI means less evidence, not a failure."""
        monkeypatch.setattr(server_mod, "db_manager", db_manager)

        await server_mod.handle_device_seen("192.168.1.90", None)

        device = await _device(db_manager, "ip-192-168-1-90")
        assert device is not None
        assert device.ip_addresses == ["192.168.1.90"]
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
    async def test_every_refusal_is_counted(self, db_manager, monkeypatch):
        """Three attempts at negotiating: three refusals recorded, one settled relay decision.

        This assertion used to be the opposite -- the second refusal returned early and left no
        trace at all -- which made a device still trying to negotiate indistinguishable from one
        that had refused once and was relayed from then on.
        """
        monkeypatch.setattr(server_mod, "db_manager", db_manager)
        await server_mod.mark_device_untrusted(CLIENT_IP, SNI)
        first = (await _device(db_manager, EXPECTED_DEVICE_ID)).extra_attributes

        await server_mod.mark_device_untrusted(CLIENT_IP, SNI)
        await server_mod.mark_device_untrusted(CLIENT_IP, SNI)
        after = (await _device(db_manager, EXPECTED_DEVICE_ID)).extra_attributes

        assert after["tls_refusals"] == REFUSAL_ATTEMPTS
        # The decision itself stays idempotent: same relay flag, same reason, no duplicate hostname.
        assert after["tls_passthrough"] is True
        assert after["tls_untrusted_reason"] == "client_refused_certificate"
        assert after["seen_hostnames"] == [SNI]
        # The first refusal is history and must not move; the last one is what moves.
        assert after["tls_first_refusal_at"] == first["tls_first_refusal_at"]
        assert after["tls_last_refusal_at"] >= first["tls_last_refusal_at"]

    @pytest.mark.asyncio
    async def test_does_not_overwrite_a_reason_recorded_by_hand(self, db_manager, monkeypatch):
        """An operator's own reason outlives the automatic one; the refusal is still counted."""
        monkeypatch.setattr(server_mod, "db_manager", db_manager)
        await server_mod.handle_device_seen(CLIENT_IP, SNI)
        async with db_manager.core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == EXPECTED_DEVICE_ID)
            )
            device = result.scalar_one()
            device.extra_attributes = {
                **(device.extra_attributes or {}),
                "tls_untrusted_reason": "recorded_by_the_operator",
            }
            await session.commit()
        db_manager.invalidate_device_cache(EXPECTED_DEVICE_ID)

        await server_mod.mark_device_untrusted(CLIENT_IP, SNI)

        after = (await _device(db_manager, EXPECTED_DEVICE_ID)).extra_attributes
        assert after["tls_untrusted_reason"] == "recorded_by_the_operator"
        assert after["tls_refusals"] == 1

    @pytest.mark.asyncio
    async def test_a_reason_stored_as_null_is_still_explained(self, db_manager, monkeypatch):
        """A null or empty reason is no reason: the relay must not stay unexplained.

        ``setdefault`` would keep the null key and leave ``capture`` reporting a relay with an
        empty reason, which is precisely the payload the operator reads to understand the device.
        """
        monkeypatch.setattr(server_mod, "db_manager", db_manager)
        await server_mod.handle_device_seen(CLIENT_IP, SNI)
        async with db_manager.core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == EXPECTED_DEVICE_ID)
            )
            device = result.scalar_one()
            device.extra_attributes = {
                **(device.extra_attributes or {}),
                "tls_untrusted_reason": None,
            }
            await session.commit()
        db_manager.invalidate_device_cache(EXPECTED_DEVICE_ID)

        await server_mod.mark_device_untrusted(CLIENT_IP, SNI)

        after = (await _device(db_manager, EXPECTED_DEVICE_ID)).extra_attributes
        assert after["tls_untrusted_reason"] == "client_refused_certificate"

    @pytest.mark.asyncio
    async def test_a_corrupted_count_does_not_block_the_refusal(self, db_manager, monkeypatch):
        """``extra_attributes`` is free-form JSON: a bad count must not raise on a refusal."""
        monkeypatch.setattr(server_mod, "db_manager", db_manager)
        await server_mod.handle_device_seen(CLIENT_IP, SNI)
        async with db_manager.core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == EXPECTED_DEVICE_ID)
            )
            device = result.scalar_one()
            device.extra_attributes = {
                **(device.extra_attributes or {}),
                "tls_refusals": "many",
            }
            await session.commit()
        db_manager.invalidate_device_cache(EXPECTED_DEVICE_ID)

        await server_mod.mark_device_untrusted(CLIENT_IP, SNI)

        after = (await _device(db_manager, EXPECTED_DEVICE_ID)).extra_attributes
        assert after["tls_refusals"] == 1

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
            writer = _FakeWriter((CLIENT_IP, 52431), local=("192.168.1.200", up_port))

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


class TestRelayedContactsAreRecorded:
    """B leaves a trace: nothing readable, but the contact itself is measured and kept.

    A relayed device used to leave no trace anywhere -- carried by the proxy and invisible in it at
    the same time. These tests pin down what is recorded and that the relay reports it.
    """

    @pytest.mark.asyncio
    async def test_records_a_contact_with_its_measured_facts(self, db_manager, monkeypatch):
        monkeypatch.setattr(server_mod, "db_manager", db_manager)

        await server_mod.record_relayed_connection(
            {
                "client_ip": CLIENT_IP,
                "server_name": SNI,
                "destination_port": 443,
                "upstream_ip": "203.0.113.10",
                "bytes_to_cloud": BYTES_TO_CLOUD,
                "bytes_to_device": BYTES_TO_DEVICE,
                "duration_ms": DURATION_MS,
                "outcome": "relayed",
            }
        )

        async with db_manager.core_session() as session:
            rows = (await session.execute(select(server_mod.RelayedConnection))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.device_id == EXPECTED_DEVICE_ID, "il contatto va attribuito al device"
        assert row.server_name == SNI
        assert (row.bytes_to_cloud, row.bytes_to_device) == (BYTES_TO_CLOUD, BYTES_TO_DEVICE)
        assert row.duration_ms == DURATION_MS
        assert row.outcome == "relayed"

    @pytest.mark.asyncio
    async def test_an_unknown_address_still_gets_a_device(self, db_manager, monkeypatch):
        """Recording must not depend on the device being registered first."""
        monkeypatch.setattr(server_mod, "db_manager", db_manager)

        await server_mod.record_relayed_connection(
            {"client_ip": "192.168.1.90", "server_name": SNI}
        )

        assert await _device(db_manager, "ip-192-168-1-90") is not None

    @pytest.mark.asyncio
    async def test_failed_attempts_are_recorded_too(self, db_manager, monkeypatch):
        """A connection that never reached the cloud is evidence as well: it tells you it tried."""
        monkeypatch.setattr(server_mod, "db_manager", db_manager)

        await server_mod.record_relayed_connection(
            {"client_ip": CLIENT_IP, "server_name": SNI, "outcome": "unreachable"}
        )

        async with db_manager.core_session() as session:
            rows = (await session.execute(select(server_mod.RelayedConnection))).scalars().all()
        assert [r.outcome for r in rows] == ["unreachable"]

    @pytest.mark.asyncio
    async def test_device_list_exposes_the_count(self, db_manager, monkeypatch):
        """The dashboard reads the count from the device list, so it must be there."""
        monkeypatch.setattr(server_mod, "db_manager", db_manager)
        for _ in range(RELAYED_COUNT):
            await server_mod.record_relayed_connection({"client_ip": CLIENT_IP, "server_name": SNI})

        listed = [
            d for d in await db_manager.list_devices() if d["device_id"] == EXPECTED_DEVICE_ID
        ]

        assert listed, "il device deve comparire nella lista"
        assert listed[0]["relayed_connections"] == RELAYED_COUNT

    @pytest.mark.asyncio
    async def test_counting_is_grouped_per_device(self, db_manager, monkeypatch):
        monkeypatch.setattr(server_mod, "db_manager", db_manager)
        await server_mod.record_relayed_connection({"client_ip": CLIENT_IP})
        await server_mod.record_relayed_connection({"client_ip": CLIENT_IP})
        await server_mod.record_relayed_connection({"client_ip": "192.168.1.90"})

        counts = await db_manager.count_relayed_connections()

        assert counts[EXPECTED_DEVICE_ID] == REPEATED_CONTACTS
        assert counts["ip-192-168-1-90"] == 1

    @pytest.mark.asyncio
    async def test_the_relay_reports_what_it_moved(self):
        """The numbers that end up in the record are the ones actually moved."""
        client_hello = _real_client_hello(SNI)
        reported: list[dict] = []

        async def handle_upstream(up_reader, up_writer):
            await up_reader.read(4096)
            up_writer.write(b"REAL-SERVER-HELLO")
            await up_writer.drain()
            up_writer.close()

        upstream = await asyncio.start_server(handle_upstream, "127.0.0.1", 0)
        up_port = upstream.sockets[0].getsockname()[1]

        async def on_relayed(contact):
            reported.append(contact)

        try:
            server = _mitm(
                on_device_seen=AsyncMock(),
                should_relay_tls=AsyncMock(return_value=True),
                resolve_upstream=AsyncMock(return_value="127.0.0.1"),
            )
            server.on_relayed = on_relayed
            reader = asyncio.StreamReader()
            reader.feed_data(client_hello)
            reader.feed_eof()
            writer = _FakeWriter((CLIENT_IP, 52431), local=("192.168.1.200", up_port))

            await asyncio.wait_for(server._handle_connection(reader, writer), timeout=10)
        finally:
            upstream.close()

        assert len(reported) == 1, "un contatto inoltrato deve essere segnalato una volta"
        contact = reported[0]
        assert contact["outcome"] == "relayed"
        assert contact["client_ip"] == CLIENT_IP
        assert contact["server_name"] == SNI
        assert contact["bytes_to_cloud"] >= len(client_hello), "il ClientHello e' stato inoltrato"
        assert contact["bytes_to_device"] >= len(b"REAL-SERVER-HELLO")
        assert contact["duration_ms"] >= 0

    @pytest.mark.asyncio
    async def test_a_hopeless_attempt_is_reported_with_its_reason(self):
        """Nothing to connect to: the reason is recorded instead of an empty contact."""
        reported: list[dict] = []

        async def on_relayed(contact):
            reported.append(contact)

        server = _mitm(
            on_device_seen=AsyncMock(),
            should_relay_tls=AsyncMock(return_value=True),
            resolve_upstream=AsyncMock(return_value=None),
        )
        server.on_relayed = on_relayed
        reader = asyncio.StreamReader()
        reader.feed_data(_real_client_hello(SNI))
        reader.feed_eof()
        writer = _FakeWriter((CLIENT_IP, 52431))

        await asyncio.wait_for(server._handle_connection(reader, writer), timeout=10)

        assert [c["outcome"] for c in reported] == ["unresolvable"]

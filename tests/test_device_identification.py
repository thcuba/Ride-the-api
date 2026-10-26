"""A device that aborts the TLS handshake must still be identified.

Devices that pin their vendor's certificate (AWS IoT among them) answer our ServerHello with a
TLS alert and never complete the handshake. Registration that ran only *after* a successful
handshake therefore left them invisible: the proxy saw every connection attempt, the DNS
interception worked, and yet the device never appeared in the registry nor on the dashboard.

Seen live on the Raspberry Pi with a Philips air purifier: AdGuard redirected it to the proxy every
60 seconds, the TCP handshake to port 443 succeeded, the ServerHello was sent — and the device
closed with a 7-byte TLS alert (bad certificate). The registry stayed empty.

The fix registers the device as soon as it identifies itself: its IP, plus the SNI it asked for,
read from the ClientHello before the handshake is attempted.
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
    """The bare minimum ``_handle_connection`` touches on a writer."""

    def __init__(self, peer: tuple[str, int]) -> None:
        self._peer = peer
        self.closed = False

    def get_extra_info(self, name, default=None):
        return self._peer if name == "peername" else default

    def write(self, _data):
        return None

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


class TestDeviceSeenOnFirstContact:
    @pytest.mark.asyncio
    async def test_mitm_reports_the_device_before_the_handshake_completes(self):
        """The callback must fire even though the handshake then fails.

        This is the regression guard: the previous code registered a device only after a successful
        handshake, which never happens for a pinning device.
        """
        seen = AsyncMock()
        server = TLSMITMServer(listen_ports=[], on_device_seen=seen)

        reader = asyncio.StreamReader()
        reader.feed_data(_real_client_hello(SNI))
        writer = _FakeWriter((CLIENT_IP, 52431))

        # The handshake will abort here (no peer, and the device would answer with an alert), which
        # is exactly the scenario being tested.
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

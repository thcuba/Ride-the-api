"""The proxy never intercepts its own traffic.

A request whose source is the loopback interface belongs to the machine running
Ride-the-API, not to a device on the network: it must be forwarded untouched and
must never become a device, a buffered pair or a device/port entry. Without this
the dashboard showed the proxy itself as a device (``127.0.0.1``) and buffered
its own requests (the ``curl`` to the intercepted hostname used from the box).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import core.server as server_mod
import core.upstream_resolver as resolver
from adapters.base import InterceptedRequest, ProtocolType
from core.database import DatabaseManager, DeviceRegistry
from core.tls_mitm import DecryptedRequest, TLSMITMServer
from core.upstream_resolver import is_loopback_ip, is_self_address, self_connection_reason

LOOPBACK = "127.0.0.1"
LAN_IP = "192.168.1.77"
SNI = "ats.iot.vendor-cloud.example"


@pytest.fixture
async def db_manager(tmp_path):
    dm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "device_dbs",
    )
    await dm.initialize()
    yield dm
    await dm.close()


class _FakeOrchestrator:
    """Records the pipeline calls it gets: a loopback peer must produce none."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def handle_request(self, **kwargs):
        self.calls.append(kwargs)
        return {"action": "forward"}


class _FakeDB:
    """Minimal DatabaseManager stand-in that records device writes."""

    def __init__(self) -> None:
        self.created: list[str] = []

    async def get_or_create_device(self, device_id: str, _vendor: str) -> None:
        self.created.append(device_id)

    async def resolve_device_id(self, _ip: str) -> str | None:
        return None

    async def is_ips_bypassed(self, _ip: str) -> bool:
        return False

    async def resolve_device_protocol(self, _device_id: str, ingress_default: str = "http") -> str:
        return ingress_default

    def invalidate_ip_lookup_cache(self) -> None:
        pass


async def _devices(db_manager) -> list[DeviceRegistry]:
    async with db_manager.core_session() as session:
        result = await session.execute(select(DeviceRegistry))
        return list(result.scalars().all())


# ── the address check itself ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("127.0.0.1", True),
        ("127.5.6.7", True),
        ("::1", True),
        ("0:0:0:0:0:0:0:1", True),
        ("::ffff:127.0.0.1", True),
        ("[::1]", True),
        ("::1%eth0", True),
        ("", False),
        (None, False),
        ("not-an-ip", False),
        (LAN_IP, False),
        ("10.0.0.5", False),
        ("8.8.8.8", False),
        ("fe80::1%eth0", False),
    ],
)
def test_is_loopback_ip(address, expected):
    assert is_loopback_ip(address) is expected


def test_lan_addresses_stay_interceptable():
    """The check must not swallow the private LAN: those are the devices."""
    assert is_loopback_ip("192.168.178.77") is False
    assert is_loopback_ip("172.16.0.9") is False


# ── TLS path ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tls_request_from_loopback_is_forwarded_not_captured(monkeypatch, db_manager):
    orch = _FakeOrchestrator()
    monkeypatch.setattr(server_mod, "db_manager", db_manager)
    monkeypatch.setattr(server_mod, "orchestrator", orch)

    req = DecryptedRequest(
        method="POST",
        path="/v1/status",
        http_version="HTTP/1.1",
        headers={"host": SNI},
        body=b"{}",
        client_ip=LOOPBACK,
        client_port=51000,
        sni=SNI,
        dst_port=443,
    )
    result = await server_mod.handle_tls_decrypted_request(req)

    assert result == {"action": "forward", "reason": "loopback"}
    assert orch.calls == []  # never reaches the pipeline
    assert await _devices(db_manager) == []  # never becomes a device


@pytest.mark.asyncio
async def test_tls_request_from_a_lan_device_is_processed(monkeypatch, db_manager):
    """Control: the same request from a LAN address still goes through the pipeline."""
    orch = _FakeOrchestrator()
    monkeypatch.setattr(server_mod, "db_manager", db_manager)
    monkeypatch.setattr(server_mod, "orchestrator", orch)
    monkeypatch.setattr(
        server_mod,
        "config_manager",
        SimpleNamespace(config=SimpleNamespace(core=SimpleNamespace(device_db_dir="/tmp"))),
    )

    req = DecryptedRequest(
        method="POST",
        path="/v1/status",
        http_version="HTTP/1.1",
        headers={"host": SNI},
        body=b"{}",
        client_ip=LAN_IP,
        client_port=51000,
        sni=SNI,
        dst_port=443,
    )
    await server_mod.handle_tls_decrypted_request(req)

    assert len(orch.calls) == 1
    assert orch.calls[0]["device_id"] == "ip-192-168-1-77"


@pytest.mark.asyncio
async def test_loopback_never_registers_a_device_or_port(monkeypatch, db_manager):
    monkeypatch.setattr(server_mod, "db_manager", db_manager)

    await server_mod.handle_device_seen(LOOPBACK, SNI)
    await server_mod.mark_device_untrusted(LOOPBACK, SNI)
    await server_mod.record_relayed_connection({"client_ip": LOOPBACK, "server_name": SNI})

    assert await _devices(db_manager) == []


@pytest.mark.asyncio
async def test_a_lan_device_is_still_registered(monkeypatch, db_manager):
    monkeypatch.setattr(server_mod, "db_manager", db_manager)

    await server_mod.handle_device_seen(LAN_IP, SNI)

    devices = await _devices(db_manager)
    assert [d.device_id for d in devices] == ["ip-192-168-1-77"]
    assert (devices[0].extra_attributes or {}).get("seen_hostnames") == [SNI]


@pytest.mark.asyncio
async def test_device_port_tracking_skips_loopback(tmp_path):
    server = TLSMITMServer(device_certs_dir=str(tmp_path / "device_certs"))

    await server._register_device_connection(LOOPBACK, 51000, 443)
    assert server.device_ports == {}

    await server._register_device_connection(LAN_IP, 51000, 443)
    assert list(server.device_ports) == [LAN_IP]


# ── protocol-server path ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_protocol_request_from_loopback_is_forwarded_not_captured(monkeypatch):
    db = _FakeDB()
    orch = _FakeOrchestrator()
    monkeypatch.setattr(server_mod, "db_manager", db)
    monkeypatch.setattr(server_mod, "orchestrator", orch)

    req = InterceptedRequest(
        device_id="http2-127-0-0-1",
        timestamp=0,
        protocol=ProtocolType.HTTPS,
        client_ip=LOOPBACK,
        method="GET",
        path="/v1/status",
    )
    result = await server_mod.handle_protocol_request(req)

    assert result == {"action": "forward", "reason": "loopback"}
    assert orch.calls == []
    assert db.created == []


# ── control plane ────────────────────────────────────────────────────────────


def test_binding_a_loopback_ip_to_a_device_is_rejected(monkeypatch, tmp_path):
    dm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "device_dbs",
    )
    monkeypatch.setattr(server_mod, "db_manager", dm)
    monkeypatch.setattr(server_mod.config_manager.config.security, "password", "testpass")
    client = TestClient(server_mod.app)

    response = client.post(
        "/api/devices/some-device/ip",
        json={"ip_address": LOOPBACK},
        headers={"X-API-Key": "testpass"},
    )

    assert response.status_code == 400  # noqa: PLR2004
    assert "this machine" in response.json()["error"]


# ── the proxy's own LAN address is just as much "not a device" ───────────────


OWN_LAN_ADDRESS = "10.9.8.7"


@pytest.fixture
def own_address(monkeypatch):
    """Pin the host's address list, so the test does not depend on the machine it runs on."""
    monkeypatch.setattr(resolver, "_self_addresses", frozenset({OWN_LAN_ADDRESS, "127.0.0.1"}))
    return OWN_LAN_ADDRESS


def test_a_foreign_lan_address_is_still_a_device():
    """The check must not swallow the LAN: that is where the devices are."""
    assert is_self_address(LAN_IP) is False
    assert is_self_address("8.8.8.8") is False
    assert self_connection_reason(LAN_IP) is None


def test_this_machines_own_addresses_are_recognised(own_address):
    assert is_self_address(own_address) is True
    assert self_connection_reason(own_address) == "own-address"
    # Loopback keeps its own reason, so the logs say which of the two it was.
    assert self_connection_reason("127.0.0.1") == "loopback"
    assert self_connection_reason(own_address + "%eth0") == "own-address"


def test_garbage_is_not_an_address_of_this_machine():
    for value in ("", "not-an-ip", "ca:75:7a:af:96:7e", None):
        assert is_self_address(value) is False


def test_the_address_list_holds_ip_literals_only(monkeypatch):
    """psutil also lists link-layer entries; a MAC must not enter the comparison set."""
    monkeypatch.setattr(resolver, "_self_addresses", None)
    addresses = resolver.self_addresses()

    assert addresses
    for value in addresses:
        assert resolver._is_ip_literal(value), value  # noqa: SLF001 - the guard under test


@pytest.mark.asyncio
async def test_tls_request_from_our_own_lan_address_is_forwarded(
    monkeypatch, db_manager, own_address
):
    """A process on this host reaching the proxy on its own IP looks exactly like a device."""
    orch = _FakeOrchestrator()
    monkeypatch.setattr(server_mod, "db_manager", db_manager)
    monkeypatch.setattr(server_mod, "orchestrator", orch)

    req = DecryptedRequest(
        method="POST",
        path="/v1/status",
        http_version="HTTP/1.1",
        headers={"host": SNI},
        body=b"{}",
        client_ip=own_address,
        client_port=51000,
        sni=SNI,
        dst_port=443,
    )
    result = await server_mod.handle_tls_decrypted_request(req)

    assert result == {"action": "forward", "reason": "own-address"}
    assert orch.calls == []
    assert await _devices(db_manager) == []

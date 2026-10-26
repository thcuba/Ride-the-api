"""The DNS interception server: intercept what is configured, forward the rest.

The rule this file protects: a name that is **not** listed in ``dns.intercept``
must be forwarded to the real resolvers. Guessing (or black-holing) would leave a
device without DNS — which is exactly what happened when a device was pointed at a
proxy that had no DNS server at all.
"""

from __future__ import annotations

import asyncio
import socket

import dns.message
import dns.query
import dns.rcode
import dns.rdataclass
import dns.rdatatype
import dns.rrset
import pytest

from core.config import Config
from core.dns_server import DNSInterceptor, _detect_lan_ip, get_dns_interceptor, set_dns_interceptor

PROXY_IP = "192.168.1.200"
UPSTREAM_IP = "203.0.113.7"
INTERCEPTED = "ats.iot.vendor-cloud.example"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _FakeUpstream(asyncio.DatagramProtocol):
    """A resolver that answers every A query with one fixed address."""

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.transport = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        query = dns.message.from_wire(data)
        question = query.question[0]
        self.queries.append(question.name.to_text())
        response = dns.message.make_response(query)
        response.answer.append(
            dns.rrset.from_text(
                question.name.to_text(),
                30,
                dns.rdataclass.IN,
                dns.rdatatype.A,
                UPSTREAM_IP,
            )
        )
        self.transport.sendto(response.to_wire(), addr)


def _config(**overrides) -> Config:
    config = Config()
    dns = config.dns
    dns.enabled = True
    dns.listen_host = "127.0.0.1"
    dns.answer_ip = PROXY_IP
    dns.intercept = [INTERCEPTED, "*.example.test"]
    dns.forward_timeout = 0.4
    for key, value in overrides.items():
        setattr(dns, key, value)
    return config


def _query(name: str, qtype: str = "A") -> dns.message.Message:
    return dns.message.make_query(name, qtype)


# ── which names are intercepted ──────────────────────────────────────────────


def test_matches_exact_names_and_wildcards():
    interceptor = DNSInterceptor(_config())

    assert interceptor.matches(INTERCEPTED)
    assert interceptor.matches(INTERCEPTED.upper())  # DNS names are case-insensitive
    assert interceptor.matches(INTERCEPTED + ".")
    assert interceptor.matches("a.example.test")  # *.example.test
    assert interceptor.matches("example.test")  # the bare domain too
    assert interceptor.matches("deep.a.example.test")

    assert not interceptor.matches("ntp.iot.vendor-cloud.example")  # real clock: never hijack
    assert not interceptor.matches("example.test.evil.com")
    assert not interceptor.matches("notexample.test")


def test_nothing_is_intercepted_by_default():
    interceptor = DNSInterceptor(_config(intercept=[]))
    assert not interceptor.matches(INTERCEPTED)


# ── what it answers ──────────────────────────────────────────────────────────


def test_intercepted_a_returns_the_proxy_address():
    interceptor = DNSInterceptor(_config())
    response = interceptor._intercept_response(_query(INTERCEPTED), _query(INTERCEPTED).question[0])

    assert response.rcode() == dns.rcode.NOERROR
    answers = [rdata.address for rrset in response.answer for rdata in rrset]
    assert answers == [PROXY_IP]
    assert response.answer[0].ttl == 60  # noqa: PLR2004


def test_intercepted_aaaa_is_empty_noerror():
    """NODATA keeps a dual-stack device on the IPv4 route we actually serve."""
    query = _query(INTERCEPTED, "AAAA")
    response = DNSInterceptor(_config())._intercept_response(query, query.question[0])

    assert response.rcode() == dns.rcode.NOERROR
    assert response.answer == []


# ── the server on a real socket ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_intercepts_configured_names_and_forwards_the_others():
    port = _free_port()
    upstream_port = _free_port()
    upstream = asyncio.get_running_loop().create_datagram_endpoint(
        _FakeUpstream, local_addr=("127.0.0.1", upstream_port)
    )
    transport, protocol = await upstream

    config = _config(
        listen_port=port,
        dns_servers=[f"127.0.0.1:{upstream_port}"],
        dns_servers_v6=[],
    )
    set_dns_interceptor(None)
    interceptor = get_dns_interceptor(config)
    assert interceptor is not None
    await interceptor.start()
    try:
        # 1. an intercepted name is answered by us, without asking anyone
        answer = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: dns.query.udp(_query(INTERCEPTED), "127.0.0.1", port=port, timeout=5),
        )
        addresses = [rdata.address for rrset in answer.answer for rdata in rrset]
        assert addresses == [PROXY_IP]

        # 2. everything else goes to the real resolver, unchanged
        answer = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: dns.query.udp(_query("www.other.test"), "127.0.0.1", port=port, timeout=5),
        )
        addresses = [rdata.address for rrset in answer.answer for rdata in rrset]
        assert addresses == [UPSTREAM_IP]
        assert protocol.queries == ["www.other.test."]
        assert interceptor.stats["intercepted"] == 1
        assert interceptor.stats["forwarded"] == 1
    finally:
        await interceptor.stop()
        transport.close()
        set_dns_interceptor(None)


@pytest.mark.asyncio
async def test_tcp_is_served_too():
    port = _free_port()
    interceptor = DNSInterceptor(_config(listen_port=port))
    set_dns_interceptor(None)
    set_dns_interceptor(interceptor)
    await interceptor.start()
    try:
        answer = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: dns.query.tcp(_query(INTERCEPTED), "127.0.0.1", port=port, timeout=5),
        )
        assert [rdata.address for rrset in answer.answer for rdata in rrset] == [PROXY_IP]
    finally:
        await interceptor.stop()
        set_dns_interceptor(None)


@pytest.mark.asyncio
async def test_no_upstream_reachable_returns_servfail():
    """An honest failure beats a fabricated address."""
    port = _free_port()
    interceptor = DNSInterceptor(
        _config(listen_port=port, dns_servers=["127.0.0.1:1"], dns_servers_v6=[])
    )
    await interceptor.start()
    try:
        answer = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: dns.query.udp(_query("www.other.test"), "127.0.0.1", port=port, timeout=5),
        )
        assert answer.rcode() == dns.rcode.SERVFAIL
        assert interceptor.stats["refused"] == 1
    finally:
        await interceptor.stop()


@pytest.mark.asyncio
async def test_disabled_server_binds_nothing():
    interceptor = DNSInterceptor(_config(enabled=False))
    await interceptor.start()
    assert interceptor._udp_transport is None
    assert interceptor._tcp_server is None


def test_upstream_port_parsing():
    assert DNSInterceptor._split_upstream("1.1.1.1") == ("1.1.1.1", 53)
    assert DNSInterceptor._split_upstream("127.0.0.1:5353") == ("127.0.0.1", 5353)
    assert DNSInterceptor._split_upstream("2001:4860:4860::8888") == ("2001:4860:4860::8888", 53)


def test_lan_address_detection_does_not_raise():
    detected = _detect_lan_ip()
    assert isinstance(detected, str)


def _ipv6_available() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as probe:
            probe.bind(("::1", 0))
    except OSError:
        return False
    return True


def _free_port_dual() -> int:
    """A port free on both families: the listeners share the same number."""
    for _ in range(20):
        port = _free_port()
        try:
            with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as probe:
                probe.bind(("::1", port))
        except OSError:
            continue
        return port
    return _free_port()


@pytest.mark.asyncio
async def test_an_any_address_listens_on_ipv6_too():
    """A dual-stack device may ask over IPv6: it must not time out in silence."""
    if not _ipv6_available():
        pytest.skip("no IPv6 in this environment")
    port = _free_port_dual()
    interceptor = DNSInterceptor(_config(listen_host="0.0.0.0", listen_port=port))
    await interceptor.start()
    try:
        answer = await dns.asyncquery.udp(
            dns.message.make_query(INTERCEPTED, "A"), "::1", port=port, timeout=5
        )
        assert [rdata.address for rrset in answer.answer for rdata in rrset] == [PROXY_IP]

        tcp = await asyncio.get_running_loop().run_in_executor(
            None, lambda: dns.query.tcp(_query(INTERCEPTED), "::1", port=port, timeout=5)
        )
        assert [rdata.address for rrset in tcp.answer for rdata in rrset] == [PROXY_IP]

        # the IPv4 side must keep working on the same port
        v4 = await asyncio.get_running_loop().run_in_executor(
            None, lambda: dns.query.udp(_query(INTERCEPTED), "127.0.0.1", port=port, timeout=5)
        )
        assert [rdata.address for rrset in v4.answer for rdata in rrset] == [PROXY_IP]
    finally:
        await interceptor.stop()


@pytest.mark.asyncio
async def test_a_specific_address_does_not_add_an_ipv6_listener():
    """An explicit listen_host is a decision, not a suggestion."""
    interceptor = DNSInterceptor(_config(listen_host="127.0.0.1", listen_port=_free_port()))
    await interceptor.start()
    try:
        assert interceptor.listening["udp_v6"] is False
        assert interceptor.listening["tcp_v6"] is False
    finally:
        await interceptor.stop()


class _TruncatingUpstream:
    """UDP answers TC=1 (the real answer does not fit); TCP serves it in full."""

    def __init__(self, *, serve_tcp: bool = True) -> None:
        self.serve_tcp = serve_tcp
        self.udp_queries: list[str] = []
        self.tcp_queries: list[str] = []

    def make_response(self, data: bytes) -> bytes:
        query = dns.message.from_wire(data)
        question = query.question[0]
        response = dns.message.make_response(query)
        response.answer.append(
            dns.rrset.from_text(
                question.name.to_text(), 30, dns.rdataclass.IN, dns.rdatatype.A, UPSTREAM_IP
            )
        )
        return response

    # UDP side
    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        query = dns.message.from_wire(data)
        self.udp_queries.append(query.question[0].name.to_text())
        truncated = dns.message.make_response(query)
        truncated.flags |= dns.flags.TC
        self.transport.sendto(truncated.to_wire(), addr)

    # TCP side
    async def handle_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                header = await reader.readexactly(2)
                payload = await reader.readexactly(int.from_bytes(header, "big"))
                query = dns.message.from_wire(payload)
                self.tcp_queries.append(query.question[0].name.to_text())
                answer = self.make_response(payload).to_wire()
                writer.write(len(answer).to_bytes(2, "big") + answer)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


@pytest.mark.asyncio
async def test_truncated_answer_is_completed_over_tcp():
    """TC=1 upstream must be resolved by *us*, otherwise the client loops forever."""
    port, upstream_port = _free_port(), _free_port()
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        _TruncatingUpstream, local_addr=("127.0.0.1", upstream_port)
    )
    tcp_server = await asyncio.start_server(
        protocol.handle_tcp, host="127.0.0.1", port=upstream_port
    )

    interceptor = DNSInterceptor(
        _config(listen_port=port, dns_servers=[f"127.0.0.1:{upstream_port}"], dns_servers_v6=[])
    )
    await interceptor.start()
    try:
        answer = await loop.run_in_executor(
            None,
            lambda: dns.query.udp(_query("big.other.test"), "127.0.0.1", port=port, timeout=6),
        )
        assert not answer.flags & dns.flags.TC, "the client must never see a truncated answer"
        assert [rdata.address for rrset in answer.answer for rdata in rrset] == [UPSTREAM_IP]
        assert protocol.tcp_queries == ["big.other.test."]
        assert interceptor.stats["tcp_retries"] == 1
    finally:
        await interceptor.stop()
        tcp_server.close()
        transport.close()


@pytest.mark.asyncio
async def test_truncated_answer_without_tcp_gives_servfail():
    """If the resolver cannot deliver it over TCP either, fail honestly — never loop."""
    port, upstream_port = _free_port(), _free_port()
    transport, protocol = await asyncio.get_running_loop().create_datagram_endpoint(
        _TruncatingUpstream, local_addr=("127.0.0.1", upstream_port)
    )
    interceptor = DNSInterceptor(
        _config(
            listen_port=port,
            dns_servers=[f"127.0.0.1:{upstream_port}"],
            dns_servers_v6=[],
            forward_timeout=0.5,
        )
    )
    await interceptor.start()
    try:
        answer = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: dns.query.udp(_query("big.other.test"), "127.0.0.1", port=port, timeout=6),
        )
        assert answer.rcode() == dns.rcode.SERVFAIL
        assert interceptor.stats["refused"] == 1
    finally:
        await interceptor.stop()
        transport.close()

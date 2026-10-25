"""DNS interception server.

Devices are pointed at this server — as their DNS, or as the upstream of the
local resolver — so the names they use to reach their vendor cloud resolve to
this proxy instead of the real cloud. Everything else is forwarded **verbatim**
to the configured upstream resolvers, so the server can be the DNS for a whole
network without breaking anything that is not being intercepted.

Which names are intercepted is explicit configuration (``dns.intercept``): exact
names and ``*.domain`` wildcards. A name that is not listed is forwarded, never
guessed — guessing would silently black-hole a device.

Intercepted A queries are answered with the proxy's own address; AAAA is answered
with an empty NOERROR so a device does not prefer an IPv6 route we do not serve.
Queries that cannot be forwarded at all return SERVFAIL: an honest failure, not a
fabricated address.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from typing import TYPE_CHECKING

import dns.asyncquery
import dns.message
import dns.rdataclass
import dns.rdatatype
import dns.rrset

if TYPE_CHECKING:
    from core.config import Config

logger = logging.getLogger(__name__)

# A single DNS message over UDP carries at most 512 bytes without EDNS, and the
# TCP length prefix is a 16-bit field: anything larger is a malformed message.
_MAX_TCP_MESSAGE = 65535


class _DNSUDPProtocol(asyncio.DatagramProtocol):
    """UDP side of the DNS server."""

    def __init__(self, interceptor: DNSInterceptor) -> None:
        self._interceptor = interceptor

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        asyncio.create_task(self._reply(data, addr))  # noqa: RUF006 - fire and forget by design

    async def _reply(self, data: bytes, addr: tuple[str, int]) -> None:
        response = await self._interceptor.handle_query(data, client=addr[0])
        if response:
            self._interceptor.send_udp(response, addr)

    def error_received(self, exc: Exception) -> None:
        logger.debug("DNS: UDP error: %s", exc)


class DNSInterceptor:
    """A DNS server that redirects configured names to this proxy.

    The upstream resolvers come from ``dns.dns_servers`` / ``dns.dns_servers_v6``,
    the same ones used for cloud forwarding, so there is a single place to
    configure name resolution for the whole project.
    """

    def __init__(self, config: Config) -> None:
        dns_cfg = config.dns
        self.enabled = bool(dns_cfg.enabled)
        self.host = dns_cfg.listen_host
        self.port = int(dns_cfg.listen_port)
        self.patterns = list(dns_cfg.intercept or [])
        self.answer_ip = dns_cfg.answer_ip or _detect_lan_ip()
        self.ttl = int(dns_cfg.answer_ttl)
        self.forward_timeout = float(dns_cfg.forward_timeout)
        self.upstreams = [*dns_cfg.dns_servers, *dns_cfg.dns_servers_v6]

        self._udp_transport: asyncio.DatagramTransport | None = None
        self._tcp_server: asyncio.AbstractServer | None = None
        self.stats = {"intercepted": 0, "forwarded": 0, "refused": 0, "errors": 0}

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Bind UDP and TCP. A port we cannot bind is reported, never fatal."""
        if not self.enabled:
            logger.info("DNS: disabled")
            return
        if not self.answer_ip:
            logger.warning("DNS: no answer_ip configured and none detected — set dns.answer_ip")
            return

        loop = asyncio.get_running_loop()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _DNSUDPProtocol(self), local_addr=(self.host, self.port)
            )
            self._udp_transport = transport
        except OSError as exc:
            logger.warning(
                "DNS: cannot listen on %s:%d/udp — %s (port 53 needs privilege: "
                "setcap cap_net_bind_service=+ep on the interpreter)",
                self.host,
                self.port,
                exc,
            )
            return

        try:
            self._tcp_server = await asyncio.start_server(
                self._handle_tcp, host=self.host, port=self.port
            )
        except OSError as exc:
            logger.warning("DNS: cannot listen on %s:%d/tcp — %s", self.host, self.port, exc)

        logger.info(
            "DNS: listening on %s:%d (udp%s) — intercepting %s, answering %s",
            self.host,
            self.port,
            "+tcp" if self._tcp_server else " only",
            self.patterns or "nothing",
            self.answer_ip,
        )

    async def stop(self) -> None:
        if self._udp_transport is not None:
            self._udp_transport.close()
            self._udp_transport = None
        if self._tcp_server is not None:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()
            self._tcp_server = None
        logger.info("DNS: stopped")

    # ── query handling ───────────────────────────────────────────────────────

    def send_udp(self, payload: bytes, addr: tuple[str, int]) -> None:
        if self._udp_transport is not None:
            self._udp_transport.sendto(payload, addr)

    async def handle_query(self, data: bytes, client: str = "unknown") -> bytes | None:
        """Answer one DNS message. ``None`` means: send nothing back."""
        try:
            query = dns.message.from_wire(data)
        except Exception as exc:  # noqa: BLE001 - malformed input must not kill the server
            self.stats["errors"] += 1
            logger.debug("DNS: unparsable query from %s: %s", client, exc)
            return None

        if not query.question:
            return None

        question = query.question[0]
        qname = question.name.to_text().lower()
        qtype = question.rdtype

        if self.matches(qname):
            self.stats["intercepted"] += 1
            logger.info(
                "DNS: intercepted %s %s from %s -> %s",
                qname,
                dns.rdatatype.to_text(qtype),
                client,
                self.answer_ip,
            )
            return self._intercept_response(query, question).to_wire()

        self.stats["forwarded"] += 1
        forwarded = await self._forward(query)
        if forwarded is not None:
            return forwarded

        self.stats["refused"] += 1
        logger.warning("DNS: no upstream could answer %s from %s", qname, client)
        return _servfail(query).to_wire()

    def matches(self, qname: str) -> bool:
        """Is this name one we intercept? (exact name or ``*.domain`` wildcard)"""
        name = qname.rstrip(".").lower()
        for pattern in self.patterns:
            candidate = pattern.strip().rstrip(".").lower()
            if not candidate:
                continue
            if candidate.startswith("*."):
                domain = candidate[2:]
                # both the domain itself and everything under it
                if name == domain or name.endswith("." + domain):
                    return True
            elif name == candidate:
                return True
        return False

    def _intercept_response(self, query: dns.message.Message, question) -> dns.message.Message:
        """A -> the proxy address; everything else an empty NOERROR.

        Answering AAAA with NODATA keeps a dual-stack device on the IPv4 route we
        actually serve, instead of letting it prefer a v6 address we cannot answer.
        """
        response = dns.message.make_response(query)
        if question.rdtype == dns.rdatatype.A:
            response.answer.append(
                dns.rrset.from_text(
                    question.name.to_text(),
                    self.ttl,
                    dns.rdataclass.IN,
                    dns.rdatatype.A,
                    self.answer_ip,
                )
            )
        return response

    @staticmethod
    def _split_upstream(server: str) -> tuple[str, int]:
        """Accept ``1.1.1.1`` and ``127.0.0.1:5353`` (an IPv6 literal keeps its colons)."""
        if server.count(":") == 1:
            host, _, port = server.partition(":")
            if port.isdigit():
                return host, int(port)
        return server, 53

    async def _forward(self, query: dns.message.Message) -> bytes | None:
        """Send the query to the upstream resolvers, in order, and return the reply.

        ``dns.asyncquery.udp`` takes a parsed message, not wire bytes, and returns
        the resolver's own answer: we hand it back as it arrived.
        """
        for server in self.upstreams:
            host, port = self._split_upstream(server)
            try:
                response = await dns.asyncquery.udp(
                    query, host, port=port, timeout=self.forward_timeout
                )
            except Exception as exc:  # noqa: BLE001 - try the next resolver
                logger.debug("DNS: upstream %s failed: %s", server, exc)
                continue
            return response.to_wire()
        return None

    # ── TCP side ─────────────────────────────────────────────────────────────

    async def _handle_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """DNS over TCP: a 16-bit length prefix in front of each message."""
        peer = writer.get_extra_info("peername", ("unknown", 0))
        try:
            while True:
                header = await reader.readexactly(2)
                length = int.from_bytes(header, "big")
                if not 0 < length <= _MAX_TCP_MESSAGE:
                    break
                payload = await reader.readexactly(length)
                response = await self.handle_query(payload, client=peer[0])
                if response is None:
                    break
                writer.write(len(response).to_bytes(2, "big") + response)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            pass
        except Exception as exc:  # noqa: BLE001 - never let one client kill the loop
            self.stats["errors"] += 1
            logger.debug("DNS: TCP client %s failed: %s", peer[0], exc)
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()


def _servfail(query: dns.message.Message) -> dns.message.Message:
    response = dns.message.make_response(query)
    response.set_rcode(dns.rcode.SERVFAIL)
    return response


def _detect_lan_ip() -> str:
    """Best-effort local address: the one the OS would use to reach the internet."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.2)
            probe.connect(("8.8.8.8", 53))
            return str(probe.getsockname()[0])
    except OSError:
        return ""


# ── singleton ────────────────────────────────────────────────────────────────

_dns_interceptor: DNSInterceptor | None = None


def get_dns_interceptor(config: Config | None = None) -> DNSInterceptor | None:
    """Return the process-wide interceptor, creating it on first use."""
    global _dns_interceptor  # noqa: PLW0603
    if _dns_interceptor is None and config is not None:
        _dns_interceptor = DNSInterceptor(config)
    return _dns_interceptor


def set_dns_interceptor(interceptor: DNSInterceptor | None) -> None:
    global _dns_interceptor  # noqa: PLW0603
    _dns_interceptor = interceptor

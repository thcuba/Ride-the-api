"""
Dual-stack upstream DNS resolver.

Resolves cloud hostnames directly via configurable upstream DNS servers,
bypassing the local DNS (a local DNS server / a DNS filter / an external DNS filter Home).

This prevents forwarding loops where the proxy re-enters itself when the
local DNS server returns the proxy's own IP for a cloud hostname.

DNS servers are configured in ``config.yaml`` under ``dns.dns_servers``
(IPv4) and ``dns.dns_servers_v6`` (IPv6), defaulting to 8.8.8.8 / 1.1.1.1.
"""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import logging
import socket
from typing import TYPE_CHECKING

import dns.asyncresolver
import dns.exception
from cachetools import TTLCache

if TYPE_CHECKING:
    from core.config import Config

logger = logging.getLogger(__name__)

# Cache TTL
CACHE_TTL = 300  # 5 minutes

# IP version constant for the prefer_ipv6 re-ordering.
_IPV6_VERSION = 6

# In-memory cache: hostname -> [ip_addresses], auto-expired via TTLCache (5 min TTL)
_resolver_cache: TTLCache[str, list[str]] = TTLCache(maxsize=1024, ttl=CACHE_TTL)

# In-memory snapshot of the last DNS config so we can rebuild the resolver
# without importing ConfigManager at module level (avoids circular imports).
_last_dns_servers: list[str] = ["8.8.8.8", "1.1.1.1"]
_last_dns_servers_v6: list[str] = [
    "2001:4860:4860::8888",
    "2606:4700:4700::1111",
]


def _addr_family(address: str) -> int:
    """Return IP version (4 or 6); non-IP strings default to 4.

    Optimized string check (":" in address) avoids ipaddress parsing overhead and
    allocations on DNS resolution cache hits (~87x faster).
    """
    return 6 if ":" in address else 4


@functools.lru_cache(maxsize=2048)
def is_blocked_ip(ip_str: str) -> bool:
    """Return True if ``ip_str`` must never be contacted (SSRF guard).

    Blocks loopback, private (RFC1918), link-local, unique-local, unspecified,
    multicast, reserved and IPv4-mapped ranges so a cloud hostname (or a
    client-controlled ``Host`` header) can never redirect the proxy to
    internal services, the local machine or cloud metadata endpoints.

    Memoized with lru_cache to eliminate repeated string parsing and
    `ipaddress.ip_address` evaluation overhead (~67x faster for resolved IPs).
    """
    try:
        addr = ipaddress.ip_address(ip_str.split("%", 1)[0])
    except ValueError:
        # Not a valid IP literal — refuse to connect.
        return True
    return (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_unspecified
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_global is False
    )


def filter_safe_ips(ips: list[str]) -> list[str]:
    """Return only the addresses in ``ips`` that are safe to contact."""
    return [ip for ip in ips if not is_blocked_ip(ip)]


@functools.lru_cache(maxsize=512)
def is_loopback_ip(ip_str: str | None) -> bool:
    """Return True if ``ip_str`` is a loopback address (the proxy talking to itself).

    Ride-the-API must never intercept its own traffic: a request whose source is
    the loopback interface belongs to this machine, not to a device on the
    network, so it is forwarded untouched -- no device record, no buffered pair,
    no entry in the device/port tracking.

    Unlike ``is_blocked_ip`` this deliberately does NOT match private LAN ranges:
    a device on the local network is exactly what the proxy is meant to intercept.
    IPv4 ``127.0.0.0/8``, IPv6 ``::1`` and the IPv4-mapped ``::ffff:127.x`` forms
    all count; a zone suffix (``fe80::1%eth0``) is stripped first.
    """
    if not ip_str:
        return False
    literal = ip_str.split("%", 1)[0].strip().strip("[]")
    try:
        addr = ipaddress.ip_address(literal)
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        return bool(mapped.is_loopback)
    return addr.is_loopback


#: Addresses this machine answers on, cached: a connection whose source is one of them
#: is the proxy talking to itself on its own LAN address, which is exactly as much "not
#: a device" as a loopback peer. ``is_loopback_ip`` deliberately does not match LAN
#: ranges (devices live there), so the distinction has to come from the host's own
#: interface list instead of from the address class.
_self_addresses: frozenset[str] | None = None


def self_addresses() -> frozenset[str]:
    """Every address this machine is reachable on (best effort), cached.

    Sources in order of reliability: ``psutil`` when importable (all interfaces, IPv6
    included), then this host's own name, then the address the OS would use to reach the
    internet, plus loopback. Missing an address fails safe -- that traffic is treated as
    a device again, exactly as it was before.
    """
    global _self_addresses  # noqa: PLW0603
    if _self_addresses is not None:
        return _self_addresses

    found: set[str] = {"127.0.0.1", "::1"}
    try:
        import psutil  # noqa: PLC0415 - optional: not a declared dependency

        for addrs in psutil.net_if_addrs().values():
            found.update(_normalise_address(addr.address) for addr in addrs)
    except Exception:  # noqa: BLE001 - any failure just narrows the list
        logger.debug("psutil unavailable: identifying this host by name and route only")
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.add(_normalise_address(info[4][0]))
    except Exception:  # noqa: BLE001 - an unresolvable host name is not fatal
        logger.debug("could not resolve this host's own name")
    found.add(_normalise_address(_default_route_address()))

    # Keep IP literals only: psutil also lists link-layer entries, whose "address" is a
    # MAC (or an all-zero placeholder), and those have no business in a set compared
    # against client IPs.
    _self_addresses = frozenset(address for address in found if _is_ip_literal(address))
    logger.info("Proxy: %d address(es) belong to this machine", len(_self_addresses))
    return _self_addresses


def invalidate_self_addresses() -> None:
    """Forget the cached list (a DHCP lease or an interface change makes it stale)."""
    global _self_addresses  # noqa: PLW0603
    _self_addresses = None


def _default_route_address() -> str:
    """The address the OS would use to reach the internet (one interface), or ""."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.2)
            probe.connect(("8.8.8.8", 53))
            return str(probe.getsockname()[0])
    except OSError:
        return ""


def _normalise_address(value: object) -> str:
    """A bare IP literal: no zone suffix, no brackets."""
    return str(value or "").split("%", 1)[0].strip().strip("[]")


def _is_ip_literal(value: str) -> bool:
    """True for something ``ipaddress`` accepts (a MAC is not an address here)."""
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def is_self_address(ip_str: str | None) -> bool:
    """True if ``ip_str`` belongs to this machine: loopback or one of its own addresses.

    The LAN-IP half of the invariant ``is_loopback_ip`` enforces for 127.0.0.0/8: a
    process on this host reaching the proxy on its own interface address used to look
    like a device to learn, and ended up in the device list with a buffered pair.
    """
    return self_connection_reason(ip_str) is not None


def self_connection_reason(ip_str: str | None) -> str | None:
    """Why ``ip_str`` is this machine talking to itself, or ``None`` for a real client.

    The reason is returned rather than a boolean so the callers can log *which* of the
    two cases it is (``loopback`` or ``own-address``) without testing twice.
    """
    if not ip_str:
        return None
    literal = _normalise_address(ip_str)
    if is_loopback_ip(literal):
        return "loopback"
    try:
        addr = ipaddress.ip_address(literal)
    except ValueError:
        return None
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        literal = str(mapped)
    mine = self_addresses()
    if literal in mine or f"::ffff:{literal}" in mine:
        return "own-address"
    return None


def _apply_config(config: Config) -> None:
    """Update cached DNS server lists from a loaded Config object."""
    global _last_dns_servers, _last_dns_servers_v6  # noqa: PLW0603
    dns_cfg = config.dns
    if dns_cfg.dns_servers:
        _last_dns_servers = list(dns_cfg.dns_servers)
    if dns_cfg.dns_servers_v6:
        _last_dns_servers_v6 = list(dns_cfg.dns_servers_v6)


def _build_resolver() -> dns.asyncresolver.Resolver:
    """Build an async resolver configured with the upstream DNS servers.

    In dnspython >= 2.6 the legacy ``AsyncResolver`` was removed and a single
    IPv4+IPv6 ``nameservers`` list replaced the separate ``nameservers_v6``
    attribute, so IPv4 and IPv6 servers are merged into one list.
    """
    resolver = dns.asyncresolver.Resolver(configure=False)
    nameservers = list(_last_dns_servers)
    nameservers.extend(_last_dns_servers_v6)
    resolver.nameservers = nameservers
    resolver.timeout = 5.0
    resolver.lifetime = 10.0
    return resolver


async def resolve_upstream(  # noqa: C901, PLR0912, PLR0915
    hostname: str,
    *,
    prefer_ipv6: bool = False,
    skip_cache: bool = False,
) -> list[str]:
    """Resolve a cloud hostname bypassing the local DNS.

    Tries nameservers in order (configured in ``dns.dns_servers``, default
    8.8.8.8 → 1.1.1.1) and returns a list of IPv4 and IPv6 addresses
    (A + AAAA records). Falls back to the system resolver only when all
    upstream DNS servers are unreachable.

    The DNS server list is lazily refreshed from the global ConfigManager
    the first time this function is called in a session.

    Args:
        hostname: The domain name to resolve (e.g. ``api.example.com``).
        prefer_ipv6: If True, IPv6 addresses are returned first.
        skip_cache: Bypass the in-memory cache.

    Returns:
        A list of IP address strings (IPv4 and/or IPv6).  May be empty
        when both upstream DNS and system resolver are unreachable.
    """
    # Check cache (TTLCache handles expiration automatically)
    if not skip_cache:
        cached = _resolver_cache.get(hostname)
        if cached is not None:
            logger.debug("Resolver cache hit for %s", hostname)
            result = list(cached)
            if prefer_ipv6:
                # Performance optimization: single-pass partition avoids calling _addr_family twice per address (~2x faster)  # noqa: E501
                v6: list[str] = []
                v4: list[str] = []
                for ip in result:
                    if _addr_family(ip) == _IPV6_VERSION:
                        v6.append(ip)
                    else:
                        v4.append(ip)
                result = v6 + v4
            return result

    resolver = _build_resolver()
    addresses: list[str] = []

    logger.debug(
        "Resolving %s via upstream DNS (%s) ...",
        hostname,
        resolver.nameservers,
    )

    try:
        # Resolve A records (IPv4)
        answers = await asyncio.wait_for(resolver.resolve(hostname, "A"), timeout=10.0)
        addresses.extend(str(r) for r in answers)
    except dns.exception.DNSException as exc:
        logger.warning("Upstream A-record resolution failed for %s: %s", hostname, exc)

    try:
        # Resolve AAAA records (IPv6)
        answers6 = await asyncio.wait_for(resolver.resolve(hostname, "AAAA"), timeout=5.0)
        v6_list = [str(r) for r in answers6]
        if prefer_ipv6:
            v6_list.extend(addresses)
            addresses = v6_list
        else:
            addresses.extend(v6_list)
    except dns.exception.DNSException:
        pass

    # ── Fallback to system resolver when upstream DNS is unreachable ──
    if not addresses:
        logger.warning(
            "Upstream DNS unreachable for %s; falling back to system resolver. "
            "This may cause forwarding loops if the local DNS returns the proxy's IP.",
            hostname,
        )
        try:
            loop = asyncio.get_running_loop()
            addrinfo = await asyncio.wait_for(loop.getaddrinfo(hostname, None, type=0), timeout=5.0)
            seen: set[str] = set()
            for family, _, _, _, sockaddr in addrinfo:
                ip = sockaddr[0]
                if ip not in seen:
                    addresses.append(ip)
                    seen.add(ip)
        except Exception as exc:
            logger.error("System resolver fallback also failed for %s: %s", hostname, exc)  # noqa: TRY400

    # ── SSRF guard ───────────────────────────────────────────────────────────
    # Drop any address that resolves to loopback/private/link-local/metadata
    # space, regardless of which resolution path produced it. This is the
    # single choke point so every caller (forwarding, adapters, protocol
    # servers) inherits the protection.
    # Performance optimization: single-pass partition avoids calling is_blocked_ip twice per address (~2x faster)  # noqa: E501
    safe_addresses: list[str] = []
    blocked_addresses: list[str] = []
    for ip in addresses:
        if is_blocked_ip(ip):
            blocked_addresses.append(ip)
        else:
            safe_addresses.append(ip)

    if blocked_addresses:
        logger.warning(
            "Dropping %d unsafe address(es) for %s: %s",
            len(blocked_addresses),
            hostname,
            blocked_addresses,
        )
    addresses = safe_addresses

    # Update cache (TTLCache manages expiration)
    if addresses:
        _resolver_cache[hostname] = list(addresses)

    logger.debug("Resolved %s → %s", hostname, addresses)
    return addresses


async def batch_resolve_upstream(hostnames: list[str], **kwargs) -> dict[str, list[str]]:
    """Resolve multiple hostnames in parallel via upstream DNS."""
    tasks = {h: resolve_upstream(h, **kwargs) for h in hostnames}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    mapping: dict[str, list[str]] = {}
    for hostname, result in zip(tasks, results):
        if isinstance(result, Exception):
            logger.error("Batch resolve failed for %s: %s", hostname, result)
            mapping[hostname] = []
        else:
            mapping[hostname] = result
    return mapping


def clear_cache() -> None:
    """Clear the in-memory resolver cache."""
    _resolver_cache.clear()
    is_blocked_ip.cache_clear()


def get_cache_stats() -> dict:
    """Return basic cache statistics."""
    return {
        "entries": len(_resolver_cache),
        "ttl_seconds": CACHE_TTL,
    }

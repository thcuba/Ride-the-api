"""
Dual-stack upstream DNS resolver.

Resolves cloud hostnames directly via configurable upstream DNS servers,
bypassing the local DNS (dnsmasq / Pi-hole / AdGuard Home).

This prevents forwarding loops where the proxy re-enters itself when the
local DNS server returns the proxy's own IP for a cloud hostname.

DNS servers are configured in ``config.yaml`` under ``dns.dns_servers``
(IPv4) and ``dns.dns_servers_v6`` (IPv6), defaulting to 8.8.8.8 / 1.1.1.1.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from typing import TYPE_CHECKING

import dns.asyncresolver
import dns.exception
import dns.resolver

if TYPE_CHECKING:
    from core.config import Config

logger = logging.getLogger(__name__)

# Cache TTL
CACHE_TTL = 300  # 5 minutes

# IP version constant for the prefer_ipv6 re-ordering.
_IPV6_VERSION = 6

# In-memory cache: hostname -> (timestamp, [ip_addresses])
_resolver_cache: dict[str, tuple[float, list[str]]] = {}

# In-memory snapshot of the last DNS config so we can rebuild the resolver
# without importing ConfigManager at module level (avoids circular imports).
_last_dns_servers: list[str] = ["8.8.8.8", "1.1.1.1"]
_last_dns_servers_v6: list[str] = [
    "2001:4860:4860::8888",
    "2606:4700:4700::1111",
]


def _addr_family(address: str) -> int:
    """Return IP version (4 or 6); non-IP strings default to 4."""
    try:
        return ipaddress.ip_address(address.split("%", maxsplit=1)[0]).version
    except ValueError:
        return 4


def _apply_config(config: Config) -> None:
    """Update cached DNS server lists from a loaded Config object."""
    global _last_dns_servers, _last_dns_servers_v6  # noqa: PLW0603
    dns_cfg = config.dns
    if dns_cfg.dns_servers:
        _last_dns_servers = list(dns_cfg.dns_servers)
    if dns_cfg.dns_servers_v6:
        _last_dns_servers_v6 = list(dns_cfg.dns_servers_v6)


def _build_resolver() -> dns.asyncresolver.AsyncResolver:
    """Build an AsyncResolver configured to use the configured upstream DNS servers."""
    resolver = dns.asyncresolver.AsyncResolver(configure=False)
    resolver.nameservers = _last_dns_servers
    resolver.nameservers_v6 = _last_dns_servers_v6
    resolver.timeout = 5.0
    resolver.lifetime = 10.0
    return resolver


async def resolve_upstream(  # noqa: C901, PLR0912
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
    now = time.time()

    # Check cache
    if not skip_cache:
        cached = _resolver_cache.get(hostname)
        if cached and (now - cached[0]) < CACHE_TTL:
            logger.debug("Resolver cache hit for %s", hostname)
            result = list(cached[1])
            if prefer_ipv6:
                v6 = [ip for ip in result if _addr_family(ip) == _IPV6_VERSION]
                v4 = [ip for ip in result if _addr_family(ip) != _IPV6_VERSION]
                result = v6 + v4
            return result

    resolver = _build_resolver()
    addresses: list[str] = []

    logger.debug(
        "Resolving %s via upstream DNS (%s / %s) ...",
        hostname,
        resolver.nameservers,
        resolver.nameservers_v6,
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

    # Update cache
    if addresses:
        _resolver_cache[hostname] = (now, list(addresses))
        expired = [h for h, (ts, _) in _resolver_cache.items() if (now - ts) >= CACHE_TTL]
        for h in expired:
            _resolver_cache.pop(h, None)

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


def get_cache_stats() -> dict:
    """Return basic cache statistics."""
    return {
        "entries": len(_resolver_cache),
        "ttl_seconds": CACHE_TTL,
    }

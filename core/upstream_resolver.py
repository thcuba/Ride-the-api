"""
Dual-stack upstream DNS resolver.

Resolves cloud hostnames directly via public DNS servers (8.8.8.8 / 1.1.1.1,
IPv4 + IPv6) bypassing the local DNS (dnsmasq / Pi-hole / AdGuard Home).

This prevents forwarding loops where the proxy re-enters itself when the
local DNS server returns the proxy's own IP for a cloud hostname.
"""

from __future__ import annotations

import asyncio
import logging
import time

import dns.asyncresolver
import dns.exception
import dns.resolver

logger = logging.getLogger(__name__)

# ── Public upstream DNS servers (primary → fallback), dual-stack ──────────────

UPSTREAM_DNS_SERVERS: list[str] = [
    "8.8.8.8",      # Google IPv4 (primary)
    "1.1.1.1",      # Cloudflare IPv4 (fallback)
]

UPSTREAM_DNS_SERVERS_V6: list[str] = [
    "2001:4860:4860::8888",   # Google IPv6
    "2606:4700:4700::1111",   # Cloudflare IPv6
]

# Cache TTL
CACHE_TTL = 300  # 5 minutes

# In-memory cache: hostname -> (timestamp, [ip_addresses])
_resolver_cache: dict[str, tuple[float, list[str]]] = {}


def _build_resolver() -> dns.asyncresolver.AsyncResolver:
    """Build an AsyncResolver configured to use the public upstream DNS servers."""
    resolver = dns.asyncresolver.AsyncResolver(configure=False)
    resolver.nameservers = UPSTREAM_DNS_SERVERS
    resolver.nameservers_v6 = UPSTREAM_DNS_SERVERS_V6
    resolver.timeout = 5.0
    resolver.lifetime = 10.0
    return resolver


async def resolve_upstream(
    hostname: str,
    *,
    prefer_ipv6: bool = False,
    skip_cache: bool = False,
) -> list[str]:
    """Resolve a cloud hostname bypassing the local DNS.

    Tries nameservers in order (8.8.8.8 → 1.1.1.1) and returns a list of
    IPv4 and IPv6 addresses (A + AAAA records). Falls back to the system
    resolver only when all upstream DNS servers are unreachable.

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
            return cached[1]

    resolver = _build_resolver()
    addresses: list[str] = []

    logger.debug("Resolving %s via upstream DNS (%s / %s) ...",
                 hostname, resolver.nameservers, resolver.nameservers_v6)

    try:
        # Resolve A records (IPv4)
        answers = await asyncio.wait_for(
            resolver.resolve(hostname, "A"), timeout=10.0
        )
        addresses.extend(str(r) for r in answers)
    except dns.exception.DNSException as exc:
        logger.warning("Upstream A-record resolution failed for %s: %s", hostname, exc)

    try:
        # Resolve AAAA records (IPv6)
        answers6 = await asyncio.wait_for(
            resolver.resolve(hostname, "AAAA"), timeout=5.0
        )
        v6_list = [str(r) for r in answers6]
        if prefer_ipv6:
            # Insert IPv6 at the front
            v6_list.extend(addresses)
            addresses = v6_list
        else:
            addresses.extend(v6_list)
    except dns.exception.DNSException:
        # AAAA is optional — many domains don't have IPv6
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
            addrinfo = await asyncio.wait_for(
                loop.getaddrinfo(hostname, None, type=0), timeout=5.0
            )
            seen: set[str] = set()
            for family, _, _, _, sockaddr in addrinfo:
                ip = sockaddr[0]
                if ip not in seen:
                    addresses.append(ip)
                    seen.add(ip)
        except Exception as exc:
            logger.error("System resolver fallback also failed for %s: %s", hostname, exc)

    # Update cache
    if addresses:
        _resolver_cache[hostname] = (now, list(addresses))
        # Opportunistic sweep: drop expired entries so the global cache cannot
        # grow unbounded with long-lived distinct hostnames.
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

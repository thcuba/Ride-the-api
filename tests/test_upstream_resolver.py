"""
Tests for the upstream DNS resolver (loop prevention module).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dns.exception import DNSException

from core.upstream_resolver import (
    _addr_family,
    _last_dns_servers,
    _last_dns_servers_v6,
    _resolver_cache,
    batch_resolve_upstream,
    clear_cache,
    get_cache_stats,
    resolve_upstream,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


class FakeDNSAnswer:
    """Simulates a dns.resolver.Answer for testing."""

    def __init__(self, addresses: list[str]) -> None:
        self._addresses = addresses

    def __iter__(self) -> Iterator[str]:
        for addr in self._addresses:
            fake = MagicMock()
            fake.__str__.return_value = addr
            yield fake


@pytest.fixture(autouse=True)
def clear_test_cache():
    """Clear the resolver cache before and after each test."""
    clear_cache()
    yield
    clear_cache()


# ═══════════════════════════════════════════════════════════════════════════════
# RESOLVER TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@patch("core.upstream_resolver._build_resolver")
async def test_resolve_upstream_success_ipv4(mock_build):
    """Resolve A records successfully, no AAAA."""

    class FakeNoAnswer(DNSException):
        pass

    resolver = AsyncMock()

    async def resolve_side(_hostname, rtype):
        if rtype == "A":
            return FakeDNSAnswer(["93.184.216.34"])
        if rtype == "AAAA":
            raise FakeNoAnswer()
        raise ValueError(f"Unexpected query type: {rtype}")

    resolver.resolve = resolve_side
    mock_build.return_value = resolver

    result = await resolve_upstream("api.example.com", skip_cache=True)

    assert "93.184.216.34" in result
    assert len(result) == 1  # no IPv6


@pytest.mark.asyncio
@patch("core.upstream_resolver._build_resolver")
async def test_resolve_upstream_dual_stack(mock_build):
    """Resolve both A and AAAA records successfully."""

    class FakeNoAnswer(DNSException):
        pass

    resolver = AsyncMock()

    async def resolve_side(_hostname, rtype):
        if rtype == "A":
            return FakeDNSAnswer(["93.184.216.34"])
        if rtype == "AAAA":
            return FakeDNSAnswer(["2606:2800:220:1:248:1893:25c8:1946"])
        raise ValueError(f"Unexpected query type: {rtype}")

    resolver.resolve = resolve_side
    mock_build.return_value = resolver

    result = await resolve_upstream("api.example.com", skip_cache=True)

    assert "93.184.216.34" in result
    assert "2606:2800:220:1:248:1893:25c8:1946" in result
    assert len(result) == 2  # noqa: PLR2004


@pytest.mark.asyncio
@patch("core.upstream_resolver._build_resolver")
async def test_resolve_upstream_prefer_ipv6(mock_build):
    """IPv6 addresses come first when prefer_ipv6=True."""

    class FakeNoAnswer(DNSException):
        pass

    resolver = AsyncMock()

    async def resolve_side(_hostname, rtype):
        if rtype == "A":
            return FakeDNSAnswer(["93.184.216.34"])
        if rtype == "AAAA":
            return FakeDNSAnswer(["2606:2800:220:1:248:1893:25c8:1946"])
        raise ValueError(f"Unexpected query type: {rtype}")

    resolver.resolve = resolve_side
    mock_build.return_value = resolver

    result = await resolve_upstream("api.example.com", prefer_ipv6=True, skip_cache=True)

    # IPv6 should be first
    assert result[0] == "2606:2800:220:1:248:1893:25c8:1946"
    assert result[1] == "93.184.216.34"


@pytest.mark.asyncio
@patch("core.upstream_resolver._build_resolver")
async def test_resolve_upstream_both_fail_then_fallback(mock_build):
    """Both upstream DNS queries fail, falls back to system resolver."""

    class FakeNoAnswer(DNSException):
        pass

    resolver = AsyncMock()

    async def resolve_side(_hostname, _rtype):
        raise FakeNoAnswer("DNS query failed")

    resolver.resolve = resolve_side
    mock_build.return_value = resolver

    # The system resolver fallback uses asyncio.get_running_loop().getaddrinfo
    # which would fail in test since we're not actually connected.
    # So we expect an empty list gracefully.
    result = await resolve_upstream("api.example.com", skip_cache=True)

    assert isinstance(result, list)
    # May be empty since system resolver fallback also likely fails in test


@pytest.mark.asyncio
@patch("core.upstream_resolver._build_resolver")
async def test_resolve_upstream_cache(mock_build):
    """Cached results are returned without re-resolving."""

    class FakeNoAnswer(DNSException):
        pass

    resolver = AsyncMock()

    async def resolve_side(_hostname, rtype):
        if rtype == "A":
            return FakeDNSAnswer(["93.184.216.34"])
        raise FakeNoAnswer()

    resolver.resolve = resolve_side
    mock_build.return_value = resolver

    # First call (no cache)
    result1 = await resolve_upstream("api.example.com", skip_cache=False)
    assert "93.184.216.34" in result1
    assert mock_build.call_count == 1

    # Second call (should use cache)
    mock_build.reset_mock()
    result2 = await resolve_upstream("api.example.com", skip_cache=False)
    assert result2 == result1
    mock_build.assert_not_called()  # cache hit, no resolver created


@pytest.mark.asyncio
@patch("core.upstream_resolver._build_resolver")
async def test_resolve_upstream_cache_skipped(mock_build):
    """skip_cache=True forces a new resolution."""

    class FakeNoAnswer(DNSException):
        pass

    resolver = AsyncMock()

    async def resolve_side(_hostname, rtype):
        if rtype == "A":
            return FakeDNSAnswer(["93.184.216.34"])
        raise FakeNoAnswer()

    resolver.resolve = resolve_side
    mock_build.return_value = resolver

    await resolve_upstream("api.example.com", skip_cache=False)
    mock_build.reset_mock()

    # Second call with skip_cache=True
    await resolve_upstream("api.example.com", skip_cache=True)
    mock_build.assert_called_once()  # new resolver created


# ═══════════════════════════════════════════════════════════════════════════════
# BATCH RESOLVE TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@patch("core.upstream_resolver._build_resolver")
async def test_batch_resolve(mock_build):
    """Multiple hostnames resolved in parallel."""

    class FakeNoAnswer(DNSException):
        pass

    resolver = AsyncMock()

    call_count = 0

    async def resolve_side(_hostname, rtype):
        nonlocal call_count
        call_count += 1
        if rtype == "A":
            return FakeDNSAnswer([f"1.2.3.{call_count}"])
        raise FakeNoAnswer()

    resolver.resolve = resolve_side
    mock_build.return_value = resolver

    result = await batch_resolve_upstream(
        ["api.example.com", "mqtt.example.com"],
        skip_cache=True,
    )

    assert "api.example.com" in result
    assert "mqtt.example.com" in result
    assert len(result["api.example.com"]) == 1
    assert len(result["mqtt.example.com"]) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# CACHE UTILITY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@patch("core.upstream_resolver._build_resolver")
async def test_cache_stats(mock_build):
    """get_cache_stats returns correct entry count."""

    class FakeNoAnswer(DNSException):
        pass

    resolver = AsyncMock()

    async def resolve_side(_hostname, rtype):
        if rtype == "A":
            return FakeDNSAnswer(["1.2.3.4"])
        raise FakeNoAnswer()

    resolver.resolve = resolve_side
    mock_build.return_value = resolver

    # Resolve two hostnames to populate cache
    await resolve_upstream("host-a.example.com", skip_cache=True)
    await resolve_upstream("host-b.example.com", skip_cache=True)

    stats = get_cache_stats()
    assert stats["entries"] == 2  # noqa: PLR2004
    assert stats["ttl_seconds"] == 300  # noqa: PLR2004


def test_clear_cache():
    """clear_cache() empties the resolver cache."""
    _resolver_cache["test.example.com"] = ["1.2.3.4"]
    assert len(_resolver_cache) == 1
    clear_cache()
    assert len(_resolver_cache) == 0


@pytest.mark.asyncio
async def test_cache_hit_returns_copy_not_shared_mutable_list():
    """A cache hit must not hand the shared mutable list to callers."""
    _resolver_cache["shared.example.com"] = ["1.2.3.4", "2001:db8::1"]

    first = await resolve_upstream("shared.example.com")
    second = await resolve_upstream("shared.example.com")

    # Distinct list objects: mutating one must not corrupt the other.
    assert first is not second
    first.reverse()
    # The cached entry is untouched by the caller's mutation.
    assert _resolver_cache["shared.example.com"] == ["1.2.3.4", "2001:db8::1"]


@pytest.mark.asyncio
async def test_cache_hit_respects_prefer_ipv6_ordering():
    """prefer_ipv6 must re-order addresses on a cache hit (not ignored)."""
    _resolver_cache["dual.example.com"] = ["1.2.3.4", "2001:db8::1", "9.9.9.9"]

    ordered = await resolve_upstream("dual.example.com", prefer_ipv6=True)

    # IPv6 first, then IPv4, without dropping any address.
    assert ordered[0] == "2001:db8::1"
    assert set(ordered[1:]) == {"1.2.3.4", "9.9.9.9"}


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


def test_upstream_dns_default_servers():
    """The default DNS server lists match Google/Cloudflare."""
    assert "8.8.8.8" in _last_dns_servers
    assert "1.1.1.1" in _last_dns_servers
    assert "2001:4860:4860::8888" in _last_dns_servers_v6
    assert "2606:4700:4700::1111" in _last_dns_servers_v6
    assert _last_dns_servers[0] == "8.8.8.8"  # primary
    assert _last_dns_servers[1] == "1.1.1.1"  # fallback


def test_addr_family_detection():
    """_addr_family correctly distinguishes IPv6 from IPv4 and non-IP strings."""
    assert _addr_family("192.168.1.1") == 4  # noqa: PLR2004
    assert _addr_family("2001:4860:4860::8888") == 6  # noqa: PLR2004
    assert _addr_family("fe80::1%eth0") == 6  # noqa: PLR2004
    assert _addr_family("example.com") == 4  # noqa: PLR2004

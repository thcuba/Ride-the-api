"""The forward must use the request's ``Host`` header, whatever its case.

The proxy route builds the intercepted request from ``dict(request.headers)``,
where uvicorn/Starlette hand the header names over lower-cased. A literal
``headers["Host"]`` lookup therefore missed **every** real device request, and the
forward silently fell back to the hostname in the adapter configuration — the
device's traffic could go to the wrong endpoint.

Found by running the live path on the Raspberry Pi: a request with
``Host: httpbingo.org`` was forwarded to the configured ``api.example.com``.
"""

from __future__ import annotations

from adapters.base import InterceptedRequest, ProtocolType, host_from_request
from adapters.example import ExampleProtocolAdapter
from adapters.shelly import ShellyProtocolAdapter


def _request(headers: dict[str, str]) -> InterceptedRequest:
    return InterceptedRequest(
        device_id="dev-1",
        timestamp=0,
        protocol=ProtocolType.HTTP,
        method="POST",
        path="/v1/commands",
        headers=headers,
        body=None,
    )


def test_helper_finds_the_host_whatever_the_case():
    """HTTP header names are case-insensitive; all three spellings must work."""
    assert host_from_request(_request({"host": "vendor.example"})) == "vendor.example"
    assert host_from_request(_request({"Host": "vendor.example"})) == "vendor.example"
    assert host_from_request(_request({"HOST": "vendor.example"})) == "vendor.example"


def test_helper_strips_an_explicit_port():
    assert host_from_request(_request({"host": "vendor.example:8443"})) == "vendor.example"


def test_helper_returns_none_without_a_host_header():
    assert host_from_request(_request({})) is None
    assert host_from_request(_request({"content-type": "application/json"})) is None
    assert host_from_request(_request({"host": ""})) is None


def test_both_forwarding_adapters_find_a_lower_cased_host():
    """The adapters use the shared helper, so the case no longer matters."""
    for adapter in (ExampleProtocolAdapter, ShellyProtocolAdapter):
        assert adapter._host_from_request(_request({"host": "vendor.example"})) == "vendor.example"
        assert adapter._host_from_request(_request({"Host": "vendor.example"})) == "vendor.example"


def test_helper_parses_an_ipv6_literal():
    """An IPv6 literal is bracketed: splitting on the first colon gave a bare '['."""
    assert host_from_request(_request({"host": "[2001:db8::1]:8443"})) == "2001:db8::1"
    assert host_from_request(_request({"host": "[2001:db8::1]"})) == "2001:db8::1"
    assert host_from_request(_request({"Host": "[::1]:80"})) == "::1"


def test_a_malformed_literal_is_left_whole_not_truncated():
    """Better an obviously broken value than a plausible-looking '['."""
    assert host_from_request(_request({"host": "[2001:db8::1"})) == "[2001:db8::1"
    assert host_from_request(_request({"host": "[::1:8443"})) == "[::1:8443"


def test_a_plain_hostname_is_unchanged_by_the_literal_handling():
    assert host_from_request(_request({"host": "vendor.example"})) == "vendor.example"
    assert host_from_request(_request({"host": "vendor.example:8443"})) == "vendor.example"
    assert host_from_request(_request({"host": "192.168.178.77:8911"})) == "192.168.178.77"

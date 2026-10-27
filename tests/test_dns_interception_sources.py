"""Interception covers every service, not one hand-listed at a time.

The rule this file protects: which names are intercepted must not depend on an operator
remembering to append them to ``dns.intercept``. A registered adapter already declares the
hostnames of the service it implements, so shipping an adapter must be enough to make that
service interceptable.

Equally protected: a name **neither** configured **nor** claimed by an adapter is still
forwarded. Widening the match must never turn into intercepting the world.
"""

from __future__ import annotations

import pytest

from adapters import register_all_adapters
from core import dns_server
from core.config import Config
from core.dns_server import DNSInterceptor

# Hostnames no adapter claims, used to prove the negative case.
UNRELATED = "unrelated.host.invalid"


@pytest.fixture(autouse=True)
def _fresh_adapter_cache():
    """The adapter hostname list is cached per process; each test starts from a cold cache."""
    original = dns_server._adapter_hostnames_cache
    dns_server._adapter_hostnames_cache = None
    yield
    dns_server._adapter_hostnames_cache = original


def _interceptor(intercept: list[str] | None = None) -> DNSInterceptor:
    config = Config()
    dns = config.dns
    dns.enabled = True
    dns.answer_ip = "192.168.1.200"
    dns.intercept = intercept or []
    return DNSInterceptor(config)


# ── the adapters are a source of intercepted names ───────────────────────────


def test_registry_exposes_adapter_hostnames():
    """The registry can enumerate the hostnames its adapters claim."""
    hostnames = register_all_adapters().list_hostnames()
    assert hostnames, "nessun hostname dagli adapter: il registry non li espone"
    # The bundled adapters declare their own names; none of them is a real service.
    assert all(isinstance(name, str) and name for name in hostnames)


def test_adapter_hostname_is_intercepted_without_configuration():
    """A name belonging to a registered adapter is intercepted with an empty dns.intercept."""
    interceptor = _interceptor(intercept=[])
    hostname = register_all_adapters().list_hostnames()[0]
    assert interceptor.matches(hostname) is True


def test_interception_covers_every_adapter_not_just_the_first():
    """All services are covered, not whichever adapter happened to be registered first."""
    interceptor = _interceptor(intercept=[])
    hostnames = register_all_adapters().list_hostnames()
    assert len(hostnames) > 1, "serve piu' di un adapter perche' il test significhi qualcosa"
    for hostname in hostnames:
        assert interceptor.matches(hostname) is True, f"{hostname} non intercettato"


def test_adapter_glob_hostname_is_intercepted():
    """An adapter declaring a device family (a glob such as ``device-*``) covers those names.

    Adapters and ``dns.intercept`` are written in different shapes - one globs a family of devices,
    the other uses ``*.domain`` - so both have to be understood.
    """
    interceptor = _interceptor(intercept=[])
    globs = [
        h for h in register_all_adapters().list_hostnames() if "*" in h and not h.startswith("*.")
    ]
    if not globs:
        pytest.skip("nessun adapter dichiara un glob di famiglia in questa configurazione")
    pattern = globs[0]
    concrete = pattern.replace("*", "abc123")
    assert interceptor.matches(concrete) is True, f"{concrete} non coperto da {pattern}"


def test_adapter_domain_wildcard_covers_subdomains():
    """Where an adapter declares ``*.domain``, the domain and its subdomains are covered."""
    interceptor = _interceptor(intercept=[])
    for hostname in register_all_adapters().list_hostnames():
        if hostname.startswith("*."):
            domain = hostname[2:]
            assert interceptor.matches(domain) is True
            assert interceptor.matches(f"anything.{domain}") is True
            return
    pytest.skip("nessun adapter dichiara una wildcard di dominio")


# ── the configured names keep working, unchanged ─────────────────────────────


def test_configured_name_is_still_intercepted():
    """An arbitrary configured name with no adapter behind it is intercepted as before."""
    interceptor = _interceptor(intercept=["ats.iot.vendor-cloud.example"])
    assert interceptor.matches("ats.iot.vendor-cloud.example") is True
    assert interceptor.matches("ATS.IOT.VENDOR-CLOUD.EXAMPLE.") is True


def test_configured_wildcard_is_still_intercepted():
    interceptor = _interceptor(intercept=["*.vendor-cloud.example"])
    assert interceptor.matches("deep.sub.vendor-cloud.example") is True
    assert interceptor.matches("vendor-cloud.example") is True


# ── the widening is bounded ──────────────────────────────────────────────────


def test_unclaimed_name_is_not_intercepted():
    """Neither configured nor claimed: forwarded, not intercepted."""
    interceptor = _interceptor(intercept=[])
    assert interceptor.matches(UNRELATED) is False
    assert interceptor.matches("www.wikipedia.org") is False


def test_name_is_not_intercepted_by_a_suffix_of_an_adapter_hostname():
    """A name merely *ending in* an adapter hostname is not the hostname."""
    hostname = register_all_adapters().list_hostnames()[0]
    if hostname.startswith("*."):
        pytest.skip("primo hostname wildcard: la regola di confine e' coperta altrove")
    interceptor = _interceptor(intercept=[])
    assert interceptor.matches(f"evil-{hostname}") is False


# ── a missing registry degrades safely ───────────────────────────────────────


def test_unavailable_registry_falls_back_to_configured_names(monkeypatch):
    """If the adapter registry cannot be read, interception falls back - it does not widen."""
    import adapters  # noqa: PLC0415

    def _boom():
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(adapters, "register_all_adapters", _boom)
    interceptor = _interceptor(intercept=["kept.vendor-cloud.example"])

    assert interceptor.matches("kept.vendor-cloud.example") is True
    assert interceptor.matches(UNRELATED) is False

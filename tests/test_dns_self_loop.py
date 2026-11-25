"""C8: il proxy DNS non inoltra una query a sé stesso.

`is_self_address` era già importato in `dns_server.py` ma serviva un solo caso —
`intercept_all` — e mai il forwarding. Con un upstream che punta a questo stesso server
(la configurazione naturale quando il proxy è anche il resolver dichiarato ai device) ogni
query rientra da `handle_query` e viene inoltrata di nuovo: misurate **301 query per una**,
cioè una richiesta DNS che non arriva mai e un processo che si riempie di lavoro.

La porta fa parte del test, ed è la ragione per cui `is_self_address` da solo non basta:
un altro resolver sullo **stesso host** e su una **porta diversa** è un upstream legittimo.
"""

import dns.asyncquery
import dns.flags
import dns.message
import pytest

from core.config import Config
from core.dns_server import DNSInterceptor

SELF_PORT = 53


def _config(**overrides) -> Config:
    config = Config()
    dns = config.dns
    dns.enabled = True
    dns.listen_host = "127.0.0.1"
    dns.listen_port = SELF_PORT
    dns.forward_timeout = 0.4
    dns.dns_servers = []
    dns.dns_servers_v6 = []
    for key, value in overrides.items():
        setattr(dns, key, value)
    return config


def _query(name: str = "example.com") -> dns.message.Message:
    return dns.message.make_query(name, "A")


def _recording_upstream(monkeypatch, *, answer: bool = True) -> list[tuple[str, int]]:
    """Sostituisce l'invio UDP e registra chi sarebbe stato interrogato."""
    seen: list[tuple[str, int]] = []

    async def fake_udp(_query, host, port=53, timeout=None, **_kwargs):  # noqa: ARG001
        seen.append((host, port))
        if not answer:
            raise AssertionError(f"non doveva essere interrogato: {host}:{port}")
        return dns.message.make_response(_query)

    monkeypatch.setattr(dns.asyncquery, "udp", fake_udp)
    return seen


# ---------------------------------------------------------------------------
#  Mai verso sé stessi
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "upstream",
    [
        "127.0.0.1",  # loopback, porta di ascolto
        "127.0.0.1:53",
        "localhost",
        "localhost:53",
        "0.0.0.0",  # su Linux arriva comunque a sé stessi
        "[::1]",
    ],
)
async def test_a_query_is_never_forwarded_to_this_server(upstream, monkeypatch):
    """Il cuore di C8: nessuna query viene inoltrata a sé stessi, quindi nessun loop."""
    seen = _recording_upstream(monkeypatch, answer=False)
    interceptor = DNSInterceptor(_config(dns_servers=[upstream]))

    result = await interceptor._forward(_query())

    assert result is None, "senza upstream utilizzabili la risposta è SERVFAIL, non un loop"
    assert seen == [], f"{upstream} non doveva essere interrogato"


@pytest.mark.asyncio
async def test_the_loop_does_not_happen_even_with_one_usable_upstream(monkeypatch):
    """Un upstream sé stesso viene saltato e si passa al successivo: la query vive."""
    seen = _recording_upstream(monkeypatch)
    interceptor = DNSInterceptor(_config(dns_servers=["127.0.0.1", "203.0.113.7"]))

    result = await interceptor._forward(_query())

    assert result is not None
    assert seen == [("203.0.113.7", SELF_PORT)], "solo l'upstream vero doveva essere chiesto"


# ---------------------------------------------------------------------------
#  Il caso legittimo che non va rotto
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_another_resolver_on_the_same_host_is_still_asked(monkeypatch):
    """Stesso host, porta diversa: non è un loop, è un resolver da usare."""
    seen = _recording_upstream(monkeypatch)
    interceptor = DNSInterceptor(_config(dns_servers=["127.0.0.1:5353"]))

    result = await interceptor._forward(_query())

    assert result is not None
    assert seen == [("127.0.0.1", 5353)]


@pytest.mark.asyncio
async def test_a_normal_upstream_is_untouched(monkeypatch):
    seen = _recording_upstream(monkeypatch)
    interceptor = DNSInterceptor(_config(dns_servers=["203.0.113.7"]))

    assert await interceptor._forward(_query()) is not None
    assert seen == [("203.0.113.7", SELF_PORT)]


@pytest.mark.asyncio
async def test_the_check_uses_the_configured_listening_port(monkeypatch):
    """Se il proxy ascolta su 5353, allora 127.0.0.1:5353 è sé stessi e 127.0.0.1:53 no."""
    seen = _recording_upstream(monkeypatch)
    interceptor = DNSInterceptor(_config(listen_port=5353, dns_servers=["127.0.0.1:5353"]))

    assert await interceptor._forward(_query()) is None
    assert seen == []


# ---------------------------------------------------------------------------
#  Il log
# ---------------------------------------------------------------------------


def test_the_warning_is_emitted_once_per_upstream(caplog):
    """Un avviso per query inonderebbe il log: la configurazione si sbaglia una volta."""
    interceptor = DNSInterceptor(_config(dns_servers=["127.0.0.1"]))

    with caplog.at_level("WARNING"):
        interceptor._warn_self_upstream("127.0.0.1")
        interceptor._warn_self_upstream("127.0.0.1")
        interceptor._warn_self_upstream("localhost:53")

    assert caplog.text.count("is this server itself") == 2  # noqa: PLR2004
    assert "127.0.0.1" in caplog.text
    assert "listening on port 53" in caplog.text

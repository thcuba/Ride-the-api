"""C5: la ``base_url`` di un profilo LLM non può portare dove non deve.

L'endpoint di test riceve ``base_url`` dal corpo della richiesta (il pulsante Test della
dashboard manda i valori del form prima di salvarli) e apre una connessione dove gli si
dice. Senza guardia la macchina contatta quello che vuole chi chiama: percorsi ``file://``,
l'endpoint dei metadata del cloud, indirizzi multicast, uno ``0.0.0.0``.

Quello che **passa** conta quanto quello che viene bloccato: un provider vive
legittimamente su loopback (Ollama su ``127.0.0.1:11434``) o sulla LAN, e una whitelist
andrebbe modificata ogni volta che se ne aggiunge uno.
"""

import socket
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

import core.server as server_mod
from core.llm.client_pool import LLMClientPool
from core.llm.models import LLMProfile
from core.llm.url_guard import LLMUrlRejected, check_base_url, reject_reason
from core.server import app

# ---------------------------------------------------------------------------
#  Rifiutati sul solo testo della URL
# ---------------------------------------------------------------------------

REFUSED = [
    "file:///etc/passwd",
    "gopher://127.0.0.1:70/_",
    "ftp://example.com/v1",
    "api.openai.com/v1",  # nessuno schema: httpx la interpreterebbe a modo suo
    "",
    "   ",
    "http:///v1",  # nessun host
    "http://169.254.169.254/latest/meta-data",  # metadata del cloud
    "http://[fe80::1]/v1",  # link-local IPv6
    "http://0.0.0.0:8080/v1",
    "http://224.0.0.1/v1",  # multicast
    "http://240.0.0.1/v1",  # riservato
    "http://[::]:8080/v1",
]


@pytest.mark.parametrize("url", REFUSED)
def test_a_url_that_cannot_be_a_provider_is_refused(url):
    assert reject_reason(url) is not None, f"{url} doveva essere rifiutata"


# ---------------------------------------------------------------------------
#  Ammessi: il provider vero, locale o in LAN
# ---------------------------------------------------------------------------

ALLOWED = [
    "https://generativelanguage.googleapis.com",
    "https://api.openai.com/v1",
    "http://127.0.0.1:11434/v1",  # Ollama
    "http://localhost:11434/v1",
    "http://[::1]:11434/v1",
    "http://192.168.178.200:8080/v1",  # provider sulla LAN
    "http://10.0.0.5:8000/v1",
]


@pytest.mark.parametrize("url", ALLOWED)
def test_a_real_provider_is_allowed(url):
    assert reject_reason(url) is None, f"{url} non doveva essere rifiutata"


# ---------------------------------------------------------------------------
#  Nomi: serve risolverli, altrimenti metadata.google.internal passa
# ---------------------------------------------------------------------------


def _answers(*addresses: str):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (addr, 0))
        for addr in addresses
    ]


@pytest.mark.asyncio
async def test_a_name_resolving_only_to_the_metadata_endpoint_is_refused():
    """Il testo non basta: metadata.google.internal è un nome come un altro."""
    with (
        patch("socket.getaddrinfo", return_value=_answers("169.254.169.254")),
        pytest.raises(LLMUrlRejected, match="resolves to"),
    ):
        await check_base_url("http://metadata.google.internal/v1")


@pytest.mark.asyncio
async def test_a_name_resolving_to_a_normal_address_passes():
    with patch("socket.getaddrinfo", return_value=_answers("142.250.1.1")):
        await check_base_url("https://generativelanguage.googleapis.com")


@pytest.mark.asyncio
async def test_one_usable_answer_is_enough():
    """Un nome può avere più indirizzi: ne basta uno raggiungibile."""
    with patch("socket.getaddrinfo", return_value=_answers("169.254.169.254", "142.250.1.1")):
        await check_base_url("https://provider.example.com/v1")


@pytest.mark.asyncio
async def test_an_unresolvable_name_is_left_to_fail_on_its_own():
    """Un refuso non è un attacco: la connessione fallisce da sola, non serve fingere."""
    with patch("socket.getaddrinfo", side_effect=socket.gaierror):
        await check_base_url("http://typo.invalid/v1")


@pytest.mark.asyncio
async def test_a_literal_address_is_never_resolved():
    """Su un IP letterale non c'è rebinding: il DNS non viene interrogato affatto."""
    with patch("socket.getaddrinfo") as resolver:
        await check_base_url("http://192.168.178.200:8080/v1")

    resolver.assert_not_called()


# ---------------------------------------------------------------------------
#  Il client del test non segue i redirect: sarebbe il modo di aggirare tutto
# ---------------------------------------------------------------------------


def _profile(base_url: str = "https://example.com/v1") -> LLMProfile:
    return LLMProfile(
        name="_test",
        base_url=base_url,
        api_key=SecretStr("k"),
        model_id="m",
        prompt_template="",
        enabled=True,
        timeout=5,
        max_retries=0,
    )


@pytest.mark.asyncio
async def test_the_test_client_does_not_follow_redirects():
    """Un redirect porterebbe la richiesta su un host che nessuno ha controllato."""
    pool = LLMClientPool()
    client = pool.build_client(_profile(), follow_redirects=False)
    try:
        assert client._client.follow_redirects is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_normal_profile_client_still_follows_redirects():
    """Il comportamento dei profili veri non cambia."""
    pool = LLMClientPool()
    client = pool.build_client(_profile())
    try:
        assert client._client.follow_redirects is True
    finally:
        await client.close()


# ---------------------------------------------------------------------------
#  L'endpoint: 400 e nessuna connessione tentata
# ---------------------------------------------------------------------------


class _RecordingService:
    """Servizio LLM finto che registra se qualcuno ha provato a connettersi."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def test_connection(self, **kwargs):
        self.calls.append(kwargs)
        return {"success": True, "content": "hi"}

    def stored_api_key(self, _name):  # noqa: ANN001
        return "k"


@pytest.fixture
def client_and_service(monkeypatch):
    service = _RecordingService()
    monkeypatch.setattr(server_mod, "llm_decipher_service", service)
    cfg = server_mod.config_manager.config
    saved_auth = cfg.security.auth_enabled
    cfg.security.auth_enabled = False
    try:
        yield TestClient(app), service
    finally:
        cfg.security.auth_enabled = saved_auth


def test_the_test_endpoint_refuses_the_metadata_endpoint(client_and_service):
    client, service = client_and_service
    resp = client.post(
        "/api/llm/profiles/test",
        json={"base_url": "http://169.254.169.254/latest/meta-data", "model_id": "m"},
    )

    assert resp.status_code == 400  # noqa: PLR2004
    assert "link-local" in resp.json()["error"]
    assert service.calls == [], "la richiesta è partita lo stesso"


def test_the_test_endpoint_refuses_a_file_url(client_and_service):
    client, service = client_and_service
    resp = client.post(
        "/api/llm/profiles/test",
        json={"base_url": "file:///etc/passwd", "model_id": "m"},
    )

    assert resp.status_code == 400  # noqa: PLR2004
    assert service.calls == []


def test_the_test_endpoint_still_reaches_a_local_provider(client_and_service):
    """Ollama in locale deve continuare a funzionare."""
    client, service = client_and_service
    resp = client.post(
        "/api/llm/profiles/test",
        json={"base_url": "http://127.0.0.1:11434/v1", "model_id": "llama3"},
    )

    assert resp.status_code == 200  # noqa: PLR2004
    assert service.calls[0]["base_url"] == "http://127.0.0.1:11434/v1"


def test_saving_a_profile_refuses_a_metadata_url(client_and_service):
    """Un profilo salvato viene poi usato dal workflow di decifratura: stessa guardia."""
    client, _service = client_and_service
    resp = client.post(
        "/api/llm/settings/profiles",
        json={
            "name": "evil",
            "base_url": "http://169.254.169.254/latest/meta-data",
            "model_id": "m",
        },
    )

    assert resp.status_code == 400  # noqa: PLR2004
    assert "link-local" in resp.json()["error"]

"""Browsers cannot drive the API on behalf of another site (S-17).

Also: the first-run password endpoint, and non-ASCII passwords.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from core import server as server_mod
from core.security import _password_matches

FORBIDDEN, UNAUTHORIZED, OK = 403, 401, 200


@pytest.fixture
def api(monkeypatch):
    security = server_mod.config_manager.config.security
    monkeypatch.setattr(security, "auth_enabled", False)
    monkeypatch.setattr(security, "password", "")
    store = MagicMock(return_value=True)
    monkeypatch.setattr(server_mod.config_manager, "set_security_password", store)
    transport = httpx.ASGITransport(app=server_mod.app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return client, store


async def _set_password(client: httpx.AsyncClient, headers: dict, password: str = "s3cret-pass"):
    return await client.post(
        "/api/auth/password",
        content=f'{{"password": "{password}"}}',
        headers={"content-type": "text/plain", **headers},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "https://evil.example"},
        {"origin": "null"},
        {"sec-fetch-site": "cross-site"},
        {"origin": "https://test.evil.example"},
    ],
)
async def test_cross_site_posts_are_refused(api, headers):
    client, store = api
    async with client:
        response = await _set_password(client, headers)
    assert response.status_code == FORBIDDEN
    assert response.json() == {"error": "Cross-site request refused"}
    store.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {},  # scripts and curl send no Origin
        {"origin": "http://test"},  # the dashboard itself
        {"origin": "http://test:8443"},  # behind a proxy that changes the port
        {"origin": "http://localhost:8911"},  # a configured trusted origin
        {"origin": "https://evil.example", "x-forwarded-host": "evil.example, test"},
        {"sec-fetch-site": "same-origin"},
    ],
)
async def test_same_site_and_non_browser_posts_pass(api, headers):
    client, store = api
    async with client:
        response = await _set_password(client, headers)
    assert response.status_code == OK, response.text
    store.assert_called_once()


@pytest.mark.asyncio
async def test_safe_methods_are_not_checked(api):
    client, _store = api
    async with client:
        response = await client.get("/api/devices", headers={"origin": "https://evil.example"})
    assert response.status_code != FORBIDDEN


@pytest.mark.asyncio
async def test_existing_password_is_not_replaced_even_with_auth_off(api, monkeypatch):
    client, store = api
    monkeypatch.setattr(server_mod.config_manager.config.security, "password", "already-set")
    async with client:
        response = await _set_password(client, {})
    assert response.status_code == FORBIDDEN
    store.assert_not_called()


@pytest.mark.asyncio
async def test_a_password_a_browser_cannot_send_is_rejected(api):
    client, store = api
    async with client:
        response = await _set_password(client, {}, password="passwort-€€€")
    assert response.status_code == 400  # noqa: PLR2004
    store.assert_not_called()


def test_password_header_matches_utf8_and_latin1_bytes():
    password = "pässwort-123"
    as_utf8 = password.encode("utf-8").decode("latin-1")  # what Starlette hands over
    assert _password_matches(as_utf8, password)
    assert _password_matches(password, password)  # a browser sends Latin-1
    assert not _password_matches("passwort-123", password)


@pytest.mark.asyncio
async def test_non_ascii_password_authenticates(monkeypatch):
    security = server_mod.config_manager.config.security
    monkeypatch.setattr(security, "auth_enabled", True)
    monkeypatch.setattr(security, "password", "pässwort-123")
    transport = httpx.ASGITransport(app=server_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        good = await client.get("/api/devices", headers={"x-api-key": "pässwort-123".encode()})
        bad = await client.get("/api/devices", headers={"x-api-key": b"nope"})
    assert good.status_code != UNAUTHORIZED
    assert bad.status_code == UNAUTHORIZED

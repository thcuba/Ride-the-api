"""A forward replays the request target the device sent (C-16)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from starlette.requests import Request

from adapters.base import CommandResult, InterceptedRequest, ProtocolType
from core import server as server_mod
from core.cloud_forward import _request_target
from core.database import DatabaseManager
from core.modification import ModificationAction, ModificationRule, get_modification_engine


def _intercepted(**fields) -> InterceptedRequest:
    return InterceptedRequest(
        device_id="dev", timestamp=datetime.now(UTC), protocol=ProtocolType.HTTP, **fields
    )


def test_raw_target_is_replayed_verbatim():
    request = _intercepted(path="/a?b", raw_target="/a%3Fb?x=1&x=2")
    assert _request_target(request) == "/a%3Fb?x=1&x=2"


def test_decoded_paths_are_encoded_where_they_must_be():
    request = _intercepted(path="/files/a b/ü", query_params={"q": "x y"})
    assert _request_target(request) == "/files/a%20b/%C3%BC?q=x+y"


def test_targets_that_carry_their_query_are_unchanged():
    """The TLS path keeps the query in ``path`` (h11 target)."""
    assert _request_target(_intercepted(path="/poll?ts=1&a=%2F")) == "/poll?ts=1&a=%2F"


def test_raw_target_from_the_asgi_scope():
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/acme/api/a b?c",
        "raw_path": b"/acme/api/a%20b%3Fc",
        "query_string": b"x=1&x=2",
        "headers": [],
    }
    assert server_mod._raw_target(Request(scope)) == "/api/a%20b%3Fc?x=1&x=2"


class _Adapter:
    def __init__(self) -> None:
        self.targets: list[str] = []

    async def parse_request(self, intercepted):
        intercepted.device_id = "dev-1"
        return intercepted

    async def forward_to_cloud(self, intercepted):
        self.targets.append(_request_target(intercepted))
        return CommandResult(
            success=True, response={"ok": True}, status_code=200, body_bytes=b'{"ok": true}'
        )


@pytest_asyncio.fixture
async def proxy(tmp_path, monkeypatch):
    dm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "devices",
    )
    await dm.initialize()
    adapter = _Adapter()
    registry = MagicMock()
    registry.get_adapter.return_value = adapter
    orch = MagicMock()
    orch.handle_request = AsyncMock(return_value={"action": "forward"})
    orch.handle_response = AsyncMock()
    monkeypatch.setattr(server_mod.config_manager.config.security, "auth_enabled", False)
    monkeypatch.setattr(server_mod.config_manager.config.learning, "signal_forward_to_cloud", False)
    monkeypatch.setattr(server_mod, "db_manager", dm)
    monkeypatch.setattr(server_mod, "orchestrator", orch)
    monkeypatch.setattr(server_mod, "adapter_registry", registry)
    monkeypatch.setattr(server_mod, "get_traffic_selector", lambda: None)
    transport = httpx.ASGITransport(app=server_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, adapter
    await dm.close()


@pytest.mark.asyncio
async def test_forward_keeps_encoding_and_repeated_keys(proxy):
    client, adapter = proxy
    response = await client.get("/acme/api/a%20b%3Fc?x=1&x=2")
    assert response.status_code == 200  # noqa: PLR2004
    assert adapter.targets == ["/api/a%20b%3Fc?x=1&x=2"]


@pytest.mark.asyncio
async def test_a_redirect_rule_wins_over_the_raw_target(proxy, monkeypatch):
    client, adapter = proxy
    monkeypatch.setattr(
        get_modification_engine(),
        "_rules",
        [
            ModificationRule(
                name="move",
                action=ModificationAction.REDIRECT,
                action_params={"path": "/v2/status"},
            )
        ],
    )
    await client.get("/acme/v1/status?x=1")
    assert adapter.targets == ["/v2/status?x=1"]

"""Modification rules act on the right data and are enforced (C-11)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio

from adapters.base import InterceptedRequest, ProtocolType
from core import server as server_mod
from core.config import ModificationRule as ConfigRule
from core.database import DatabaseManager
from core.modification import (
    BLOCKED_STATUS,
    InterceptedMessage,
    ModificationAction,
    ModificationEngine,
    ModificationRule,
    get_modification_engine,
)


class _Config:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class _ConfigManager:
    def __init__(self, rules: list) -> None:
        self.config = _Config(
            modification=_Config(enabled=True, rules=rules, audit_log_enabled=False)
        )

    def register_callback(self, _callback) -> None:
        pass


def _engine(*rules: dict) -> ModificationEngine:
    return ModificationEngine(config_manager=_ConfigManager([ConfigRule(**r) for r in rules]))


def _request(path: str = "/v1/command", body: dict | None = None, host: str = "api.example.com"):
    return InterceptedRequest(
        device_id="dev-1",
        timestamp=datetime.now(UTC),
        protocol=ProtocolType.HTTP,
        method="POST",
        path=path,
        headers={"host": host, "content-type": "application/json"},
        body=body if body is not None else {"temp": 25},
    )


LOCAL_RESPONSE = {
    "status_code": 200,
    "headers": {"content-type": "application/json"},
    "body": {"temp": 21, "secret": "x"},
}


# --- response rules see the body, not the wrapper ---------------------------


def test_a_loaded_rule_does_not_pollute_untouched_responses():
    engine = _engine({"name": "other", "match_vendor": "other", "direction": "response"})
    out, modified = engine.process_response(_request(), dict(LOCAL_RESPONSE))
    assert modified is False
    assert out["body"] == {"temp": 21, "secret": "x"}


def test_response_rules_change_the_body_and_keep_the_wrapper():
    engine = _engine(
        {
            "name": "warmer",
            "match_field_path": "temp",
            "direction": "response",
            "action": "modify",
            "action_params": {"operation": "set", "value": 23},
        },
        {
            "name": "no-secret",
            "match_field_path": "secret",
            "direction": "response",
            "action": "modify",
            "action_params": {"operation": "remove"},
        },
    )
    out, modified = engine.process_response(_request(), dict(LOCAL_RESPONSE))
    assert modified is True
    assert out["status_code"] == 200  # noqa: PLR2004
    assert out["headers"] == {"content-type": "application/json"}
    assert out["body"] == {"temp": 23}


def test_a_blocked_response_is_a_403():
    engine = _engine({"name": "deny", "direction": "response", "action": "block"})
    out, modified = engine.process_response(_request(), dict(LOCAL_RESPONSE))
    assert modified is True
    assert out["status_code"] == BLOCKED_STATUS
    assert out["body"]["error"] == "blocked"


def test_remove_deletes_the_field():
    rule = ModificationRule(
        name="rm",
        match_field_path="a.b",
        action=ModificationAction.MODIFY,
        action_params={"operation": "remove"},
    )
    msg = rule.apply(InterceptedMessage.from_request(_request(body={"a": {"b": 1, "c": 2}})))
    assert msg.body == {"a": {"c": 2}}  # deleted, not set to null


# --- legacy rules -------------------------------------------------------------


def test_documented_legacy_path_block_only_blocks_that_path():
    engine = _engine(
        {
            "name": "Block dangerous command",
            "match_type": "path",
            "match_value": "/cmd/reboot",
            "action": "block",
        }
    )
    blocked, _ = engine.process_request(_request(path="/cmd/reboot?now=1"))
    allowed, _ = engine.process_request(_request(path="/api/status"))
    rebooted, _ = engine.process_request(_request(path="/cmd/rebooted"))
    assert getattr(blocked, "blocked", False) is True
    assert getattr(allowed, "blocked", False) is False
    assert getattr(rebooted, "blocked", False) is False


def test_legacy_modify_sets_its_target_field():
    engine = _engine(
        {
            "name": "Override temperature",
            "match_type": "hostname",
            "match_value": "thermostat.local",
            "action": "modify",
            "target_field": "target_temp",
            "target_value": "22.0",
        }
    )
    out, modified = engine.process_request(_request(host="thermostat.local"))
    assert modified is True
    assert out.body == {"temp": 25, "target_temp": "22.0"}


def test_untranslatable_legacy_rule_is_disabled_not_global():
    engine = _engine(
        {"name": "payload", "match_type": "payload", "match_value": "x", "action": "block"}
    )
    assert engine.get_rules() == []
    out, _ = engine.process_request(_request())
    assert getattr(out, "blocked", False) is False


def test_native_field_value_rule_gets_no_host_requirement():
    """``match_value`` with ``match_field_path`` is native, not a legacy host."""
    engine = _engine(
        {"name": "no-reboot", "match_field_path": "cmd", "match_value": "reboot", "action": "block"}
    )
    out, _ = engine.process_request(_request(body={"cmd": "reboot"}))
    assert getattr(out, "blocked", False) is True


# --- the proxy route enforces BLOCK and DELAY ---------------------------------


class _Adapter:
    async def parse_request(self, intercepted):
        intercepted.device_id = "dev-1"
        return intercepted


class _Registry:
    def get_adapter(self, _vendor):
        return _Adapter()

    def list_vendors(self):
        return ["acme"]


@pytest_asyncio.fixture
async def proxy(tmp_path, monkeypatch):
    dm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "devices",
    )
    await dm.initialize()
    orch = MagicMock()
    orch.handle_request = AsyncMock(
        return_value={"action": "local_response", "response": dict(LOCAL_RESPONSE)}
    )
    monkeypatch.setattr(server_mod.config_manager.config.security, "auth_enabled", False)
    monkeypatch.setattr(server_mod, "db_manager", dm)
    monkeypatch.setattr(server_mod, "orchestrator", orch)
    monkeypatch.setattr(server_mod, "adapter_registry", _Registry())
    monkeypatch.setattr(server_mod, "get_traffic_selector", lambda: None)
    transport = httpx.ASGITransport(app=server_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, orch
    await dm.close()


@pytest.mark.asyncio
async def test_block_rule_answers_403_without_reaching_the_pipeline(proxy, monkeypatch):
    client, orch = proxy
    monkeypatch.setattr(
        get_modification_engine(),
        "_rules",
        [
            ModificationRule(
                name="no-reboot", match_path_pattern="^/cmd/reboot", action=ModificationAction.BLOCK
            )
        ],
    )
    response = await client.post("/acme/cmd/reboot", json={})
    assert response.status_code == BLOCKED_STATUS
    assert response.json()["error"] == "blocked"
    orch.handle_request.assert_not_called()

    response = await client.post("/acme/api/status", json={})
    assert response.status_code == 200  # noqa: PLR2004
    assert response.json() == {"temp": 21, "secret": "x"}


@pytest.mark.asyncio
async def test_delay_rule_reaches_the_route(proxy, monkeypatch):
    client, _orch = proxy
    delay = AsyncMock()
    monkeypatch.setattr(server_mod, "_modification_delay", delay)
    monkeypatch.setattr(
        get_modification_engine(),
        "_rules",
        [
            ModificationRule(
                name="slow", action=ModificationAction.DELAY, action_params={"delay_ms": 250}
            )
        ],
    )
    response = await client.post("/acme/api/status", json={})
    assert response.status_code == 200  # noqa: PLR2004
    delay.assert_any_await(250)


@pytest.mark.asyncio
async def test_delays_are_capped(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(server_mod.asyncio, "sleep", fake_sleep)
    for value in (250, 10**9, "x", None, -5):
        await server_mod._modification_delay(value)
    assert slept == [0.25, 30.0]

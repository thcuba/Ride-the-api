"""The engine serves what the device DB holds, plus file-supplied seeds (C-8).

The engine serves its in-memory pattern set exclusively when it has one, so
every change to the device DB has to reach it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete

from core import server as server_mod
from core.database import DatabaseManager, RequestPattern
from core.pattern_db.decipher_ingest import DecipherIngest
from core.pattern_db.schemas import (
    ClientConfig,
    ClientEndpoint,
    PatternDB,
    PatternMeta,
    ServerConfig,
    ServerResponse,
    StateVariable,
)
from core.pipeline import LearningOrchestrator

POWER = 5


def _pattern_db(endpoints: dict[str, str], state: list[StateVariable] | None = None) -> PatternDB:
    """A pattern set with one GET endpoint (and response) per ``{id: path}``."""
    return PatternDB(
        meta=PatternMeta(pattern_id="meter-patterns", vendor="acme", device_type="meter"),
        client=ClientConfig(
            endpoints=[
                ClientEndpoint(id=ep_id, intent=ep_id, method="GET", path=path)
                for ep_id, path in endpoints.items()
            ]
        ),
        server=ServerConfig(
            responses=[
                ServerResponse(id=f"r-{ep_id}", triggers=[ep_id], body_template={"ep": ep_id})
                for ep_id in endpoints
            ],
            state_variables=state or [],
        ),
    )


async def _make_dm(directory) -> DatabaseManager:
    dm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{directory / 'core.db'}",
        device_db_dir=directory / "devices",
    )
    await dm.initialize()
    await dm.get_or_create_device("meter", "acme", "meter")
    return dm


@pytest_asyncio.fixture
async def dm(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the auto-loader scans ./patterns and ./data
    manager = await _make_dm(tmp_path / "state")
    yield manager
    await manager.close()


def _orchestrator(dm: DatabaseManager) -> LearningOrchestrator:
    llm = MagicMock()
    llm.resolve_task_profile = MagicMock(return_value=None)
    orch = LearningOrchestrator(dm)
    orch.initialize(llm)
    return orch


def _served(orch: LearningOrchestrator) -> set[str]:
    cached = orch.engine.cached_patterns("meter")
    return {ep.path for ep in cached.client.endpoints} if cached else set()


@pytest_asyncio.fixture
async def api(dm, monkeypatch):
    orch = _orchestrator(dm)
    monkeypatch.setattr(server_mod.config_manager.config.security, "auth_enabled", False)
    monkeypatch.setattr(server_mod, "db_manager", dm)
    monkeypatch.setattr(server_mod, "orchestrator", orch)
    transport = httpx.ASGITransport(app=server_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, orch


@pytest.mark.asyncio
async def test_api_delete_stops_serving_the_pattern(api, dm):
    client, orch = api
    await DecipherIngest(dm).import_patterns("meter", _pattern_db({"a": "/api/a", "b": "/api/b"}))
    await orch.sync_engine("meter")
    assert _served(orch) == {"/api/a", "/api/b"}

    response = await client.delete("/api/devices/meter/patterns/a")
    assert response.status_code == 200  # noqa: PLR2004
    assert _served(orch) == {"/api/b"}

    response = await client.delete("/api/devices/meter/patterns/b")
    assert response.status_code == 200  # noqa: PLR2004
    assert orch.engine.cached_patterns("meter") is None, "nothing left to serve"


@pytest.mark.asyncio
async def test_api_import_is_served_immediately(api):
    client, orch = api
    payload = _pattern_db({"a": "/api/a"}).model_dump(by_alias=True, exclude_none=True)
    response = await client.post("/api/devices/meter/patterns/import", json=payload)
    assert response.status_code == 200, response.text  # noqa: PLR2004
    assert _served(orch) == {"/api/a"}


@pytest.mark.asyncio
async def test_learning_keeps_file_supplied_patterns(dm, tmp_path):
    seed = _pattern_db(
        {"seed": "/api/seed"}, state=[StateVariable(name="power", type="int", default=POWER)]
    )
    (tmp_path / "patterns").mkdir()
    (tmp_path / "patterns" / "meter.ride-pattern.json").write_text(
        seed.model_dump_json(by_alias=True)
    )
    orch = _orchestrator(dm)
    assert _served(orch) == {"/api/seed"}

    result = await orch.handle_request("meter", "acme", "https", "GET", "/api/x", {}, None, {})
    await orch.handle_response(
        "meter", "acme", "https", 200, {}, {}, correlation_key=result["correlation_key"]
    )
    orch.pipeline._analyze_with_llm = AsyncMock(
        return_value={
            "patterns": [
                {
                    "intent": "learned",
                    "method": "GET",
                    "path": "/api/learned",
                    "response": {"status_code": 200, "body": {"ok": True}},
                }
            ]
        }
    )
    assert (await orch.flush_and_learn("meter"))["success"] is True

    assert _served(orch) == {"/api/seed", "/api/learned"}
    assert orch.engine.get_state_store("meter").get("power") == POWER


@pytest.mark.asyncio
async def test_restart_reloads_the_export_without_resurrecting_deletes(dm):
    await dm.write_device_meta(
        "meter",
        {
            "protocols": ["http"],
            "connection_mode": "http",
            "state_variables": [{"name": "power", "type": "int", "default": POWER}],
        },
    )
    await DecipherIngest(dm).import_patterns("meter", _pattern_db({"a": "/api/a", "b": "/api/b"}))
    await _orchestrator(dm).sync_engine("meter")  # writes the device's export file

    restarted = _orchestrator(dm)
    assert _served(restarted) == {"/api/a", "/api/b"}, "the export dir is loaded at startup"
    assert restarted.engine.get_state_store("meter").get("power") == POWER

    async with dm.device_session("meter") as session:
        await session.execute(delete(RequestPattern).where(RequestPattern.pattern_id == "a"))
        await session.commit()
    await restarted.sync_engine("meter")
    assert _served(restarted) == {"/api/b"}, "a DB snapshot is not a seed"

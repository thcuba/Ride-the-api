"""The /api/independence routes answer as documented."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from core import server as server_mod
from core.database import DatabaseManager, FieldMapping
from core.pattern_db.decipher_ingest import DecipherIngest
from core.pattern_db.schemas import (
    ClientConfig,
    ClientEndpoint,
    PatternDB,
    PatternMeta,
    ServerConfig,
    ServerResponse,
)
from core.pattern_db.schemas import FieldMapping as SchemaFieldMapping
from core.pipeline import LearningOrchestrator
from core.resilience import CloudIndependenceVerifier

OK = 200
FORMULA = "{request.body.t} * 2"


def _pattern_db() -> PatternDB:
    return PatternDB(
        meta=PatternMeta(pattern_id="p", vendor="acme", device_type="meter"),
        client=ClientConfig(
            endpoints=[ClientEndpoint(id="status", intent="status", method="GET", path="/status")]
        ),
        server=ServerConfig(
            responses=[
                ServerResponse(
                    id="r",
                    triggers=["status"],
                    body_template={"on": True},
                    field_mappings=[
                        SchemaFieldMapping(
                            source="body.t",
                            target="result.t2",
                            transform="formula",
                            formula=FORMULA,
                            mapping={"a": 1},
                        )
                    ],
                )
            ]
        ),
    )


@pytest_asyncio.fixture
async def dm(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "devices",
    )
    await manager.initialize()
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def api(dm, monkeypatch):
    llm = MagicMock()
    llm.resolve_task_profile = MagicMock(return_value=None)
    orch = LearningOrchestrator(dm)
    orch.initialize(llm)
    monkeypatch.setattr(server_mod.config_manager.config.security, "auth_enabled", False)
    monkeypatch.setattr(server_mod, "db_manager", dm)
    monkeypatch.setattr(server_mod, "orchestrator", orch)
    transport = httpx.ASGITransport(app=server_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, orch


@pytest.mark.asyncio
async def test_export_then_import_into_another_device(api, dm):
    client, orch = api
    await DecipherIngest(dm).import_patterns("meter-a", _pattern_db())

    exported = await client.get("/api/independence/meter-a/export")
    assert exported.status_code == OK
    response = await client.post("/api/independence/meter-b/import", json=exported.json())
    assert response.status_code == OK, response.text
    assert response.json() == {"device_id": "meter-b", "patterns_imported": 1}

    async with dm.device_session("meter-b") as session:
        mapping = (await session.execute(select(FieldMapping))).scalar_one()
    assert (mapping.formula, mapping.enum_values) == (FORMULA, {"a": 1})
    served = orch.engine.cached_patterns("meter-b")
    assert served is not None
    assert [ep.path for ep in served.client.endpoints] == ["/status"]


@pytest.mark.asyncio
async def test_bad_import_bodies_are_400s(api):
    client, _orch = api
    not_json = await client.post(
        "/api/independence/meter/import",
        content=b"nope",
        headers={"content-type": "application/json"},
    )
    a_list = await client.post("/api/independence/meter/import", json=[1, 2])
    missing_columns = await client.post(
        "/api/independence/meter/import", json={"patterns": [{"pattern_id": "p"}]}
    )
    assert [not_json.status_code, a_list.status_code, missing_columns.status_code] == [400] * 3


@pytest.mark.asyncio
async def test_import_ignores_unknown_keys(dm):
    payload = {
        "patterns": [
            {
                "pattern_id": "p",
                "method": "GET",
                "path_pattern": "/x",
                "protocol": "http",
                "intent": "x",
                "from_a_newer_version": True,
                "created_at": "yesterday",
            },
            "not a row",
        ],
        "templates": ["junk"],
    }
    assert await CloudIndependenceVerifier(dm).import_device_patterns("meter", payload) == 1


@pytest.mark.asyncio
async def test_unready_service_is_a_real_503(api, monkeypatch):
    client, _orch = api
    monkeypatch.setattr(server_mod, "db_manager", None)
    response = await client.get("/api/independence/meter")
    assert response.status_code == 503  # noqa: PLR2004
    assert response.json() == {"error": "Service not ready"}


@pytest.mark.asyncio
async def test_trailing_slash_lists_devices(api, dm):
    client, _orch = api
    await dm.get_or_create_device("meter", "acme", "meter")
    response = await client.get("/api/independence/")
    assert response.status_code == OK
    assert "devices" in response.json()

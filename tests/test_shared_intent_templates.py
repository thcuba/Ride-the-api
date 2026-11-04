"""Endpoints that share an intent must each serve their own response (C-7)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from core.database import DatabaseManager, ResponseTemplate
from core.pattern_db.decipher_ingest import DecipherIngest
from core.pattern_db.pattern_engine import PatternEngine
from core.pattern_db.schemas import (
    ClientConfig,
    ClientEndpoint,
    PatternDB,
    PatternMeta,
    ServerConfig,
    ServerResponse,
)


def _two_endpoints_one_intent(*, id_triggers: bool = True) -> PatternDB:
    def triggers(endpoint_id):
        return [endpoint_id, "unknown"] if id_triggers else ["unknown"]

    return PatternDB(
        meta=PatternMeta(pattern_id="p", vendor="acme", device_type="meter"),
        client=ClientConfig(
            endpoints=[
                ClientEndpoint(id="ep-fw", intent="unknown", method="GET", path="/api/v1/firmware"),
                ClientEndpoint(id="ep-kwh", intent="unknown", method="GET", path="/api/v1/energy"),
            ]
        ),
        server=ServerConfig(
            responses=[
                ServerResponse(
                    id="r-fw", triggers=triggers("ep-fw"), body_template={"version": "1.4.2"}
                ),
                ServerResponse(
                    id="r-kwh", triggers=triggers("ep-kwh"), body_template={"kwh": 12.5}
                ),
            ]
        ),
    )


@pytest.mark.asyncio
async def test_cached_matcher_serves_each_endpoint_its_own_response():
    engine = PatternEngine(MagicMock())
    engine.apply_pattern_db("meter", _two_endpoints_one_intent())

    _p, fw, _s = await engine.find_best_match("meter", "GET", "/api/v1/firmware", {}, None, {})
    _p, kwh, _s = await engine.find_best_match("meter", "GET", "/api/v1/energy", {}, None, {})
    assert fw.body_template == {"version": "1.4.2"}
    assert kwh.body_template == {"kwh": 12.5}


@pytest.mark.asyncio
async def test_intent_only_triggers_still_resolve():
    """Hand-written pattern files with intent triggers keep working."""
    engine = PatternEngine(MagicMock())
    engine.apply_pattern_db("meter", _two_endpoints_one_intent(id_triggers=False))
    _p, template, _s = await engine.find_best_match(
        "meter", "GET", "/api/v1/firmware", {}, None, {}
    )
    assert template is not None


@pytest_asyncio.fixture
async def db_manager(tmp_path):
    dm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "device_dbs",
    )
    await dm.initialize()
    yield dm
    await dm.close()


@pytest.mark.asyncio
async def test_import_gives_each_endpoint_its_own_template(db_manager):
    await DecipherIngest(db_manager).import_patterns("meter", _two_endpoints_one_intent())
    async with db_manager.device_session("meter") as session:
        rows = (await session.execute(select(ResponseTemplate))).scalars().all()
    bodies = {row.pattern_id: row.body_template for row in rows}
    assert bodies == {"ep-fw": {"version": "1.4.2"}, "ep-kwh": {"kwh": 12.5}}


@pytest.mark.asyncio
async def test_v2_export_keeps_each_endpoint_its_own_response(db_manager):
    ingest = DecipherIngest(db_manager)
    await ingest.import_patterns("meter", _two_endpoints_one_intent())
    model = await ingest.export_device_model("meter", "acme", "meter")

    engine = PatternEngine(MagicMock())
    engine.apply_pattern_db("meter", model.to_pattern_db())
    _p, fw, _s = await engine.find_best_match("meter", "GET", "/api/v1/firmware", {}, None, {})
    _p, kwh, _s = await engine.find_best_match("meter", "GET", "/api/v1/energy", {}, None, {})
    assert fw.body_template == {"version": "1.4.2"}
    assert kwh.body_template == {"kwh": 12.5}

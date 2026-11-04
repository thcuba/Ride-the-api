"""Pattern files can be imported again, and formulas survive the SQL round trip."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from core.database import DatabaseManager, FieldMapping, RequestPattern, ResponseTemplate
from core.migrations import SchemaMigrator
from core.pattern_db.decipher_ingest import DecipherIngest
from core.pattern_db.schemas import (
    ClientConfig,
    ClientEndpoint,
    Command,
    DeviceModel,
    PatternDB,
    PatternMeta,
    ProtocolInfo,
    ServerConfig,
    ServerResponse,
)
from core.pattern_db.schemas import FieldMapping as SchemaFieldMapping

FORMULA = "{request.body.t} * 1.8 + 32"


def _formula_mapping() -> SchemaFieldMapping:
    return SchemaFieldMapping(
        source="body.t", target="result.f", transform="formula", formula=FORMULA
    )


def _pattern_db(body: dict) -> PatternDB:
    return PatternDB(
        meta=PatternMeta(pattern_id="p", vendor="acme", device_type="meter"),
        client=ClientConfig(
            endpoints=[
                ClientEndpoint(id="ep-temp", intent="get_temp", method="POST", path="/api/temp")
            ]
        ),
        server=ServerConfig(
            responses=[
                ServerResponse(
                    id="r-temp",
                    triggers=["ep-temp"],
                    body_template=body,
                    field_mappings=[_formula_mapping()],
                )
            ]
        ),
    )


@pytest_asyncio.fixture
async def db_manager(tmp_path):
    dm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "device_dbs",
    )
    await dm.initialize()
    yield dm
    await dm.close()


async def _all(dm: DatabaseManager, model: type) -> list:
    async with dm.device_session("meter") as session:
        return list((await session.execute(select(model))).scalars().all())


@pytest.mark.asyncio
async def test_importing_a_pattern_file_again_updates_it_in_place(db_manager):
    ingest = DecipherIngest(db_manager)
    await ingest.import_patterns("meter", _pattern_db({"f": 0}))
    assert await ingest.import_patterns("meter", _pattern_db({"f": 1, "unit": "F"})) == 1

    assert [p.pattern_id for p in await _all(db_manager, RequestPattern)] == ["ep-temp"]
    templates = await _all(db_manager, ResponseTemplate)
    assert [t.body_template for t in templates] == [{"f": 1, "unit": "F"}]
    assert len(await _all(db_manager, FieldMapping)) == 1


@pytest.mark.asyncio
async def test_a_device_reimports_its_own_export(db_manager):
    ingest = DecipherIngest(db_manager)
    await ingest.import_patterns("meter", _pattern_db({"f": 0}))
    exported = await ingest.export_patterns("meter", "acme", "meter")
    assert await ingest.import_patterns("meter", exported) == 1


@pytest.mark.asyncio
async def test_formula_survives_the_sql_round_trip(db_manager):
    ingest = DecipherIngest(db_manager)
    await ingest.import_patterns("meter", _pattern_db({"f": 0}))

    v1 = await ingest.export_patterns("meter", "acme", "meter")
    assert [m.formula for r in v1.server.responses for m in r.field_mappings] == [FORMULA]
    v2 = await ingest.export_device_model("meter", "acme", "meter")
    assert [m.formula for m in v2.interactions] == [FORMULA]


@pytest.mark.asyncio
async def test_a_learned_formula_is_kept(db_manager):
    delta = DeviceModel(
        meta=PatternMeta(pattern_id="p", vendor="acme", device_type="meter"),
        protocol=ProtocolInfo(protocol="http", handler="http"),
        commands=[
            Command(id="c1", kind="get_temp", protocol="http", method="POST", path="/api/temp")
        ],
        responses=[
            ServerResponse(
                id="r1",
                triggers=["get_temp"],
                body_template={"f": 0},
                field_mappings=[_formula_mapping()],
            )
        ],
    )
    await DecipherIngest(db_manager).merge_device_model("meter", delta)
    assert [m.formula for m in await _all(db_manager, FieldMapping)] == [FORMULA]


@pytest.mark.asyncio
async def test_existing_databases_gain_the_formula_column(tmp_path):
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE field_mappings (
                id INTEGER PRIMARY KEY, mapping_id VARCHAR(64) UNIQUE NOT NULL,
                request_field VARCHAR(128) NOT NULL, request_type VARCHAR(32) NOT NULL,
                response_field VARCHAR(128) NOT NULL, response_type VARCHAR(32) NOT NULL,
                transform VARCHAR(64), enum_values JSON, intent VARCHAR(64) NOT NULL,
                confidence FLOAT NOT NULL, created_at DATETIME
            );
            CREATE TABLE _schema_migrations (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, applied_at TEXT NOT NULL
            );
            INSERT INTO _schema_migrations VALUES (1, 'baseline', '2026-01-01T00:00:00');
            """
        )

    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        assert await SchemaMigrator(engine).run() == ["field_mapping_formula"]
        assert await SchemaMigrator(engine).run() == []  # idempotent
    finally:
        await engine.dispose()

    with closing(sqlite3.connect(path)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(field_mappings)")}
    assert "formula" in columns

"""
Tests for the Decipher Ingest (LLM output → pattern DB records).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy import select

from core.database import DatabaseManager, MatchStats, RequestPattern
from core.pattern_db.decipher_ingest import DecipherIngest
from core.pattern_db.pattern_engine import PatternEngine
from core.pattern_db.schemas import (
    Command,
    DeviceModel,
    Observation,
    ObservationKind,
    PatternMeta,
    ProtocolInfo,
    ServerResponse,
    StateVariable,
    VirtualSensor,
)
from core.pattern_db.state_manager import DeviceStateStore


@pytest_asyncio.fixture
async def db_manager(tmp_path):
    core_db_path = tmp_path / "core.db"
    dm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{core_db_path}",
        device_db_dir=tmp_path / "device_dbs",
    )
    await dm.initialize()
    yield dm
    await dm.close()


SAMPLE_OUTPUT = {
    "patterns": [
        {
            "pattern_id": "pat_repeat",
            "method": "POST",
            "path_pattern": "/rpc/Status",
            "protocol": "http",
            "intent": "status",
            "field_mappings": [],
        }
    ]
}


async def _count_patterns(db_manager: DatabaseManager, device_id: str) -> int:
    async with db_manager.device_session(device_id) as session:
        result = await session.execute(select(RequestPattern))
        return len(result.scalars().all())


async def test_ingest_is_idempotent(db_manager: DatabaseManager):
    """Re-ingesting the same LLM output must not raise or duplicate patterns."""
    ingester = DecipherIngest(db_manager)
    device_id = "device-idem"

    first = await ingester.ingest(device_id, SAMPLE_OUTPUT)
    assert first == 1
    assert await _count_patterns(db_manager, device_id) == 1

    # Second identical import must be a no-op (skip the existing pattern),
    # not raise IntegrityError and wipe the whole batch.
    second = await ingester.ingest(device_id, SAMPLE_OUTPUT)
    assert second == 0
    assert await _count_patterns(db_manager, device_id) == 1


async def test_ingest_creates_match_stats_row_on_first_import(db_manager: DatabaseManager):
    """The first ingest must materialize a MatchStats row (not skip it)."""
    ingester = DecipherIngest(db_manager)
    device_id = "device-stats"

    await ingester.ingest(device_id, SAMPLE_OUTPUT)

    async with db_manager.device_session(device_id) as session:
        result = await session.execute(select(MatchStats).where(MatchStats.device_id == device_id))
        stats = result.scalar_one_or_none()
        assert stats is not None
        assert (stats.patterns_learned or 0) == 1
        assert (stats.templates_created or 0) == 1

# ???????????????????????????????????????????????????????????????????????????????
# v2 DeviceModel round-trip (A3): export/import must not drop state, virtual
# sensors, protocol or observation_history.
# ???????????????????????????????????????????????????????????????????????????????


async def test_export_device_model_carries_state_and_protocol(db_manager):
    """export_device_model must include state_variables/virtual_sensors from the
    in-memory applied PatternDB and ProtocolInfo from DeviceMeta."""
    ingester = DecipherIngest(db_manager)
    device_id = "device-v2export"

    await ingester.import_device_model(
        device_id,
        DeviceModel(
            meta=PatternMeta(pattern_id=f"{device_id}-patterns", vendor="Shelly",
                             device_type="plug"),
            protocol=ProtocolInfo(protocol="mqtt", handler="mqtt", identity="shelly-plug",
                                  confidence=0.95),
            commands=[Command(id="c1", kind="set_relay", protocol="mqtt",
                              topic="shellies/plug/relay")],
            responses=[ServerResponse(id="r1", triggers=["set_relay"], status_code=200,
                                      field_mappings=[])],
            state_variables=[StateVariable(name="relay", type="boolean", default=False)],
            virtual_sensors=[VirtualSensor(name="power", type="float")],
            observation_history=[
                Observation(id="o1", device_id=device_id, timestamp=datetime.now(UTC),
                            protocol="mqtt", kind=ObservationKind.PUBLISH,
                            content={"relay": True}),
            ],
        ),
    )

    # Build an applied PatternDB (state config lives in the engine memory).
    applied = await ingester.export_patterns(device_id, "Shelly", "plug")

    exported = await ingester.export_device_model(
        device_id,
        "Shelly",
        "plug",
        applied=applied,
        observations=[
            Observation(
                id="o1", device_id=device_id, timestamp=datetime.now(UTC),
                protocol="mqtt", kind=ObservationKind.PUBLISH, content={"relay": True},
            )
        ],
    )
    assert exported.schema_url == "https://ride-the-api.dev/pattern-schema/v2"
    assert exported.protocol.protocol == "mqtt"
    assert exported.protocol.identity == "shelly-plug"
    assert exported.state_variables[0].name == "relay"
    assert exported.virtual_sensors[0].name == "power"
    assert len(exported.observation_history) == 1


async def test_v2_pattern_file_loads_on_second_install(db_manager, tmp_path):
    """A v2 .ride-pattern.json must load through PatternEngine.load_pattern_file
    and be projected to v1 so a cloned device serves without LLM."""
    engine = PatternEngine(db_manager)
    device_id = "device-v2load"

    model = DeviceModel(
        meta=PatternMeta(pattern_id=f"{device_id}-patterns", vendor="Shelly",
                         device_type="plug"),
        protocol=ProtocolInfo(protocol="http", handler="http", identity="shelly-plug"),
        commands=[Command(id="c1", kind="status", protocol="http", method="GET",
                          path="/rpc/Status")],
        responses=[ServerResponse(id="r1", triggers=["status"], status_code=200,
                                  field_mappings=[])],
        state_variables=[StateVariable(name="relay", type="boolean", default=False)],
        virtual_sensors=[VirtualSensor(name="power", type="float")],
        observation_history=[
            Observation(id="o1", device_id=device_id, timestamp=datetime.now(UTC),
                        protocol="http", kind=ObservationKind.REQUEST,
                        content={"id": 1}),
        ],
    )
    filepath = str(tmp_path / "device.ride-pattern.json")
    engine.save_pattern_file(model, filepath)

    # Simulate a fresh install: a fresh engine loads the v2 file directly.
    engine2 = PatternEngine(db_manager)
    pdb = engine2.load_pattern_file(device_id, filepath)
    # Projected v1 keeps endpoints and state for the runtime.
    assert pdb.client.endpoints[0].intent == "status"
    assert pdb.server.state_variables[0].name == "relay"
    assert pdb.server.virtual_sensors[0].name == "power"
    # State store got initialized from the projected config.
    assert isinstance(engine2._state_stores.get(device_id), DeviceStateStore)
    assert engine2._state_stores[device_id].get("power") is not None


async def test_protocol_info_round_trips_full_fields(db_manager):
    """export->import->export must preserve all ProtocolInfo fields (B1).

    The first-flush mode="auto" identification (transport/security/
    proprietary/identity/ports/confidence) is persisted into DeviceMeta and
    must round-trip losslessly through the portable v2 DeviceModel.
    """
    ingester = DecipherIngest(db_manager)
    device_id = "device-protoinfo-roundtrip"

    model = DeviceModel(
        meta=PatternMeta(pattern_id=f"{device_id}-patterns", vendor="Shelly",
                         device_type="plug"),
        protocol=ProtocolInfo(
            protocol="mqtt",
            handler="mqtt",
            transport="tcp",
            security="tls",
            proprietary=False,
            identity="shelly-plug",
            ports=[8883],
            confidence=0.93,
        ),
        commands=[Command(id="c1", kind="set_relay", protocol="mqtt",
                          topic="shellies/plug/relay")],
        responses=[ServerResponse(id="r1", triggers=["set_relay"], status_code=200,
                                  field_mappings=[])],
    )
    await ingester.import_device_model(device_id, model)

    exported = await ingester.export_device_model(device_id, "Shelly", "plug")
    assert exported.protocol.protocol == "mqtt"
    assert exported.protocol.handler == "mqtt"
    assert exported.protocol.transport == "tcp"
    assert exported.protocol.security == "tls"
    assert exported.protocol.proprietary is False
    assert exported.protocol.identity == "shelly-plug"
    assert exported.protocol.ports == [8883]  # noqa: PLR2004
    assert exported.protocol.confidence == 0.93  # noqa: PLR2004

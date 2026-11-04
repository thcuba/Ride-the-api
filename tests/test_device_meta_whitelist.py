"""LLM output cannot rewrite a device's protocol header (S-14).

The header drives ingress (``connection_mode``) and is mostly written from LLM
answers whose input is the device's own traffic.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from core.database import DatabaseManager, DeviceMetaRow
from core.pattern_db.decipher_ingest import DecipherIngest
from core.pattern_db.schemas import (
    Command,
    DeviceMeta,
    DeviceModel,
    PatternMeta,
    ProtocolInfo,
)
from core.pattern_db.validator import ValidationError

MAX_TEXT = 200


@pytest_asyncio.fixture
async def db_manager(tmp_path):
    dm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "device_dbs",
    )
    await dm.initialize()
    yield dm
    await dm.close()


def _model(protocol: str, handler: str, *, identity: str = "") -> DeviceModel:
    return DeviceModel(
        meta=PatternMeta(pattern_id="p", vendor="acme", device_type="plug"),
        protocol=ProtocolInfo(protocol=protocol, handler=handler, identity=identity),
        commands=[
            Command(id="c1", kind="get_state", protocol=protocol, method="GET", path="/api/state")
        ],
    )


def test_header_model_keeps_only_whitelisted_values():
    meta = DeviceMeta(
        protocols=["MQTT", "x-evil", "mqtt"],
        connection_mode="modbus-totally-not-https",
        identity="x" * 5000,
    )
    assert meta.protocols == ["mqtt"]
    assert meta.connection_mode == "auto"
    assert len(meta.identity) == MAX_TEXT
    assert DeviceMeta(protocols="x-evil").protocols == [""]
    assert DeviceMeta(connection_mode=" Modbus ").connection_mode == "modbus"


@pytest.mark.asyncio
async def test_llm_delta_cannot_rewrite_an_established_ingress_decision(db_manager):
    await db_manager.get_or_create_device("dev", "acme", "plug")
    await db_manager.write_device_meta("dev", {"protocols": ["http"], "connection_mode": "http"})

    await DecipherIngest(db_manager).merge_device_model(
        "dev", _model("mqtt", "mqtt", identity="relabelled")
    )

    meta = await db_manager.read_device_meta("dev")
    assert meta["connection_mode"] == "http"
    assert meta["protocols"] == ["http"]
    assert meta["identity"] == "relabelled"  # descriptive fields still update
    assert await db_manager.resolve_device_protocol("dev") == "http"


@pytest.mark.asyncio
async def test_undecided_device_takes_a_valid_decision_from_a_delta(db_manager):
    await db_manager.get_or_create_device("dev", "acme", "plug")
    await DecipherIngest(db_manager).merge_device_model("dev", _model("mqtt", "mqtt"))
    meta = await db_manager.read_device_meta("dev")
    assert (meta["protocols"], meta["connection_mode"]) == (["mqtt"], "mqtt")


@pytest.mark.asyncio
async def test_unknown_tokens_never_reach_the_header(db_manager):
    await db_manager.get_or_create_device("dev", "acme", "plug")
    await DecipherIngest(db_manager).merge_device_model(
        "dev", _model("x-evil", "modbus-totally-not-https")
    )
    meta = await db_manager.read_device_meta("dev")
    assert meta["protocols"] == [""]
    assert meta["connection_mode"] == "auto"


@pytest.mark.asyncio
async def test_a_header_stored_before_the_whitelist_cannot_steer_ingress(db_manager):
    await db_manager.get_or_create_device("dev", "acme", "plug")
    async with db_manager.device_session("dev") as session:
        session.add(DeviceMetaRow(device_id="dev", meta={"connection_mode": "x-evil"}))
        await session.commit()
    assert await db_manager.resolve_device_protocol("dev", ingress_default="https") == "https"


@pytest.mark.asyncio
async def test_rejected_import_leaves_no_header(db_manager):
    with pytest.raises(ValidationError):
        await DecipherIngest(db_manager).import_device_model("dev", _model("x-evil", "mqtt"))
    assert await db_manager.read_device_meta("dev") is None


@pytest.mark.asyncio
async def test_protocols_the_system_emits_can_be_imported(db_manager):
    """A device identified as ``tls`` used to fail its own re-import."""
    count = await DecipherIngest(db_manager).import_device_model("dev", _model("tls", "tls"))
    assert count == 1
    assert (await db_manager.read_device_meta("dev"))["connection_mode"] == "tls"

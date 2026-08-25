"""Smoke test: device state mutated via patterns persists across a DB restart.

End-to-end check that a ``{state.xxx}``-mutating field mapping writes the
device state to the ``device_state`` table and that a fresh PatternEngine
(backed by a re-opened DB) restores that persisted state.
"""
import pytest
import pytest_asyncio

from core.database import DatabaseManager
from core.pattern_db.pattern_engine import PatternEngine
from core.pattern_db.schemas import (
    FieldMapping,
    PatternDB,
    PatternMeta,
    ServerConfig,
    ServerResponse,
    StateVariable,
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


def make_pattern_db(state_value: int) -> PatternDB:
    return PatternDB(
        meta=PatternMeta(
            name=f"smoke-state-{state_value}",
            pattern_id=f"smoke-{state_value}",
            vendor="smoke",
            device_type="test",
        ),
        server=ServerConfig(
            state_variables=[StateVariable(name="relay", default=0)],
            responses=[
                ServerResponse(
                    id="smoke-response",
                    triggers=["SET"],
                    body_template={"echo": "{state.relay}"},
                    field_mappings=[
                                            FieldMapping(
                                                source=f"constant.{state_value}",
                                                target="state.relay",
                                            ),
                                        ],
                )
            ],
        ),
    )


@pytest.mark.asyncio
async def test_device_state_persists_across_restart(db_manager):
    dev_id = "dev-1"
    pattern_db = make_pattern_db(7)

    # First "boot": apply pattern, build response (mutates state), persist.
    eng1 = PatternEngine(db_manager)
    eng1.apply_pattern_db(dev_id, pattern_db)
    resp = await eng1.build_local_response(dev_id, pattern_db.server.responses[0], {})
    assert resp["body"]["echo"] == "7"
    assert await eng1.persist_state(dev_id) is True

    # Second pass: fresh engine over the same DB must see the persisted value.
    eng2 = PatternEngine(db_manager)
    await eng2.load_state(dev_id)
    store = eng2.get_state_store(dev_id)
    assert str(store.get("relay")) == "7"
    # A response built from restored state must resolve even without re-applying
    # the pattern DB (load_state pulls persisted variables).
    resp2 = await eng2.build_local_response(dev_id, pattern_db.server.responses[0], {})
    assert resp2["body"]["echo"] == "7"
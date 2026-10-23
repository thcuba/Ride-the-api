import pytest
import pytest_asyncio
from sqlalchemy import select

from core.database import DatabaseManager, FieldMapping, RequestPattern, ResponseTemplate
from core.resilience import CloudIndependenceVerifier


@pytest_asyncio.fixture
async def db_manager(tmp_path):
    core_db_path = tmp_path / "core.db"
    dm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{core_db_path}",
        device_db_dir=tmp_path / "device_dbs",
    )
    await dm.initialize()
    await dm.get_or_create_device(
        device_id="device-import-test",
        vendor="shelly",
        device_type="plug",
        name="Import Test Plug",
    )
    yield dm
    await dm.close()


@pytest.mark.asyncio
async def test_import_device_patterns_batching(db_manager):
    device_id = "device-import-test"
    verifier = CloudIndependenceVerifier(db_manager)

    # First import batch
    data1 = {
        "patterns": [
            {
                "pattern_id": "p1",
                "method": "GET",
                "path_pattern": "/status",
                "protocol": "http",
                "intent": "get_status",
            },
            {
                "pattern_id": "p2",
                "method": "POST",
                "path_pattern": "/control",
                "protocol": "http",
                "intent": "control",
            },
        ],
        "templates": [
            {
                "template_id": "t1",
                "pattern_id": "p1",
                "status_code": 200,
            }
        ],
        "field_mappings": [
            {
                "mapping_id": "m1",
                "request_field": "state",
                "request_type": "str",
                "response_field": "ison",
                "response_type": "bool",
                "intent": "power",
            }
        ],
    }

    count1 = await verifier.import_device_patterns(device_id, data1)
    assert count1 == 2  # noqa: PLR2004

    # Second import batch containing a duplicate (p1) and a new item (p3)
    data2 = {
        "patterns": [
            {
                "pattern_id": "p1",
                "method": "GET",
                "path_pattern": "/status",
                "protocol": "http",
                "intent": "get_status",
            },
            {
                "pattern_id": "p3",
                "method": "GET",
                "path_pattern": "/info",
                "protocol": "http",
                "intent": "get_info",
            },
        ],
        "templates": [
            {
                "template_id": "t1",
                "pattern_id": "p1",
                "status_code": 200,
            },
            {
                "template_id": "t2",
                "pattern_id": "p3",
                "status_code": 200,
            },
        ],
        "field_mappings": [
            {
                "mapping_id": "m1",
                "request_field": "state",
                "request_type": "str",
                "response_field": "ison",
                "response_type": "bool",
                "intent": "power",
            },
            {
                "mapping_id": "m2",
                "request_field": "info",
                "request_type": "str",
                "response_field": "app",
                "response_type": "str",
                "intent": "info",
            },
        ],
    }

    count2 = await verifier.import_device_patterns(device_id, data2)
    assert count2 == 1

    async with db_manager.device_session(device_id) as session:
        patterns = (await session.execute(select(RequestPattern))).scalars().all()
        templates = (await session.execute(select(ResponseTemplate))).scalars().all()
        mappings = (await session.execute(select(FieldMapping))).scalars().all()

        assert len(patterns) == 3  # noqa: PLR2004
        assert len(templates) == 2  # noqa: PLR2004
        assert len(mappings) == 2  # noqa: PLR2004

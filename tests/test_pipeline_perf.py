import time
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from core.database import DatabaseManager, FieldMapping, RequestPattern, ResponseTemplate
from core.pipeline import ContextBuffer, LearningPipeline, MatchRateTracker


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


@pytest.mark.asyncio
async def test_save_patterns_benchmark(db_manager):
    dev_id = "dev-perf-test"
    await db_manager.get_or_create_device(dev_id, "shelly")

    llm = MagicMock()
    buffer = ContextBuffer(db_manager)
    tracker = MatchRateTracker(db_manager)
    pipeline = LearningPipeline(db_manager, llm, buffer, tracker)

    count = 100
    # Generate 100 patterns with templates and field mappings
    patterns = []
    for i in range(count):
        patterns.append(
            {
                "intent": f"intent_{i}",
                "method": "POST",
                "path": f"/rpc/Method.{i}",
                "protocol": "http",
                "required_headers": ["Content-Type"],
                "body_schema": {"type": "object"},
                "query_param_keys": ["id"],
                "confidence": 0.9,
                "response": {
                    "status_code": 200,
                    "headers": {"Content-Type": "application/json"},
                    "body": {"result": i},
                    "field_mappings": {"val": "result"},
                    "expected_variables": ["val"],
                    "confidence": 0.9,
                },
                "field_mappings": {
                    f"req_field_{j}": {"response_field": f"resp_field_{j}", "type": "string"}
                    for j in range(3)
                },
            }
        )

    analysis = {"patterns": patterns}

    start_time = time.perf_counter()
    await pipeline._save_patterns(dev_id, analysis)
    elapsed = time.perf_counter() - start_time

    print(f"\n[BENCHMARK] _save_patterns elapsed for 100 patterns: {elapsed * 1000:.2f} ms")  # noqa: T201

    async with db_manager.device_session(dev_id) as session:
        pat_count = len((await session.execute(select(RequestPattern))).scalars().all())
        tpl_count = len((await session.execute(select(ResponseTemplate))).scalars().all())
        map_count = len((await session.execute(select(FieldMapping))).scalars().all())
        assert pat_count == count
        assert tpl_count == count
        assert map_count == count * 3

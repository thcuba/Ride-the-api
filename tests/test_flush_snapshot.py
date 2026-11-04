"""Pairs captured during a flush stay for the next cycle (C-17)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from core.database import DatabaseManager
from core.pipeline import LearningOrchestrator


@pytest_asyncio.fixture
async def orchestrator(tmp_path):
    dm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "devices",
    )
    await dm.initialize()
    llm = MagicMock()
    llm.resolve_task_profile = MagicMock(return_value=None)
    orch = LearningOrchestrator(dm)
    orch.initialize(llm)
    await dm.get_or_create_device("dev1", "acme", device_type="plug")
    yield orch
    await dm.close()


async def _exchange(orch, i: int) -> None:
    result = await orch.handle_request("dev1", "acme", "https", "GET", f"/api/r{i}", {}, None, {})
    await orch.handle_response(
        "dev1", "acme", "https", 200, {}, {"i": i}, correlation_key=result["correlation_key"]
    )


@pytest.mark.asyncio
async def test_pairs_arriving_mid_flush_are_kept_for_the_next_cycle(orchestrator):
    for i in range(3):
        await _exchange(orchestrator, i)

    sent: list[str] = []
    llm_started = asyncio.Event()
    release_llm = asyncio.Event()

    async def slow_llm(context, *_args, **_kwargs):
        sent.extend(p["path"] for p in context["pairs"])
        llm_started.set()
        await release_llm.wait()
        return {"patterns": []}

    orchestrator.pipeline._analyze_with_llm = slow_llm

    async def traffic_during_flush():
        await llm_started.wait()
        for i in range(3, 6):
            await _exchange(orchestrator, i)
        release_llm.set()

    result, _ = await asyncio.gather(orchestrator.flush_and_learn("dev1"), traffic_during_flush())
    assert result["success"] is True
    assert sorted(sent) == ["/api/r0", "/api/r1", "/api/r2"]

    left = await orchestrator.pipeline.buffer.get_buffer_pairs("dev1")
    assert sorted(p["pair"]["path"] for p in left) == ["/api/r3", "/api/r4", "/api/r5"]

"""Automatic flushes run off the response path, one at a time (R-3).

Also covers LLM replies that ignore the requested shape: they must fail the
analysis or be saved, never crash the flush.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select

import core.pipeline as pipeline_mod
from core.database import (
    DatabaseManager,
    DeviceRegistry,
    FieldMapping,
    RequestPattern,
    ResponseTemplate,
)
from core.pipeline import LearningOrchestrator

STEP_TIMEOUT_S = 2
DEFAULT_CONFIDENCE = 0.5


@pytest_asyncio.fixture
async def dm(tmp_path):
    manager = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "devices",
    )
    await manager.initialize()
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def orchestrator(dm):
    llm = MagicMock()
    llm.resolve_task_profile = MagicMock(return_value=None)
    orch = LearningOrchestrator(dm)
    orch.initialize(llm)
    await dm.get_or_create_device("dev1", "acme", device_type="plug")
    yield orch
    await orch.cancel_background_flushes()


async def _flush_on_every_pair(dm: DatabaseManager) -> None:
    async with dm.core_session() as session:
        device = (
            await session.execute(select(DeviceRegistry).where(DeviceRegistry.device_id == "dev1"))
        ).scalar_one()
        device.context_buffer_size = 1
        await session.commit()
    dm.invalidate_device_cache("dev1")


async def _exchange(orch: LearningOrchestrator, i: int) -> dict:
    async def run() -> dict:
        result = await orch.handle_request(
            "dev1", "acme", "https", "GET", f"/api/r{i}", {}, None, {}
        )
        return await orch.handle_response(
            "dev1", "acme", "https", 200, {}, {"i": i}, correlation_key=result["correlation_key"]
        )

    # Bounded: before the fix the response path awaited the LLM inline.
    return await asyncio.wait_for(run(), STEP_TIMEOUT_S)


class _GatedLLM:
    """Stands in for ``_analyze_with_llm``; blocks until released."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, context: dict, *_args, **_kwargs) -> dict:
        self.calls.append(sorted(p["path"] for p in context["pairs"]))
        self.started.set()
        await self.release.wait()
        return {"patterns": []}


async def _buffered_paths(orch: LearningOrchestrator) -> list[str]:
    return sorted(p["pair"]["path"] for p in await orch.pipeline.buffer.get_buffer_pairs("dev1"))


@pytest.mark.asyncio
async def test_response_returns_while_the_llm_is_still_working(orchestrator, dm):
    await _exchange(orchestrator, 0)  # default buffer size: no flush yet
    llm = _GatedLLM()
    orchestrator.pipeline._analyze_with_llm = llm
    await _flush_on_every_pair(dm)

    result = await _exchange(orchestrator, 1)  # returns while the LLM is blocked
    assert result["buffer_flushed"] is True
    await asyncio.wait_for(llm.started.wait(), STEP_TIMEOUT_S)
    assert llm.calls == [["/api/r0", "/api/r1"]]

    llm.release.set()
    await orchestrator.pipeline.wait_for_flush("dev1")
    assert await _buffered_paths(orchestrator) == []


@pytest.mark.asyncio
async def test_responses_during_a_flush_do_not_start_another(orchestrator, dm):
    await _exchange(orchestrator, 0)
    llm = _GatedLLM()
    orchestrator.pipeline._analyze_with_llm = llm
    await _flush_on_every_pair(dm)

    await _exchange(orchestrator, 1)
    await asyncio.wait_for(llm.started.wait(), STEP_TIMEOUT_S)
    for i in range(2, 6):
        assert (await _exchange(orchestrator, i))["buffer_flushed"] is True
    assert len(llm.calls) == 1, "one flush per device at a time"

    llm.release.set()
    await orchestrator.pipeline.wait_for_flush("dev1")
    assert llm.calls == [["/api/r0", "/api/r1"]]
    # Pairs that arrived mid-flush wait for the next cycle.
    assert await _buffered_paths(orchestrator) == [f"/api/r{i}" for i in range(2, 6)]


@pytest.mark.asyncio
async def test_manual_flush_waits_for_the_background_one(orchestrator, dm):
    await _exchange(orchestrator, 0)
    llm = _GatedLLM()
    orchestrator.pipeline._analyze_with_llm = llm
    await _flush_on_every_pair(dm)

    await _exchange(orchestrator, 1)
    await asyncio.wait_for(llm.started.wait(), STEP_TIMEOUT_S)
    await _exchange(orchestrator, 2)

    manual = asyncio.create_task(orchestrator.flush_and_learn("dev1"))
    await asyncio.sleep(0.05)
    llm.release.set()
    result = await asyncio.wait_for(manual, STEP_TIMEOUT_S)

    assert result["success"] is True
    # The manual flush only saw what the background flush had not analysed.
    assert llm.calls == [["/api/r0", "/api/r1"], ["/api/r2"]]
    assert await _buffered_paths(orchestrator) == []


@pytest.mark.asyncio
async def test_failed_flush_is_not_retried_on_every_response(orchestrator, dm, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(pipeline_mod, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    sent: list[int] = []

    async def failing_llm(context: dict, *_args, **_kwargs) -> None:
        sent.append(len(context["pairs"]))

    await _exchange(orchestrator, 0)
    orchestrator.pipeline._analyze_with_llm = failing_llm
    await _flush_on_every_pair(dm)

    assert (await _exchange(orchestrator, 1))["buffer_flushed"] is True
    await orchestrator.pipeline.wait_for_flush("dev1")
    assert sent == [2]

    for i in range(2, 5):
        assert (await _exchange(orchestrator, i))["buffer_flushed"] is False
    await orchestrator.pipeline.wait_for_flush("dev1")
    assert sent == [2], "no LLM call per response while cooling down"

    clock[0] += pipeline_mod._FLUSH_RETRY_COOLDOWN_S + 1
    assert (await _exchange(orchestrator, 5))["buffer_flushed"] is True
    await orchestrator.pipeline.wait_for_flush("dev1")
    assert sent == [2, 6], "one retry after the cooldown, with every kept pair"


@pytest.mark.asyncio
async def test_flush_that_raises_is_logged_and_backs_off(orchestrator, dm):
    calls: list[int] = []

    async def llm(context: dict, *_args, **_kwargs) -> dict:
        calls.append(len(context["pairs"]))
        return {"patterns": []}

    await _exchange(orchestrator, 0)
    orchestrator.pipeline._analyze_with_llm = llm
    orchestrator.pipeline._save_patterns = AsyncMock(side_effect=RuntimeError("disk full"))
    await _flush_on_every_pair(dm)

    await _exchange(orchestrator, 1)
    await orchestrator.pipeline.wait_for_flush("dev1")  # does not raise
    assert (await _exchange(orchestrator, 2))["buffer_flushed"] is False
    assert calls == [2]
    assert await _buffered_paths(orchestrator) == ["/api/r0", "/api/r1", "/api/r2"]


@pytest.mark.asyncio
async def test_shutdown_cancels_a_running_flush(orchestrator, dm):
    await _exchange(orchestrator, 0)
    llm = _GatedLLM()
    orchestrator.pipeline._analyze_with_llm = llm
    await _flush_on_every_pair(dm)
    await _exchange(orchestrator, 1)
    await asyncio.wait_for(llm.started.wait(), STEP_TIMEOUT_S)

    await asyncio.wait_for(orchestrator.cancel_background_flushes(), STEP_TIMEOUT_S)
    assert not orchestrator.flushes.is_running("dev1")
    # Nothing was marked analysed: the pairs wait for the next run.
    assert await _buffered_paths(orchestrator) == ["/api/r0", "/api/r1"]


@pytest.mark.asyncio
async def test_pipeline_rebuild_keeps_the_flush_single_flight(orchestrator, dm, monkeypatch):
    await _exchange(orchestrator, 0)
    llm = _GatedLLM()
    orchestrator.pipeline._analyze_with_llm = llm
    await _flush_on_every_pair(dm)
    await _exchange(orchestrator, 1)
    await asyncio.wait_for(llm.started.wait(), STEP_TIMEOUT_S)

    # A buffer backend switch drops the pipeline; the next capture rebuilds it.
    rebuilt_llm = AsyncMock(return_value={"patterns": []})
    monkeypatch.setattr(pipeline_mod.LearningPipeline, "_analyze_with_llm", rebuilt_llm)
    orchestrator.reset_buffers()
    assert (await _exchange(orchestrator, 2))["buffer_flushed"] is True
    assert orchestrator.pipeline.flushes is orchestrator.flushes

    llm.release.set()
    await orchestrator.flushes.wait("dev1")
    await asyncio.sleep(0.05)
    rebuilt_llm.assert_not_called()  # no second flush beside the running one
    assert llm.calls == [["/api/r0", "/api/r1"]]


# --- LLM replies that ignore the requested shape --------------------------


async def _flush_reply(orchestrator: LearningOrchestrator, reply: object) -> dict:
    await _exchange(orchestrator, 0)
    orchestrator.pipeline._analyze_with_llm = AsyncMock(return_value=reply)
    return await orchestrator.flush_and_learn("dev1")


async def _rows(dm: DatabaseManager, model: type) -> list:
    async with dm.device_session("dev1") as session:
        return list((await session.execute(select(model))).scalars().all())


@pytest.mark.asyncio
async def test_bare_list_reply_with_nulls_is_saved(orchestrator, dm):
    result = await _flush_reply(
        orchestrator,
        [
            {
                "intent": "status",
                "method": "GET",
                "path": None,
                "request_path": "/api/status",
                "confidence": "high",
                "response": {"status_code": "200 OK", "body": {"on": True}, "confidence": 7},
            },
            {
                "intent": None,
                "method": None,
                "path": "/api/ping",
                "response": "OK",
                "field_mappings": {"a": 5, "b": "b_out"},
            },
            "not a pattern",
        ],
    )
    assert result["success"] is True

    patterns = {p.path_pattern: p for p in await _rows(dm, RequestPattern)}
    assert set(patterns) == {"/api/status", "/api/ping"}
    assert patterns["/api/status"].confidence == DEFAULT_CONFIDENCE  # "high" is not a number
    assert patterns["/api/ping"].intent == "unknown"
    assert patterns["/api/ping"].method == "POST"

    templates = await _rows(dm, ResponseTemplate)
    assert [(t.status_code, t.confidence) for t in templates] == [(200, 1.0)]
    assert [m.request_field for m in await _rows(dm, FieldMapping)] == ["b"]


@pytest.mark.asyncio
async def test_empty_pattern_list_learns_nothing(orchestrator, dm):
    result = await _flush_reply(orchestrator, {"patterns": [], "protocols": ["https"]})
    assert result["success"] is True
    assert await _rows(dm, RequestPattern) == []


@pytest.mark.asyncio
async def test_single_pattern_object_under_patterns_is_saved(orchestrator, dm):
    result = await _flush_reply(
        orchestrator, {"patterns": {"intent": "status", "method": "GET", "path": "/api/status"}}
    )
    assert result["success"] is True
    assert [p.path_pattern for p in await _rows(dm, RequestPattern)] == ["/api/status"]


@pytest.mark.asyncio
async def test_scalar_reply_is_a_failed_analysis(orchestrator):
    result = await _flush_reply(orchestrator, "I could not find any patterns")
    assert result["success"] is False
    assert result["error"] == "LLM analysis failed"
    assert await _buffered_paths(orchestrator) == ["/api/r0"], "kept for the next attempt"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.8, 0.8), ("0.25", 0.25), (7, 1.0), (-1, 0.0), ("high", 0.5), (None, 0.5), (True, 0.5)],
)
def test_confidence_coercion(value, expected):
    assert pipeline_mod._confidence(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(201, 201), ("404", 404), ("200 OK", 200), ("OK", 200), (99, 200), (None, 200), (True, 200)],
)
def test_status_code_coercion(value, expected):
    assert pipeline_mod._status_code(value) == expected

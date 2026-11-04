"""Concurrent use of the RAM buffer backend must not lose or corrupt pairs (C-9).

Every RAM store shares one in-memory SQLite connection. Before sessions were
serialized, overlapping sessions shared a transaction: one session's commit
committed another's half-done work and a pool rollback discarded another's
writes, so successful adds went missing and counters drifted.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from core.buffer import create_buffer_store, dispose_memory_db, memory_session, set_buffer_backend
from core.database import DatabaseManager, LLMContextBuffer

DEVICES = [f"ram-dev-{i}" for i in range(4)]
ADDS_PER_DEVICE = 20
PAIR_SIZE = 100


@pytest_asyncio.fixture
async def db_manager(tmp_path):
    dm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "device_dbs",
    )
    await dm.initialize()
    yield dm
    await dm.close()


@pytest_asyncio.fixture(autouse=True)
async def _ram_backend():
    set_buffer_backend("memory")
    yield
    set_buffer_backend("disk")
    await dispose_memory_db()


@pytest.mark.asyncio
async def test_concurrent_adds_across_devices_are_all_kept(db_manager):
    # Two independent store instances, as the pipeline and the buffer manager have.
    stores = [create_buffer_store(db_manager), create_buffer_store(db_manager)]

    async def add(i: int, device_id: str) -> int:
        store = stores[i % 2]
        return await store.add_pair(device_id, {"n": i, "device": device_id}, PAIR_SIZE)

    async def read(device_id: str) -> int:
        return await stores[0].get_current_size(device_id)

    adds = [add(i, dev) for dev in DEVICES for i in range(ADDS_PER_DEVICE)]
    reads = [read(dev) for dev in DEVICES for _ in range(5)]
    results = await asyncio.gather(*adds, *reads)
    assert len(results) == len(adds) + len(reads)  # nothing raised

    async with memory_session("") as session:
        rows = (await session.execute(select(func.count(LLMContextBuffer.id)))).scalar_one()
    assert rows == len(DEVICES) * ADDS_PER_DEVICE

    for dev in DEVICES:
        assert await stores[0].get_current_size(dev) == ADDS_PER_DEVICE * PAIR_SIZE


@pytest.mark.asyncio
async def test_nested_session_in_same_task_does_not_deadlock():
    async with memory_session("a") as outer:
        await outer.execute(select(1))
        async with memory_session("b") as inner:
            await inner.execute(select(1))

"""Tests for the RAM-buffer session factory guard (memory_session)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

from core.buffer import memory


def test_memory_session_yields_and_commits() -> None:
    """A normal memory_session round-trips a real SQLAlchemy session."""

    async def _run() -> None:
        await memory.dispose_memory_db()
        try:
            async with memory.memory_session("dev-1") as session:
                assert session is not None
                # A SELECT against the shared RAM engine proves the session is live.
                result = await session.execute(text("SELECT 1"))
                assert result.scalar() == 1
        finally:
            await memory.dispose_memory_db()

    asyncio.run(_run())


def test_guard_is_explicit_not_assert() -> None:
    """The factory guard is a real condition, not an assert, so ``-O`` cannot strip it."""
    source = sys.modules[memory.__name__].__file__
    assert source is not None
    text_in_file = Path(source).read_text(encoding="utf-8")
    assert "if _factory is None:" in text_in_file
    assert "raise RuntimeError" in text_in_file
    assert "assert _factory" not in text_in_file

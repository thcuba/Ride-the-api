"""Runtime dependency contracts for the LLM client stack.

``core.llm_decipher`` talks to providers through the ``openai`` SDK, which
requires the classic ``httpx`` (0.x) API. If a build resolves/ships ``httpx2``
(or only ``httpx`` 2.x), every outbound LLM call fails with ``Connection
error.`` regardless of configuration — while the rest of the process (and a
shell) still reaches the same endpoints. These tests pin that contract so a CI
build cannot silently regress to httpx2.
"""

from __future__ import annotations

import httpx
import openai


def test_openai_client_runs_on_classic_httpx() -> None:
    """openai>=2 uses httpx (0.x); httpx2 is a different, incompatible stack."""
    assert openai.__version__.startswith("2.")
    assert httpx.__version__.startswith("0.")


def test_llm_decipher_imports_without_conflict() -> None:
    """The LLM module must import cleanly with the pinned httpx (no shadowing)."""
    import core.llm_decipher  # noqa: PLC0415

    assert core.llm_decipher.AsyncOpenAI is not None  # type: ignore[attr-defined]

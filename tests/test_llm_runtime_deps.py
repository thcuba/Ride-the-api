"""Runtime dependency contract for the LLM client stack.

``core.llm_decipher`` talks to providers through the ``openai`` SDK, which
requires the classic ``httpx`` (0.x) API. If a build resolves/ships only
``httpx2`` (2.x) instead of ``httpx``, every outbound LLM call fails with
``Connection error.`` regardless of configuration — while the rest of the
process (and a shell) still reaches the same endpoints. These tests pin that
contract so a CI build cannot silently regress to httpx2. Note ``openai``
itself must NOT be version-pinned here: the project allows 2.x and 3.x, so the
guard is about ``httpx``, not the openai minor.
"""

from __future__ import annotations

import httpx


def test_classic_httpx_is_the_runtime_client() -> None:
    """openai requires httpx 0.x; httpx2 is a different, incompatible stack."""
    assert httpx.__version__.startswith("0.")


def test_llm_decipher_imports_without_conflict() -> None:
    """The LLM module must import cleanly with the pinned httpx (no shadowing)."""
    import core.llm_decipher  # noqa: PLC0415

    assert core.llm_decipher.AsyncOpenAI is not None  # type: ignore[attr-defined]

"""Shared small helpers used across core modules.

Hosts tiny pure functions that were previously copy-pasted in several places
(e.g. ``pattern_engine``, ``modification``, ``llm_decipher``, ``decipher_ingest``)
so they have a single source of truth instead of divergent copies.
"""

from __future__ import annotations

import functools


@functools.lru_cache(maxsize=2048)
def dot_to_dpath(path: str) -> str:
    """Convert dot/bracket path notation ('a.b[0].c') to dpath slash notation.

    dpath uses '/' as its default separator; map '.' and '[0]' → '/', and
    drop the closing ']'. Array indexes become segments (e.g. ``items/0/name``).

    Memoized with lru_cache for fast O(1) path conversion during request/response
    mapping and rule evaluation.
    """
    p = path.lstrip("$")
    p = p.replace("[", "/").replace("]", "")
    return p.replace(".", "/")


def safe_float(value, default: float = 0.0) -> float:
    """Parse a float from untrusted LLM-returned value without raising.

    LLMs often emit ``confidence`` as ``"high"``, ``"90%"`` or ``0.9``, which
    would make a bare ``float(...)`` raise and abort the whole ingest batch.
    """
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().rstrip("%")
        try:
            f = float(cleaned)
        except ValueError:
            return default
        return f
    return default

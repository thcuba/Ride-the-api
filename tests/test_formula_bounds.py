"""Formula evaluation is bounded by result size, not only operand size (S-18).

Pattern files are shared and auto-loaded, and formula inputs come from device
traffic, so a formula must never be able to stall the event loop.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from core.pattern_db.pattern_engine import PatternEngine

FAST_S = 0.5


@pytest.fixture
def engine() -> PatternEngine:
    return PatternEngine(MagicMock())


def _eval(engine: PatternEngine, formula: str, request: dict | None = None, **state):
    store = engine.get_state_store("dev")
    for key, value in state.items():
        store.set(key, value)
    return engine._eval_formula(formula, request or {}, store)


@pytest.mark.parametrize(
    "formula",
    [
        "4000000 ** 4000000",  # passed simpleeval's operand cap: ~30 s
        "40000 ** 40000",
        "(3 ** 2000) * (3 ** 2000)",
        "(1 << 4000) << 4000",
        '"%0999999999d" % 1',
        '"%*d" % 1',
        'b"%0999999999d" % 1',
    ],
)
def test_explosive_formulas_are_refused_quickly(engine, formula):
    start = time.perf_counter()
    assert _eval(engine, formula) == 0
    assert time.perf_counter() - start < FAST_S


def test_long_device_values_are_not_scanned_or_cached(engine):
    request = {"body": {"x": "random(" * 20_000}}
    start = time.perf_counter()
    assert _eval(engine, '{request.body.x} + "!"', request) == 0
    assert time.perf_counter() - start < FAST_S


def test_random_is_a_real_function(engine):
    values = [_eval(engine, "random(1 + 1, {state.hi})", hi=5) for _ in range(20)]
    assert all(2 <= value <= 5 for value in values)  # noqa: PLR2004
    assert len(set(values)) > 1


@pytest.mark.parametrize(
    ("formula", "state", "expected"),
    [
        ("{state.t} * 1.8 + 32", {"t": 20}, 68.0),
        ("2 ** 10", {}, 1024),
        ("3 ** 2000 > 0", {}, True),
        ("(1 << 100) >> 99", {}, 2),
        ('"%.1f" % {state.t}', {"t": 3.14159}, "3.1"),
        ('"%5d" % 42', {}, "   42"),
        ("round({state.t} / 3, 2)", {"t": 10}, 3.33),
    ],
)
def test_ordinary_formulas_still_work(engine, formula, state, expected):
    assert _eval(engine, formula, **state) == expected

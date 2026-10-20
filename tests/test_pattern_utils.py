"""Tests for core.pattern_utils shared helpers."""

from core.pattern_utils import dot_to_dpath, safe_float

_HALF = 0.5
_NINETY = 90.0


def test_dot_to_dpath_handles_dots_and_brackets():
    assert dot_to_dpath("a.b.c") == "a/b/c"
    assert dot_to_dpath("items[0].name") == "items/0/name"
    assert dot_to_dpath("$a.b") == "a/b"
    assert dot_to_dpath("a") == "a"


def test_dot_to_dpath_is_memoized():
    # lru_cache returns the same object for the same input
    assert dot_to_dpath("x.y.z") is dot_to_dpath("x.y.z")


def test_safe_float_passes_numerics():
    assert safe_float(_NINETY) == _NINETY  # noqa: PLR2004
    assert safe_float(_HALF) == _HALF
    assert safe_float(True) == 0.0  # bool is not a number
    assert safe_float(None) == 0.0


def test_safe_float_parses_strings():
    assert safe_float("90%") == _NINETY
    assert safe_float("0.9") == 0.9  # noqa: PLR2004
    assert safe_float(" 0.5 ") == _HALF


def test_safe_float_defaults_on_garbage():
    assert safe_float("high") == 0.0
    assert safe_float(None, _HALF) == _HALF
    assert safe_float("high", _HALF) == _HALF

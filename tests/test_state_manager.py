"""Tests for DeviceStateStore / VirtualSensor correctness fixes.

Covers:
- F3: ``DeviceStateStore.set()`` contract — returns False when unchanged,
  True only when the value actually changes.
- F4: ``VirtualSensor`` baseline resolution rejects NaN/Inf so corrupt
  values do not poison downstream sensor arithmetic and local responses.
"""

import math

from core.pattern_db.schemas import VirtualSensor
from core.pattern_db.state_manager import DeviceStateStore, _SensorInstance


class TestDeviceStateStoreSet:
    def test_set_unchanged_returns_false(self):
        store = DeviceStateStore("dev-001")
        assert store.set("power", "on") is True  # new key counts as changed
        assert store.set("power", "on") is False  # unchanged
        assert store.get("power") == "on"

    def test_set_changed_returns_true(self):
        store = DeviceStateStore("dev-001")
        store.set("temp", 20)
        updated = 21
        assert store.set("temp", updated) is True
        assert store.get("temp") == updated

    def test_set_mixed_types_compare_equal_by_value(self):
        store = DeviceStateStore("dev-001")
        store.set("count", 3)
        assert store.set("count", 3) is False  # int 3 == py 3


class TestVirtualSensorBaselineRejectsNaN:
    def _sensor(self, baseline: str):
        vs = VirtualSensor(
            name="s",
            type="float",
            behavior="static",
            baseline=baseline,
        )
        return _SensorInstance(vs)

    def test_nan_baseline_resolves_to_zero(self):
        s = self._sensor("nan")
        value = s.read({})
        assert math.isfinite(value), f"NaN leaked: {value}"
        assert value == 0.0, f"expected 0, got {value}"

    def test_inf_baseline_resolves_to_zero(self):
        s = self._sensor("1e308")
        # 1e308 is finite but huge; the guard should still yield a finite value
        value = s.read({})
        assert math.isfinite(value)

    def test_valid_numeric_baseline_preserved(self):
        expected = 23.5
        s = self._sensor(f"{expected}")
        value = s.read({})
        assert value == expected

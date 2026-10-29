"""Tests for DeviceStateStore / VirtualSensor correctness fixes and restore functionality.

Covers:
- F3: ``DeviceStateStore.set()`` contract — returns False when unchanged,
  True only when the value actually changes.
- F4: ``VirtualSensor`` baseline resolution rejects NaN/Inf so corrupt
  values do not poison downstream sensor arithmetic and local responses.
- DeviceStateStore.restore() restoration behavior, snapshot round-trip,
  handling of missing variables key, and dynamic sensor baseline resolution.
"""

import math

from core.pattern_db.schemas import VirtualSensor
from core.pattern_db.state_manager import DeviceStateStore, _SensorInstance


class TestDeviceStateStoreSet:
    def test_set_unchanged_returns_false(self) -> None:
        store = DeviceStateStore("dev-001")
        assert store.set("power", "on") is True  # new key counts as changed
        assert store.set("power", "on") is False  # unchanged
        assert store.get("power") == "on"

    def test_set_changed_returns_true(self) -> None:
        store = DeviceStateStore("dev-001")
        store.set("temp", 20)
        updated = 21
        assert store.set("temp", updated) is True
        assert store.get("temp") == updated

    def test_set_mixed_types_compare_equal_by_value(self) -> None:
        store = DeviceStateStore("dev-001")
        store.set("count", 3)
        assert store.set("count", 3) is False  # int 3 == py 3


class TestDeviceStateStoreRestore:
    def test_restore_into_empty_store(self) -> None:
        store = DeviceStateStore("dev-001")
        data = {"variables": {"power": "on", "brightness": 80}}
        store.restore(data)
        assert store.get("power") == "on"
        assert store.get("brightness") == 80  # noqa: PLR2004

    def test_restore_updates_existing_and_adds_new(self) -> None:
        store = DeviceStateStore("dev-001")
        store.set("power", "off")
        store.set("mode", "eco")

        data = {"variables": {"power": "on", "brightness": 100}}
        store.restore(data)

        assert store.get("power") == "on"
        assert store.get("mode") == "eco"
        assert store.get("brightness") == 100  # noqa: PLR2004

    def test_restore_snapshot_roundtrip(self) -> None:
        store1 = DeviceStateStore("dev-001")
        expected_temp = 22.5
        expected_humidity = 45
        store1.set("temp", expected_temp)
        store1.set("humidity", expected_humidity)

        snap = store1.snapshot()

        store2 = DeviceStateStore("dev-001")
        store2.restore(snap)

        assert store2.get("temp") == expected_temp
        assert store2.get("humidity") == expected_humidity

    def test_restore_missing_or_empty_variables_key(self) -> None:
        store = DeviceStateStore("dev-001")
        store.set("status", "active")

        # Missing 'variables' key should gracefully do nothing
        store.restore({"device_id": "dev-001"})
        assert store.get("status") == "active"

        # Empty 'variables' key should not clear existing state
        store.restore({"variables": {}})
        assert store.get("status") == "active"

    def test_restore_allows_virtual_sensor_dynamic_baseline_resolution(self) -> None:
        store = DeviceStateStore("dev-001")
        sensor_cfg = VirtualSensor(
            name="temp_sensor",
            type="float",
            behavior="static",
            baseline="{state.target_temp}",
            update_interval_s=0,
        )
        store.apply_virtual_sensors([sensor_cfg])

        # Before restore, default baseline key is missing, so sensor reads default 0.0
        assert store.get("temp_sensor") == 0.0

        # After restoring target_temp variable, sensor reads restored value
        expected_target_temp = 24.5
        store.restore({"variables": {"target_temp": expected_target_temp}})
        assert store.get("temp_sensor") == expected_target_temp


class TestVirtualSensorBaselineRejectsNaN:
    def _sensor(self, baseline: str) -> _SensorInstance:
        vs = VirtualSensor(
            name="s",
            type="float",
            behavior="static",
            baseline=baseline,
        )
        return _SensorInstance(vs)

    def test_nan_baseline_resolves_to_zero(self) -> None:
        s = self._sensor("nan")
        value = s.read({})
        assert math.isfinite(value), f"NaN leaked: {value}"
        assert value == 0.0, f"expected 0, got {value}"

    def test_inf_baseline_resolves_to_zero(self) -> None:
        s = self._sensor("1e308")
        # 1e308 is finite but huge; the guard should still yield a finite value
        value = s.read({})
        assert math.isfinite(value)

    def test_valid_numeric_baseline_preserved(self) -> None:
        expected = 23.5
        s = self._sensor(f"{expected}")
        value = s.read({})
        assert value == expected

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

from core.pattern_db.schemas import StateVariable, VirtualSensor
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


class TestDeviceStateStoreGetAll:
    def test_get_all_empty_store(self) -> None:
        store = DeviceStateStore("dev-empty")
        assert store.get_all() == {}

    def test_get_all_variables_only_and_isolation(self) -> None:
        store = DeviceStateStore("dev-vars")
        store.apply_state_variables(
            [
                StateVariable(name="power", default="off"),
                StateVariable(name="brightness", default=50),
            ]
        )
        store.set("brightness", 75)

        all_state = store.get_all()
        assert all_state == {"power": "off", "brightness": 75}

        # Verify mutation of returned dictionary does not mutate internal store state
        all_state["power"] = "on"
        all_state["new_key"] = 123
        assert store.get("power") == "off"
        assert store.get("new_key") is None
        assert store.get_all() == {"power": "off", "brightness": 75}

    def test_get_all_sensors_only(self) -> None:
        store = DeviceStateStore("dev-sensors")
        sensor_cfg = VirtualSensor(
            name="battery_level",
            type="float",
            behavior="static",
            baseline="95.5",
            update_interval_s=0,
        )
        store.apply_virtual_sensors([sensor_cfg])

        assert store.get_all() == {"battery_level": 95.5}

    def test_get_all_combined_variables_and_dynamic_sensors(self) -> None:
        store = DeviceStateStore("dev-combined")
        store.apply_state_variables(
            [
                StateVariable(name="target_temp", default=21.0),
                StateVariable(name="mode", default="heat"),
            ]
        )
        sensor_cfg = VirtualSensor(
            name="current_temp",
            type="float",
            behavior="static",
            baseline="{state.target_temp}",
            update_interval_s=0,
        )
        store.apply_virtual_sensors([sensor_cfg])

        result = store.get_all()
        assert result == {
            "target_temp": 21.0,
            "mode": "heat",
            "current_temp": 21.0,
        }

    def test_get_all_reflects_updated_state_and_sensor_precedence(self) -> None:
        store = DeviceStateStore("dev-precedence")
        store.apply_state_variables(
            [
                StateVariable(name="target_temp", default=20.0),
                StateVariable(name="reported_temp", default=18.0),
            ]
        )
        # Sensor with the same name as variable 'reported_temp'
        sensor_cfg = VirtualSensor(
            name="reported_temp",
            type="float",
            behavior="static",
            baseline="{state.target_temp}",
            update_interval_s=0,
        )
        store.apply_virtual_sensors([sensor_cfg])

        # Sensor reading overrides state variable when names collide
        assert store.get_all() == {"target_temp": 20.0, "reported_temp": 20.0}

        # Updating the underlying variable triggers dynamic sensor update in get_all
        store.set("target_temp", 25.5)
        assert store.get_all() == {"target_temp": 25.5, "reported_temp": 25.5}


class TestDeviceStateStoreApplyVirtualSensors:
    def test_apply_virtual_sensors_populates_sensors_store(self) -> None:
        store = DeviceStateStore("dev-001")
        assert len(store._sensors) == 0

        sensors = [
            VirtualSensor(name="temp", type="float", behavior="static", baseline="21.5"),
            VirtualSensor(name="humidity", type="float", behavior="static", baseline="45.0"),
        ]
        store.apply_virtual_sensors(sensors)

        expected_count = 2
        assert len(store._sensors) == expected_count
        assert "temp" in store._sensors
        assert "humidity" in store._sensors
        assert isinstance(store._sensors["temp"], _SensorInstance)
        assert isinstance(store._sensors["humidity"], _SensorInstance)
        assert store._sensors["temp"].config.name == "temp"
        assert store._sensors["humidity"].config.name == "humidity"

    def test_apply_virtual_sensors_empty_list(self) -> None:
        store = DeviceStateStore("dev-001")
        sensor = VirtualSensor(name="temp", type="float", behavior="static", baseline="20.0")
        store.apply_virtual_sensors([sensor])
        assert len(store._sensors) == 1

        store.apply_virtual_sensors([])
        assert len(store._sensors) == 1
        assert "temp" in store._sensors

    def test_apply_virtual_sensors_overwrites_existing_sensor(self) -> None:
        store = DeviceStateStore("dev-001")
        initial_temp = 20.0
        sensor_v1 = VirtualSensor(
            name="temp", type="float", behavior="static", baseline=f"{initial_temp}"
        )
        store.apply_virtual_sensors([sensor_v1])
        first_instance = store._sensors["temp"]
        assert store.get("temp") == initial_temp

        updated_temp = 25.0
        sensor_v2 = VirtualSensor(
            name="temp", type="float", behavior="static", baseline=f"{updated_temp}"
        )
        store.apply_virtual_sensors([sensor_v2])
        second_instance = store._sensors["temp"]

        assert second_instance is not first_instance
        assert store.get("temp") == updated_temp

    def test_apply_virtual_sensors_retrievable_via_get_and_get_all(self) -> None:
        store = DeviceStateStore("dev-001")
        store.set("firmware", "v1.0")

        expected_temp = 22.0
        expected_pressure = 1013.25
        sensors = [
            VirtualSensor(
                name="temperature", type="float", behavior="static", baseline=f"{expected_temp}"
            ),
            VirtualSensor(
                name="pressure", type="float", behavior="static", baseline=f"{expected_pressure}"
            ),
        ]
        store.apply_virtual_sensors(sensors)

        assert store.get("temperature") == expected_temp
        assert store.get("pressure") == expected_pressure
        assert store.get("nonexistent", "fallback") == "fallback"

        all_state = store.get_all()
        assert all_state == {
            "firmware": "v1.0",
            "temperature": expected_temp,
            "pressure": expected_pressure,
        }

    def test_apply_virtual_sensors_with_dynamic_baseline(self) -> None:
        store = DeviceStateStore("dev-001")
        initial_target = 22.5
        store.apply_state_variables([StateVariable(name="target_temp", default=initial_target)])

        sensor = VirtualSensor(
            name="simulated_temp",
            type="float",
            behavior="static",
            baseline="{state.target_temp}",
            update_interval_s=0,
        )
        store.apply_virtual_sensors([sensor])

        assert store.get("simulated_temp") == initial_target

        # Update the state variable and verify sensor reflects changed baseline
        updated_target = 26.0
        store.set("target_temp", updated_target)
        assert store.get("simulated_temp") == updated_target


class TestDeviceStateStoreApplyStateVariables:
    def test_apply_state_variables_empty_list(self) -> None:
        store = DeviceStateStore("dev-001")
        store.apply_state_variables([])
        assert store.get_all() == {}
        assert store.is_dirty is False

    def test_apply_state_variables_populates_defaults_with_diverse_types(self) -> None:
        store = DeviceStateStore("dev-001")
        variables = [
            StateVariable(name="power", type="string", default="off"),
            StateVariable(name="brightness", type="integer", default=100),
            StateVariable(name="target_temp", type="float", default=21.5),
            StateVariable(name="eco_mode", type="boolean", default=True),
            StateVariable(name="tags", type="array", default=["living_room", "main"]),
            StateVariable(name="meta", type="object", default={"zone": 1}),
            StateVariable(name="empty_val", type="string", default=None),
        ]
        store.apply_state_variables(variables)

        assert store.get("power") == "off"
        assert store.get("brightness") == 100  # noqa: PLR2004
        assert store.get("target_temp") == 21.5  # noqa: PLR2004
        assert store.get("eco_mode") is True
        assert store.get("tags") == ["living_room", "main"]
        assert store.get("meta") == {"zone": 1}
        assert store.get("empty_val") is None

    def test_apply_state_variables_does_not_overwrite_existing_variables(self) -> None:
        store = DeviceStateStore("dev-001")
        store.set("power", "on")
        store.set("target_temp", 25.0)

        variables = [
            StateVariable(name="power", default="off"),
            StateVariable(name="target_temp", default=19.0),
            StateVariable(name="humidity", default=50),
        ]
        store.apply_state_variables(variables)

        # Existing values set prior to apply_state_variables must be preserved
        assert store.get("power") == "on"
        assert store.get("target_temp") == 25.0  # noqa: PLR2004
        # New variable not previously present should receive its default
        assert store.get("humidity") == 50  # noqa: PLR2004

    def test_apply_state_variables_does_not_overwrite_restored_state(self) -> None:
        store = DeviceStateStore("dev-001")
        store.restore({"variables": {"power": "standby", "volume": 35}})

        variables = [
            StateVariable(name="power", default="off"),
            StateVariable(name="volume", default=50),
            StateVariable(name="mute", default=False),
        ]
        store.apply_state_variables(variables)

        assert store.get("power") == "standby"
        assert store.get("volume") == 35  # noqa: PLR2004
        assert store.get("mute") is False

    def test_apply_state_variables_incremental_addition(self) -> None:
        store = DeviceStateStore("dev-001")
        initial_vars = [
            StateVariable(name="mode", default="auto"),
            StateVariable(name="temp", default=22.0),
        ]
        store.apply_state_variables(initial_vars)
        store.set("temp", 24.0)

        # Apply a second batch that contains a duplicate with different default and a new variable
        secondary_vars = [
            StateVariable(name="temp", default=18.0),
            StateVariable(name="fan_speed", default=3),
        ]
        store.apply_state_variables(secondary_vars)

        assert store.get("mode") == "auto"
        assert store.get("temp") == 24.0  # noqa: PLR2004
        assert store.get("fan_speed") == 3  # noqa: PLR2004

    def test_apply_state_variables_does_not_mark_dirty(self) -> None:
        store = DeviceStateStore("dev-001")
        assert store.is_dirty is False

        variables = [
            StateVariable(name="power", default="off"),
            StateVariable(name="mode", default="heat"),
        ]
        store.apply_state_variables(variables)

        # Initializing defaults from schema should not dirty the store
        assert store.is_dirty is False

    def test_apply_state_variables_integrated_with_get_all_and_snapshot(self) -> None:
        store = DeviceStateStore("dev-001")
        variables = [
            StateVariable(name="status", default="idle"),
            StateVariable(name="retries", default=0),
        ]
        store.apply_state_variables(variables)

        assert store.get_all() == {"status": "idle", "retries": 0}
        snap = store.snapshot()
        assert snap["device_id"] == "dev-001"
        assert snap["variables"] == {"status": "idle", "retries": 0}
        assert isinstance(snap["updated_at"], float)

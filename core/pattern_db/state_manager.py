"""
State Manager — manages persistent device state and virtual sensors.

This is the server-side state engine that maintains simulated device state
(state_variables) and generates realistic sensor data (virtual_sensors).
"""

from __future__ import annotations

import logging
import math
import random
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.pattern_db.schemas import StateVariable, VirtualSensor

logger = logging.getLogger(__name__)


class DeviceStateStore:
    """In-memory state store for a single device, populated from pattern DB."""

    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self._variables: dict[str, Any] = {}
        self._sensors: dict[str, _SensorInstance] = {}
        self._last_update: float = time.time()
        self._dirty: bool = False

    def apply_state_variables(self, variables: list[StateVariable]):
        """Initialize state from pattern DB state_variables."""
        for v in variables:
            if v.name not in self._variables:
                self._variables[v.name] = v.default

    def apply_virtual_sensors(self, sensors: list[VirtualSensor]):
        """Initialize virtual sensors from pattern DB."""
        for s in sensors:
            self._sensors[s.name] = _SensorInstance(s)

    def get(self, name: str, default: Any = None) -> Any:  # noqa: ANN401
        """Get a state variable or sensor value by name."""
        if name in self._variables:
            return self._variables[name]
        sensor = self._sensors.get(name)
        if sensor:
            return sensor.read(self._variables)
        return default

    def set(self, name: str, value: Any) -> bool:  # noqa: ANN401
        """Set a state variable. Returns True if changed, False if unchanged."""
        if name in self._variables and self._variables[name] == value:
            return False
        self._variables[name] = value
        self._dirty = True
        return True

    def get_all(self) -> dict[str, Any]:
        """Get all current state variables + sensor readings."""
        result = dict(self._variables)
        for name, sensor in self._sensors.items():
            result[name] = sensor.read(self._variables)
        return result

    def snapshot(self) -> dict[str, Any]:
        """Snapshot for persistence."""
        return {
            "device_id": self.device_id,
            "variables": dict(self._variables),
            "updated_at": time.time(),
        }

    def restore(self, data: dict):
        """Restore from a snapshot."""
        self._variables.update(data.get("variables", {}))

    @property
    def is_dirty(self) -> bool:
        """Whether any variable changed since the last persisted snapshot."""
        return self._dirty

    def clear_dirty(self) -> None:
        """Mark the current variables as persisted."""
        self._dirty = False


class _SensorInstance:
    """Runtime instance of a virtual sensor."""

    def __init__(self, config: VirtualSensor) -> None:
        self.config = config
        self._last_read: float = 0
        self._current_value: Any = None

    def read(self, state: dict[str, Any]) -> Any:  # noqa: ANN401
        now = time.time()
        if (
            now - self._last_read < self.config.update_interval_s
            and self._current_value is not None
        ):
            return self._current_value

        self._last_read = now
        baseline = self._resolve_baseline(state)

        if self.config.behavior == "static":
            self._current_value = baseline
        elif self.config.behavior == "random":
            self._current_value = self._random_value(baseline)
        elif self.config.behavior == "drift":
            self._current_value = self._drift(baseline)
        elif self.config.behavior == "periodic":
            self._current_value = self._periodic(now, baseline)
        else:
            self._current_value = baseline

        return self._current_value

    def _resolve_baseline(self, state: dict) -> float:
        raw = self.config.baseline
        if raw.startswith("{") and raw.endswith("}"):
            key = raw[1:-1]
            val = state.get(key[6:], 0) if key.startswith("state.") else state.get(raw, 0)
            try:
                value = float(val or 0)
            except (ValueError, TypeError):
                return 0.0
            # Reject NaN/Inf: they poison every downstream reading (drift,
            # random.uniform, amplitude arithmetic) and flow into local
            # responses as corrupt data.
            return value if math.isfinite(value) else 0.0
        try:
            value = float(raw)
        except (ValueError, TypeError):
            return 0.0
        return value if math.isfinite(value) else 0.0

    def _random_value(self, baseline: float) -> float:
        dr = self.config.drift_range or [-5, 5]
        return baseline + random.uniform(dr[0], dr[1])

    def _drift(self, baseline: float) -> float:
        dr = self.config.drift_range or [-5, 5]
        if self._current_value is None:
            return baseline
        return self._current_value + random.uniform(dr[0] * 0.1, dr[1] * 0.1)

    def _periodic(self, now: float, baseline: float) -> float:
        amp = self.config.amplitude or 10
        period = max(self.config.period_s, 1)
        return baseline + amp * math.sin(2 * math.pi * now / period)

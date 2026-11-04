"""The buffer backend set in config.yaml must actually apply (C-22)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from core.config import Config

SHIPPED_CONFIG = Path(__file__).resolve().parent.parent / "config" / "config.yaml"


def _shipped() -> dict:
    return yaml.safe_load(SHIPPED_CONFIG.read_text())


def test_shipped_config_has_buffer_at_top_level():
    data = _shipped()
    assert "buffer" in data
    assert "buffer" not in data.get("core", {})


def test_editing_the_shipped_buffer_backend_takes_effect():
    data = _shipped()
    data["buffer"]["backend"] = "memory"
    assert Config(**data).buffer.backend == "memory"


def test_nested_core_buffer_from_older_configs_is_honoured():
    cfg = Config(**{"core": {"buffer": {"backend": "memory"}}})
    assert cfg.buffer.backend == "memory"


def test_invalid_nested_backend_is_rejected_instead_of_ignored():
    with pytest.raises(ValidationError):
        Config(**{"core": {"buffer": {"backend": "ramm"}}})


def test_top_level_buffer_wins_over_nested():
    cfg = Config(**{"core": {"buffer": {"backend": "memory"}}, "buffer": {"backend": "disk"}})
    assert cfg.buffer.backend == "disk"

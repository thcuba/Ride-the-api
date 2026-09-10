"""Runtime LLM settings persistence.

The LLM decipher profiles are defined in ``config.yaml`` under
``llm_decipher``. The dashboard lets the operator change which LLM is used and
its configuration at runtime. To avoid rewriting (and losing the comments in)
``config.yaml``, these runtime overrides are stored in a small JSON file next
to the device DB directory — the same pattern used by the buffer backend.

The overrides are a *superset* of the effective LLM settings: once the
dashboard saves, the file holds the complete ``enabled`` / ``default_profile``
/ ``profiles`` state. On load the :class:`~core.llm_decipher.LLMDecipherService`
merges these overrides on top of the ``config.yaml`` values, so a profile
edited in the dashboard wins over the file default while profiles that were
never touched still come from ``config.yaml``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from core.config import get_config

logger = logging.getLogger(__name__)

__all__ = ["load_llm_settings", "save_llm_settings", "llm_settings_path"]


def llm_settings_path() -> Path:
    """Runtime LLM settings file living next to the device DB directory."""
    cfg = get_config()
    return Path(cfg.core.device_db_dir).parent / "llm_runtime.json"


def load_llm_settings() -> dict[str, Any] | None:
    """Load persisted LLM settings overrides, or ``None`` if none exist.

    Returns ``None`` when the file is absent or unreadable so callers fall back
    to the ``config.yaml`` values.
    """
    path = llm_settings_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read %s — ignoring LLM runtime settings", path)
        return None
    if not isinstance(data, dict):
        logger.warning("%s is not a JSON object — ignoring LLM runtime settings", path)
        return None
    return data


def save_llm_settings(settings: dict[str, Any]) -> None:
    """Persist LLM settings overrides so they survive a restart."""
    path = llm_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    logger.info("LLM runtime settings saved to %s", path)

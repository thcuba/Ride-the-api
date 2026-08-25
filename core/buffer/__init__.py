"""Buffer backend selection (disk vs in-process RAM) and store factory."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from core.buffer.memory import dispose_memory_db, memory_session
from core.buffer.sqlalchemy_store import SqlAlchemyBufferStore
from core.config import get_config

if TYPE_CHECKING:
    from core.buffer.store import BufferStore
    from core.database import DatabaseManager

logger = logging.getLogger(__name__)

__all__ = [
    "VALID_BACKENDS",
    "dispose_memory_db",
    "memory_session",
    "SqlAlchemyBufferStore",
    "get_buffer_backend",
    "set_buffer_backend",
    "load_persisted_backend",
    "persist_backend",
    "initialize_buffer_backend",
    "create_buffer_store",
]

VALID_BACKENDS = ("disk", "memory")

# Module-level runtime choice. Defaults to the config value; overloaded at
# startup by the persisted runtime setting and changeable via the settings API.
_current_backend: str = "disk"


def get_buffer_backend() -> str:
    """Return the currently active buffer backend (``disk`` or ``memory``)."""
    return _current_backend


def set_buffer_backend(backend: str) -> None:
    """Switch the runtime buffer backend used by new stores."""
    global _current_backend  # noqa: PLW0603
    if backend not in VALID_BACKENDS:
        raise ValueError(f"Unknown buffer backend: {backend!r}")
    _current_backend = backend
    logger.info("Buffer backend set to %s", backend)


def _settings_path() -> Path:
    """Runtime settings file living next to the device DB directory."""
    cfg = get_config()
    return Path(cfg.core.device_db_dir).parent / "runtime_settings.json"


def load_persisted_backend() -> str:
    """Load the persisted backend choice, falling back to the config default."""
    default = getattr(get_config().buffer, "backend", "disk")
    path = _settings_path()
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read %s — using default buffer backend", path)
        return default
    backend = data.get("buffer_backend", default)
    return backend if backend in VALID_BACKENDS else default


def persist_backend(backend: str) -> None:
    """Persist the backend choice so it survives a restart."""
    if backend not in VALID_BACKENDS:
        raise ValueError(f"Unknown buffer backend: {backend!r}")
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    data["buffer_backend"] = backend
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def initialize_buffer_backend() -> str:
    """Load the persisted/config backend into module state (call at startup)."""
    backend = load_persisted_backend()
    set_buffer_backend(backend)
    return backend


def create_buffer_store(db_manager: DatabaseManager) -> BufferStore:
    """Build a buffer store for the current runtime backend.

    RAM mode uses the process-shared in-memory engine, so every store
    instance (BufferManager, ContextBuffer, ...) reads and writes the same
    in-process buffer. The durable ``MatchStats`` row is synced on flush so
    on-disk statistics stay coherent while the hot path stays RAM-only.
    """
    if _current_backend == "memory":
        return SqlAlchemyBufferStore(
            session_provider=memory_session,
            durable_stats_provider=db_manager.device_session,
        )
    return SqlAlchemyBufferStore(session_provider=db_manager.device_session)

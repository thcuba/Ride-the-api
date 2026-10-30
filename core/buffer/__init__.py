"""Buffer backend selection (disk vs in-process RAM) and store factory."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from core.atomic_io import write_json
from core.backend_names import BACKEND_ALIASES, VALID_BACKENDS, normalise_backend
from core.buffer.memory import dispose_memory_db, memory_session
from core.buffer.sqlalchemy_store import SqlAlchemyBufferStore
from core.config import get_config

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.buffer.store import BufferStore
    from core.database import DatabaseManager

logger = logging.getLogger(__name__)

__all__ = [
    "BACKEND_ALIASES",
    "VALID_BACKENDS",
    "dispose_memory_db",
    "memory_session",
    "SqlAlchemyBufferStore",
    "get_buffer_backend",
    "normalise_backend",
    "set_buffer_backend",
    "switch_backend",
    "load_persisted_backend",
    "persist_backend",
    "initialize_buffer_backend",
    "create_buffer_store",
]


# Module-level runtime choice. Defaults to the config value; overloaded at startup by the
# persisted runtime setting and changeable via the settings API.
_current_backend: str = "disk"


def get_buffer_backend() -> str:
    """Return the currently active buffer backend (``disk`` or ``memory``)."""
    return _current_backend


def set_buffer_backend(backend: str) -> None:
    """Switch the runtime buffer backend used by new stores.

    Accepts the documented aliases (``sqlite`` for ``disk``, ``ram`` for ``memory``).
    """
    global _current_backend  # noqa: PLW0603
    canonical = normalise_backend(backend)
    if canonical is None:
        raise ValueError(f"Unknown buffer backend: {backend!r}")
    _current_backend = canonical
    logger.info("Buffer backend set to %s", canonical)


def _settings_path() -> Path:
    """Runtime settings file living next to the device DB directory."""
    cfg = get_config()
    return Path(cfg.core.device_db_dir).parent / "runtime_settings.json"


def load_persisted_backend() -> str:
    """Load the persisted backend choice, falling back to the config default."""
    default = normalise_backend(getattr(get_config().buffer, "backend", "disk")) or "disk"
    path = _settings_path()
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read %s — using default buffer backend", path)
        return default
    chosen = normalise_backend(data.get("buffer_backend", default)) or normalise_backend(default)
    return chosen or "disk"


def persist_backend(backend: str) -> None:
    """Persist the backend choice so it survives a restart."""
    canonical = normalise_backend(backend)
    if canonical is None:
        raise ValueError(f"Unknown buffer backend: {backend!r}")
    backend = canonical
    path = _settings_path()
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    data["buffer_backend"] = backend
    write_json(path, data, indent=2)


def initialize_buffer_backend() -> str:
    """Load the persisted/config backend into module state (call at startup)."""
    backend = load_persisted_backend()
    set_buffer_backend(backend)
    return backend


def create_buffer_store(db_manager: DatabaseManager, backend: str | None = None) -> BufferStore:
    """Build a buffer store for the current runtime backend (or for ``backend``).

    RAM mode uses the process-shared in-memory engine, so every store
    instance (BufferManager, ContextBuffer, ...) reads and writes the same
    in-process buffer. The durable ``MatchStats`` row is synced on flush so
    on-disk statistics stay coherent while the hot path stays RAM-only.

    Passing ``backend`` builds a store for a backend that is not the active one — the
    copy step of a switch needs the old store and the new one at the same time.
    """
    if (normalise_backend(backend) or _current_backend) == "memory":
        return SqlAlchemyBufferStore(
            session_provider=memory_session,
            durable_stats_provider=db_manager.device_session,
        )
    return SqlAlchemyBufferStore(
        session_provider=db_manager.device_session,
        device_ids_provider=db_manager.list_device_ids_with_storage,
    )


async def switch_backend(
    db_manager: DatabaseManager,
    backend: str,
    *,
    rebind: Callable[[BufferStore], None] | None = None,
) -> dict[str, int | str]:
    """Switch the buffer backend **without losing the pairs in flight**.

    Flipping the flag and dropping the live buffers — which is what this used to do — threw
    away whatever had not been flushed yet when leaving RAM mode, and left the durable rows
    unflushed when leaving the SQLite backend, ready to be counted twice on the way back.

    Order matters: the pairs are copied into the new backend *first* (with the old backend
    still active, so a failure leaves everything where it was), then the old store is marked
    flushed, then the runtime choice is flipped and persisted, and finally every live buffer
    is repointed at the new store. A pair is never buffered twice and never disappears.

    Returns a summary ``{backend, from, devices, pairs}``.
    """
    canonical = normalise_backend(backend)
    if canonical is None:
        raise ValueError(f"Unknown buffer backend: {backend!r}")

    previous = _current_backend
    if canonical == previous:
        return {"backend": canonical, "from": previous, "devices": 0, "pairs": 0}

    old_store = create_buffer_store(db_manager, backend=previous)
    new_store = create_buffer_store(db_manager, backend=canonical)

    devices = 0
    pairs = 0
    for device_id in await old_store.pending_device_ids():
        pending = await old_store.get_buffer_pairs(device_id)
        if not pending:
            continue
        for entry in pending:
            await new_store.add_pair(device_id, entry["pair"], int(entry.get("size") or 0))
            pairs += 1
        devices += 1
        # Only after a successful copy: leaving these unflushed would resurrect them.
        await old_store.flush(device_id)

    set_buffer_backend(canonical)
    persist_backend(canonical)
    if rebind is not None:
        rebind(new_store)

    logger.info(
        "Buffer backend switched %s -> %s carrying %d pair(s) from %d device(s)",
        previous,
        canonical,
        pairs,
        devices,
    )
    return {"backend": canonical, "from": previous, "devices": devices, "pairs": pairs}

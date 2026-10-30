"""The names of the buffer backends, in one place that nothing else has to import.

``core.config`` validates ``buffer.backend`` and ``core.buffer`` selects the store, so the
mapping cannot live in either package: importing ``core.buffer.store`` pulls in the
``core.buffer`` package, whose ``__init__`` reads the config, and the two would deadlock.
"""

from __future__ import annotations

from typing import Final

#: Canonical names. ``disk`` **is** the SQLite backend: buffered pairs live in the device
#: database and survive a restart. ``memory`` is a process-shared in-memory engine, faster
#: and gone with the process.
VALID_BACKENDS: Final = ("disk", "memory")

#: Accepted spellings, so the documented name and the configured name cannot drift apart.
BACKEND_ALIASES: Final[dict[str, str]] = {
    "disk": "disk",
    "sqlite": "disk",
    "memory": "memory",
    "ram": "memory",
}


def normalise_backend(value: object) -> str | None:
    """Map a user-supplied backend name onto its canonical form, or ``None`` if unknown."""
    return BACKEND_ALIASES.get(str(value or "").strip().lower())

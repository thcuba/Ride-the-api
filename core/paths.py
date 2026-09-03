"""Path helpers that work in both source and frozen (PyInstaller) layouts.

In a PyInstaller onedir bundle, data files land in ``_internal/<rel>`` while
``__file__`` of modules loaded from the PYZ archive points inside
``_internal/PYZ-00.pyz/...``. Any ``Path(__file__).parent``-relative lookup for
a bundled data file therefore breaks in frozen builds. These helpers resolve
data files from the bundle root (``sys._MEIPASS``) and fall back to the source
tree for dev runs.
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """True when running inside a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path | None:
    """Return the PyInstaller bundle root (``_internal``) or None in source runs."""
    if not is_frozen():
        return None
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(sys.executable).resolve().parent


def resource_path(rel: str) -> Path:
    """Resolve a bundled data file, falling back to the source-tree layout.

    Args:
        rel: Bundle-relative path, e.g. ``"webui"`` or
            ``"core/pattern_db/schemas"``.

    Returns:
        The path to the resource. In frozen builds the first candidate that
        exists is returned (defensive against ``_MEIPASS`` pointing at the
        ``_internal`` dir itself vs. its parent); in source runs the repo root
        (two levels above ``core/``) is used.
    """
    root = bundle_root()
    if root is not None:
        candidates = [root / rel]
        if root.name != "_internal":
            candidates.append(root / "_internal" / rel)
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0]
    return Path(__file__).resolve().parent.parent / rel

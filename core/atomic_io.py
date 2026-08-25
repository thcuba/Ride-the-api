"""
Atomic, crash-safe file I/O helpers.

Guarantees that data on disk is never left in a half-written state:

- ``write_text`` / ``write_json`` write to a temp file in the same directory,
  fsync it, then ``os.replace`` over the destination (atomic on POSIX and
  Windows). Readers either see the old complete file or the new complete file.
- ``append_jsonl`` appends a JSON line with a flush+fsync so an audit /
  capture log survives abrupt termination without losing the last record.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

# Path separators / traversal tokens that must never appear in a path component
# derived from external input.  This mirrors the allowlist validation used by the
# cert manager: anything outside the safe set raises instead of silently
# rewriting, so the guard is clearly visible to static analysis.
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")


def sanitize_filename_component(value: str) -> str:
    """Return ``value`` safe for use as a single path/DB component.

    Raises ``ValueError`` unless ``value`` is entirely made of letters, digits,
    dot, underscore and hyphen (the same allowlist the cert manager uses for
    hostnames).  Path separators, traversal tokens (``..``), NUL and control
    characters are rejected outright so an externally supplied device id can
    never escape the directory it is placed in.
    """
    if not value:
        raise ValueError("Empty filename component")
    if not _SAFE_FILENAME_RE.fullmatch(value):
        raise ValueError(f"Unsafe filename component: {value!r}")
    return value


def _validate_destination(path: Path) -> Path:
    """Reject destinations that could escape their intended location.

    Blocks NUL bytes and parent-directory traversal (``..``) before any file
    operation, so tainted values can never redirect writes outside the
    directory tree the caller intended.
    """
    raw = str(path)
    if "\x00" in raw:
        raise ValueError(f"Unsafe path (NUL byte): {path!s}")
    if ".." in PurePosixPath(Path(os.path.normpath(raw)).as_posix()).parts:
        raise ValueError(f"Unsafe path (traversal): {path!s}")
    # The destination name must itself be a single, non-empty safe component.
    safe = path.name
    if not safe or safe in (".", "..") or "/" in safe or "\\" in safe:
        raise ValueError(f"Unsafe path name: {path!s}")
    return path


def write_text(path: Path | str, data: str, encoding: str = "utf-8") -> None:
    """Atomically write ``data`` to ``path`` via temp file + fsync + replace."""
    dest = _validate_destination(Path(path)).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(dest.parent), prefix=f".{dest.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, dest)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_json(path: Path | str, obj: Any, indent: int | None = 2) -> None:  # noqa: ANN401
    """Atomically write ``obj`` as pretty JSON to ``path``."""
    write_text(path, json.dumps(obj, indent=indent, default=str) + "\n")


def append_jsonl(path: Path | str, obj: Any) -> None:  # noqa: ANN401
    """Crash-safe append of a single JSON line to a JSONL audit log."""
    dest = _validate_destination(Path(path))
    dest.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, default=str)
    with open(dest, "a", encoding="utf-8", newline="\n") as f:  # noqa: PTH123
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
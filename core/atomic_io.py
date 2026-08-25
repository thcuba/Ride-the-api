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

# Characters that are never allowed in a path component derived from external
# input.  Anything else (e.g. spaces) is preserved verbatim.
_UNSAFE_FILENAME_CHARS = re.compile(r'[^\w.\- ]')


def sanitize_filename_component(value: str) -> str:
    """Return ``value`` safe for use as a single path/DB component.

    Strips path separators (``/`` ``\\``), traversal tokens (``..``), NUL, and
    control characters so an externally supplied value (device id, client id)
    can never escape the directory it is placed in.
    """
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", value).strip()
    # Reject empty results outright, and prohibit traversal even after cleaning.
    if not cleaned or ".." in PurePosixPath("x/" + cleaned).parts:
        raise ValueError(f"Unsafe filename component: {value!r}")
    return cleaned


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
    return path


def write_text(path: Path | str, data: str, encoding: str = "utf-8") -> None:
    """Atomically write ``data`` to ``path`` via temp file + fsync + replace."""
    dest = _validate_destination(Path(path))
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
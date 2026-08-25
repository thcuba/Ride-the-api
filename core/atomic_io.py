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
import tempfile
from pathlib import Path
from typing import Any


def write_text(path: Path | str, data: str, encoding: str = "utf-8") -> None:
    """Atomically write ``data`` to ``path`` via temp file + fsync + replace."""
    dest = Path(path)
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
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, default=str)
    with open(dest, "a", encoding="utf-8", newline="\n") as f:  # noqa: PTH123
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
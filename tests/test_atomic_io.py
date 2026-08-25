"""Tests for core.atomic_io atomic/crash-safe file I/O helpers."""
import json
import os
from pathlib import Path

import pytest

from core.atomic_io import append_jsonl, write_json, write_text


def test_write_text_creates_dirs_and_content(tmp_path):
    dest = tmp_path / "nested" / "sub" / "file.txt"
    write_text(dest, "hello world")
    assert dest.read_text(encoding="utf-8") == "hello world"


def test_write_text_is_atomic_no_leftover_tmp(tmp_path):
    dest = tmp_path / "file.txt"
    write_text(dest, "v1")
    write_text(dest, "v2")
    assert dest.read_text(encoding="utf-8") == "v2"
    # No temp files left behind.
    assert list(tmp_path.glob(".*.tmp")) == []


def test_write_json_round_trip(tmp_path):
    dest = tmp_path / "data.json"
    write_json(dest, {"a": 1, "b": [1, 2]})
    assert json.loads(dest.read_text(encoding="utf-8")) == {"a": 1, "b": [1, 2]}


def test_write_json_overwrites_completely(tmp_path):
    dest = tmp_path / "data.json"
    write_json(dest, {"a": 1})
    write_json(dest, {"c": 3})
    # Old key must be gone (full overwrite, not append).
    assert json.loads(dest.read_text(encoding="utf-8")) == {"c": 3}


def test_append_jsonl_appends_multiple_lines(tmp_path):
    log = tmp_path / "modifications.jsonl"
    append_jsonl(log, {"id": 1})
    append_jsonl(log, {"id": 2})
    lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x]
    assert [line["id"] for line in lines] == [1, 2]


def test_append_jsonl_creates_parent_dirs(tmp_path):
    log = tmp_path / "logs" / "deep" / "audit.jsonl"
    append_jsonl(log, {"ok": True})
    assert log.exists()


def test_write_text_utf8_bytes(tmp_path):
    dest = tmp_path / "unicode.txt"
    write_text(dest, "café — émoji ✓")
    assert dest.read_text(encoding="utf-8") == "café — émoji ✓"
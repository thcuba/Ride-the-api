"""Unit tests for the headless launcher exit-code logic (gui_main)."""

from __future__ import annotations

import io
import sys
import threading

import gui_main


def _run_with_capture(server_failed: threading.Event) -> tuple[int, str]:
    """Call ``_headless_exit_code`` capturing the launcher log emitted."""
    buf = io.StringIO()

    class _Capture:
        def write(self, data: str) -> int:
            return buf.write(data)

        def flush(self) -> None:
            pass

    orig_stdout = sys.stdout
    sys.stdout = _Capture()  # noqa: A001
    try:
        code = gui_main._headless_exit_code(server_failed)
    finally:
        sys.stdout = orig_stdout
    return code, buf.getvalue()


def test_failed_server_returns_1_and_logs_unexpected_termination() -> None:
    failed = threading.Event()
    failed.set()
    code, log = _run_with_capture(failed)
    assert code == 1
    assert "terminated unexpectedly" in log


def test_clean_thread_exit_returns_0_and_logs_stopped() -> None:
    failed = threading.Event()  # not set -> clean exit
    code, log = _run_with_capture(failed)
    assert code == 0
    assert "thread exited" in log


def test_clean_thread_exit_returns_0_without_regression() -> None:
    failed = threading.Event()
    code, _ = _run_with_capture(failed)
    assert code == 0

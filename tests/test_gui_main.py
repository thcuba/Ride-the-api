"""Unit tests for the headless launcher exit-code logic (gui_main)."""

from __future__ import annotations

import io
import sys
import threading

import pytest
import uvicorn

import core.logging_config
import core.server
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


# --- a failed server start must reach the exit code (R-8) --------------------


@pytest.mark.parametrize(
    ("status", "failed"), [(1, True), (3, True), ("boom", True), (0, False), (None, False)]
)
def test_server_exit_status_reaches_the_launcher(monkeypatch, status, failed) -> None:
    """uvicorn ends a failed start with sys.exit(); a thread used to drop it."""

    def fake_main() -> None:
        raise SystemExit(status)

    monkeypatch.setattr(core.server, "main", fake_main)
    server_failed = threading.Event()
    thread = threading.Thread(target=gui_main._run_server, args=(server_failed.set,))
    thread.start()
    thread.join(timeout=5)
    assert server_failed.is_set() is failed
    code, _ = _run_with_capture(server_failed)
    assert code == (1 if failed else 0)


class _FakeUvicornServer:
    def __init__(self, _config) -> None:  # noqa: ANN001
        self.started = False
        self.should_exit = False


@pytest.mark.parametrize("outcome", ["exit-1", "returned-unstarted"])
def test_gui_controller_flags_a_failed_start(monkeypatch, outcome) -> None:
    class Server(_FakeUvicornServer):
        def run(self) -> None:
            if outcome == "exit-1":
                raise SystemExit(1)  # what uvicorn does when the bind fails

    monkeypatch.setattr(uvicorn, "Server", Server)
    monkeypatch.setattr(core.logging_config, "setup_logging", lambda **_kwargs: None)
    controller = gui_main.ServerController()
    controller.start()
    controller._thread.join(timeout=5)
    assert controller.error.is_set()

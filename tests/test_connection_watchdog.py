"""Tests for the outbound-connection watchdog (auto-heal of a wedged process)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

from core.config import ConnectionWatchdogConfig
from core.connection_watchdog import ConnectionWatchdog

_DEFAULT_MAX_FAILURES = 5
_DEFAULT_COOLDOWN = 600
_MIN_OK_CALLS = 2


class _Cfg:
    """Duck-typed watchdog config for the behaviour tests.

    The real ``ConnectionWatchdogConfig`` coerces interval/timeout to int
    (seconds), which forbids the tiny intervals these tests need to run fast.
    The watchdog reads its fields via ``self._cfg`` only, so behaviour is
    exercised against a plain object; the Pydantic defaults are asserted
    separately in ``test_config_defaults``.
    """

    def __init__(self, **kwargs: object) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


def _config(**overrides: object) -> _Cfg:
    base: dict[str, object] = dict(
        enabled=True,
        interval=0.01,
        timeout=5,
        max_failures=3,
        probe_url="http://127.0.0.1:1/probe",  # closed port: instant refusal
        restart_command=["systemctl", "--user", "restart", "ride-the-api"],
        cooldown=600,
    )
    base.update(overrides)
    return _Cfg(**base)


def _make_watchdog(**overrides: object) -> ConnectionWatchdog:
    return ConnectionWatchdog(_config(**overrides))  # type: ignore[arg-type]


def test_config_defaults() -> None:
    """The Pydantic config ships with sane, safe defaults (int seconds)."""
    cfg = ConnectionWatchdogConfig()
    assert cfg.enabled is True
    assert cfg.max_failures == _DEFAULT_MAX_FAILURES
    assert cfg.cooldown == _DEFAULT_COOLDOWN
    assert cfg.restart_command == ["systemctl", "--user", "restart", "ride-the-api"]


def test_probe_returns_false_on_transport_error() -> None:
    """A fresh client against a closed local port must be reported as failed
    (the exact signal the watchdog listens for) without needing network access."""
    wd = _make_watchdog(timeout=1)
    ok = asyncio.run(wd._probe())
    assert ok is False


def test_probe_returns_true_on_response_or_non_transport_error() -> None:
    """Any HTTP response (even 4xx) or a non-transport error proves the network
    path works — only transport/timeout errors count as a wedge."""
    wd = _make_watchdog(probe_url="http://127.0.0.1:1/nope")

    class _FakeClient:
        _error: Exception | None = None

        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, _url: str) -> None:
            if self._error is not None:
                raise self._error

    class _FakeHttpxModule:
        AsyncClient = _FakeClient
        TransportError = OSError
        TimeoutException = TimeoutError

    with patch("core.connection_watchdog.httpx", _FakeHttpxModule):
        for err in (None, ValueError("unexpected")):
            _FakeClient._error = err
            assert asyncio.run(wd._probe()) is True


def test_successful_probe_resets_failure_count() -> None:
    """A single success after some failures must reset the counter."""
    wd = _make_watchdog(max_failures=2)
    calls = {"n": 0}
    restarts = {"n": 0}

    async def flaky_probe() -> bool:
        calls["n"] += 1
        return calls["n"] != 1  # first probe fails, then everything succeeds

    async def fake_restart() -> None:
        restarts["n"] += 1

    async def scenario() -> None:
        wd._probe = flaky_probe  # type: ignore[method-assign]
        wd._trigger_restart = fake_restart  # type: ignore[method-assign]
        # A healthy watchdog loop never stops on its own, so time it out.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(wd._run(), timeout=0.2)

    asyncio.run(scenario())
    assert calls["n"] >= _MIN_OK_CALLS
    assert restarts["n"] == 0
    assert wd._failures == 0


def test_persistent_failures_trigger_restart_once() -> None:
    """After max_failures consecutive transport errors the restart fires and the
    loop stops (the process is being replaced)."""
    wd = _make_watchdog(max_failures=3)
    restarts = {"n": 0}

    async def failing_probe() -> bool:
        return False

    async def fake_restart() -> None:
        restarts["n"] += 1

    async def fresh_process_gets_through() -> bool:
        return True  # the wedge: only this process is cut off

    async def scenario() -> None:
        wd._probe = failing_probe  # type: ignore[method-assign]
        wd._reachable_from_fresh_process = fresh_process_gets_through  # type: ignore[method-assign]
        wd._trigger_restart = fake_restart  # type: ignore[method-assign]
        await asyncio.wait_for(wd._run(), timeout=5)

    asyncio.run(scenario())
    assert restarts["n"] == 1
    assert wd._failures == 0


def test_cooldown_suppresses_restart() -> None:
    """A restart right after a previous one is suppressed (no reboot loop)."""
    wd = _make_watchdog(max_failures=2)
    restarts = {"n": 0}

    async def failing_probe() -> bool:
        return False

    async def fake_restart() -> None:
        restarts["n"] += 1

    async def scenario() -> None:
        wd._probe = failing_probe  # type: ignore[method-assign]
        wd._trigger_restart = fake_restart  # type: ignore[method-assign]
        wd._last_restart = time.time()  # a restart just happened
        task = asyncio.create_task(wd._run())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert restarts["n"] == 0
    assert wd._failures >= wd._cfg.max_failures  # still wedged, just not restarted


def test_empty_restart_command_launches_nothing() -> None:
    """With restart_command: [] the watchdog must not spawn anything (log only)."""
    wd = _make_watchdog(max_failures=1, restart_command=[])

    async def failing_probe() -> bool:
        return False

    async def scenario() -> None:
        wd._probe = failing_probe  # type: ignore[method-assign]
        wd._reachable_from_fresh_process = _always  # type: ignore[method-assign]
        await asyncio.wait_for(wd._run(), timeout=5)

    with patch("core.connection_watchdog.subprocess.Popen") as mock_popen:
        asyncio.run(scenario())
    mock_popen.assert_not_called()


def test_restart_command_strips_loader_paths_and_sets_runtime_dir() -> None:
    """The spawned restart must not inherit PyInstaller LD_LIBRARY_PATH and must
    carry XDG_RUNTIME_DIR so `systemctl --user` can talk to the manager."""
    wd = _make_watchdog()

    async def scenario() -> None:
        await wd._trigger_restart()

    with (
        patch("core.connection_watchdog.subprocess.Popen") as mock_popen,
        patch.dict(
            "os.environ",
            {"LD_LIBRARY_PATH": "/bundle/_internal", "HOME": "/home/pi4ride"},
            clear=True,
        ),
    ):
        asyncio.run(scenario())
    mock_popen.assert_called_once()
    env = mock_popen.call_args.kwargs["env"]
    assert "LD_LIBRARY_PATH" not in env
    assert env.get("XDG_RUNTIME_DIR") == f"/run/user/{os.geteuid()}"
    assert env.get("HOME") == "/home/pi4ride"


# --- offline installs and restarts that replace the process (R-4) -----------


async def _never() -> bool:
    return False


async def _always() -> bool:
    return True


def _run_briefly(wd: ConnectionWatchdog, *, fresh, seconds: float = 0.1) -> int:  # noqa: ANN001
    """Run the loop with a failing in-process probe; return how many restarts fired."""
    restarts = {"n": 0}

    async def fake_restart() -> None:
        restarts["n"] += 1

    async def scenario() -> None:
        wd._probe = _never  # type: ignore[method-assign]
        wd._reachable_from_fresh_process = fresh  # type: ignore[method-assign]
        wd._trigger_restart = fake_restart  # type: ignore[method-assign]
        task = asyncio.create_task(wd._run())
        await asyncio.sleep(seconds)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    return restarts["n"]


def test_offline_network_does_not_restart() -> None:
    """A fresh process cannot get through either: a restart would not help."""
    assert _run_briefly(_make_watchdog(max_failures=2), fresh=_never) == 0


def test_cooldown_outlives_the_restart(tmp_path) -> None:  # noqa: ANN001
    state = tmp_path / "watchdog-state.json"
    first = ConnectionWatchdog(_config(max_failures=1), state_path=state)  # type: ignore[arg-type]
    assert _run_briefly(first, fresh=_always) == 1
    assert state.exists()

    # The restart replaced the process: the new watchdog must still cool down.
    second = ConnectionWatchdog(_config(max_failures=1), state_path=state)  # type: ignore[arg-type]
    assert _run_briefly(second, fresh=_always) == 0


def test_a_restart_stamp_from_the_future_is_ignored(tmp_path) -> None:  # noqa: ANN001
    state = tmp_path / "watchdog-state.json"
    state.write_text(json.dumps({"last_restart": time.time() + 86_400}))  # clock moved back
    wd = ConnectionWatchdog(_config(max_failures=1), state_path=state)  # type: ignore[arg-type]
    assert _run_briefly(wd, fresh=_always) == 1


class _NotFound(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(404)
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        pass


@pytest.mark.parametrize("use_curl", [True, False])
def test_fresh_process_probe_tells_a_response_from_no_route(use_curl, monkeypatch) -> None:  # noqa: ANN001
    if use_curl and not shutil.which("curl"):
        pytest.skip("curl not installed")
    if not use_curl:
        monkeypatch.setattr("core.connection_watchdog.shutil.which", lambda _name: None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NotFound)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        up = _make_watchdog(probe_url=f"http://127.0.0.1:{server.server_port}/probe")
        down = _make_watchdog(probe_url="http://127.0.0.1:1/probe")
        assert asyncio.run(up._reachable_from_fresh_process()) is True  # a 404 is a response
        assert asyncio.run(down._reachable_from_fresh_process()) is False
    finally:
        server.shutdown()
        server.server_close()

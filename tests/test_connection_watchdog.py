"""Tests for the outbound-connection watchdog (auto-heal of a wedged process)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from unittest.mock import patch

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

    async def scenario() -> None:
        wd._probe = failing_probe  # type: ignore[method-assign]
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
        wd._last_restart = time.monotonic()  # a restart just happened
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

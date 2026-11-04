"""Outbound-connection watchdog that auto-heals a wedged process.

Long-running installs occasionally enter a state where EVERY outgoing HTTP
request fails with ``Connection error.`` (httpx.TransportError) while local
listeners keep serving and the same call succeeds from a shell. The failure is
not provider/credential-specific and is not cleared by recreating one client — a
process restart is required. This module runs a periodic check that detects that
state and restarts the process so a long-lived box recovers on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from typing import TYPE_CHECKING

from core.atomic_io import write_json

# httpx is a hard dependency of the project; the try/except only keeps the
# module importable in the rare case a build omits it (optional at import time).
try:
    import httpx
except ImportError:  # pragma: no cover - exercised only when httpx is absent
    httpx = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from pathlib import Path

    from core.config import ConnectionWatchdogConfig

logger = logging.getLogger(__name__)

# PyInstaller sets these to point at its bundled libssl/libcrypto, which breaks
# spawned systemd/curl. Always strip them from the restart child environment.
_LOADER_PATH_ENV = ("LD_LIBRARY_PATH", "LD_PRELOAD", "DYLD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES")


def _xdg_runtime_dir() -> str:
    """Return the per-user systemd runtime dir so ``systemctl --user`` works."""
    return f"/run/user/{os.geteuid()}"


def _child_env() -> dict[str, str]:
    """Environment for spawned helpers: no bundled loader paths, a runtime dir."""
    env = {k: v for k, v in os.environ.items() if k not in _LOADER_PATH_ENV}
    env["XDG_RUNTIME_DIR"] = env.get("XDG_RUNTIME_DIR") or _xdg_runtime_dir()
    return env


# One-shot probe for a fresh interpreter: exit 0 on any HTTP response (even an
# error status), 1 when the request cannot get through. Proxies are ignored,
# like the in-process probe (trust_env=False).
_PY_PROBE = """
import sys, urllib.error, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    opener.open(sys.argv[1], timeout=float(sys.argv[2]))
except urllib.error.HTTPError:
    pass
except Exception:
    sys.exit(1)
"""


class ConnectionWatchdog:
    """Periodically probe outbound reachability and restart on persistent failure.

    A probe with a FRESH ``httpx.Client`` is used deliberately: when the process
    is wedged, even a newly-created client cannot connect, so a repeated
    transport error is a reliable signal that the whole outbound stack (not just
    one cached client) is broken. Any HTTP response — including a 4xx/3xx — proves
    the transport path works, so only transport/timeout errors count as failures.
    """

    def __init__(self, config: ConnectionWatchdogConfig, state_path: Path | None = None) -> None:
        self._cfg = config
        self._failures = 0
        # The last restart (wall clock) is kept in ``state_path``: the restart
        # replaces this process, so an in-memory stamp never reached the next
        # one and the cooldown never applied -- an offline install restarted
        # every few minutes forever. ``-inf`` = no previous restart.
        self._state_path = state_path
        self._last_restart = self._load_last_restart()
        self._task: asyncio.Task | None = None

    def _load_last_restart(self) -> float:
        if self._state_path is None:
            return float("-inf")
        try:
            return float(json.loads(self._state_path.read_text(encoding="utf-8"))["last_restart"])
        except (OSError, ValueError, KeyError, TypeError):
            return float("-inf")

    def _record_restart(self, now: float) -> None:
        self._last_restart = now
        if self._state_path is None:
            return
        try:
            write_json(self._state_path, {"last_restart": now})
        except OSError:
            logger.warning(
                "Connection watchdog: could not record the restart in %s", self._state_path
            )

    def _cooling_down(self, now: float) -> bool:
        # A stamp from the future (the clock moved back) is not trusted.
        return 0 <= now - self._last_restart < self._cfg.cooldown

    async def start(self) -> None:
        """Begin the periodic probe loop as a background task."""
        self._task = asyncio.create_task(self._run(), name="connection-watchdog")

    async def stop(self) -> None:
        """Cancel the probe loop."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.interval)
            ok = await self._probe()
            if ok:
                self._failures = 0
                continue

            self._failures += 1
            logger.warning(
                "Connection watchdog: probe failed (%d/%d) %s",
                self._failures,
                self._cfg.max_failures,
                self._cfg.probe_url,
            )
            if self._failures < self._cfg.max_failures:
                continue

            # Only a restart clears the wedge; but never restart onto a reboot
            # loop if the probe is unreachable for unrelated reasons.
            now = time.time()
            if self._cooling_down(now):
                logger.info(
                    "Connection watchdog: restart suppressed by cooldown (%ds)",
                    self._cfg.cooldown,
                )
                continue
            if await self._reachable_from_fresh_process() is False:
                # The wedge breaks outbound HTTP in this process only. When a
                # new process cannot get through either, the network itself is
                # down (an offline install, a dead uplink): a restart cannot help.
                logger.warning(
                    "Connection watchdog: %s is unreachable from a fresh process too; "
                    "the network is down, not this process -- not restarting",
                    self._cfg.probe_url,
                )
                self._failures = 0
                continue
            self._record_restart(now)
            self._failures = 0
            logger.critical(
                "Connection watchdog: outgoing HTTP is wedged (%d transport errors), "
                "restarting process",
                self._cfg.max_failures,
            )
            await self._trigger_restart()
            return

    async def _probe(self) -> bool:
        """Return True when a fresh outbound GET completes (any HTTP status)."""
        if httpx is None:
            return True
        try:
            async with httpx.AsyncClient(timeout=self._cfg.timeout, trust_env=False) as client:
                await client.get(self._cfg.probe_url)
        except (httpx.TransportError, httpx.TimeoutException):
            return False
        except Exception:  # noqa: BLE001 - a non-transport error (e.g. a 4xx body) is not the wedge
            return True
        else:
            return True

    def _fresh_probe_command(self) -> list[str] | None:
        """A command that probes ``probe_url`` from a new process, if there is one."""
        url, timeout = str(self._cfg.probe_url), str(self._cfg.timeout)
        curl = shutil.which("curl")
        if curl:
            return [curl, "-s", "-o", os.devnull, "--noproxy", "*", "--max-time", timeout, url]
        if not getattr(sys, "frozen", False):  # a frozen bundle has no interpreter to spawn
            return [sys.executable, "-I", "-c", _PY_PROBE, url, timeout]
        return None

    async def _reachable_from_fresh_process(self) -> bool | None:
        """Whether a new process reaches ``probe_url`` (None: cannot tell)."""
        cmd = self._fresh_probe_command()
        if cmd is None:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=_child_env(),
            )
        except Exception:  # noqa: BLE001 - cannot tell; never let the watchdog die
            logger.debug("Connection watchdog: fresh-process probe could not start", exc_info=True)
            return None
        try:
            return await asyncio.wait_for(proc.wait(), timeout=float(self._cfg.timeout) + 5) == 0
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            return False

    async def _trigger_restart(self) -> None:
        cmd = self._cfg.restart_command
        if not cmd:
            logger.critical(
                "Connection watchdog: restart_command is empty; cannot auto-restart — "
                "set it in config.yaml or disable the watchdog"
            )
            return
        env = _child_env()
        try:
            # Detached: start_new_session + explicit /dev/null stdio so the
            # child outlives this process's restart and nothing blocks on pipes.
            subprocess.Popen(
                cmd,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            logger.info("Connection watchdog: restart command launched: %s", cmd)
        except Exception:  # noqa: BLE001
            logger.exception("Connection watchdog: failed to launch restart command")

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
import logging
import os
import subprocess
import time
from typing import TYPE_CHECKING

# httpx is a hard dependency of the project; the try/except only keeps the
# module importable in the rare case a build omits it (optional at import time).
try:
    import httpx
except ImportError:  # pragma: no cover - exercised only when httpx is absent
    httpx = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from core.config import ConnectionWatchdogConfig

logger = logging.getLogger(__name__)

# PyInstaller sets these to point at its bundled libssl/libcrypto, which breaks
# spawned systemd/curl. Always strip them from the restart child environment.
_LOADER_PATH_ENV = ("LD_LIBRARY_PATH", "LD_PRELOAD", "DYLD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES")


def _xdg_runtime_dir() -> str:
    """Return the per-user systemd runtime dir so ``systemctl --user`` works."""
    return f"/run/user/{os.geteuid()}"


class ConnectionWatchdog:
    """Periodically probe outbound reachability and restart on persistent failure.

    A probe with a FRESH ``httpx.Client`` is used deliberately: when the process
    is wedged, even a newly-created client cannot connect, so a repeated
    transport error is a reliable signal that the whole outbound stack (not just
    one cached client) is broken. Any HTTP response — including a 4xx/3xx — proves
    the transport path works, so only transport/timeout errors count as failures.
    """

    def __init__(self, config: ConnectionWatchdogConfig) -> None:
        self._cfg = config
        self._failures = 0
        self._last_restart = 0.0
        self._task: asyncio.Task | None = None

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
            now = time.monotonic()
            if now - self._last_restart < self._cfg.cooldown:
                logger.info(
                    "Connection watchdog: restart suppressed by cooldown (%ds)",
                    self._cfg.cooldown,
                )
                continue
            self._last_restart = now
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

    async def _trigger_restart(self) -> None:
        cmd = self._cfg.restart_command
        if not cmd:
            logger.critical(
                "Connection watchdog: restart_command is empty; cannot auto-restart — "
                "set it in config.yaml or disable the watchdog"
            )
            return
        env = {k: v for k, v in os.environ.items() if k not in _LOADER_PATH_ENV}
        env["XDG_RUNTIME_DIR"] = env.get("XDG_RUNTIME_DIR") or _xdg_runtime_dir()
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

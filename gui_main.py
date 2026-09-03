"""GUI / headless launcher for the frozen ride-the-api bundle.

Frozen builds (PyInstaller onedir) start here instead of a bare console, so on
Windows the user gets a small control panel — server status, a dashboard link
and live logs — while the real configuration stays in the browser dashboard.
On Linux/macOS the default remains headless so the tarball keeps behaving like
the previous server binary; pass ``--gui`` to get the same window there.

Run modes
---------
* default (Windows)   -> tkinter control panel; the server auto-starts in the
                         background unless another instance already listens on
                         the configured port (e.g. the installed NSSM service).
* ``--service``       -> headless, no window; used by the Windows service
                         (NSSM) and other daemons.
* ``--headless``      -> alias of ``--service``.
* ``--gui``           -> force the tkinter panel (mainly useful on POSIX).
* ``--no-browser``    -> do not auto-open the dashboard when the server is up.

Data / config resolution
------------------------
All runtime state (``config.yaml``, databases, certs, logs, ``ridebase/``) is
resolved from a single writable "data dir" that the launcher makes the process
CWD, so every relative path in the codebase works no matter where the
executable was launched from:

  1. ``$RIDE_THE_API_DATA`` if set (used by the NSSM service installer);
  2. the current directory if it already contains ``config/config.yaml``
     (keeps the Linux/macOS tarball layout working unchanged);
  3. ``%LOCALAPPDATA%\\ride-the-api`` (Windows) or ``$XDG_DATA_HOME`` /
     ``~/.local/share/ride-the-api`` (POSIX).

A default ``config/config.yaml`` is seeded from the bundle on first run.
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import queue
import shutil
import socket
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import scrolledtext, ttk
except ImportError:  # tkinter missing (minimal POSIX installs)
    tk = None  # type: ignore[assignment]

if getattr(sys, "frozen", False):  # PyInstaller: enable multiprocessing in bundles
    multiprocessing.freeze_support()

DEFAULT_PORT = 8911


def _log(msg: str) -> None:
    """Print a launcher message without crashing when there is no console."""
    stream = getattr(sys, "stdout", None)
    if stream is None:
        try:
            stream = open(os.devnull, "w")  # noqa: SIM115
        except OSError:
            return
    try:
        stream.write(msg + "\n")
        stream.flush()
    except (OSError, ValueError):
        pass


def _bundled_path(rel: str) -> Path | None:
    """Resolve a data file inside the PyInstaller bundle (``_internal/``)."""
    if not getattr(sys, "frozen", False):
        return None
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / rel)
    exe_dir = Path(sys.executable).resolve().parent
    candidates += [exe_dir / "_internal" / rel, exe_dir / rel]
    for c in candidates:
        if c.exists():
            return c
    return None


def _default_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData/Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share"))
    return base / "ride-the-api"


def prepare_runtime() -> Path:
    """Pick the writable data dir, create it, seed the default config, chdir.

    Returns the data dir; the process CWD is changed onto it so the frozen
    binary resolves ``config/config.yaml`` and the relative ``./certs``,
    ``./data``, ``./ridebase`` paths regardless of the launch location.
    """
    override = os.environ.get("RIDE_THE_API_DATA")
    if override:
        data_dir = Path(override)
    elif (Path.cwd() / "config" / "config.yaml").exists():
        data_dir = Path.cwd()
    else:
        data_dir = _default_data_dir()

    data_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("config", "data", "certs", "logs", "ridebase"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)

    cfg = data_dir / "config" / "config.yaml"
    if not cfg.exists():
        bundled = _bundled_path("config/config.yaml")
        if bundled is not None:
            shutil.copyfile(bundled, cfg)
            _log(f"[launcher] Copies default config from bundle -> {cfg}")
        else:
            _log(f"[launcher] WARNING: config.yaml not found at {cfg}")

    os.chdir(data_dir)
    return data_dir


class LogWriter:
    """Thread-safe stdout/stderr target: appends to a file and, when a queue
    is supplied, pushes chunks to the GUI log widget."""

    def __init__(self, path: Path, q: queue.Queue | None = None) -> None:
        self._fh = path.open("a", encoding="utf-8", buffering=1)
        self._q = q
        self._lock = threading.Lock()

    def write(self, data: str) -> int:
        if not data:
            return 0
        with self._lock:
            self._fh.write(data)
            self._fh.flush()
        if self._q is not None:
            self._q.put(data)
        return len(data)

    def flush(self) -> None:
        with self._lock:
            self._fh.flush()

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return self._fh.fileno()

    def close(self) -> None:
        with self._lock:
            self._fh.close()


def _run_server(on_error) -> None:
    try:
        from core.server import main as server_main

        server_main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        if on_error is not None:
            on_error()


def run_headless(data_dir: Path) -> int:
    """Headless service/daemon mode: no window, logs to ``logs/ride-the-api.log``."""
    log_path = data_dir / "logs" / "ride-the-api.log"
    writer = LogWriter(log_path)
    sys.stdout = writer
    sys.stderr = writer
    _log(f"[launcher] Headless mode (data dir: {data_dir})")
    _log(f"[launcher] Logs: {log_path}")

    server_failed = threading.Event()
    thread = threading.Thread(
        target=_run_server,
        args=(lambda: server_failed.set(),),
        daemon=True,
        name="server",
    )
    thread.start()
    while thread.is_alive():
        thread.join(timeout=2.0)
    if server_failed.is_set():
        _log("[launcher] Server terminated unexpectedly, check the log for details.")
        return 1
        if not thread.is_alive():
            _log("[launcher] Server stopped (thread exited).")
        return 0


def _proxy_addr() -> tuple[str, int]:
    try:
        from core.config import get_config

        cfg = get_config()
        return cfg.proxy.host, cfg.proxy.port
    except Exception:
        return "0.0.0.0", DEFAULT_PORT


def _probe(host: str, port: int, timeout: float = 0.4) -> bool:
    """Return True if something already listens on host:port."""
    targets = [host] if host not in ("0.0.0.0", "::", "") else ["127.0.0.1", "::1"]
    for target in targets:
        try:
            with socket.create_connection((target, int(port)), timeout=timeout):
                return True
        except OSError:
            continue
    return False


class App:
    """Tiny tkinter control panel for the server."""

    def __init__(self, root: tk.Tk, data_dir: Path, args: argparse.Namespace) -> None:
        self.root = root
        self.data_dir = data_dir
        self.args = args
        self.log_q: queue.Queue[str] = queue.Queue()
        self.online = False
        self.browser_opened = False

        self.host, self.port = _proxy_addr()
        self.url = f"http://127.0.0.1:{self.port}"
        self.need_start = not _probe(self.host, self.port)

        root.title("ride-the-api")
        root.geometry("720x460")
        root.minsize(480, 320)

        top = ttk.Frame(root, padding=(10, 8))
        top.pack(fill=tk.X)

        self.status = tk.Label(top, text="", font=("Segoe UI", 10))
        self.status.pack(side=tk.LEFT)

        dash = ttk.Button(top, text="Apri dashboard", command=self.open_dashboard)
        dash.pack(side=tk.RIGHT, padx=(6, 0))

        ttk.Button(top, text="Esci", command=self.do_exit).pack(side=tk.RIGHT)

        self.log = scrolledtext.ScrolledText(root, state=tk.DISABLED, wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self._start_log_capture()
        self._set_state("starting" if self.need_start else "external")
        if self.need_start:
            self._thread = threading.Thread(
                target=_run_server,
                args=(lambda: self.root.after(0, lambda: self._set_state("error")),),
                daemon=True,
                name="server",
            )
            self._thread.start()
        root.protocol("WM_DELETE_WINDOW", self.do_exit)
        self._poll()

    def _start_log_capture(self) -> None:
        writer = LogWriter(self.data_dir / "logs" / "ride-the-api.log", self.log_q)
        sys.stdout = writer
        sys.stderr = writer

    def _set_state(self, state: str) -> None:
        if state == "starting":
            text, fg = "Avvio del server...", "#b8860b"
        elif state == "external":
            text, fg = "Server gi\u00e0 in esecuzione", "#2e8b57"
        elif state == "online":
            text, fg = f"Online su {self.url}", "#2e8b57"
        elif state == "error":
            text, fg = "Errore di avvio (vedi log)", "#c0392b"
        else:
            text, fg = state, "#333333"
        self.status.config(text=text, foreground=fg)

    def open_dashboard(self, background: bool = False) -> None:
        if background:
            threading.Thread(target=lambda: webbrowser.open(self.url), daemon=True).start()
        else:
            webbrowser.open(self.url)

    def do_exit(self) -> None:
        try:
            self.root.destroy()
        finally:
            # Both the server thread and tkinter are daemon-ish; force-exit so a
            # stuck uvicorn loop cannot leave an orphan process around.
            os._exit(0)  # noqa: SLF001

    def _poll(self) -> None:
        self._drain_log()
        if self.need_start and not self.online and _probe(self.host, self.port):
            self.online = True
            self._set_state("online")
            if not self.browser_opened:
                self.browser_opened = True
                if not self.args.no_browser:
                    self.open_dashboard(background=True)
        self.root.after(250, self._poll)

    def _drain_log(self) -> None:
        try:
            while True:
                text = self.log_q.get_nowait()
                self._append(text)
        except queue.Empty:
            pass

    def _append(self, text: str) -> None:
        self.log.config(state=tk.NORMAL)
        self.log.insert(tk.END, text)
        if int(self.log.index("end-1c").split(".")[0].split("-")[0]) > 2000:
            self.log.delete("1.0", "100.0")
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)


def get_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ride-the-api",
        description="ride-the-api launcher (GUI on Windows, headless elsewhere)",
    )
    parser.add_argument("--service", action="store_true", help="run headless as a service/daemon")
    parser.add_argument("--headless", action="store_true", help="alias of --service")
    parser.add_argument("--gui", action="store_true", help="force the tkinter control panel")
    parser.add_argument("--no-browser", action="store_true", help="do not auto-open the dashboard")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = get_args(argv)
    try:
        data_dir = prepare_runtime()
    except Exception:
        traceback.print_exc()
        return 1
    os.chdir(data_dir)

    if args.service or args.headless:
        return run_headless(data_dir)

    want_gui = args.gui or os.name == "nt"
    if want_gui:
        if tk is None:
            _log("[launcher] tkinter non disponibile: avvio in modalit\u00e0 headless")
            return run_headless(data_dir)
        try:
            root = tk.Tk()
        except Exception as e:  # e.g. no display on headless POSIX
            _log(f"[launcher] Impossibile aprire la finestra GUI ({e}): avvio headless")
            return run_headless(data_dir)
        App(root, data_dir, args)
        root.mainloop()
        return 0
    return run_headless(data_dir)


def entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entry()

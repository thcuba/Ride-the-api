# Deployment on Windows (native server + installer)

This document explains how to run **ride-the-api** as a native Windows service
and how to build the Windows installer. It is complementary to
[`deployment.md`](deployment.md), which covers Linux/Docker.

> **Scope of the Windows build**: the exact same project
> (`core/server.py`, API on `:8911`, TLS-MITM listeners on the configured
> ports) packaged with **PyInstaller** (onedir) and distributed as an
> **Inno Setup installer** that registers the server as an auto-start
> **Windows service** (NSSM).

---

## 1. What the installer provides

- The `ride-the-api.exe` server and its runtime.
- A **tkinter control panel** (the exe entry point is `gui_main.py`): server
  status, an "Open dashboard" button and a live log view. No console window.
  Configuration still happens in the browser dashboard.
- A Windows service `ride-the-api` set to **auto-start** (via NSSM), running
  headless (`--service`) with working directory `%ProgramData%\ride-the-api`.
- Logs written to the data dir (`logs/ride-the-api.log`, service mode) or
  `logs\service-*.log` (NSSM redirection).

### Data / config resolution

`gui_main.py` resolves a single writable **data dir** and makes it the process
CWD before the server starts, so the frozen binary works no matter where it
was launched from:

1. `$RIDE_THE_API_DATA` if set — used by the NSSM service (points at
   `%ProgramData%\ride-the-api`);
2. the current directory if it already contains `config/config.yaml` (keeps the
   Linux/macOS tarball layout working unchanged);
3. `%LOCALAPPDATA%\ride-the-api` (Windows) or `$XDG_DATA_HOME`/`~/.local/share`
   (POSIX).

A default `config/config.yaml` is **seeded from the bundle** (`_internal`) on
first run if the data dir has none.

## 2. Building the installer (from a Windows machine / CI)

PyInstaller does **not** cross-compile: the `.exe` must be produced on
Windows. Two paths:

### A) GitHub Action (recommended)

A push of a `v*` tag (or a manual `workflow_dispatch`) runs
`.github/workflows/build-platforms.yml`, a matrix workflow that builds on
**every platform's own hosted runner** (PyInstaller cannot cross-compile):
Linux x64, Linux arm64, Windows x64, macOS arm64.

Each job:

1. Installs the package + PyInstaller (`python -m pip install -e . pyinstaller`).
2. Builds the PyInstaller `onedir` via `packaging\rta.spec`.
3. Packages: **Windows** → compiles `packaging\windows\setup.iss` with Inno
   Setup to `ride-the-api-windows-x64-setup.exe`; **Linux/macOS** → `tar.gz`
   of the onedir bundle.
4. Uploads the asset as an artifact; on a `v*` tag it is also attached to the
   GitHub Release under a per-platform name.

> Note: macOS Intel hosted runners were retired (Dec 2025); macOS ships
> **arm64 (Apple Silicon)** only.

### B) Local build

On a Windows PC with Python 3.11:

```powershell
# 1. PyInstaller onedir
powershell -ExecutionPolicy Bypass -File packaging\windows\pyinstaller_build.ps1

# 2. Installer (requires Inno Setup 6, iscc on PATH)
iscc packaging\windows\setup.iss
```

Output: `dist\installer\ride-the-api-setup.exe`.

## 3. Installing / running

Run `ride-the-api-setup.exe` as **Administrator** (the service registration and
`%ProgramData%` seeding need elevation). The installer:

1. Installs the app into `%ProgramFiles%\ride-the-api`.
2. Runs `packaging\install_service.ps1` (bundled NSSM) which registers and
   starts the `ride-the-api` service (`--service`, headless).
3. Opens the dashboard at <http://localhost:8911>.

To use the **control panel** instead of (or in addition to) the service, launch
`ride-the-api.exe` (Start Menu → "ride-the-api"). If the service is already
listening on `:8911` the panel just shows "Server già in esecuzione" and the
dashboard button; otherwise it starts its own server instance. Command-line
switches: `--service`/`--headless` (no window), `--gui`, `--no-browser`.

> Note: avoid running the control panel and the NSSM service at the same time
> — both would try to bind `:8911`. If the service is enabled, use the panel
> only as a dashboard launcher.

### Managing the service

```powershell
# Status
Get-Service ride-the-api
# Restart / stop via NSSM
& "$env:TEMP\nssm\nssm.exe" restart ride-the-api
& "$env:TEMP\nssm\nssm.exe" stop ride-the-api
```

Logs: `%ProgramData%\ride-the-api\logs\`.

## 4. Important Windows-specific considerations

### Transparent TLS-MITM interception

On Linux the proxy is wired into traffic via **dnsmasq + iptables
REDIRECT**. Windows has **no iptables**; the transparent interception of the
MITM listeners requires **WinDivert / WFP** and is **not** currently
implemented. On Windows ride-the-api behaves as a **server**: point your
device's cloud hostnames at the PC's IP via your **DNS resolver** (dnsmasq /
Pi-hole / router DNS override) and, optionally, configure the devices to use
the PC as a gateway/proxy. The generated CA must be **trusted on the client
devices**.

Port 443 is frequently taken on Windows (IIS, other servers). Check and free
it, or reconfigure `config.yaml` (`tls_decrypt.listen_ports`) to an alternate.

### Paths and permissions

- Data/state live under `%ProgramData%\ride-the-api` (shared, machine-wide),
  not in the `Program Files` install tree, to avoid write-permission issues
  with the service account (typical user has no write to `%PF%`).
- The service runs under the default **LocalSystem** account unless configured
  otherwise; adjust `install_service.ps1` (`ObjectName`) if you prefer a
  dedicated service account.

### Dependencies excluded from the build

`onnxruntime`, `numpy`, `asyncpg`, and `psycopg2` are excluded from the
bundle: onnxruntime is dead scaffolding (never imported by the code) and the
DB uses the async SQLite backend.

## 5. Existing firewall / antivirus notes

- Allow inbound to the API port (`8911`) and the TLS-MITM listen ports the
  first time your PC starts the service.
- Some AV products flag fresh PyInstaller executables. Sign the binary or add
  an exclusion if your AD drains performance.

## 6. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Service starts then stops | Check `%ProgramData%\ride-the-api\logs\service-stderr.log`; most often a port already in use or a missing `config/`. |
| Can't reach `localhost:8911` | Service not running: `Get-Service ride-the-api`; start via NSSM. |
| Devices can't reach the cloud | DNS not pointing the device's cloud hostname at the PC; re-check DNS override and that Port 8911 / TLS ports listen. |
| CA not trusted on devices | Install the generated CA (`certs/ca.pem`) into the device's trust store. |
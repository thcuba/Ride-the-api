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
- A default `config/config.yaml` copied into the working data folder.
- A Windows service `ride-the-api` set to **auto-start** (via NSSM),
  running with working directory `%ProgramData%\ride-the-api`.
- Logs written to `%ProgramData%\ride-the-api\logs\`.

The server is configured just like on Linux. Because `config_manager` is a
singleton loading `config/config.yaml` relative to the process working
directory (`core/config.py`), the service's `AppDirectory` is set to
`%ProgramData%\ride-the-api` so the relative `./data`, `./certs` paths keep
working unchanged.

## 2. Building the installer (from a Windows machine / CI)

PyInstaller does **not** cross-compile: the `.exe` must be produced on
Windows. Two paths:

### A) GitHub Action (recommended)

A push of a `v*` tag (or a manual `workflow_dispatch`) runs
`.github/workflows/build-windows.yml` on a `windows-latest` runner:

1. Builds the PyInstaller `onedir` via `packaging\windows\pyinstaller_build.ps1`.
2. Installs Inno Setup (chocolatey) and compiles `packaging\windows\setup.iss`.
3. Uploads `ride-the-api-setup.exe` as an artifact; on a tag it is also
   attached to the GitHub Release.

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
2. Copies the default config and creates `data/`, `certs/`, `logs/` under
   `%ProgramData%\ride-the-api`.
3. Runs `packaging\install_service.ps1` (bundled NSSM) which registers and
   starts the `ride-the-api` service.

Open the dashboard at <http://localhost:8911>.

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
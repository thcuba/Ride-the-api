# Build script for building the Windows executable with PyInstaller.
# Usage: powershell -ExecutionPolicy Bypass -File packaging/windows/pyinstaller_build.ps1
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $RepoRoot

Write-Host "==> Creating virtualenv"
if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

Write-Host "==> Installing package + PyInstaller"
& ".venv\Scripts\python.exe" -m pip install --upgrade pip
& ".venv\Scripts\python.exe" -m pip install -e . "pyinstaller>=6.0"

Write-Host "==> Running PyInstaller (onedir)"
& ".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean packaging\rta.spec

if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller failed with exit code $LASTEXITCODE"
    exit 1
}

$Out = Join-Path $RepoRoot "dist\ride-the-api\ride-the-api.exe"
if (-not (Test-Path $Out)) {
    Write-Error "Expected output not found: $Out"
    exit 1
}

Write-Host "==> Build OK: $Out"
Write-Host "==> dist\ride-the-api ready for Inno Setup"
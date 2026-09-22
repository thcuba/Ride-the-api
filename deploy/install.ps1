# Ride the API — Windows installer
# --------------------------------
# Downloads the prebuilt Inno-Setup installer for the latest GitHub release of
# thcuba/Ride-the-api and runs it. Rerunning upgrades to the newest release.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\deploy\install.ps1        # latest
#   powershell -ExecutionPolicy Bypass -File .\deploy\install.ps1 -Version 1.1
#   powershell -ExecutionPolicy Bypass -File .\deploy\install.ps1 -Silent
#
# Param:
#   -Version <tag>  Install a specific release tag (default: latest).
#   -Silent         Run the installer silently (no GUI prompts).

param(
    [string]$Version = "latest",
    [switch]$Silent
)

$ErrorActionPreference = "Stop"

$Repo   = "thcuba/Ride-the-api"
$Base   = "https://github.com/${Repo}/releases"
$Asset  = "ride-the-api-windows-x64-setup.exe"

if ($Version -eq "latest") {
    $Url  = "${Base}/latest/download/${Asset}"
    $Tag  = "latest"
} else {
    $Url  = "${Base}/download/${Version}/${Asset}"
    $Tag  = $Version
}

$Out = Join-Path $env:TEMP $Asset

Write-Host "Downloading ${Asset} (${Tag}) from ${Repo} ..."
Invoke-WebRequest -Uri $Url -OutFile $Out -UseBasicParsing

Write-Host "Installing ride-the-api (${Tag}) ..."
if ($Silent) {
    Start-Process -FilePath $Out -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -Wait
} else {
    Start-Process -FilePath $Out -Wait
}

Write-Host "Install finished. Re-run this script to upgrade to a newer release."
Remove-Item $Out -ErrorAction SilentlyContinue
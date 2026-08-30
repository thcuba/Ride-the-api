# Installs/uninstalls ride-the-api as a Windows service via NSSM.
# Requires Administrator. Downloads a portable NSSM if not present.
#
# The service runs ride-the-api.exe with the working/AppDirectory set to
# %ProgramData%\ride-the-api so config/server.py resolves config/config.yaml
# and the relative ./data ./certs path the same way as on Linux.
param(
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$ServiceName = "ride-the-api"
$InstallDir  = "${env:ProgramFiles}\ride-the-api"
$DataDir     = "${env:ProgramData}\ride-the-api"
$ExePath     = Join-Path $InstallDir "ride-the-api.exe"
$NssmPath    = Join-Path $env:TEMP "nssm\nssm.exe"

function Get-Nssm {
    if (Test-Path $NssmPath) { return $NssmPath }
    Write-Host "==> Downloading NSSM (portable)"
    $zip = Join-Path $env:TEMP "nssm.zip"
    Invoke-WebRequest -Uri "https://nssm.cc/release/nssm-2.24.zip" -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath (Join-Path $env:TEMP "nssm-raw") -Force
    New-Item -ItemType Directory -Force -Path (Split-Path $NssmPath) | Out-Null
    Copy-Item (Join-Path $env:TEMP "nssm-raw\nssm-2.24\win64\nssm.exe") $NssmPath -Force
    return $NssmPath
}

if ($Uninstall) {
    $nssm = Get-Nssm
    & $nssm stop $ServiceName 2>$null
    & $nssm remove $ServiceName confirm 2>$null
    Write-Host "Service removed."
    exit 0
}

if (-not (Test-Path $ExePath)) {
    Write-Error "ride-the-api.exe not found at $ExePath. Run the installer first."
    exit 1
}
if (-not (Test-Path (Join-Path $DataDir "config\config.yaml"))) {
    Write-Host "==> Seeding default config into $DataDir"
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    Copy-Item (Join-Path $InstallDir "config\config.yaml") (Join-Path $DataDir "config\config.yaml") -Force
}
New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "data")   | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "certs")  | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "logs")   | Out-Null

$nssm = Get-Nssm

Write-Host "==> Installing service '$ServiceName'"
& $nssm install $ServiceName $ExePath
& $nssm set $ServiceName AppDirectory $DataDir
& $nssm set $ServiceName AppEnvironmentExtra "RIDE_THE_API_DATA=$DataDir"
& $nssm set $ServiceName Description "ride-the-api: local cloud replacement proxy"
& $nssm set $ServiceName Start SERVICE_AUTO_START
& $nssm set $ServiceName AppRotateFiles 1
& $nssm set $ServiceName AppStderrFile "$DataDir\logs\service-stderr.log"
& $nssm set $ServiceName AppStdoutFile "$DataDir\logs\service-stdout.log"

Write-Host "==> Starting service"
& $nssm start $ServiceName
Write-Host "Service '$ServiceName' installed and started."
Write-Host "Logs: $DataDir\logs | Data: $DataDir\data | Certs: $DataDir\certs"
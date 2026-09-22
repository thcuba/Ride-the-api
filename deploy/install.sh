#!/usr/bin/env bash
#
# Ride the API — cross-platform installer (Linux + macOS)
#
# Downloads a prebuilt PyInstaller bundle from the latest GitHub release of
# thcuba/Ride-the-api and installs it into a target directory. Rerunning this
# script UPGRADES to the newest release while PRESERVING any existing config/,
# data/, certs/ and logs/ (only the bundled binary is replaced).
#
# Usage:
#   ./install.sh [--version <tag>] [--dir <path>] [--systemd]
#
# Options:
#   --version <tag>   Install a specific release tag (default: latest).
#   --dir <path>      Install directory (default: $HOME/ride-the-api).
#   --systemd         Also write a systemd unit that runs it as a user service.
#   -h, --help        Show this help.
#
# Env overrides: RTA_VERSION, RTA_DIR — same effect as the flags.

set -euo pipefail

REPO="thcuba/Ride-the-api"
BASE_URL="https://github.com/${REPO}/releases"
LATEST_SUFFIX="latest/download"

# ── Option parsing ─────────────────────────────────────────────────────────────
VERSION="${RTA_VERSION:-label:latest}"
INSTALL_DIR="${RTA_DIR:-$HOME/ride-the-api}"
WITH_SYSTEMD=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="${2:-label:latest}"; shift 2 ;;
    --dir)     INSTALL_DIR="${2:?--dir needs a path}"; shift 2 ;;
    --systemd) WITH_SYSTEMD=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

# ── Detect OS + arch → asset name ──────────────────────────────────────────────
OS="$(uname -s)"
ARCH="$(uname -m)"
case "${OS}" in
  Linux)
    platform="linux"
    ;;
  Darwin)
    platform="macos"
    ;;
  *)
    echo "Unsupported OS: ${OS} (use install.ps1 on Windows / different shell)." >&2
    exit 2
    ;;
esac
case "${ARCH}" in
  x86_64|amd64)  arch="x64" ;;
  aarch64|arm64) arch="arm64" ;;
  *)
    echo "Unsupported architecture: ${ARCH} (supported: x86_64/arm64 on Linux, arm64 on macOS)." >&2
    exit 2
    ;;
esac
if [[ "${platform}" == "macos" && "${arch}" == "x64" ]]; then
  echo "This release bundles macOS arm64 only (Apple Silicon). Use a different host." >&2
  exit 2
fi

ASSET="ride-the-api-${platform}-${arch}.tar.gz"

# Resolve the download URL.
#   requested "latest" → use the stable releases/latest/download/ path (no API).
#   requested specific tag → use /download/<tag>/ path.
if [[ "${VERSION}" == "label:latest" ]]; then
  DL_URL="${BASE_URL}/${LATEST_SUFFIX}/${ASSET}"
  VERSION="latest"
else
  DL_URL="${BASE_URL}/download/${VERSION}/${ASSET}"
fi

# ── Download ───────────────────────────────────────────────────────────────────
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
echo "▸ Downloading ${ASSET} (${VERSION}) from ${REPO} …"
echo "  ${DL_URL}"
curl -fL --retry 3 "${DL_URL}" -o "${TMP}/${ASSET}"

# ── Install (preserve config/data/certs/logs) ─────────────────────────────────
mkdir -p "${INSTALL_DIR}"
echo "▸ Installing into ${INSTALL_DIR} …"
tar -xzf "${TMP}/${ASSET}" -C "${TMP}"
BUNDLE="${TMP}/ride-the-api"

# Replace only the binary bundle; keep runtime dirs if they already exist.
if [[ -d "${INSTALL_DIR}/ride-the-api" ]]; then
  rm -rf "${INSTALL_DIR}/ride-the-api"
fi
mv "${BUNDLE}" "${INSTALL_DIR}/ride-the-api"

# On a FRESH install, seed the runtime dirs from the bundle defaults only if
# they are absent (upgrades never overwrite user data).
seed_dir() {  # seed_dir <dir>
  local d="$1"
  if [[ -d "${TMP}/${d}" && ! -e "${INSTALL_DIR}/${d}" ]]; then
    mv "${TMP}/${d}" "${INSTALL_DIR}/${d}"
  fi
}
seed_dir config
seed_dir data
seed_dir certs
seed_dir logs

chmod +x "${INSTALL_DIR}/ride-the-api/ride-the-api"

echo "✔ Installed ride-the-api (${VERSION}) into ${INSTALL_DIR}"
echo ""
echo "  Launch:      cd ${INSTALL_DIR} && ./ride-the-api/ride-the-api"
echo "  Upgrade:     re-run this script (preserves config/data/certs/logs)"

# ── Optional systemd (user unit) ───────────────────────────────────────────────
if [[ "${WITH_SYSTEMD}" -eq 1 ]]; then
  UNIT_DIR="${HOME}/.config/systemd/user"
  mkdir -p "${UNIT_DIR}"
  UNIT="${UNIT_DIR}/ride-the-api.service"
  cat > "${UNIT}" <<EOF
[Unit]
Description=Ride the API — Local Cloud Replacement Proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/ride-the-api/ride-the-api
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
  echo ""
  echo "  Systemd (user) unit written to ${UNIT}"
  echo "  Start:  systemctl --user daemon-reload && systemctl --user enable --now ride-the-api"
  echo "  Logs:   journalctl --user -u ride-the-api -f"
fi
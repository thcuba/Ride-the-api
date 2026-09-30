#!/usr/bin/env bash
#
# Ride the API — self-update (called by the web UI "Update" button)
#
# Reaches the LATEST state of the project on THIS machine:
#   - if the install dir is a git clone  -> git pull (main) + pip install -e .
#   - otherwise (prebuilt bundle)        -> download the latest release asset
#                                            via deploy/rideapi
# Then restarts the running service so the new version goes live.
#
# It is launched DETACHED from the API endpoint: the HTTP response is sent
# before the service restarts (otherwise the restart would kill the in-flight
# connection). Log output goes to <install-dir>/update.log.
#
# Usage:
#   deploy/update.sh [--dir <install-dir>] [--restart (systemd|none)]
#
# Options:
#   --dir <path>   Install root. Default: parent of this script (source tree).
#   --restart      How to restart: "systemd" (systemctl --user restart
#                  ride-the-api) or "none". Default: auto-detect (systemd if
#                  the unit exists, else none).
#   -h, --help     Show this help.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$SCRIPT_DIR/.."     # repo root (source layout)
RESTART_MODE="auto"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) INSTALL_DIR="${2:?--dir needs a path}"; shift 2 ;;
    --restart) RESTART_MODE="${2:?--restart needs systemd|none}"; shift 2 ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

INSTALL_DIR="$(cd "$INSTALL_DIR" && pwd)"
LOG="$INSTALL_DIR/update.log"

log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$LOG"; }

log "=== Updater avviato (install dir: $INSTALL_DIR) ==="

# ── 1. Update the code ───────────────────────────────────────────────────────
if [[ -d "$INSTALL_DIR/.git" ]]; then
  # Source install via git clone.
  log "Installazione da sorgente: git pull + pip install..."
  (
    cd "$INSTALL_DIR"
    git fetch --quiet origin || { log "git fetch fallito"; exit 1; }
    git pull --ff-only origin main || { log "git pull fallito"; exit 1; }
  )
  if [[ -x "$INSTALL_DIR/.venv/bin/python" ]]; then
    "$INSTALL_DIR/.venv/bin/python" -m pip install --quiet -e "$INSTALL_DIR" \
      || { log "pip install fallito"; exit 1; }
  else
    python3 -m pip install --quiet -e "$INSTALL_DIR" \
      || { log "pip install fallito"; exit 1; }
  fi
  log "Codice aggiornato (git pull + pip)."
else
  log "Installazione prebuilt: scarico l'ultima release..."
  "$SCRIPT_DIR/rideapi" --dir "$INSTALL_DIR" \
    || { log "Download release fallito"; exit 1; }
  log "Bundle prebuilt aggiornato."
fi

# ── 2. Restart the service ───────────────────────────────────────────────────
do_restart() {
  if [[ "$RESTART_MODE" == "systemd" ]] \
    || { [[ "$RESTART_MODE" == "auto" ]] && systemctl --user list-unit-files 'ride-the-api.service' >/dev/null 2>&1; }; then
    log "Riavvio del servizio (systemctl --user restart ride-the-api)..."
    systemctl --user restart ride-the-api || { log "Restart fallito (usa lo start manuale se serve)."; }
  else
    log "Nessun riavvio automatico (servizio systemd non rilevato)."
    log "Riavvia il servizio a mano: systemctl --user restart ride-the-api"
  fi
}
do_restart

log "=== Aggiornamento completato ==="
exit 0
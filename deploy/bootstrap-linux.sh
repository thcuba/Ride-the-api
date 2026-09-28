#!/usr/bin/env bash
#
# Ride the API — Linux bootstrap (install from source)
#
# Run ONE command on any 64-bit Linux (Ubuntu, Debian, Fedora, Arch...) and the
# machine is already installed and RUNNING: Python, all dependencies and the
# server are set up, the server starts at boot (via systemd, when available)
# and is accessible from the network.
#
# What this script does:
#   1. Verifies a 64-bit architecture (amd64/x86_64 or aarch64/arm64).
#   2. Installs missing prerequisites (git, curl, python3, python3-venv).
#   3. Clones the repo into <dir>/ride-the-api (or updates if present).
#   4. Creates a virtualenv and installs ALL dependencies: pip install -e .
#   5. If systemd is available: installs a user service (host 0.0.0.0, port
#      8911 → reachable from the LAN), enables boot autostart and lingers the
#      user session. Otherwise it only prints the launch command.
#   6. Waits until GET /health returns success.
#   7. Prints the final status (UI URL, logs, upgrade note).
#
# Usage (single command):
#   curl -fsSL https://raw.githubusercontent.com/thcuba/Ride-the-api/main/deploy/bootstrap-linux.sh -o bootstrap-linux.sh
#   bash bootstrap-linux.sh [--fg] [--dir <path>]
#
# Options:
#   --fg          Run in the FOREGROUND (no systemd service). For quick tests.
#   --dir <path>  Parent directory (default: $HOME). Repo goes in <dir>/ride-the-api.
#   -h, --help    Show this help.
#
# Env overrides: RTA_DIR.

set -euo pipefail

REPO="thcuba/Ride-the-api"
REPO_URL="https://github.com/${REPO}.git"

show_help() { sed -n '2,35p' "$0"; exit 0; }

MODE="systemd"          # systemd (default) | fg
BASE_DIR="${RTA_DIR:-$HOME}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --fg) MODE="fg"; shift ;;
    --dir) BASE_DIR="${2:?--dir needs a path}"; shift 2 ;;
    -h|--help) show_help ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
SRC_DIR="${BASE_DIR}/ride-the-api"
VENV="${SRC_DIR}/.venv"
PORT="8911"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✔ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m⚠ %s\033[0m\n' "$*"; }
fail() { printf '\033[31m✘ %s\033[0m\n' "$*"; exit 1; }

IP() { local ip; ip="$(hostname -I 2>/dev/null | awk '{print $1}')"; echo "${ip:-<ip-della-macchina>}"; }

# sudo non interattivo: `sudo -n` fallisce subito se serve password (niente blocchi).
# Ritorna l'exit code; il chiamante decide se fail-hard o warn.
sudorun() { if command -v sudo >/dev/null 2>&1; then sudo -n "$@"; else return 127; fi; }

HAS_SYSTEMD=0
command -v systemctl >/dev/null 2>&1 && command -v loginctl >/dev/null 2>&1 && HAS_SYSTEMD=1

echo
bold "Ride the API — Linux bootstrap (da sorgente, one-shot)"

# ── 1. 64-bit check ──────────────────────────────────────────────────────────
echo; printf '\033[1m▸ 1/6  Verifica architettura a 64 bit\033[0m\n'
ARCH="$(uname -m)"
case "${ARCH}" in
  x86_64|amd64) ARCH_LBL="amd64" ;; aarch64|arm64) ARCH_LBL="arm64" ;;
  *) fail "Architettura '${ARCH}' non supportata (servono 64-bit: amd64 o arm64)." ;;
esac
echo "  Architettura: ${ARCH_LBL}"; ok "64-bit OK."

# ── 2. Prerequisites ─────────────────────────────────────────────────────────
echo; printf '\033[1m▸ 2/6  Prerequisiti (git, curl, python3, python3-venv)\033[0m\n'
MISSING=""
for cmd in git curl python3; do command -v "$cmd" >/dev/null 2>&1 || MISSING="$MISSING $cmd"; done
python3 -m venv --help >/dev/null 2>&1 || MISSING="$MISSING python3-venv"
if [[ -n "${MISSING}" ]]; then
  warn "Mancano:$MISSING — provo a installarli (richiede sudo)..."
  if command -v apt-get >/dev/null 2>&1; then
    sudorun apt-get update -y >/dev/null 2>&1 || true
    for pkg in git curl python3 python3-venv python3-pip; do
      dpkg -s "$pkg" >/dev/null 2>&1 || sudorun apt-get install -y "$pkg" >/dev/null
    done
  elif command -v dnf >/dev/null 2>&1; then
    sudorun dnf install -y git curl python3 python3-venv >/dev/null 2>&1 || true
  elif command -v pacman >/dev/null 2>&1; then
    sudorun pacman -S --noconfirm git curl python python-virtualenv >/dev/null 2>&1 || true
  else
    fail "Gestore pacchetti non riconosciuto. Installa a mano: git, curl, python3, python3-venv."
  fi
fi
for cmd in git curl python3; do
  command -v "$cmd" >/dev/null 2>&1 || fail "Prerequisito mancante: $cmd"
done
python3 -m venv --help >/dev/null 2>&1 || fail "python3-venv mancante."
ok "Prerequisiti OK."

# ── 3. Clone / update source ─────────────────────────────────────────────────
echo; printf '\033[1m▸ 3/6  Scarico il codice sorgente\033[0m\n'
mkdir -p "$BASE_DIR"
if [[ -d "${SRC_DIR}/.git" ]]; then
  echo "  Repo già presente, aggiorno..."
  ( cd "$SRC_DIR" && git fetch --quiet --tags origin && git pull --ff-only origin main ) \
    || fail "Aggiornamento del repo fallito. Guarda l'output qui sopra."
else
  # Se la cartella esiste ma NON è un repo git (es. residuo di un tentativo
  # precedente, download incompleto), la spostiamo in backup invece di
  # fallire il clone: altrimenti git darebbe un errore fuorviante.
  if [[ -e "${SRC_DIR}" ]]; then
    STALE="${SRC_DIR}.stale.$(date +%s)"
    warn "Trovata cartella ${SRC_DIR} senza repo git: la sposto in ${STALE}"
    mv "${SRC_DIR}" "${STALE}" || fail "Impossibile spostare ${SRC_DIR}."
  fi
  echo "  Clono ${REPO_URL} ..."
  git clone --quiet "$REPO_URL" "$SRC_DIR" \
    || fail "Clone fallito. Verifica l'errore qui sopra o prova:
  time curl -s -o /dev/null -w 'github: %{http_code}\n' https://github.com
  (connessione a github.com assente/lenta se non stampa un codice 2xx/3xx)."
fi
ok "Sorgente in ${SRC_DIR}."

# ── 4. venv + install dependencies ───────────────────────────────────────────
echo; printf '\033[1m▸ 4/6  Creo il virtualenv e installo tutte le dipendenze\033[0m\n'
if [[ ! -d "${VENV}/bin" ]]; then
  python3 -m venv "$VENV" || fail "Creazione venv fallita."
fi
"${VENV}/bin/python" -m pip install --upgrade pip >/dev/null
"${VENV}/bin/python" -m pip install -e "${SRC_DIR}" \
  || fail "pip install è fallito. Verifica: ${VENV}/bin/pip install -e ${SRC_DIR}"
ok "Dipendenze installate (venv in ${VENV})."

# ── 5. Start ─────────────────────────────────────────────────────────────────
if [[ "${MODE}" == "systemd" && "${HAS_SYSTEMD}" -eq 1 ]]; then
  echo; printf '\033[1m▸ 5/6  Avvio come servizio con autostart al boot\033[0m\n'
  UNIT_DIR="${HOME}/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  cat > "${UNIT_DIR}/ride-the-api.service" <<EOF
[Unit]
Description=Ride the API — Local Cloud Replacement Proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${SRC_DIR}
ExecStart=${VENV}/bin/python -m core.server
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF
  sudorun loginctl enable-linger "$(whoami)" 2>/dev/null || warn "Impossibile abilitare lingering (serve sudo -n)."
  systemctl --user daemon-reload
  systemctl --user enable --now ride-the-api 2>/dev/null \
    || warn "Avvio manuale: systemctl --user start ride-the-api"
elif [[ "${MODE}" == "systemd" ]]; then
  echo
  warn "systemd non rilevato: avvio in primo piano (niente servizio)."
  MODE="fg"
fi

if [[ "${MODE}" == "fg" ]]; then
  echo; printf '\033[1m▸ 5/6  Avvio in primo piano (premi Ctrl+C per fermare)\033[0m\n'
  cd "$SRC_DIR"
  exec "$VENV/bin/python" -m core.server
fi

# ── 6. Health check + summary ────────────────────────────────────────────────
echo; printf '\033[1m▸ 6/6  Verifico che il server risponda (health check)\033[0m\n'
for _ in $(seq 1 30); do
  if curl -fsS -m 2 "http://localhost:${PORT}/health" >/dev/null 2>&1; then
    ok "Health check OK."; break
  fi
  sleep 2
done
HEALTH="$(curl -fsS -m 2 "http://localhost:${PORT}/health" 2>/dev/null || echo unreachable)"

echo
bold "✅ Linux pronto e operativo"
cat <<EOF

  Interfaccia web:  http://$(IP):${PORT}/
  Stato server:     ${HEALTH}
  Sorgente:         ${SRC_DIR}
  Python/venv:      ${VENV}
  Log:              journalctl --user -u ride-the-api -f
  Upgrade:          ri-lancia questo script (fa git pull + pip install)

  Da fare dopo (LLM + routing):
    - Chiave LLM:  edita ${SRC_DIR}/config/config.yaml (sezione llm_decipher)
      e inserisci la tua api_key al posto di "\${OPENAI_API_KEY}".
      Poi riavvia: systemctl --user restart ride-the-api
    - DNS: punta i domini del dispositivo verso questa macchina, es. dnsmasq:
        address=/mqtt.example.com/$(IP)
        address=/api.example.com/$(IP)
    - TLS opzionale: installa il CA da http://$(IP):${PORT}/api/tls/ca-cert sul dispositivo
EOF
[[ "${HEALTH}" == "unreachable" ]] && fail "
Il servizio non risponde ancora sulla porta ${PORT}. Guarda i log:
journalctl --user -u ride-the-api -f
oppure avvia in primo piano con: bash bootstrap-linux.sh --fg"
exit 0
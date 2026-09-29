#!/usr/bin/env bash
#
# Ride the API — Raspberry Pi bootstrap (install from source)
#
# Run ONE command on Raspberry Pi OS (64-bit) and the Pi is already
# installed and RUNNING: Python, all dependencies and the server are set up,
# the server starts at boot and is accessible from the network.
#
# What this script does:
#   1. Verifies the OS is Raspberry Pi OS 64-bit (arm64/aarch64) — the #1 blocker on Pi.
#   2. Installs missing prerequisites (git, curl, python3, python3-venv).
#   3. Clones the repo into <dir>/ride-the-api (or updates if present).
#   4. Creates a virtualenv and installs ALL dependencies: pip install -e .
#   5. Installs a systemd user service that starts the server from the venv
#      (host 0.0.0.0, port 8911 → reachable from the LAN), enables boot
#      autostart and lingers the user session so it starts at boot.
#   6. Waits until GET /health returns success.
#   7. Prints the final status (UI URL, logs, upgrade note).
#
# Usage (single command):
#   curl -fsSL https://raw.githubusercontent.com/thcuba/Ride-the-api/main/deploy/bootstrap-rpi.sh -o bootstrap-rpi.sh
#   bash bootstrap-rpi.sh [--fg] [--dir <path>]
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

show_help() { sed -n '2,34p' "$0"; exit 0; }

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

IP() { local ip; ip="$(hostname -I 2>/dev/null | awk '{print $1}')"; echo "${ip:-<ip-della-pi>}"; }

# sudo non interattivo: `sudo -n` fallisce subito se serve password (niente blocchi).
# Ritorna l'exit code; il chiamante decide se fail-hard o warn.
sudorun() { if command -v sudo >/dev/null 2>&1; then sudo -n "$@"; else return 127; fi; }

echo
bold "Ride the API — Raspberry Pi bootstrap (da sorgente, one-shot)"

# ── 1. OS + 64-bit check ──────────────────────────────────────────────────────
echo; printf '\033[1m▸ 1/6  Verifica Raspberry Pi OS a 64 bit\033[0m\n'

# OS: rileva da /etc/os-release (Raspberry Pi OS si identifica come "raspbian").
OS_ID="$(sed -n 's/^ID=//p' /etc/os-release 2>/dev/null | tr -d '"' | head -1)"
OS_NAME="$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release 2>/dev/null | tr -d '"' | head -1)"
if [[ "${OS_ID}" == "raspbian" ]]; then
  echo "  OS: ${OS_NAME}"
  ok "Raspberry Pi OS rilevato."
elif command -v uname >/dev/null 2>&1 && uname -a 2>/dev/null | grep -qi "raspberry pi"; then
  warn "OS aspetta Raspberry Pi OS, ma /etc/os-release non lo conferma (ID='${OS_ID:-?}')."
  echo "  Rilevato: ${OS_NAME:-Host sconosciuto}"
elif [[ -n "${OS_NAME}" ]]; then
  warn "Non sembra Raspberry Pi OS (ID='${OS_ID}', '${OS_NAME}')."
  warn "Questo script è pensato per Raspberry Pi OS: alcuni passi potrebbero non adattarsi."
else
  warn "Impossibile leggere /etc/os-release: continuo, ma potresti non essere su Raspberry Pi OS."
fi

# Architettura: serve a 64 bit.
ARCH="$(uname -m)"
if [[ "${ARCH}" != "aarch64" && "${ARCH}" != "arm64" ]]; then
  fail "Architettura '${ARCH}' rilevata. Serve Raspberry Pi OS a 64 bit (arm64/aarch64).
Reinstalla con 'Raspberry Pi OS Lite 64-bit' e riprova."
fi
echo "  Architettura: ${ARCH}"; ok "64-bit OK."

# ── 2. Prerequisites ─────────────────────────────────────────────────────────
echo; printf '\033[1m▸ 2/6  Prerequisiti (git, curl, python3, python3-venv)\033[0m\n'
MISSING=""
for cmd in git curl python3; do command -v "$cmd" >/dev/null 2>&1 || MISSING="$MISSING $cmd"; done
python3 -m venv --help >/dev/null 2>&1 || MISSING="$MISSING python3-venv"
if [[ -n "${MISSING}" ]]; then
  warn "Mancano:$MISSING — li installo (serve internet + sudo)..."
  sudorun apt-get update -y >/dev/null 2>&1 || warn "apt-get update non riuscito."
  for pkg in git curl python3 python3-venv python3-pip; do
    dpkg -s "$pkg" >/dev/null 2>&1 || sudorun apt-get install -y "$pkg" >/dev/null || warn "Install di $pkg fallito (già presente o sudo negato)."
  done
fi
for cmd in git curl python3; do
  command -v "$cmd" >/dev/null 2>&1 || fail "Prerequisito mancante: $cmd"
done
python3 -m venv --help >/dev/null 2>&1 || fail "python3-venv mancante: sudo apt-get install python3-venv"
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
# fast: piattaforma arm64; i wheel manylinux non servono compilazione locale.
"${VENV}/bin/python" -m pip install --upgrade pip >/dev/null
"${VENV}/bin/python" -m pip install -e "${SRC_DIR}" \
  || fail "pip install è fallito. Verifica dipendenze: ${VENV}/bin/pip install -e ${SRC_DIR}"
ok "Dipendenze installate (venv in ${VENV})."

# ── 5. Start ─────────────────────────────────────────────────────────────────
if [[ "${MODE}" == "systemd" ]]; then
  echo; printf '\033[1m▸ 5/6  Avvio come servizio con autostart al boot\033[0m\n'
  UNIT_DIR="${HOME}/.config/systemd/user"
  unit() {
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
  }
  mkdir -p "$UNIT_DIR"
  unit
  # A user service won't start at boot unless the user session lingers.
  if command -v loginctl >/dev/null 2>&1; then
    sudorun loginctl enable-linger "$(whoami)" || warn "Impossibile abilitare lingering (serve sudo -n)."
  fi
  systemctl --user daemon-reload
  systemctl --user enable --now ride-the-api 2>/dev/null \
    || warn "Avvio manuale: systemctl --user start ride-the-api"
else
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
bold "✅ Raspberry pronto e operativo"
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
    - DNS: punta i domini del dispositivo verso questa Pi, es. dnsmasq:
        address=/mqtt.example.com/$(IP)
        address=/api.example.com/$(IP)
    - TLS opzionale: installa il CA da http://$(IP):${PORT}/api/tls/ca-cert sul dispositivo
EOF
[[ "${HEALTH}" == "unreachable" ]] && fail "
Il servizio non risponde ancora sulla porta ${PORT}. Guarda i log:
journalctl --user -u ride-the-api -f
oppure avvia in primo piano con: bash bootstrap-rpi.sh --fg"
exit 0
#!/usr/bin/env bash
#
# Ride the API — bootstrap wrapper (compatibilità)
#
# Questo script è stato sostituito da deploy/bootstrap.sh (unico per tutte le
# distribuzioni Linux, Raspberry Pi OS inclusa). Il wrapper scarica ed esegue
# lo script unificato, così i comandi e i link esistenti continuano a
# funzionare senza cambiare nulla.
#
# Usage:
#   bash bootstrap-linux.sh [--fg] [--dir <path>]
#
# È l'equivalente di: bash bootstrap.sh [--fg] [--dir <path>]

set -euo pipefail

SCRIPT_URL="https://raw.githubusercontent.com/thcuba/Ride-the-api/main/deploy/bootstrap.sh"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

curl -fsSL "$SCRIPT_URL" -o "$TMP" \
  || { echo "Errore: impossibile scaricare ${SCRIPT_URL}" >&2; exit 1; }

exec bash "$TMP" "$@"
#!/bin/bash

# Helper script to run CSP_automation.py end-to-end.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

STATE_DIR="state"
CREDS_FILE="$STATE_DIR/kite_credentials.env"
mkdir -p "$STATE_DIR"

create_credentials_file() {
  echo "Creating credentials file at $CREDS_FILE"
  : > "$CREDS_FILE"
  echo "# Kite API credentials" >> "$CREDS_FILE"
  echo "# chmod 600 to restrict access" >> "$CREDS_FILE"
  for var in "${REQUIRED_ENV_VARS[@]}"; do
    local value=""
    while [ -z "$value" ]; do
      read -rsp "Enter $var (input hidden): " value
      echo
      if [ -z "$value" ]; then
        echo "$var cannot be empty."
      fi
    done
    printf '%s=%q\n' "$var" "$value" >> "$CREDS_FILE"
  done
  chmod 600 "$CREDS_FILE"
  echo "Credentials stored in $CREDS_FILE"
}

REQUIRED_ENV_VARS=(KITE_API_KEY KITE_API_SECRET KITE_ACCESS_TOKEN)
if [ ! -f "$CREDS_FILE" ]; then
  create_credentials_file
fi

set -a
# shellcheck disable=SC1090
source "$CREDS_FILE"
set +a

for var in "${REQUIRED_ENV_VARS[@]}"; do
  if [ -z "${!var:-}" ]; then
    echo "error: $var is empty in $CREDS_FILE" >&2
    exit 1
  fi
done

PYTHON_BIN="${PYTHON:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "error: $PYTHON_BIN not found. Set PYTHON env var to a valid interpreter." >&2
  exit 1
fi

REQUIRED_PACKAGES=(kiteconnect nsetools)
missing=()
for pkg in "${REQUIRED_PACKAGES[@]}"; do
  if ! "$PYTHON_BIN" - <<PY >/dev/null 2>&1
import importlib
importlib.import_module("$pkg")
PY
  then
    missing+=("$pkg")
  fi
done

if [ ${#missing[@]} -gt 0 ]; then
  echo "Installing missing packages: ${missing[*]}"
  "$PYTHON_BIN" -m pip install "${missing[@]}"
fi

echo "Running CSP automation..."
"$PYTHON_BIN" CSP_automation.py

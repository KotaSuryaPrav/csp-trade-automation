#!/bin/bash

# Helper script to run CSP_automation_IBKR.py with all prerequisites.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

STATE_DIR="state"
IB_CREDS_FILE="$STATE_DIR/ib_credentials.env"
mkdir -p "$STATE_DIR"

REQUIRED_IB_VARS=(IB_HOST IB_PORT IB_CLIENT_ID)

create_ib_credentials_file() {
  echo "Creating IB credentials file at $IB_CREDS_FILE"
  : > "$IB_CREDS_FILE"
  echo "# IB Gateway / TWS connection settings" >> "$IB_CREDS_FILE"
  echo "# chmod 600 to restrict access" >> "$IB_CREDS_FILE"
  for var in "${REQUIRED_IB_VARS[@]}"; do
    local prompt default value
    case "$var" in
      IB_HOST) default="127.0.0.1" ;;
      IB_PORT) default="7497" ;;
      IB_CLIENT_ID) default="1" ;;
    esac
    while true; do
      read -rp "Enter $var [${default}]: " value
      value="${value:-$default}"
      if [ -n "$value" ]; then
        printf '%s=%q\n' "$var" "$value" >> "$IB_CREDS_FILE"
        break
      fi
    done
  done
  chmod 600 "$IB_CREDS_FILE"
  echo "IB connection settings stored in $IB_CREDS_FILE"
}

if [ ! -f "$IB_CREDS_FILE" ]; then
  create_ib_credentials_file
fi

set -a
# shellcheck disable=SC1090
source "$IB_CREDS_FILE"
set +a

for var in "${REQUIRED_IB_VARS[@]}"; do
  if [ -z "${!var:-}" ]; then
    echo "error: $var is empty in $IB_CREDS_FILE" >&2
    exit 1
  fi
done

export BROKER="${BROKER:-IBKR}"

PYTHON_BIN="${PYTHON:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "error: $PYTHON_BIN not found. Set PYTHON env var to a valid interpreter." >&2
  exit 1
fi

REQUIRED_PACKAGES=(kiteconnect nsetools ib_insync)
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

cat <<'INFO'
------------------------------------------------------------
Running CSP_automation_IBKR.py
- Edit BROKER / UNIVERSE constants in the script as needed.
- Ensure IBKR TWS/Gateway is running if using IBKR mode.
------------------------------------------------------------
INFO

"$PYTHON_BIN" CSP_automation_IBKR.py

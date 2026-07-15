#!/usr/bin/env bash
set -euo pipefail
APP_ID="gld80-mcu-bridge"
LAUNCHER="$HOME/.local/bin/$APP_ID"
if [[ ! -x "$LAUNCHER" ]]; then
    echo "GLD80 MCU Bridge is not installed for this user."
    echo "Run INSTALL_LINUX.sh first."
    exit 1
fi
exec "$LAUNCHER" "$@"

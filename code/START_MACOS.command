#!/bin/bash
set -euo pipefail
APP_BUNDLE="$HOME/Applications/GLD80 MCU Bridge.app"
if [[ ! -d "$APP_BUNDLE" ]]; then
    echo "GLD80 MCU Bridge is not installed for this user."
    echo "Run INSTALL_MACOS.command first."
    read -r -p "Press Return to close..." _
    exit 1
fi
if [[ $# -gt 0 ]]; then
    open "$APP_BUNDLE" --args "$@"
else
    open "$APP_BUNDLE"
fi

#!/usr/bin/env bash
set -euo pipefail
APP_ID="gld80-mcu-bridge"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
INSTALL_ROOT="$DATA_HOME/$APP_ID"
LAUNCHER="$HOME/.local/bin/$APP_ID"
DESKTOP_FILE="$DATA_HOME/applications/$APP_ID.desktop"
ICON_FILE="$DATA_HOME/icons/hicolor/256x256/apps/$APP_ID.png"
AUTOSTART_FILE="$CONFIG_HOME/autostart/$APP_ID.desktop"
PURGE=false
if [[ "${1:-}" == "--purge" ]]; then
    PURGE=true
fi

pkill -f "$INSTALL_ROOT/app/run.py" >/dev/null 2>&1 || true
rm -rf "$INSTALL_ROOT"
rm -f "$LAUNCHER" "$DESKTOP_FILE" "$ICON_FILE" "$AUTOSTART_FILE"
if $PURGE; then
    rm -rf "$HOME/.gld80_mcu_bridge"
fi

echo "GLD80 MCU Bridge has been uninstalled."
if ! $PURGE; then
    echo "Your settings were kept in $HOME/.gld80_mcu_bridge"
    echo "Run this script with --purge to remove them too."
fi

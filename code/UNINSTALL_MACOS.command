#!/bin/bash
set -euo pipefail
APP_BUNDLE="$HOME/Applications/GLD80 MCU Bridge.app"
INSTALL_ROOT="$HOME/Library/Application Support/GLD80 MCU Bridge"
LAUNCH_AGENT="$HOME/Library/LaunchAgents/com.tripledots.gld80-mcu-bridge.plist"
PURGE=false
if [[ "${1:-}" == "--purge" ]]; then
    PURGE=true
fi

osascript -e 'tell application "GLD80 MCU Bridge" to quit' >/dev/null 2>&1 || true
pkill -f "$INSTALL_ROOT/app/run.py" >/dev/null 2>&1 || true
launchctl bootout "gui/$(id -u)" "$LAUNCH_AGENT" >/dev/null 2>&1 || true
rm -rf "$APP_BUNDLE" "$INSTALL_ROOT"
rm -f "$LAUNCH_AGENT"
if $PURGE; then
    rm -rf "$HOME/.gld80_mcu_bridge"
fi

echo "GLD80 MCU Bridge has been uninstalled."
if ! $PURGE; then
    echo "Your settings were kept in $HOME/.gld80_mcu_bridge"
    echo "Run this script with --purge to remove them too."
fi
read -r -p "Press Return to close..." _

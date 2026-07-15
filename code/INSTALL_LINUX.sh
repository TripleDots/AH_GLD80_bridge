#!/usr/bin/env bash
set -euo pipefail

APP_NAME="GLD80 MCU Bridge"
APP_ID="gld80-mcu-bridge"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
INSTALL_ROOT="$DATA_HOME/$APP_ID"
APP_DIR="$INSTALL_ROOT/app"
VENV_DIR="$INSTALL_ROOT/.venv"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/$APP_ID"
DESKTOP_DIR="$DATA_HOME/applications"
DESKTOP_FILE="$DESKTOP_DIR/$APP_ID.desktop"
ICON_DIR="$DATA_HOME/icons/hicolor/256x256/apps"
ICON_FILE="$ICON_DIR/$APP_ID.png"

find_python() {
    local candidate
    for candidate in python3.12 python3.11 python3.10 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; raise SystemExit(0 if (3,10) <= sys.version_info[:2] < (3,13) else 1)' >/dev/null 2>&1; then
                command -v "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

echo
echo "============================================================"
echo "  $APP_NAME - Linux installation"
echo "============================================================"
echo

PYTHON_BIN="$(find_python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
    echo "Python 3.10, 3.11 or 3.12 was not found."
    echo "Install a supported Python plus the venv module, then run this script again."
    echo "On Debian/Ubuntu this is commonly: sudo apt install python3.12 python3.12-venv"
    exit 1
fi

echo "Python: $PYTHON_BIN"
mkdir -p "$INSTALL_ROOT" "$BIN_DIR" "$DESKTOP_DIR" "$ICON_DIR"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR"

# Copy only the distributable project. User settings live separately in
# ~/.gld80_mcu_bridge and are never overwritten by an update.
tar -C "$SCRIPT_DIR" \
    --exclude='./.build-venv' --exclude='./.venv' --exclude='./build' --exclude='./dist' \
    --exclude='./__pycache__' --exclude='./*.pyc' \
    -cf - . | tar -C "$APP_DIR" -xf -

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    rm -rf "$VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check --upgrade pip wheel
if ! "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -r "$APP_DIR/requirements.txt"; then
    echo
    echo "Dependency installation failed."
    echo "If python-rtmidi must be built locally, install a compiler and ALSA headers"
    echo "(for Debian/Ubuntu: sudo apt install build-essential libasound2-dev)."
    exit 1
fi

cat > "$LAUNCHER" <<LAUNCHER_EOF
#!/usr/bin/env bash
set -euo pipefail
exec "$VENV_DIR/bin/python" "$APP_DIR/run.py" "\$@"
LAUNCHER_EOF
chmod +x "$LAUNCHER"

install -m 0644 "$APP_DIR/assets/gld80_bridge.png" "$ICON_FILE"
cat > "$DESKTOP_FILE" <<DESKTOP_EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=GLD80 MCU Bridge
Comment=Allen & Heath GLD-80 MCU, HUI and Raw MIDI DAW bridge
Exec=$LAUNCHER
Icon=$APP_ID
Terminal=false
Categories=AudioVideo;Audio;Midi;
StartupNotify=true
DESKTOP_EOF
chmod 0644 "$DESKTOP_FILE"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "$DATA_HOME/icons/hicolor" >/dev/null 2>&1 || true
fi

echo
echo "Installation complete."
echo "Application: $APP_DIR"
echo "Launcher:    $LAUNCHER"
echo "Menu entry:  $DESKTOP_FILE"
echo
"$LAUNCHER" >/dev/null 2>&1 &

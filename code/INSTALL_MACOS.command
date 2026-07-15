#!/bin/bash
set -euo pipefail

APP_NAME="GLD80 MCU Bridge"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_ROOT="$HOME/Library/Application Support/GLD80 MCU Bridge"
APP_DIR="$INSTALL_ROOT/app"
VENV_DIR="$INSTALL_ROOT/.venv"
APP_BUNDLE="$HOME/Applications/GLD80 MCU Bridge.app"
CONTENTS="$APP_BUNDLE/Contents"
MACOS_DIR="$CONTENTS/MacOS"
RESOURCES_DIR="$CONTENTS/Resources"
EXECUTABLE="$MACOS_DIR/GLD80 MCU Bridge"

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
echo "  $APP_NAME - macOS installation"
echo "============================================================"
echo

PYTHON_BIN="$(find_python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
    echo "Python 3.10, 3.11 or 3.12 was not found."
    echo "Install Python 3.12 from python.org or Homebrew, then run this file again."
    read -r -p "Press Return to close..." _
    exit 1
fi

echo "Python: $PYTHON_BIN"
mkdir -p "$INSTALL_ROOT" "$HOME/Applications"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR"

# macOS ships rsync; using it keeps updates clean without copying build caches.
rsync -a --delete \
    --exclude '.build-venv' --exclude '.venv' --exclude 'build' --exclude 'dist' \
    --exclude '__pycache__' --exclude '*.pyc' \
    "$SCRIPT_DIR/" "$APP_DIR/"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    rm -rf "$VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check --upgrade pip wheel
"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -r "$APP_DIR/requirements.txt"

rm -rf "$APP_BUNDLE"
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR"
cp "$APP_DIR/assets/gld80_bridge.png" "$RESOURCES_DIR/gld80_bridge.png"
cat > "$EXECUTABLE" <<LAUNCHER_EOF
#!/bin/bash
set -euo pipefail
exec "$VENV_DIR/bin/python" "$APP_DIR/run.py" "\$@"
LAUNCHER_EOF
chmod +x "$EXECUTABLE"

cat > "$CONTENTS/Info.plist" <<'PLIST_EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>GLD80 MCU Bridge</string>
  <key>CFBundleDisplayName</key><string>GLD80 MCU Bridge</string>
  <key>CFBundleIdentifier</key><string>com.tripledots.gld80-mcu-bridge</string>
  <key>CFBundleVersion</key><string>0.6.42</string>
  <key>CFBundleShortVersionString</key><string>0.6.42</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>GLD80 MCU Bridge</string>
  <key>LSMinimumSystemVersion</key><string>10.15</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST_EOF

# Remove quarantine from the locally assembled app bundle only. The source
# archive itself is left untouched.
xattr -dr com.apple.quarantine "$APP_BUNDLE" >/dev/null 2>&1 || true

echo
echo "Installation complete."
echo "Application: $APP_BUNDLE"
echo "Data:        $INSTALL_ROOT"
echo
open "$APP_BUNDLE"

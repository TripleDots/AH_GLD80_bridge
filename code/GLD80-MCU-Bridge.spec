# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_submodules

rtmidi_datas, rtmidi_binaries, rtmidi_hidden = collect_all("rtmidi")
mido_hidden = collect_submodules("mido.backends")

a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=rtmidi_binaries,
    datas=rtmidi_datas + [("README.md", "."), ("CHANGELOG.md", "."), ("assets/gld80_bridge.png", "assets"), ("integrations/reaper/README_REAPER.md", "integrations/reaper"), ("integrations/reaper/GLD80 Bridge - Sync REAPER track names and colours.lua", "integrations/reaper")],
    hiddenimports=rtmidi_hidden + mido_hidden + ["rtmidi"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GLD80 MCU Bridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/gld80_bridge.ico",
    version="windows/version_info.txt",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="GLD80 MCU Bridge",
)

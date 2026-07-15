from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

APP_RUN_VALUE = "GLD80 MCU Bridge"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MACOS_LAUNCH_AGENT_LABEL = "com.tripledots.gld80-mcu-bridge"
LINUX_DESKTOP_ID = "gld80-mcu-bridge.desktop"


def platform_name() -> str:
    if sys.platform == "win32":
        return "Windows"
    if sys.platform == "darwin":
        return "macOS"
    if sys.platform.startswith("linux"):
        return "Linux"
    return sys.platform


def is_windows() -> bool:
    return sys.platform == "win32"


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def is_startup_supported() -> bool:
    return is_windows() or is_macos() or is_linux()


def _launch_argv() -> list[str]:
    """Return the executable arguments used by per-user boot integration."""
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), "--minimized"]

    package_dir = Path(__file__).resolve().parent
    project_root = package_dir.parent
    launcher = project_root / "run.py"
    executable = Path(sys.executable).resolve()
    if is_windows():
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.exists():
            executable = pythonw
    return [str(executable), str(launcher), "--minimized"]


def _windows_command() -> str:
    return subprocess.list2cmdline(_launch_argv())


def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MACOS_LAUNCH_AGENT_LABEL}.plist"


def _linux_autostart_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "autostart" / LINUX_DESKTOP_ID


def _desktop_exec_arg(value: str) -> str:
    """Quote one freedesktop Exec argument without invoking a shell."""
    text = str(value).replace("%", "%%")
    if text and all(ch not in text for ch in ' \t\n\"\\`$;|&<>*?()[]{}'):
        return text
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
    )
    return f'"{escaped}"'


def startup_location() -> Path | None:
    if is_macos():
        return _macos_plist_path()
    if is_linux():
        return _linux_autostart_path()
    return None


def is_startup_enabled() -> bool:
    if is_windows():
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
                value, _kind = winreg.QueryValueEx(key, APP_RUN_VALUE)
            return bool(str(value).strip())
        except FileNotFoundError:
            return False

    path = startup_location()
    return bool(path and path.is_file())


def set_startup_enabled(enabled: bool) -> None:
    """Enable or disable per-user boot startup on Windows, macOS or Linux.

    The application is launched with ``--minimized``. No administrator/root
    privileges are required because every platform uses the current user's
    startup mechanism.
    """
    if not is_startup_supported():
        raise RuntimeError(f"Start on bootup is not supported on {platform_name()}.")

    if is_windows():
        import winreg

        if enabled:
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER,
                RUN_KEY,
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.SetValueEx(key, APP_RUN_VALUE, 0, winreg.REG_SZ, _windows_command())
            return
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                RUN_KEY,
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.DeleteValue(key, APP_RUN_VALUE)
        except FileNotFoundError:
            pass
        return

    path = startup_location()
    if path is None:
        raise RuntimeError(f"Could not determine the startup location on {platform_name()}.")

    if not enabled:
        path.unlink(missing_ok=True)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    argv = _launch_argv()
    if is_macos():
        payload = {
            "Label": MACOS_LAUNCH_AGENT_LABEL,
            "ProgramArguments": argv,
            "RunAtLoad": True,
            "KeepAlive": False,
            "ProcessType": "Interactive",
            "LimitLoadToSessionType": "Aqua",
        }
        with path.open("wb") as handle:
            plistlib.dump(payload, handle, sort_keys=False)
        return

    # Freedesktop/XDG autostart entry used by GNOME, KDE, Cinnamon, XFCE, etc.
    command = " ".join(_desktop_exec_arg(arg) for arg in argv)
    desktop = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.0\n"
        "Name=GLD-80 MCU Bridge\n"
        "Comment=Start the GLD-80 DAW bridge minimized\n"
        f"Exec={command}\n"
        "Terminal=false\n"
        "StartupNotify=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    path.write_text(desktop, encoding="utf-8")


def open_startup_apps_settings() -> None:
    """Open the platform's startup location/settings where practical."""
    if is_windows():
        subprocess.Popen(["cmd", "/c", "start", "", "ms-settings:startupapps"], shell=False)
        return
    path = startup_location()
    if path is None:
        raise RuntimeError(f"Startup settings are not available on {platform_name()}.")
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_macos():
        subprocess.Popen(["open", str(path.parent)])
    elif is_linux():
        subprocess.Popen(["xdg-open", str(path.parent)])

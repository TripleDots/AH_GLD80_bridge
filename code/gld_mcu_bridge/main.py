from __future__ import annotations

import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .resources import resource_path
from .ui import MainWindow


def main() -> int:
    start_hidden = "--tray" in sys.argv  # legacy compatibility
    start_minimized = "--minimized" in sys.argv
    qt_argv = [arg for arg in sys.argv if arg not in {"--tray", "--minimized"}]

    app = QApplication(qt_argv)
    app.setApplicationName("GLD-80 MCU Bridge")
    app.setApplicationDisplayName("GLD-80 MCU Bridge")
    app.setOrganizationName("GLD80 MCU Bridge community project")
    app.setQuitOnLastWindowClosed(False)

    icon_path = resource_path("assets/gld80_bridge.png")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    window = MainWindow()
    app.aboutToQuit.connect(window._shutdown)
    if start_hidden:
        window.start_hidden()
    elif start_minimized:
        window.start_minimized()
    else:
        window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

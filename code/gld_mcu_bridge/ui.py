from __future__ import annotations

import json
import sys
from collections import deque
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QMenu,
    QPushButton,
    QPlainTextEdit,
    QScrollArea,
    QSpinBox,
    QStyle,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .bridge import BridgeEngine
from .config import load_config, save_config
from .control_mapping import (
    CONTROL_ROWS,
    DEFAULT_CONTROL_MAPPINGS,
    HUI_CONTROL_OPTIONS,
    MCU_CONTROL_OPTIONS,
    control_action_label,
    hui_softkey_options,
    mapping_file_payload,
    mappings_from_file_payload,
    mcu_softkey_options,
    normalise_control_mappings,
    softkey_action_label,
)
from .midi_io import MidiRouter
from .resources import resource_path
from .protocols import mcu
from .startup import (
    is_startup_enabled,
    is_startup_supported,
    platform_name,
    set_startup_enabled,
)
from .widgets import ChannelStrip


class LogWindow(QMainWindow):
    """Detached activity/raw-traffic viewer used for troubleshooting."""

    capture_requested = Signal(bool)

    def __init__(self, app_lines: list[str], raw_lines: list[str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("GLD-80 Bridge logs and raw data")
        self.resize(1100, 650)
        self._paused = False

        central = QWidget()
        root = QVBoxLayout(central)
        controls = QHBoxLayout()
        self.pause_button = QPushButton("Pause live updates")
        self.pause_button.setCheckable(True)
        self.pause_button.toggled.connect(self._set_paused)
        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self.clear_all)
        save_button = QPushButton("Save log…")
        save_button.clicked.connect(self.save_log)
        controls.addWidget(self.pause_button)
        controls.addWidget(clear_button)
        controls.addWidget(save_button)
        controls.addStretch(1)
        root.addLayout(controls)

        self.tabs = QTabWidget()
        self.app_text = QPlainTextEdit()
        self.app_text.setReadOnly(True)
        self.app_text.setMaximumBlockCount(5000)
        self.raw_text = QPlainTextEdit()
        self.raw_text.setReadOnly(True)
        self.raw_text.setMaximumBlockCount(20000)
        self.raw_text.setPlaceholderText(
            "Raw capture starts while this window is open. Move a control or reconnect to generate traffic."
        )
        mono = "font-family: Consolas, 'Courier New', monospace; font-size: 10pt;"
        self.app_text.setStyleSheet(mono)
        self.raw_text.setStyleSheet(mono)
        self.tabs.addTab(self.app_text, "Activity")
        self.tabs.addTab(self.raw_text, "Raw MIDI / TCP")
        root.addWidget(self.tabs, stretch=1)
        self.setCentralWidget(central)

        if app_lines:
            self.app_text.setPlainText("\n".join(app_lines))
        if raw_lines:
            self.raw_text.setPlainText("\n".join(raw_lines))
        self._scroll_to_end(self.app_text)
        self._scroll_to_end(self.raw_text)

    @staticmethod
    def _scroll_to_end(widget: QPlainTextEdit) -> None:
        bar = widget.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)
        self.pause_button.setText("Resume live updates" if paused else "Pause live updates")

    def append_app(self, text: str) -> None:
        if self._paused:
            return
        self.app_text.appendPlainText(text)

    def append_raw(self, text: str) -> None:
        if self._paused:
            return
        self.raw_text.appendPlainText(text)

    def clear_all(self) -> None:
        self.app_text.clear()
        self.raw_text.clear()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API name
        self.capture_requested.emit(True)
        super().showEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API name
        self.capture_requested.emit(False)
        super().closeEvent(event)

    def save_log(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save GLD-80 Bridge log",
            "gld80-bridge-log.txt",
            "Text files (*.txt);;All files (*)",
        )
        if not path:
            return
        content = (
            "=== ACTIVITY ===\n"
            + self.app_text.toPlainText()
            + "\n\n=== RAW MIDI / TCP ===\n"
            + self.raw_text.toPlainText()
            + "\n"
        )
        try:
            Path(path).write_text(content, encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "Save log", str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"GLD-80 MCU Bridge v{__version__}")
        icon_path = resource_path("assets/gld80_bridge.png")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(1100, 720)
        self.setMinimumSize(760, 520)
        self._force_quit = False
        self._shutting_down = False
        self._disconnect_pending = False
        self._reset_pending = False
        self._tray_notice_shown = False
        self.tray_icon: QSystemTrayIcon | None = None
        self.log_window: LogWindow | None = None
        self._app_log_lines: deque[str] = deque(maxlen=5000)
        self._raw_log_lines: deque[str] = deque(maxlen=20000)

        self.cfg = load_config()
        self.engine = BridgeEngine(tracks=int(self.cfg.get("tracks", 32)))
        self.router = MidiRouter()
        self.engine.attach_router(self.router)
        self.engine.log.connect(self.log)
        self.router.raw_data.connect(self.raw_log)
        self.router.editor_connection_changed.connect(self._update_editor_connection_status)
        self.router.closed.connect(self._on_router_closed)
        self.engine.channel_changed.connect(self.update_channel)
        self.engine.channels_reset.connect(self._on_channels_reset)
        self.channel_strips: list[ChannelStrip] = []
        self._loading_options = False

        self._build_menu()
        self._build_ui()
        self.engine.labels_changed.connect(self._save_manual_labels)
        self.engine.reaper_sync_status.connect(self._update_reaper_status)
        self.refresh_ports()
        self._load_cfg_to_ui()
        self._setup_tray()

    def _build_menu(self) -> None:
        help_action = QAction("About / limitations", self)
        help_action.triggered.connect(self.show_about)
        logs_action = QAction("Logs / raw data", self)
        logs_action.triggered.connect(self.show_logs)
        github_action = QAction("Project on GitHub", self)
        github_action.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://github.com/TripleDots/AH_GLD80_bridge"))
        )
        self.menuBar().addAction(help_action)
        self.menuBar().addAction(logs_action)
        self.menuBar().addAction(github_action)

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(5)

        # Settings are split into tabs instead of occupying most of the window.
        # Each page scrolls independently on smaller/lower-resolution displays.
        self.settings_tabs = QTabWidget()
        self.settings_tabs.setMinimumHeight(150)
        self.settings_tabs.setMaximumHeight(300)

        routing_page = QWidget()
        routing = QGridLayout(routing_page)
        routing.setContentsMargins(8, 8, 8, 8)
        self.gld_connection_mode = QComboBox()
        self.gld_connection_mode.addItem("Direct Ethernet / TCP", "tcp")
        self.gld_connection_mode.addItem("MIDI ports", "midi")
        self.gld_host = QLineEdit()
        self.gld_host.setPlaceholderText("192.168.1.50")
        self.gld_tcp_port = QSpinBox()
        self.gld_tcp_port.setRange(1, 65535)
        self.gld_tcp_port.setValue(51325)
        routing.addWidget(QLabel("GLD connection"), 0, 0)
        routing.addWidget(self.gld_connection_mode, 0, 1)
        routing.addWidget(QLabel("GLD IP"), 0, 2)
        routing.addWidget(self.gld_host, 0, 3)
        routing.addWidget(QLabel("TCP port"), 0, 4)
        routing.addWidget(self.gld_tcp_port, 0, 5)

        self.gld_in = QComboBox()
        self.gld_out = QComboBox()
        self.gld_in.setEditable(True)
        self.gld_out.setEditable(True)
        routing.addWidget(QLabel("GLD MIDI IN → app"), 1, 0)
        routing.addWidget(self.gld_in, 1, 1, 1, 2)
        routing.addWidget(QLabel("App → GLD MIDI OUT"), 1, 3)
        routing.addWidget(self.gld_out, 1, 4, 1, 2)

        self.daw_protocol = QComboBox()
        self.daw_protocol.addItem("MCU — Mackie Control Universal", "mcu")
        self.daw_protocol.addItem("HUI — Pro Tools", "hui")
        self.daw_protocol.addItem("Raw MIDI — transparent", "raw")
        self.daw_protocol.setToolTip(
            "MCU is the default for most DAWs. HUI provides four 8-channel HUI surfaces for Pro Tools. "
            "Raw MIDI forwards the GLD MIDI stream unchanged through the first DAW port pair."
        )
        routing.addWidget(QLabel("DAW protocol"), 1, 6)
        routing.addWidget(self.daw_protocol, 1, 7)

        self.daw_in = []
        self.daw_out = []
        self.daw_in_labels = []
        self.daw_out_labels = []
        for bank in range(4):
            in_box = QComboBox()
            out_box = QComboBox()
            in_box.setEditable(True)
            out_box.setEditable(True)
            in_label = QLabel()
            out_label = QLabel()
            self.daw_in.append(in_box)
            self.daw_out.append(out_box)
            self.daw_in_labels.append(in_label)
            self.daw_out_labels.append(out_label)
            routing.addWidget(in_label, 2, bank * 2)
            routing.addWidget(in_box, 2, bank * 2 + 1)
            routing.addWidget(out_label, 3, bank * 2)
            routing.addWidget(out_box, 3, bank * 2 + 1)

        self.protocol_note = QLabel()
        self.protocol_note.setWordWrap(True)
        routing.addWidget(self.protocol_note, 4, 0, 1, 8)

        self.refresh_btn = QPushButton("Refresh ports")
        self.connect_btn = QPushButton("Connect")
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.setEnabled(False)
        self.reset_channels_btn = QPushButton("Reset channels")
        self.reset_channels_btn.setEnabled(False)
        self.reset_channels_btn.setToolTip(
            "Keep the GLD and DAW connected, but reset names, colours, faders, "
            "rotaries, Mute, MIX/Select and PAFL/Solo on all 32 strips."
        )
        self.logs_btn = QPushButton("Logs / raw data")
        self.refresh_btn.clicked.connect(self.refresh_ports)
        self.connect_btn.clicked.connect(self.connect_midi)
        self.disconnect_btn.clicked.connect(self.disconnect_midi)
        self.reset_channels_btn.clicked.connect(self.reset_channels)
        self.logs_btn.clicked.connect(self.show_logs)
        routing.addWidget(self.refresh_btn, 5, 0)
        routing.addWidget(self.connect_btn, 5, 1)
        routing.addWidget(self.disconnect_btn, 5, 2)
        routing.addWidget(self.reset_channels_btn, 5, 3)
        routing.addWidget(self.logs_btn, 5, 4)

        self.virtual_ports = QCheckBox("Create virtual DAW ports (when supported by the selected MIDI backend)")
        if sys.platform.startswith("win"):
            self.virtual_ports.setEnabled(False)
            self.virtual_ports.setToolTip(
                "The bundled Windows backend is WinMM and cannot create ports itself. "
                "Create A/B app-to-app or loopback endpoints in Windows MIDI Services, leave this unchecked, "
                "then select those existing endpoints above."
            )
        else:
            self.virtual_ports.setToolTip(
                "Creates named virtual MIDI endpoints through the active Python MIDI backend. "
                "Support depends on the operating system and backend."
            )
        routing.addWidget(self.virtual_ports, 6, 0, 1, 8)
        routing.setRowStretch(7, 1)
        self.settings_tabs.addTab(self._scroll_page(routing_page), "Connections")

        options_page = QWidget()
        opt = QGridLayout(options_page)
        opt.setContentsMargins(8, 8, 8, 8)
        self.pan_sensitivity = QDoubleSpinBox()
        self.pan_sensitivity.setRange(0.10, 16.00)
        self.pan_sensitivity.setDecimals(2)
        self.pan_sensitivity.setSingleStep(0.10)
        self.pan_sensitivity.setSuffix("×")
        self.pan_sensitivity.setToolTip(
            "Shared physical rotary speed for Pan, legacy Send-level control and Plug-in parameters. "
            "Use values below 1.00 to slow those rotaries down; 1.00 is normal; values above 1.00 accelerate them. "
            "GAIN Send selection and Custom 1/2 navigation remain one action per gesture."
        )
        self.pan_sync_note = QLabel(
            "GAIN now opens the standard MCU Send + Flip page: the motor faders control the selected Send, "
            "and turning GAIN chooses the previous or next Send. PAN restores normal track faders. "
            "The optional REAPER helper mirrors this workflow because stock REAPER MCU does not expose the standard Send page."
        )
        self.pan_sync_note.setWordWrap(True)
        self.echo_feedback = QCheckBox("DAW automation → GLD MIDI Strips (required for MCU/HUI)")
        self.echo_feedback.setToolTip(
            "Always enabled in MCU/HUI mode. It returns motor-fader positions, Pan values and key tallies from the DAW to the GLD."
        )
        self.editor_labels = QCheckBox("Use GLD Editor control for live Pan LCD, names and colours")
        self.editor_labels.setToolTip(
            "Uses the separate reverse-engineered GLD Editor protocol on TCP 51321. Close the official GLD Editor while this is active."
        )
        self.editor_port = QSpinBox()
        self.editor_port.setRange(1, 65535)
        self.editor_port.setValue(51321)
        self.sync_labels_btn = QPushButton("Sync all labels to GLD")
        self.sync_labels_btn.clicked.connect(self.engine.sync_all_labels)
        self.reconnect_editor_btn = QPushButton("Reconnect Editor control")
        self.reconnect_editor_btn.clicked.connect(self.router.restart_editor_control)
        self.editor_connection_status = QLabel("Editor control: disconnected")
        self.editor_connection_status.setWordWrap(True)
        self.label_limit_note = QLabel(
            "If the GLD reports ‘all available connections are in use’, close GLD Editor or another unused remote client. "
            "The bridge waits 60 seconds after a busy rejection before trying again, so it cannot keep filling the GLD connection table. Use Reconnect Editor control for one immediate retry. All Pan/name/colour values are resent after reconnecting."
        )
        self.label_limit_note.setWordWrap(True)

        self.reaper_sync = QCheckBox("REAPER companion (required for the Send fader page in REAPER): selected Sends, exact Pan, FX, names and colours")
        self.reaper_sync.setToolTip(
            "Companion v1.23 mirrors the basic MCU Send + Flip workflow that stock REAPER omits. A GAIN layer refresh opens the Send faders immediately, GAIN rotation selects the Send, and PAN restores track-volume faders. It also adds exact Pan and FX pages."
        )
        self.reaper_sync_status = QLabel("Waiting for REAPER companion v1.23; stock REAPER does not expose the standard MCU Send page")
        self.reaper_sync_status.setWordWrap(True)
        self.send_fader_flip = QCheckBox("Also use SoftKey 8 to toggle the selected Send on the motor faders")
        self.send_fader_flip.setToolTip(
            "Reliable explicit fallback when the GLD firmware does not publish a GAIN/PAN layer refresh. GAIN still chooses the previous/next Send. "
            "REAPER uses companion v1.23; other MCU hosts receive standard Send assignment followed by standard MCU Flip."
        )
        self.record_arm_blink = QCheckBox("Pulse strip colour for REC-armed tracks")
        self.record_arm_blink.setToolTip(
            "Uses a short red/white LCD-colour pulse and then restores the DAW track colour. "
            "Channel names and Mute/MIX/PAFL LEDs are not repurposed. Requires GLD Editor control."
        )
        self.open_reaper_guide = QPushButton("Open REAPER setup folder")
        self.open_reaper_guide.clicked.connect(self._open_reaper_guide)

        self.custom_navigation = QCheckBox("Use one MIDI Strip Custom rotary for standard MCU navigation")
        self.custom_navigation.setToolTip(
            "On the selected strip: Custom 1 = Bank Left/Right and Custom 2 = Channel Left/Right. "
            "Send mode never repurposes these controls. During the optional REAPER FX page they navigate FX/parameter pages; otherwise they are ordinary standard MCU track banking."
        )
        self.custom_navigation_strip = QSpinBox()
        self.custom_navigation_strip.setRange(1, 32)
        self.custom_navigation_strip.setValue(32)
        self.custom_navigation_strip.setToolTip("GLD MIDI Strip whose Custom 1 and Custom 2 rotary values control navigation.")
        self.bank_left_btn = QPushButton("Bank ◀")
        self.bank_right_btn = QPushButton("Bank ▶")
        self.channel_left_btn = QPushButton("Channel ◀")
        self.channel_right_btn = QPushButton("Channel ▶")
        self.bank_left_btn.clicked.connect(lambda: self.engine.manual_navigation("bank_left"))
        self.bank_right_btn.clicked.connect(lambda: self.engine.manual_navigation("bank_right"))
        self.channel_left_btn.clicked.connect(lambda: self.engine.manual_navigation("channel_left"))
        self.channel_right_btn.clicked.connect(lambda: self.engine.manual_navigation("channel_right"))
        self.navigation_buttons = [
            self.bank_left_btn, self.bank_right_btn, self.channel_left_btn, self.channel_right_btn
        ]

        opt.addWidget(QLabel("Rotary speed"), 0, 0)
        opt.addWidget(self.pan_sensitivity, 0, 1)
        opt.addWidget(self.echo_feedback, 0, 2, 1, 4)
        opt.addWidget(self.pan_sync_note, 1, 0, 1, 6)
        opt.addWidget(self.editor_labels, 2, 0, 1, 3)
        opt.addWidget(QLabel("Editor TCP port"), 2, 3)
        opt.addWidget(self.editor_port, 2, 4)
        opt.addWidget(self.sync_labels_btn, 2, 5)
        opt.addWidget(self.reconnect_editor_btn, 3, 0, 1, 2)
        opt.addWidget(self.editor_connection_status, 3, 2, 1, 4)
        opt.addWidget(self.label_limit_note, 4, 0, 1, 6)
        opt.addWidget(self.reaper_sync, 5, 0, 1, 4)
        opt.addWidget(self.open_reaper_guide, 5, 4, 1, 2)
        opt.addWidget(self.reaper_sync_status, 6, 0, 1, 6)
        opt.addWidget(self.send_fader_flip, 7, 0, 1, 5)
        opt.addWidget(self.record_arm_blink, 8, 0, 1, 4)
        opt.addWidget(self.custom_navigation, 9, 0, 1, 3)
        opt.addWidget(QLabel("Navigation strip"), 9, 3)
        opt.addWidget(self.custom_navigation_strip, 9, 4)
        opt.addWidget(self.bank_left_btn, 10, 0)
        opt.addWidget(self.bank_right_btn, 10, 1)
        opt.addWidget(self.channel_left_btn, 10, 2)
        opt.addWidget(self.channel_right_btn, 10, 3)
        opt.setRowStretch(11, 1)
        self.settings_tabs.addTab(self._scroll_page(options_page), "Sync / options")

        mapping_page = QWidget()
        mapping_root = QVBoxLayout(mapping_page)
        mapping_root.setContentsMargins(8, 8, 8, 8)
        mapping_note = QLabel(
            "Advanced control mapping. The defaults exactly preserve the established GLD workflow. "
            "Only assignments that are meaningful for each physical control are offered; Raw MIDI mode is untouched."
        )
        mapping_note.setWordWrap(True)
        mapping_root.addWidget(mapping_note)
        self.mapping_protocol_tabs = QTabWidget()
        self.mapping_combos = {
            "mcu": {"controls": {}, "softkeys": []},
            "hui": {"controls": {}, "softkeys": []},
        }
        for protocol, title, control_options, soft_options in (
            ("mcu", "MCU", MCU_CONTROL_OPTIONS, mcu_softkey_options()),
            ("hui", "HUI", HUI_CONTROL_OPTIONS, hui_softkey_options()),
        ):
            page = QWidget()
            grid = QGridLayout(page)
            grid.setContentsMargins(6, 6, 6, 6)
            grid.addWidget(QLabel("Physical GLD control"), 0, 0)
            grid.addWidget(QLabel(f"{title} action"), 0, 1)
            row = 1
            for control, label in CONTROL_ROWS:
                combo = QComboBox()
                for action, action_label in control_options[control]:
                    combo.addItem(action_label, action)
                combo.currentIndexChanged.connect(self._mapping_changed)
                self.mapping_combos[protocol]["controls"][control] = combo
                grid.addWidget(QLabel(label), row, 0)
                grid.addWidget(combo, row, 1)
                row += 1
            row += 1
            grid.addWidget(QLabel("GLD SoftKey"), row, 0)
            grid.addWidget(QLabel(f"{title} action"), row, 1)
            row += 1
            for index in range(10):
                combo = QComboBox()
                for action, action_label in soft_options:
                    combo.addItem(action_label, action)
                combo.currentIndexChanged.connect(self._mapping_changed)
                self.mapping_combos[protocol]["softkeys"].append(combo)
                grid.addWidget(QLabel(f"Soft {index + 1}"), row, 0)
                grid.addWidget(combo, row, 1)
                row += 1
            grid.setColumnStretch(1, 1)
            grid.setRowStretch(row, 1)
            self.mapping_protocol_tabs.addTab(self._scroll_page(page), title)
        mapping_root.addWidget(self.mapping_protocol_tabs, stretch=1)
        mapping_buttons = QHBoxLayout()
        self.save_mapping_btn = QPushButton("Save mapping…")
        self.load_mapping_btn = QPushButton("Load mapping…")
        self.reset_mapping_btn = QPushButton("Restore default mapping")
        self.save_mapping_btn.clicked.connect(self._save_mapping_file)
        self.load_mapping_btn.clicked.connect(self._load_mapping_file)
        self.reset_mapping_btn.clicked.connect(self._reset_mapping_defaults)
        mapping_buttons.addWidget(self.save_mapping_btn)
        mapping_buttons.addWidget(self.load_mapping_btn)
        mapping_buttons.addWidget(self.reset_mapping_btn)
        mapping_buttons.addStretch(1)
        mapping_root.addLayout(mapping_buttons)
        self.settings_tabs.addTab(mapping_page, "Control mapping")

        system_page = QWidget()
        system_layout = QGridLayout(system_page)
        system_layout.setContentsMargins(8, 8, 8, 8)
        self.start_with_windows = QCheckBox("Start on bootup, minimized")
        self.close_to_tray = QCheckBox("Closing the window hides it in the system tray")
        self.start_with_windows.setToolTip(
            "Adds a per-user startup entry on Windows, macOS or Linux and launches the bridge with --minimized."
        )
        self.close_to_tray.setToolTip("Use the tray icon menu and choose Quit to fully close the bridge.")
        if not is_startup_supported():
            self.start_with_windows.setEnabled(False)
            self.start_with_windows.setText(f"Start on bootup, minimized (unsupported on {platform_name()})")
        system_layout.addWidget(self.start_with_windows, 0, 0, 1, 2)
        system_layout.addWidget(self.close_to_tray, 0, 2, 1, 2)

        self.bpm = QSpinBox()
        self.bpm.setRange(20, 300)
        self.bpm.setValue(120)
        self.vegas_colours = QCheckBox("Animate MIDI Strip colours")
        self.start_vegas = QPushButton("Start Vegas")
        self.stop_vegas = QPushButton("Stop Vegas")
        self.start_vegas.clicked.connect(lambda: self.engine.start_vegas(self.bpm.value()))
        self.stop_vegas.clicked.connect(lambda: self.engine.stop_vegas())
        system_layout.addWidget(QLabel("Vegas BPM"), 1, 0)
        system_layout.addWidget(self.bpm, 1, 1)
        system_layout.addWidget(self.vegas_colours, 1, 2)
        system_layout.addWidget(self.start_vegas, 1, 3)
        system_layout.addWidget(self.stop_vegas, 1, 4)
        warning = QLabel(
            "Vegas is isolated from the DAW and restores faders, Mute/Mix/PAFL LEDs and colours on stop. "
            "SoftKeys are not included in Vegas."
        )
        warning.setWordWrap(True)
        system_layout.addWidget(warning, 2, 0, 1, 5)
        system_layout.setRowStretch(3, 1)
        self.settings_tabs.addTab(self._scroll_page(system_page), "System / Vegas")
        root.addWidget(self.settings_tabs)

        # Four bank tabs keep eight strips usable on laptop-sized displays while
        # preserving the same 1-8 / 9-16 / 17-24 / 25-32 MCU bank structure.
        self.mixer_tabs = QTabWidget()
        self.channel_strips = []
        for bank in range(4):
            page = QWidget()
            bank_layout = QHBoxLayout(page)
            bank_layout.setContentsMargins(3, 3, 3, 3)
            bank_layout.setSpacing(3)
            first = bank * 8
            for i in range(first, min(first + 8, self.engine.tracks)):
                strip = ChannelStrip(i)
                strip.fader_changed.connect(self.engine.manual_fader)
                strip.pan_changed.connect(self.engine.manual_pan)
                strip.mute_changed.connect(self.engine.manual_mute)
                strip.solo_changed.connect(self.engine.manual_solo)
                strip.select_changed.connect(self.engine.manual_select)
                strip.name_changed.connect(self._set_manual_name)
                strip.colour_changed.connect(self._set_manual_colour)
                self.channel_strips.append(strip)
                bank_layout.addWidget(strip, stretch=1)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            scroll.setWidget(page)
            self.mixer_tabs.addTab(scroll, f"Bank {bank + 1} · {first + 1}–{first + 8}")
        root.addWidget(self.mixer_tabs, stretch=1)

        soft_group = QGroupBox("SoftKeys — MCU workflow")
        soft_layout = QGridLayout(soft_group)
        self.softkey_buttons = []
        for index in range(10):
            label = mcu.SOFTKEY_MCU_LABELS[index]
            button = QPushButton(f"Soft {index + 1}\n{label}")
            button.setToolTip(
                f"Send standard MCU {label}. Physical GLD SoftKey {index + 1} can use custom MIDI "
                f"press 9F {index:02X} 7F and release 9F {index:02X} 00."
            )
            button.clicked.connect(lambda _checked=False, i=index: self.engine.manual_softkey(i))
            self.softkey_buttons.append(button)
            soft_layout.addWidget(button, index // 5, index % 5)
        self.softkey_note = QLabel(
            "Soft 1–8 = MCU F1–F8. When the optional Send fader-page shortcut is enabled, Soft 8 toggles "
            "the currently selected Send on the motor faders instead of F8. Soft 9 = MCU Plug-in: short-press the physical key to "
            "open/cycle Plug-in views, hold it for 0.7 s to return to normal Pan; the UI button "
            "toggles Plug-in/Pan. MIX 1–8 act as V-Pot pushes. Soft 10 toggles MCU REC/RDY on all "
            "currently selected channels (it does not start transport recording). HUI/Raw leave these disabled."
        )
        self.softkey_note.setWordWrap(True)
        soft_layout.addWidget(self.softkey_note, 2, 0, 1, 5)
        root.addWidget(soft_group)

        self.setCentralWidget(central)
        self.statusBar().showMessage("Ready")

        self.daw_protocol.currentIndexChanged.connect(self._update_protocol_fields)
        self.daw_protocol.currentIndexChanged.connect(self._apply_options)
        self.pan_sensitivity.valueChanged.connect(self._apply_options)
        self.echo_feedback.toggled.connect(self._apply_options)
        self.editor_labels.toggled.connect(self._update_gld_connection_fields)
        self.editor_labels.toggled.connect(self._apply_options)
        self.editor_port.valueChanged.connect(self._apply_options)
        self.reaper_sync.toggled.connect(self._apply_options)
        self.send_fader_flip.toggled.connect(self._update_protocol_fields)
        self.send_fader_flip.toggled.connect(self._apply_options)
        self.record_arm_blink.toggled.connect(self._apply_options)
        self.custom_navigation.toggled.connect(self._update_protocol_fields)
        self.custom_navigation.toggled.connect(self._apply_options)
        self.custom_navigation_strip.valueChanged.connect(self._apply_options)
        self.gld_connection_mode.currentIndexChanged.connect(self._update_gld_connection_fields)
        self.gld_connection_mode.currentIndexChanged.connect(self._apply_options)
        self.gld_host.textChanged.connect(self._apply_options)
        self.gld_tcp_port.valueChanged.connect(self._apply_options)
        self.start_with_windows.toggled.connect(self._set_boot_startup)
        self.close_to_tray.toggled.connect(self._apply_options)
        self.vegas_colours.toggled.connect(self._apply_options)
        self.virtual_ports.toggled.connect(self._apply_options)
        for combo in [self.gld_in, self.gld_out, *self.daw_in, *self.daw_out]:
            combo.currentTextChanged.connect(self._apply_options)

    @staticmethod
    def _scroll_page(widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(widget)
        return scroll

    def _mapping_from_ui(self) -> dict:
        payload = {"version": 1, "mcu": {"controls": {}, "softkeys": []}, "hui": {"controls": {}, "softkeys": []}}
        for protocol in ("mcu", "hui"):
            for control, _label in CONTROL_ROWS:
                combo = self.mapping_combos[protocol]["controls"][control]
                payload[protocol]["controls"][control] = str(combo.currentData() or "disabled")
            payload[protocol]["softkeys"] = [
                str(combo.currentData() or "disabled")
                for combo in self.mapping_combos[protocol]["softkeys"]
            ]
        return normalise_control_mappings(payload)

    def _set_mapping_ui(self, mappings) -> None:
        mappings = normalise_control_mappings(mappings)
        previous = self._loading_options
        self._loading_options = True
        try:
            for protocol in ("mcu", "hui"):
                profile = mappings[protocol]
                for control, _label in CONTROL_ROWS:
                    combo = self.mapping_combos[protocol]["controls"][control]
                    action = str(profile["controls"][control])
                    index = combo.findData(action)
                    if index >= 0:
                        combo.setCurrentIndex(index)
                for index, action in enumerate(profile["softkeys"][:10]):
                    combo = self.mapping_combos[protocol]["softkeys"][index]
                    combo_index = combo.findData(str(action))
                    if combo_index >= 0:
                        combo.setCurrentIndex(combo_index)
        finally:
            self._loading_options = previous
        self._refresh_mapping_labels()

    def _mapping_changed(self, _index: int = -1) -> None:
        if self._loading_options:
            return
        self._apply_options()
        self._refresh_mapping_labels()

    def _save_mapping_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save GLD-80 control mapping",
            "gld80-control-mapping.gldmap.json",
            "GLD mapping files (*.gldmap.json *.json);;All files (*)",
        )
        if not path:
            return
        try:
            Path(path).write_text(
                json.dumps(mapping_file_payload(self._mapping_from_ui()), indent=2),
                encoding="utf-8",
            )
            self.log(f"Control mapping saved: {path}")
        except (OSError, TypeError, ValueError) as exc:
            QMessageBox.critical(self, "Save mapping", str(exc))

    def _load_mapping_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load GLD-80 control mapping",
            "",
            "GLD mapping files (*.gldmap.json *.json);;All files (*)",
        )
        if not path:
            return
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            mappings = mappings_from_file_payload(payload)
            self._set_mapping_ui(mappings)
            self._apply_options()
            self.log(f"Control mapping loaded: {path}")
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            QMessageBox.critical(self, "Load mapping", str(exc))

    def _reset_mapping_defaults(self) -> None:
        answer = QMessageBox.question(
            self,
            "Restore default mapping",
            "Restore the original GLD-80 MCU/HUI mapping used by previous bridge versions?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._set_mapping_ui(DEFAULT_CONTROL_MAPPINGS)
        self._apply_options()
        self.log("Default control mapping restored")

    def _refresh_mapping_labels(self) -> None:
        if not hasattr(self, "mapping_combos"):
            return
        protocol = str(self.daw_protocol.currentData() or "mcu")
        mappings = self._mapping_from_ui()
        profile = mappings.get(protocol, {}) if protocol in {"mcu", "hui"} else {}
        controls = profile.get("controls", {}) if isinstance(profile, dict) else {}
        for strip in getattr(self, "channel_strips", []):
            strip.set_button_mapping(
                str(controls.get("mute", "track_mute")),
                str(controls.get("pafl", "track_solo")),
                str(controls.get("mix", "context_select")),
            )
            strip.set_state(self.engine.channels[strip.index])
        softkeys = profile.get("softkeys", []) if isinstance(profile, dict) else []
        for index, button in enumerate(getattr(self, "softkey_buttons", [])):
            action = str(softkeys[index]) if index < len(softkeys) else "disabled"
            if index == 7 and protocol == "mcu" and self.send_fader_flip.isChecked():
                label = "Send fader flip"
                button.setText("Soft 8\nSend Flip")
                button.setToolTip(
                    "Toggle the selected Send on the motor faders. GAIN chooses the previous/next Send; PAN restores normal track faders. "
                    "REAPER uses companion v1.23; other hosts receive standard MCU Send + Flip."
                )
                continue
            label = softkey_action_label(protocol, action)
            if len(label) > 18:
                label = label[:16] + "…"
            button.setText(f"Soft {index + 1}\n{label}")
            button.setToolTip(f"Mapped {protocol.upper()} action: {softkey_action_label(protocol, action)}")

    def _load_cfg_to_ui(self) -> None:
        self._loading_options = True
        connection_mode = str(self.cfg.get("gld_connection_mode", "tcp"))
        idx = self.gld_connection_mode.findData(connection_mode)
        if idx >= 0:
            self.gld_connection_mode.setCurrentIndex(idx)
        protocol = str(self.cfg.get("daw_protocol", "mcu"))
        protocol_idx = self.daw_protocol.findData(protocol)
        if protocol_idx >= 0:
            self.daw_protocol.setCurrentIndex(protocol_idx)
        self.gld_host.setText(str(self.cfg.get("gld_host", "192.168.1.50")))
        self.gld_tcp_port.setValue(int(self.cfg.get("gld_tcp_port", 51325)))
        self.editor_labels.setChecked(bool(self.cfg.get("editor_labels_enabled", True)))
        self.editor_port.setValue(int(self.cfg.get("gld_editor_port", 51321)))
        self.pan_sensitivity.setValue(float(self.cfg.get("pan_sensitivity", 1.0)))
        self.echo_feedback.setChecked(bool(self.cfg.get("echo_daw_feedback_to_gld_midi_strips", True)))
        self.reaper_sync.setChecked(bool(self.cfg.get("reaper_sync_enabled", False)))
        self.send_fader_flip.setChecked(bool(self.cfg.get("send_fader_flip_softkey8", False)))
        self.record_arm_blink.setChecked(bool(self.cfg.get("record_arm_blink_enabled", True)))
        self.custom_navigation.setChecked(bool(self.cfg.get("custom_navigation_enabled", True)))
        self.custom_navigation_strip.setValue(int(self.cfg.get("custom_navigation_strip", 32)))
        self.vegas_colours.setChecked(bool(self.cfg.get("vegas_colours_enabled", True)))
        self.close_to_tray.setChecked(bool(self.cfg.get("close_to_tray", False)))
        self._set_mapping_ui(self.cfg.get("control_mappings", DEFAULT_CONTROL_MAPPINGS))
        if is_startup_supported():
            try:
                startup_enabled = is_startup_enabled()
                if startup_enabled:
                    # Rewrite legacy --tray entries to the cross-platform
                    # --minimized command during upgrade.
                    set_startup_enabled(True)
                self.start_with_windows.setChecked(startup_enabled)
            except Exception:
                self.start_with_windows.setChecked(False)
        manual_names = list(self.cfg.get("manual_names", []))
        manual_colours = list(self.cfg.get("manual_colours", []))
        for i, strip in enumerate(self.channel_strips):
            if i < len(manual_names):
                self.engine.channels[i].name = str(manual_names[i])[:8]
            if i < len(manual_colours):
                self.engine.channels[i].colour = str(manual_colours[i])
            strip.set_state(self.engine.channels[i])
        self.virtual_ports.setChecked(
            bool(self.cfg.get("use_virtual_daw_ports", False))
            and not sys.platform.startswith("win")
        )
        self._select_combo_text(self.gld_in, str(self.cfg.get("gld_midi_in_port", "")))
        self._select_combo_text(self.gld_out, str(self.cfg.get("gld_midi_out_port", "")))
        for index, value in enumerate(list(self.cfg.get("daw_in_ports", []))[:4]):
            self._select_combo_text(self.daw_in[index], str(value))
        for index, value in enumerate(list(self.cfg.get("daw_out_ports", []))[:4]):
            self._select_combo_text(self.daw_out[index], str(value))
        self._loading_options = False
        self._update_gld_connection_fields()
        self._update_protocol_fields()
        self._apply_options()

    def _apply_options(self) -> None:
        if self._loading_options:
            return

        protocol = str(self.daw_protocol.currentData() or "mcu")
        # This return path is not optional for translated MCU/HUI operation: it
        # is what drives the GLD motors, Pan values and key indicators.
        feedback_enabled = protocol != "raw"
        if self.echo_feedback.isChecked() != feedback_enabled:
            self.echo_feedback.setChecked(feedback_enabled)

        self.engine.configure(
            daw_protocol=protocol,
            pan_sensitivity=self.pan_sensitivity.value(),
            echo_daw_feedback_to_gld=feedback_enabled,
            send_names_to_gld=self.editor_labels.isChecked(),
            send_colours_to_gld=self.editor_labels.isChecked(),
            vegas_colours_enabled=self.vegas_colours.isChecked(),
            record_arm_blink_enabled=self.record_arm_blink.isChecked(),
            reaper_sync_enabled=self.reaper_sync.isChecked(),
            reaper_sync_names=True,
            reaper_sync_colours=True,
            reaper_sync_pan=True,
            reaper_sync_plugins=True,
            send_fader_flip_softkey8=self.send_fader_flip.isChecked(),
            custom_navigation_enabled=self.custom_navigation.isChecked(),
            custom_navigation_strip=self.custom_navigation_strip.value() - 1,
            control_mappings=self._mapping_from_ui(),
        )
        self.cfg.update(
            gld_connection_mode=self.gld_connection_mode.currentData(),
            daw_protocol=protocol,
            gld_host=self.gld_host.text().strip(),
            gld_tcp_port=self.gld_tcp_port.value(),
            editor_labels_enabled=self.editor_labels.isChecked(),
            gld_editor_port=self.editor_port.value(),
            pan_sensitivity=self.pan_sensitivity.value(),
            echo_daw_feedback_to_gld_midi_strips=feedback_enabled,
            send_names_to_gld=self.editor_labels.isChecked(),
            send_colours_to_gld=self.editor_labels.isChecked(),
            vegas_colours_enabled=self.vegas_colours.isChecked(),
            record_arm_blink_enabled=self.record_arm_blink.isChecked(),
            close_to_tray=self.close_to_tray.isChecked(),
            reaper_sync_enabled=self.reaper_sync.isChecked(),
            reaper_sync_names=True,
            reaper_sync_colours=True,
            reaper_sync_pan=True,
            reaper_sync_plugins=True,
            send_fader_flip_softkey8=self.send_fader_flip.isChecked(),
            custom_navigation_enabled=self.custom_navigation.isChecked(),
            custom_navigation_strip=self.custom_navigation_strip.value(),
            control_mappings=self._mapping_from_ui(),
            manual_names=[ch.name for ch in self.engine.channels],
            manual_colours=[ch.colour for ch in self.engine.channels],
            gld_midi_in_port=self.gld_in.currentText(),
            gld_midi_out_port=self.gld_out.currentText(),
            daw_in_ports=[box.currentText() for box in self.daw_in],
            daw_out_ports=[box.currentText() for box in self.daw_out],
            use_virtual_daw_ports=(
                self.virtual_ports.isChecked() and not sys.platform.startswith("win")
            ),
        )
        try:
            save_config(self.cfg)
        except Exception:
            pass
        self._refresh_mapping_labels()

    def _update_protocol_fields(self) -> None:
        protocol = str(self.daw_protocol.currentData() or "mcu")
        if protocol == "mcu":
            prefix = "MCU"
            note = (
                "MCU mode: each port pair controls eight strips. In REAPER use one Mackie Control Universal "
                "for bank 1 (offset 0), followed by three Mackie Control Extenders with offsets 8, 16 and 24; "
                "set Size tweak to 8 for every surface and disable these endpoints under MIDI Devices. With a "
                "Windows app-to-app A/B cable, use the same side for both bridge fields and the opposite side for "
                "both REAPER fields. Never cross A/B inside one application."
            )
        elif protocol == "hui":
            prefix = "HUI"
            note = (
                "HUI mode: in Pro Tools open Setup > Peripherals > MIDI Controllers and add up to four HUI devices, "
                "each set to 8 channels and assigned to the matching port pair. Fader touch is emulated while values move."
            )
        else:
            prefix = "Raw MIDI"
            note = (
                "Raw MIDI mode: the first DAW port pair is a transparent bidirectional pipe. GLD messages are not translated; "
                "banks 2–4 are disabled. Avoid enabling MIDI Thru back to the same port, which can create a feedback loop."
            )
        self.protocol_note.setText(note)
        for bank in range(4):
            active = protocol != "raw" or bank == 0
            if protocol == "raw":
                self.daw_in_labels[bank].setText("Raw MIDI OUT → app" if bank == 0 else "Unused")
                self.daw_out_labels[bank].setText("App → Raw MIDI IN" if bank == 0 else "Unused")
            else:
                first = bank * 8 + 1
                last = first + 7
                self.daw_in_labels[bank].setText(f"DAW {prefix} {bank + 1} OUT → app ({first}–{last})")
                self.daw_out_labels[bank].setText(f"App → DAW {prefix} {bank + 1} IN ({first}–{last})")
            self.daw_in[bank].setEnabled(active)
            self.daw_out[bank].setEnabled(active)
        # The translated return path is mandatory in MCU/HUI and not used in
        # transparent Raw MIDI mode. Show the effective state but do not let an
        # accidental click disable motor-fader or Pan feedback.
        self.echo_feedback.setChecked(protocol != "raw")
        self.echo_feedback.setEnabled(False)
        for button in getattr(self, "softkey_buttons", []):
            button.setEnabled(protocol in {"mcu", "hui"})
        if hasattr(self, "send_fader_flip"):
            self.send_fader_flip.setEnabled(protocol == "mcu")
        if hasattr(self, "custom_navigation"):
            self.custom_navigation.setEnabled(protocol == "mcu")
            self.custom_navigation_strip.setEnabled(protocol == "mcu" and self.custom_navigation.isChecked())
        for button in getattr(self, "navigation_buttons", []):
            button.setEnabled(protocol == "mcu")
        if hasattr(self, "mapping_protocol_tabs") and protocol in {"mcu", "hui"}:
            self.mapping_protocol_tabs.setCurrentIndex(0 if protocol == "mcu" else 1)
        self._refresh_mapping_labels()

    def _update_gld_connection_fields(self) -> None:
        tcp = self.gld_connection_mode.currentData() == "tcp"
        self.gld_host.setEnabled(tcp)
        self.gld_tcp_port.setEnabled(tcp)
        self.editor_labels.setEnabled(tcp)
        self.editor_port.setEnabled(tcp and self.editor_labels.isChecked())
        self.sync_labels_btn.setEnabled(tcp and self.editor_labels.isChecked())
        self.reconnect_editor_btn.setEnabled(tcp and self.editor_labels.isChecked())
        self.gld_in.setEnabled(not tcp)
        self.gld_out.setEnabled(not tcp)

    def _update_reaper_status(self, text: str) -> None:
        self.reaper_sync_status.setText(text)

    def _update_editor_connection_status(self, connected: bool) -> None:
        if connected:
            self.editor_connection_status.setText("Editor control: connected — Pan/name/colour resync queued")
            self.editor_connection_status.setStyleSheet("font-weight: bold; color: #17833b;")
        else:
            self.editor_connection_status.setText("Editor control: disconnected / safe retry")
            self.editor_connection_status.setStyleSheet("font-weight: bold; color: #b36b00;")

    def show_logs(self) -> None:
        if self.log_window is None:
            self.log_window = LogWindow(
                list(self._app_log_lines), list(self._raw_log_lines), self
            )
            self.log_window.capture_requested.connect(self.router.set_raw_capture)
        self.log_window.showNormal()
        self.log_window.raise_()
        self.log_window.activateWindow()

    def _open_reaper_guide(self) -> None:
        if getattr(sys, "frozen", False):
            folder = Path(sys.executable).resolve().parent / "integrations" / "reaper"
        else:
            folder = resource_path("integrations/reaper")
        if not folder.exists():
            folder = resource_path("integrations/reaper")
        if not folder.exists():
            QMessageBox.warning(self, "REAPER setup", f"REAPER integration folder not found: {folder}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_icon = None
            self.close_to_tray.setEnabled(False)
            self.close_to_tray.setToolTip("No system tray is available in this desktop session.")
            return

        icon = self.windowIcon()
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        tray = QSystemTrayIcon(icon, self)
        tray.setToolTip("GLD-80 MCU Bridge")

        menu = QMenu(self)
        show_action = QAction("Show GLD-80 MCU Bridge", self)
        show_action.triggered.connect(self.show_from_tray)
        logs_action = QAction("Logs / raw data", self)
        logs_action.triggered.connect(self.show_logs)
        start_vegas_action = QAction("Start Vegas", self)
        start_vegas_action.triggered.connect(lambda: self.engine.start_vegas(self.bpm.value()))
        stop_vegas_action = QAction("Stop Vegas and restore", self)
        stop_vegas_action.triggered.connect(lambda: self.engine.stop_vegas())
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit_application)
        menu.addAction(show_action)
        menu.addAction(logs_action)
        menu.addSeparator()
        menu.addAction(start_vegas_action)
        menu.addAction(stop_vegas_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(self._tray_activated)
        tray.show()
        self.tray_icon = tray

    def _tray_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_from_tray()

    def show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def start_hidden(self) -> None:
        """Legacy --tray behaviour retained for older startup entries."""
        if self.tray_icon is None:
            self.showMinimized()
            return
        self.hide()
        self.log("Started hidden in the system tray (legacy --tray mode)")

    def start_minimized(self) -> None:
        """Show the main window minimized on Windows, macOS and Linux."""
        self.showMinimized()
        self.log("Started on bootup, minimized")

    def _hide_to_tray(self) -> None:
        if self.tray_icon is None:
            return
        self.hide()
        if not self._tray_notice_shown:
            self._tray_notice_shown = True
            self.tray_icon.showMessage(
                "GLD-80 MCU Bridge",
                "The bridge is still running. Use the tray icon menu to show it or quit.",
                QSystemTrayIcon.MessageIcon.Information,
                3500,
            )

    def _set_boot_startup(self, enabled: bool) -> None:
        if self._loading_options:
            return
        try:
            set_startup_enabled(bool(enabled))
        except Exception as exc:
            self.start_with_windows.blockSignals(True)
            self.start_with_windows.setChecked(not bool(enabled))
            self.start_with_windows.blockSignals(False)
            QMessageBox.critical(self, "Bootup startup error", str(exc))
            return
        state = "enabled" if enabled else "disabled"
        self.log(f"Start on bootup, minimized {state} for {platform_name()}")

    def _shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        if self.engine.vegas_active:
            self.engine.stop_vegas(immediate_reset=True)
        if self.router.connected:
            self.engine.reset_channels()
        self.router.close_async()
        self._apply_options()
        if self.tray_icon is not None:
            self.tray_icon.hide()

    def quit_application(self) -> None:
        self._force_quit = True
        self._shutdown()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def refresh_ports(self) -> None:
        inputs = [""] + MidiRouter.input_names()
        outputs = [""] + MidiRouter.output_names()
        combos_in = [self.gld_in, *self.daw_in]
        combos_out = [self.gld_out, *self.daw_out]
        for combo in combos_in:
            old = combo.currentText()
            combo.clear()
            combo.addItems(inputs)
            if old:
                idx = combo.findText(old)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
        for combo in combos_out:
            old = combo.currentText()
            combo.clear()
            combo.addItems(outputs)
            if old:
                idx = combo.findText(old)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
        self.log("MIDI ports refreshed")

    @staticmethod
    def _select_combo_text(combo: QComboBox, value: str) -> None:
        if not value:
            return
        index = combo.findText(value)
        if index >= 0:
            combo.setCurrentIndex(index)
        elif combo.isEditable():
            combo.setEditText(value)

    @staticmethod
    def _duplicate_names(values: list[str]) -> list[str]:
        seen: set[str] = set()
        duplicates: list[str] = []
        for value in (v.strip() for v in values if v.strip()):
            if value in seen and value not in duplicates:
                duplicates.append(value)
            seen.add(value)
        return duplicates

    def _validate_daw_port_pairs(self) -> tuple[list[str], list[str]]:
        protocol = str(self.daw_protocol.currentData() or "mcu")
        inputs = [box.currentText().strip() for box in self.daw_in]
        outputs = [box.currentText().strip() for box in self.daw_out]
        active_count = 1 if protocol == "raw" else 4
        inputs = inputs[:active_count]
        outputs = outputs[:active_count]
        for bank, (input_name, output_name) in enumerate(zip(inputs, outputs), start=1):
            if bool(input_name) != bool(output_name):
                raise ValueError(
                    f"DAW bank {bank} has only one direction selected. Choose both the DAW→app and app→DAW "
                    "endpoint, or clear both fields."
                )
        # MCU/HUI banks must be contiguous from bank 1. A later bank with an
        # earlier gap is almost always an extender setup mistake.
        seen_blank = False
        for bank, (input_name, output_name) in enumerate(zip(inputs, outputs), start=1):
            populated = bool(input_name and output_name)
            if not populated:
                seen_blank = True
            elif seen_blank:
                raise ValueError(
                    f"DAW bank {bank} is configured after an empty earlier bank. Configure banks consecutively "
                    "from bank 1 (tracks 1–8)."
                )

        duplicate_inputs = self._duplicate_names(inputs)
        duplicate_outputs = self._duplicate_names(outputs)
        if duplicate_inputs or duplicate_outputs:
            details = []
            if duplicate_inputs:
                details.append("DAW→app: " + ", ".join(duplicate_inputs))
            if duplicate_outputs:
                details.append("app→DAW: " + ", ".join(duplicate_outputs))
            raise ValueError(
                "Each MCU/HUI bank needs its own endpoint. Duplicate selections found: " + "; ".join(details)
            )
        if not any(inputs) and not any(outputs):
            raise ValueError("Select at least one complete DAW MIDI port pair before connecting.")
        # Pad Raw mode back to four entries for the router.
        return inputs + [""] * (4 - len(inputs)), outputs + [""] * (4 - len(outputs))

    def connect_midi(self) -> None:
        try:
            daw_inputs, daw_outputs = self._validate_daw_port_pairs()
            for bank, (input_name, output_name) in enumerate(zip(daw_inputs, daw_outputs), start=1):
                if input_name and output_name and input_name != output_name:
                    self.log(
                        f"DAW bank {bank} uses different bridge input/output endpoint names. This is valid for "
                        "separate one-way MIDI ports, but with a Windows app-to-app A/B cable the bridge should "
                        "use the same side for both fields and REAPER should use the opposite side for both fields."
                    )
            self.engine.reset_transport_state()
            self.router.connect_ports(
                self.gld_in.currentText(),
                self.gld_out.currentText(),
                daw_inputs,
                daw_outputs,
                use_virtual_daw_ports=(
                    self.virtual_ports.isChecked() and not sys.platform.startswith("win")
                ),
                gld_connection_mode=str(self.gld_connection_mode.currentData()),
                gld_host=self.gld_host.text().strip(),
                gld_tcp_port=self.gld_tcp_port.value(),
                enable_editor_labels=self.editor_labels.isChecked(),
                gld_editor_port=self.editor_port.value(),
            )
            self.engine.centre_send_rotaries()
            self.connect_btn.setEnabled(False)
            self.disconnect_btn.setEnabled(True)
            self.reset_channels_btn.setEnabled(True)
            self.daw_protocol.setEnabled(False)
        except Exception as exc:
            QMessageBox.critical(self, "MIDI error", str(exc))

    def reset_channels(self) -> None:
        if self._reset_pending or self._disconnect_pending:
            return
        self._reset_pending = True
        self.reset_channels_btn.setEnabled(False)
        self.reset_channels_btn.setText("Resetting…")
        QApplication.processEvents()
        if self.engine.vegas_active:
            self.engine.stop_vegas(immediate_reset=True)
        self.engine.reset_channels()

    def _on_channels_reset(self) -> None:
        # Explicitly repaint every widget from the neutral model, even when a
        # name field still has keyboard focus. This makes Disconnect visibly
        # reset the bridge at the same moment as the physical GLD.
        for index, strip in enumerate(self.channel_strips):
            if index < len(self.engine.channels):
                strip.set_state(self.engine.channels[index], force_text=True)
        self._save_manual_labels()
        QApplication.processEvents()
        if self._disconnect_pending:
            self.disconnect_btn.setText("Closing ports…")
            self.router.close_async()
            return
        self._reset_pending = False
        self.reset_channels_btn.setText("Reset channels")
        self.reset_channels_btn.setEnabled(bool(self.router.connected))

    def disconnect_midi(self) -> None:
        if self._disconnect_pending:
            return
        self._disconnect_pending = True
        self._reset_pending = False
        self.disconnect_btn.setEnabled(False)
        self.reset_channels_btn.setEnabled(False)
        self.connect_btn.setEnabled(False)
        self.disconnect_btn.setText("Resetting channels…")
        QApplication.processEvents()
        if self.engine.vegas_active:
            self.engine.stop_vegas(immediate_reset=True)
        # The reset itself contains no sleeps. Backend port.close() calls run
        # asynchronously after the reset signal, so a stuck WinMM/rtmidi driver
        # can no longer freeze the Qt event loop.
        self.engine.reset_channels()

    def _on_router_closed(self) -> None:
        if not self._disconnect_pending:
            return
        self._disconnect_pending = False
        self._reset_pending = False
        self.disconnect_btn.setText("Disconnect")
        self.disconnect_btn.setEnabled(False)
        self.reset_channels_btn.setText("Reset channels")
        self.reset_channels_btn.setEnabled(False)
        self.connect_btn.setEnabled(True)
        self.daw_protocol.setEnabled(True)

    def _set_manual_name(self, index: int, name: str) -> None:
        self.engine.set_name(index, name)
        self._save_manual_labels()

    def _set_manual_colour(self, index: int, colour: str) -> None:
        self.engine.set_colour(index, colour)
        self._save_manual_labels()

    def _save_manual_labels(self) -> None:
        self.cfg["manual_names"] = [ch.name for ch in self.engine.channels]
        self.cfg["manual_colours"] = [ch.colour for ch in self.engine.channels]
        try:
            save_config(self.cfg)
        except Exception:
            pass

    def update_channel(self, index: int, state) -> None:
        if 0 <= index < len(self.channel_strips):
            self.channel_strips[index].set_state(state)

    @staticmethod
    def _timestamped(text: str) -> str:
        return f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]}  {text}"

    def log(self, text: str) -> None:
        message = str(text)
        line = self._timestamped(message)
        self._app_log_lines.append(line)
        self.statusBar().showMessage(message, 8000)
        if message == "GLD Pan/name/colour resync completed":
            self.editor_connection_status.setText("Editor control: connected — synchronized")
            self.editor_connection_status.setStyleSheet("font-weight: bold; color: #17833b;")
        elif "all remote-control connections are in use" in message:
            self.editor_connection_status.setText("Editor control: GLD slots busy — 60 s safety cooldown")
            self.editor_connection_status.setStyleSheet("font-weight: bold; color: #b36b00;")
        if self.log_window is not None:
            self.log_window.append_app(line)

    def raw_log(self, text: str) -> None:
        line = self._timestamped(str(text))
        self._raw_log_lines.append(line)
        if self.log_window is not None:
            self.log_window.append_raw(line)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API name
        if (
            not self._force_quit
            and self.close_to_tray.isChecked()
            and self.tray_icon is not None
        ):
            self._hide_to_tray()
            event.ignore()
            return

        self._force_quit = True
        self._shutdown()
        event.accept()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def show_about(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle(f"About GLD-80 MCU Bridge v{__version__}")
        box.setIcon(QMessageBox.Icon.Information)
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        box.setText(
            "<b>GLD-80 MCU Bridge</b> uses the factory-default GLD MIDI Strip mapping and offers MCU, HUI and transparent Raw MIDI.<br><br>"
            "MCU/HUI use four independent 8-channel MIDI port pairs. In REAPER, bank 1 must be a Mackie Control Universal and banks 2–4 Mackie Control Extenders with offsets 8, 16 and 24. Each bank requires a unique endpoint pair. With paired A/B virtual cables, use one side for both bridge directions and the opposite side for both REAPER directions; crossing A/B inside an app creates a loop.<br><br>"
            "Physical MIDI Strip Pan redraw, names and colours use the reverse-engineered GLD Editor connection on TCP 51321 and address only MIDI Strip 1–32. If the desk has no free control connection, the bridge uses a 60-second safety cooldown and performs a paced complete resync after reconnecting. All fader traffic is direct, without smoothing. In the default MCU mapping, a GLD GAIN layer refresh opens Send + Flip and moves the motors directly to the selected Send; a PAN layer refresh restores cached track-volume faders. GAIN rotation chooses previous/next Send. The selector buttons have no dedicated public MIDI message, so the first turn or optional SoftKey 8 remains a fallback on firmware that does not publish the refresh. REAPER companion v1.23 mirrors that standard workflow because stock REAPER omits the MCU Send page. Bank/Channel and track buttons remain standard MCU.<br><br>"
            "The scalable four-bank mixer includes Mute, PAFL/Solo, MIX/Select, Pan centre and measured 0 dB controls. Ten standard-MCU SoftKeys, Custom-rotary MCU Bank/Channel navigation and a detached raw MIDI/TCP troubleshooting log are included.<br><br>"
            "Vegas animates only MIDI Strip faders, Mute/Mix/PAFL LEDs and optional strip colours. It does not touch DCAs or SoftKeys, isolates animation echoes from the DAW, and restores the exact pre-test surface state on stop.<br><br>"
            "Project, source code and issue tracker: "
            "<a href='https://github.com/TripleDots/AH_GLD80_bridge'>github.com/TripleDots/AH_GLD80_bridge</a>"
        )
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        for label in box.findChildren(QLabel):
            label.setOpenExternalLinks(True)
        box.exec()

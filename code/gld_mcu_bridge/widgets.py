from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDial,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .model import ChannelState
from .protocols.gld import COLOURS, fader_value_to_db


# Measured GLD MIDI Strip position corresponding to 0.0 dB.
GLD_FADER_0DB_VALUE = 98


class ChannelStrip(QWidget):
    fader_changed = Signal(int, int)
    pan_changed = Signal(int, int)
    mute_changed = Signal(int, bool)
    solo_changed = Signal(int, bool)
    select_changed = Signal(int, bool)
    name_changed = Signal(int, str)
    colour_changed = Signal(int, str)

    def __init__(self, index: int) -> None:
        super().__init__()
        self.index = index
        self._updating = False
        self._button_actions = {
            "mute": "track_mute",
            "pafl": "track_solo",
            "mix": "context_select",
        }
        self.setMinimumWidth(82)
        self.setMaximumWidth(132)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(3)

        self.name = QLineEdit(f"MIDI {index + 1:02d}")
        self.name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.name.setMinimumWidth(68)
        self.name.setMaxLength(8)
        self.name.setToolTip(
            "Editable channel name (maximum 8 characters). The GLD strip LCD shows up to 5 characters."
        )
        self.name.editingFinished.connect(self._name_edited)
        layout.addWidget(self.name)

        self.colour_bar = QFrame()
        self.colour_bar.setFixedHeight(6)
        self.colour_bar.setFrameShape(QFrame.Shape.Box)
        layout.addWidget(self.colour_bar)

        self.pan = QDial()
        self.pan.setRange(0, 127)
        self.pan.setValue(64)
        self.pan.setNotchesVisible(True)
        self.pan.setWrapping(False)
        self.pan.setMinimumSize(46, 46)
        self.pan.setMaximumSize(72, 72)
        self.pan.setToolTip("Pan / MCU V-Pot")
        self.pan.valueChanged.connect(self._pan_changed)
        layout.addWidget(self.pan, alignment=Qt.AlignmentFlag.AlignCenter)

        self.pan_text = QLabel("CENTER")
        self.pan_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pan_text.setToolTip("Exact Pan position: L 100% … CENTER … R 100%")
        layout.addWidget(self.pan_text)

        self.pan_center = QPushButton("CENTER")
        self.pan_center.setToolTip("Set Pan to the exact MCU/GLD centre value (64)")
        self.pan_center.clicked.connect(self._center_pan)
        layout.addWidget(self.pan_center)

        self.fader = QSlider(Qt.Orientation.Vertical)
        self.fader.setRange(0, 127)
        self.fader.setValue(0)
        self.fader.setInvertedAppearance(False)
        self.fader.setMinimumHeight(150)
        self.fader.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.fader.valueChanged.connect(self._fader_changed)
        layout.addWidget(self.fader, alignment=Qt.AlignmentFlag.AlignCenter, stretch=1)

        self.db = QLabel("-∞")
        self.db.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.db)

        self.zero_db = QPushButton("0 dB")
        self.zero_db.setToolTip("Move this fader to the measured GLD MIDI Strip 0.0 dB position")
        self.zero_db.clicked.connect(self._set_zero_db)
        layout.addWidget(self.zero_db)

        self.mute = QPushButton("MUTE")
        self.mute.setCheckable(True)
        self.mute.toggled.connect(self._mute_changed)
        layout.addWidget(self.mute)

        key_row = QHBoxLayout()
        key_row.setContentsMargins(0, 0, 0, 0)
        key_row.setSpacing(2)
        self.solo = QPushButton("PAFL")
        self.solo.setCheckable(True)
        self.solo.setToolTip("Solo / MCU Solo")
        self.solo.toggled.connect(self._solo_changed)
        self.select_button = QPushButton("MIX")
        self.select_button.setCheckable(True)
        self.select_button.setToolTip("Select / MCU Select")
        self.select_button.toggled.connect(self._select_changed)
        key_row.addWidget(self.solo)
        key_row.addWidget(self.select_button)
        layout.addLayout(key_row)

        self.colour = QComboBox()
        display_names = {
            "off": "Off",
            "red": "Red",
            "green": "Green",
            "yellow": "Yellow",
            "blue": "Blue",
            "purple": "Purple",
            "light_blue": "Light blue",
            "white": "White",
        }
        for value in COLOURS.keys():
            self.colour.addItem(display_names.get(value, value), value)
        self.colour.currentIndexChanged.connect(self._colour_changed)
        self.colour.setToolTip(
            "MIDI Strip colour. With GLD Editor control enabled, this is also sent to the physical LCD."
        )
        layout.addWidget(self.colour)

        self.set_state(ChannelState(index))

    @staticmethod
    def _state_for_action(state: ChannelState, action: str) -> bool:
        return {
            "track_mute": bool(state.mute),
            "track_solo": bool(state.solo),
            "track_select": bool(state.select),
            "context_select": bool(state.select),
            "track_record": bool(getattr(state, "record", False)),
        }.get(str(action), False)

    @staticmethod
    def _label_for_action(source: str, action: str) -> str:
        defaults = {
            ("mute", "track_mute"): "MUTE",
            ("pafl", "track_solo"): "PAFL",
            ("mix", "context_select"): "MIX",
            ("mix", "track_select"): "MIX",
        }
        if (source, action) in defaults:
            return defaults[(source, action)]
        return {
            "track_mute": "MUTE",
            "track_solo": "SOLO",
            "track_select": "SELECT",
            "context_select": "MIX/VP",
            "track_record": "REC",
            "vpot_push": "VPUSH",
            "disabled": "OFF",
        }.get(action, action[:7].upper())

    @staticmethod
    def _style_for_action(action: str, active: bool) -> str:
        colour = {
            "track_mute": "#b33",
            "track_record": "#b33",
            "track_solo": "#d7a900",
            "track_select": "#2d78c8",
            "context_select": "#2d78c8",
        }.get(action, "#666")
        return ChannelStrip._active_style(active, colour)

    def set_button_mapping(self, mute_action: str, pafl_action: str, mix_action: str) -> None:
        self._button_actions = {
            "mute": str(mute_action),
            "pafl": str(pafl_action),
            "mix": str(mix_action),
        }
        self.mute.setText(self._label_for_action("mute", self._button_actions["mute"]))
        self.solo.setText(self._label_for_action("pafl", self._button_actions["pafl"]))
        self.select_button.setText(self._label_for_action("mix", self._button_actions["mix"]))
        self.mute.setToolTip(f"Mapped action: {self._button_actions['mute']}")
        self.solo.setToolTip(f"Mapped action: {self._button_actions['pafl']}")
        self.select_button.setToolTip(f"Mapped action: {self._button_actions['mix']}")

    def _fader_changed(self, value: int) -> None:
        self.db.setText(fader_value_to_db(value))
        if not self._updating:
            self.fader_changed.emit(self.index, value)

    def _set_zero_db(self) -> None:
        self.fader.setValue(GLD_FADER_0DB_VALUE)

    def _pan_changed(self, value: int) -> None:
        self.pan_text.setText(self._format_pan(value))
        if not self._updating:
            self.pan_changed.emit(self.index, value)

    def _center_pan(self) -> None:
        self.pan.setValue(64)

    @staticmethod
    def _format_pan(value: int) -> str:
        value = max(0, min(127, int(value)))
        if value == 64:
            return "CENTER"
        if value < 64:
            percent = int(round((64 - value) * 100 / 64))
            return f"L {percent}%"
        percent = int(round((value - 64) * 100 / 63))
        return f"R {percent}%"

    def _mute_changed(self, value: bool) -> None:
        if not self._updating:
            self.mute_changed.emit(self.index, value)

    def _solo_changed(self, value: bool) -> None:
        if not self._updating:
            self.solo_changed.emit(self.index, value)

    def _select_changed(self, value: bool) -> None:
        if not self._updating:
            self.select_changed.emit(self.index, value)

    def _name_edited(self) -> None:
        if self._updating:
            return
        name = self.name.text()[:8]
        self.name_changed.emit(self.index, name)

    def _colour_changed(self, _index: int) -> None:
        value = str(self.colour.currentData() or "off")
        self._apply_colour(value)
        if not self._updating:
            self.colour_changed.emit(self.index, value)

    def _apply_colour(self, colour: str) -> None:
        css = {
            "off": "background: #333;",
            "red": "background: #cc3333;",
            "green": "background: #35a854;",
            "yellow": "background: #c7a600;",
            "blue": "background: #3268d3;",
            "purple": "background: #7b42cc;",
            "light_blue": "background: #40a8d8;",
            "white": "background: #ddd;",
        }.get(colour, "background: #333;")
        self.colour_bar.setStyleSheet(css)

    @staticmethod
    def _active_style(active: bool, colour: str) -> str:
        return f"font-weight: bold; background: {colour};" if active else ""

    def set_state(self, state: ChannelState, *, force_text: bool = False) -> None:
        self._updating = True
        try:
            # Do not overwrite text while the user is actively typing, especially
            # during Vegas mode when fader updates arrive frequently.
            if (force_text or not self.name.hasFocus()) and self.name.text() != state.name[:8]:
                self.name.setText(state.name[:8])
            self.fader.setValue(state.fader)
            self.db.setText(fader_value_to_db(state.fader))
            self.pan.setValue(state.pan)
            self.pan_text.setText(self._format_pan(state.pan))
            mute_active = self._state_for_action(state, self._button_actions["mute"])
            solo_active = self._state_for_action(state, self._button_actions["pafl"])
            select_active = self._state_for_action(state, self._button_actions["mix"])
            self.mute.setChecked(mute_active)
            self.mute.setStyleSheet(self._style_for_action(self._button_actions["mute"], mute_active))
            self.solo.setChecked(solo_active)
            self.solo.setStyleSheet(self._style_for_action(self._button_actions["pafl"], solo_active))
            self.select_button.setChecked(select_active)
            self.select_button.setStyleSheet(self._style_for_action(self._button_actions["mix"], select_active))
            idx = self.colour.findData(state.colour)
            if idx >= 0 and idx != self.colour.currentIndex():
                self.colour.setCurrentIndex(idx)
            self._apply_colour(state.colour)
        finally:
            self._updating = False

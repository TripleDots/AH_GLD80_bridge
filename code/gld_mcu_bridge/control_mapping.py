from __future__ import annotations

from copy import deepcopy
from typing import Any

MAPPING_FILE_VERSION = 1

CONTROL_ROWS: list[tuple[str, str]] = [
    ("fader", "Motor fader"),
    ("gain", "Gain rotary layer"),
    ("pan", "Pan rotary layer"),
    ("custom1", "Custom 1 rotary layer"),
    ("custom2", "Custom 2 rotary layer"),
    ("mute", "Mute button"),
    ("mix", "MIX button"),
    ("pafl", "PAFL button"),
]

DEFAULT_MCU_MAPPING: dict[str, Any] = {
    "controls": {
        "fader": "track_fader",
        "gain": "send_fader_select",
        "pan": "context_pan",
        "custom1": "context_bank",
        "custom2": "context_channel",
        "mute": "track_mute",
        "mix": "context_select",
        "pafl": "track_solo",
    },
    "softkeys": [
        *[f"mcu_note:{0x36 + index:02X}" for index in range(8)],
        "plugin_toggle",
        "record_selected",
    ],
}

DEFAULT_HUI_MAPPING: dict[str, Any] = {
    "controls": {
        "fader": "track_fader",
        "gain": "disabled",
        "pan": "track_pan",
        "custom1": "disabled",
        "custom2": "disabled",
        "mute": "track_mute",
        "mix": "track_select",
        "pafl": "track_solo",
    },
    "softkeys": ["disabled"] * 10,
}

DEFAULT_CONTROL_MAPPINGS: dict[str, Any] = {
    "version": MAPPING_FILE_VERSION,
    "mcu": DEFAULT_MCU_MAPPING,
    "hui": DEFAULT_HUI_MAPPING,
}

MCU_CONTROL_OPTIONS: dict[str, list[tuple[str, str]]] = {
    "fader": [
        ("track_fader", "Track volume (motor fader)"),
        ("disabled", "Disabled"),
    ],
    "gain": [
        ("send_fader_select", "Send fader flip + previous/next Send"),
        ("context_send", "Legacy Send level rotary"),
        ("track_pan", "Track Pan"),
        ("disabled", "Disabled"),
    ],
    "pan": [
        ("context_pan", "Context V-Pot: Pan / plug-in parameter"),
        ("track_pan", "Track Pan only"),
        ("context_send", "Send level"),
        ("disabled", "Disabled"),
    ],
    "custom1": [
        ("context_bank", "Context: Bank / plug-in page"),
        ("bank_navigation", "Bank Left / Right"),
        ("channel_navigation", "Channel Left / Right"),
        ("disabled", "Disabled"),
    ],
    "custom2": [
        ("context_channel", "Context: Channel / previous-next plug-in"),
        ("bank_navigation", "Bank Left / Right"),
        ("channel_navigation", "Channel Left / Right"),
        ("disabled", "Disabled"),
    ],
    "mute": [
        ("track_mute", "Track Mute"),
        ("track_solo", "Track Solo"),
        ("track_select", "Track Select"),
        ("track_record", "Track REC/RDY"),
        ("vpot_push", "V-Pot push"),
        ("disabled", "Disabled"),
    ],
    "mix": [
        ("context_select", "Context: Track Select / plug-in V-Pot push"),
        ("track_select", "Track Select only"),
        ("track_mute", "Track Mute"),
        ("track_solo", "Track Solo"),
        ("track_record", "Track REC/RDY"),
        ("vpot_push", "V-Pot push"),
        ("disabled", "Disabled"),
    ],
    "pafl": [
        ("track_solo", "Track Solo"),
        ("track_mute", "Track Mute"),
        ("track_select", "Track Select"),
        ("track_record", "Track REC/RDY"),
        ("vpot_push", "V-Pot push"),
        ("disabled", "Disabled"),
    ],
}

HUI_CONTROL_OPTIONS: dict[str, list[tuple[str, str]]] = {
    "fader": [
        ("track_fader", "Track volume (motor fader)"),
        ("disabled", "Disabled"),
    ],
    "gain": [
        ("track_pan", "Track Pan"),
        ("disabled", "Disabled"),
    ],
    "pan": [
        ("track_pan", "Track Pan"),
        ("disabled", "Disabled"),
    ],
    "custom1": [("disabled", "Disabled")],
    "custom2": [("disabled", "Disabled")],
    "mute": [
        ("track_mute", "Track Mute"),
        ("track_solo", "Track Solo"),
        ("track_select", "Track Select"),
        ("disabled", "Disabled"),
    ],
    "mix": [
        ("track_select", "Track Select"),
        ("track_mute", "Track Mute"),
        ("track_solo", "Track Solo"),
        ("disabled", "Disabled"),
    ],
    "pafl": [
        ("track_solo", "Track Solo"),
        ("track_mute", "Track Mute"),
        ("track_select", "Track Select"),
        ("disabled", "Disabled"),
    ],
}

KNOWN_MCU_NOTES: dict[int, str] = {
    0x28: "Track assignment",
    0x29: "Send assignment",
    0x2A: "Pan assignment",
    0x2B: "Plug-in assignment",
    0x2C: "EQ assignment",
    0x2D: "Instrument assignment",
    0x2E: "Bank Left",
    0x2F: "Bank Right",
    0x30: "Channel Left",
    0x31: "Channel Right",
    0x32: "Flip",
    0x33: "Global View",
    0x36: "F1",
    0x37: "F2",
    0x38: "F3",
    0x39: "F4",
    0x3A: "F5",
    0x3B: "F6",
    0x3C: "F7",
    0x3D: "F8",
    0x50: "Save",
    0x51: "Undo",
    0x60: "Cursor Up",
    0x61: "Cursor Down",
    0x62: "Cursor Left",
    0x63: "Cursor Right",
}


def mcu_softkey_options() -> list[tuple[str, str]]:
    options = [
        ("disabled", "Disabled"),
        ("plugin_toggle", "Bridge: Plug-in / Pan toggle"),
        ("record_selected", "Bridge: REC/RDY selected channels"),
    ]
    for note in range(128):
        label = KNOWN_MCU_NOTES.get(note, "DAW/host-specific")
        options.append((f"mcu_note:{note:02X}", f"MCU Note 0x{note:02X} — {label}"))
    return options


def hui_softkey_options() -> list[tuple[str, str]]:
    options = [
        ("disabled", "Disabled"),
        ("hui_selected:mute", "Selected HUI channel: Mute"),
        ("hui_selected:solo", "Selected HUI channel: Solo"),
        ("hui_selected:select", "Selected HUI channel: Select"),
    ]
    for zone in range(8):
        for port in range(8):
            options.append(
                (
                    f"hui_switch:{zone}:{port}",
                    f"Raw HUI switch — zone {zone}, port {port}",
                )
            )
    return options


def _valid_action_ids(protocol: str, control: str) -> set[str]:
    options = MCU_CONTROL_OPTIONS if protocol == "mcu" else HUI_CONTROL_OPTIONS
    return {action for action, _label in options[control]}


def normalise_control_mappings(value: Any) -> dict[str, Any]:
    """Return a complete, safe mapping profile while preserving known values."""
    result = deepcopy(DEFAULT_CONTROL_MAPPINGS)
    if not isinstance(value, dict):
        return result

    for protocol in ("mcu", "hui"):
        source = value.get(protocol)
        if not isinstance(source, dict):
            continue
        controls = source.get("controls")
        if isinstance(controls, dict):
            for control, _label in CONTROL_ROWS:
                action = str(controls.get(control, ""))
                if action in _valid_action_ids(protocol, control):
                    result[protocol]["controls"][control] = action
        softkeys = source.get("softkeys")
        if isinstance(softkeys, list):
            valid_prefixes = (
                ("disabled", "plugin_toggle", "record_selected", "mcu_note:")
                if protocol == "mcu"
                else ("disabled", "hui_selected:", "hui_switch:")
            )
            for index, action in enumerate(softkeys[:10]):
                action = str(action)
                if any(action == prefix or action.startswith(prefix) for prefix in valid_prefixes):
                    result[protocol]["softkeys"][index] = action

    result["version"] = MAPPING_FILE_VERSION
    return result


def mapping_file_payload(mappings: Any) -> dict[str, Any]:
    return {
        "format": "GLD80 MCU Bridge control mapping",
        "version": MAPPING_FILE_VERSION,
        "mappings": normalise_control_mappings(mappings),
    }


def mappings_from_file_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("The selected file does not contain a mapping object.")
    if payload.get("format") == "GLD80 MCU Bridge control mapping":
        return normalise_control_mappings(payload.get("mappings"))
    # Also accept the plain mapping object for hand-edited profiles.
    return normalise_control_mappings(payload)


def control_action_label(protocol: str, control: str, action: str) -> str:
    options = MCU_CONTROL_OPTIONS if str(protocol).lower() == "mcu" else HUI_CONTROL_OPTIONS
    return dict(options.get(control, [])).get(str(action), str(action))


def softkey_action_label(protocol: str, action: str) -> str:
    options = mcu_softkey_options() if str(protocol).lower() == "mcu" else hui_softkey_options()
    return dict(options).get(str(action), str(action))

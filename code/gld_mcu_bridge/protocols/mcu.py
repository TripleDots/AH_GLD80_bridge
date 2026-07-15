"""Mackie Control Universal helpers.

This is intentionally conservative and uses the common MCU core messages:
- 8 faders per MCU device: pitch bend on channels 1..8
- Mute: note 16..23
- Solo: note 8..15
- Select: note 24..31
- V-Pot rotation: CC 16..23 with relative values
- Scribble strip: F0 00 00 66 14 12 <offset> <ascii...> F7
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

import mido

MCU_CHANNELS_PER_PORT = 8
NOTE_REC = 0
NOTE_SOLO = 8
NOTE_MUTE = 16
NOTE_SELECT = 24
NOTE_VPOT_PUSH = 32
CC_VPOT = 16
CC_VPOT_LED = 48

# Common MCU global button notes. Soft 1-8 map to F1-F8, Soft 9 to the
# standard Plug-in assignment button, and Soft 10 is handled as per-channel
# REC/RDY clicks by the bridge. Bank/Channel order follows the MCU layout:
# bank down, channel down, bank up, channel up.
NOTE_BANK_LEFT = 0x2E
NOTE_BANK_RIGHT = 0x2F
NOTE_CHANNEL_LEFT = 0x30
NOTE_CHANNEL_RIGHT = 0x31
NOTE_FLIP = 0x32
NOTE_SEND_ASSIGNMENT = 0x29
NOTE_PAN_ASSIGNMENT = 0x2A
NOTE_PLUGIN = 0x2B
NOTE_CURSOR_UP = 0x60
NOTE_CURSOR_DOWN = 0x61
NOTE_CURSOR_LEFT = 0x62
NOTE_CURSOR_RIGHT = 0x63
NOTE_F1 = 0x36
NOTE_F8 = 0x3D
NOTE_SAVE = 0x50
NOTE_UNDO = 0x51
SOFTKEY_MCU_NOTES = [
    NOTE_F1 + 0, NOTE_F1 + 1, NOTE_F1 + 2, NOTE_F1 + 3,
    NOTE_F1 + 4, NOTE_F1 + 5, NOTE_F1 + 6, NOTE_F1 + 7,
    NOTE_PLUGIN,
]
SOFTKEY_MCU_LABELS = [
    "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "Plug-in", "REC sel"
]


@dataclass(frozen=True)
class MCUEvent:
    kind: Literal["fader", "rec", "mute", "solo", "select", "scribble", "vpot", "vpot_led"]
    track: int  # 0..7 within one MCU unit
    value: int | bool | str


def clamp7(value: int) -> int:
    return max(0, min(127, int(value)))


def value7_to_pitch14(value: int) -> int:
    """Map 0..127 GLD/CC value to MCU 14-bit fader value 0..16383."""
    return int(round(clamp7(value) * 16383 / 127))


def pitch14_to_value7(value: int) -> int:
    return clamp7(int(round(max(0, min(16383, int(value))) * 127 / 16383)))


def make_fader14(track: int, value14: int) -> mido.Message:
    """Create an MCU motor-fader message without reducing it to 7-bit first."""
    track = int(track) % MCU_CHANNELS_PER_PORT
    value14 = max(0, min(16383, int(value14)))
    return mido.Message("pitchwheel", channel=track, pitch=value14 - 8192)


def make_fader(track: int, value7: int) -> mido.Message:
    """Compatibility helper for callers that have a GLD 7-bit value."""
    return make_fader14(track, value7_to_pitch14(value7))


def make_note(note_base: int, track: int, on: bool) -> mido.Message:
    return mido.Message("note_on", channel=0, note=note_base + (int(track) % 8), velocity=127 if on else 0)


def make_record(track: int, on: bool) -> mido.Message:
    return make_note(NOTE_REC, track, on)


def make_mute(track: int, on: bool) -> mido.Message:
    return make_note(NOTE_MUTE, track, on)


def make_solo(track: int, on: bool) -> mido.Message:
    return make_note(NOTE_SOLO, track, on)


def make_select(track: int, on: bool) -> mido.Message:
    return make_note(NOTE_SELECT, track, on)


def make_button_click(note_base: int, track: int) -> list[mido.Message]:
    """Create one complete MCU button click (press followed by release).

    MCU buttons are momentary controls.  Their LED/state feedback is a
    separate message sent by the DAW.  Sending only an ON or only an OFF
    message makes toggles appear intermittent, especially when an on-screen
    checkable button is used as the source.
    """
    return [
        make_note(note_base, track, True),
        make_note(note_base, track, False),
    ]


def make_record_click(track: int) -> list[mido.Message]:
    return make_button_click(NOTE_REC, track)


def make_mute_click(track: int) -> list[mido.Message]:
    return make_button_click(NOTE_MUTE, track)


def make_solo_click(track: int) -> list[mido.Message]:
    return make_button_click(NOTE_SOLO, track)


def make_select_click(track: int) -> list[mido.Message]:
    return make_button_click(NOTE_SELECT, track)


def make_vpot_push_click(track: int) -> list[mido.Message]:
    return make_button_click(NOTE_VPOT_PUSH, track)


def make_global_button_click(note: int) -> list[mido.Message]:
    """Create a standard MCU global-button press/release pair."""
    note = max(0, min(127, int(note)))
    return [
        mido.Message("note_on", channel=0, note=note, velocity=127),
        mido.Message("note_on", channel=0, note=note, velocity=0),
    ]


def make_flip_click() -> list[mido.Message]:
    """Toggle the standard MCU Flip state."""
    return make_global_button_click(NOTE_FLIP)


def make_send_assignment_click() -> list[mido.Message]:
    """Select the standard MCU Send assignment/view."""
    return make_global_button_click(NOTE_SEND_ASSIGNMENT)


def make_pan_assignment_click() -> list[mido.Message]:
    """Select the standard MCU Pan/Surround assignment/view."""
    return make_global_button_click(NOTE_PAN_ASSIGNMENT)


def make_plugin_exit_click() -> list[mido.Message]:
    """Compatibility alias for returning a host to Pan/Surround assignment."""
    return make_pan_assignment_click()


def make_softkey_click(index: int) -> list[mido.Message]:
    """Map bridge Soft 1-10 to standard MCU global controls.

    Soft 1-8 are F1-F8 and Soft 9 is the standard MCU Plug-in button.
    Soft 10 is context-sensitive in :class:`BridgeEngine` and record-arms the
    selected channel(s), so it has no single global MCU note here.
    """
    index = int(index)
    if not 0 <= index < len(SOFTKEY_MCU_NOTES):
        return []
    return make_global_button_click(SOFTKEY_MCU_NOTES[index])


NAVIGATION_MCU_NOTES = {
    "bank_left": NOTE_BANK_LEFT,
    "bank_right": NOTE_BANK_RIGHT,
    "channel_left": NOTE_CHANNEL_LEFT,
    "channel_right": NOTE_CHANNEL_RIGHT,
}


def make_navigation_click(action: str) -> list[mido.Message]:
    """Create a standard MCU Bank/Channel navigation press/release pair."""
    note = NAVIGATION_MCU_NOTES.get(str(action).lower())
    if note is None:
        return []
    return make_global_button_click(note)



PLUGIN_NAVIGATION_MCU_NOTES = {
    # The GLD Custom rotary keeps its familiar Bank/Channel gesture names,
    # but a real MCU plug-in editor uses cursor keys for page/insert movement.
    "bank_left": NOTE_CURSOR_LEFT,
    "bank_right": NOTE_CURSOR_RIGHT,
    "channel_left": NOTE_CURSOR_UP,
    "channel_right": NOTE_CURSOR_DOWN,
}





def make_plugin_navigation_click(action: str) -> list[mido.Message]:
    """Create standard MCU cursor navigation for plug-in pages/inserts.

    Bank-style left/right gestures become Cursor Left/Right (parameter pages),
    and Channel-style left/right gestures become Cursor Up/Down (insert slot).
    """
    note = PLUGIN_NAVIGATION_MCU_NOTES.get(str(action).lower())
    if note is None:
        return []
    return make_global_button_click(note)



def make_send_navigation_click(direction: str) -> list[mido.Message]:
    """Choose the previous/next Send in a standard MCU Send assignment page.

    MCU hosts conventionally use Cursor Up/Down to move through Send slots.
    Clockwise GLD GAIN movement maps to ``next`` (Cursor Down);
    counter-clockwise movement maps to ``previous`` (Cursor Up).
    """
    direction = str(direction).strip().lower()
    if direction in {"previous", "prev", "up", "left"}:
        note = NOTE_CURSOR_UP
    elif direction in {"next", "down", "right"}:
        note = NOTE_CURSOR_DOWN
    else:
        return []
    return make_global_button_click(note)

def make_vpot_relative(track: int, steps: int) -> list[mido.Message]:
    """Make MCU relative V-Pot ticks.

    Positive steps rotate clockwise; negative steps rotate counter-clockwise.
    The step value is split to keep data bytes in the simple 1..15 / 65..79 range.
    """
    messages: list[mido.Message] = []
    steps = max(-127, min(127, int(steps)))
    if steps == 0:
        return messages
    direction_positive = steps > 0
    remaining = abs(steps)
    while remaining:
        chunk = min(15, remaining)
        data = chunk if direction_positive else 0x40 + chunk
        messages.append(mido.Message("control_change", channel=0, control=CC_VPOT + (track % 8), value=data))
        remaining -= chunk
    return messages


def pan_value_delta_to_vpot(old_value7: int, new_value7: int, sensitivity: int = 1) -> list[mido.Message]:
    raise NotImplementedError("Use make_pan_delta(track, old, new, sensitivity) instead.")


def make_pan_delta(track: int, old_value7: int, new_value7: int, sensitivity: int = 1) -> list[mido.Message]:
    """Translate an absolute UI Pan change into relative MCU ticks.

    ``sensitivity`` is a speed multiplier: 1 means one MCU tick per UI step,
    2 is twice as fast, and so on.
    """
    delta = int(new_value7) - int(old_value7)
    if delta == 0:
        return []
    steps = max(-127, min(127, delta * max(1, int(sensitivity))))
    return make_vpot_relative(track, steps)



def decode_vpot_relative(value: int) -> int:
    """Decode the common MCU relative V-Pot byte to signed ticks."""
    value = int(value) & 0x7F
    if 0x01 <= value <= 0x3F:
        return value
    if 0x41 <= value <= 0x7F:
        return -(value - 0x40)
    return 0


def vpot_led_to_pan(value: int) -> int | None:
    """Decode a standard MCU V-Pot LED-ring position.

    Positions 1..11 map to 0..127. Position 0 is returned as ``-1`` so the
    bridge can distinguish an intentionally dark ring (useful for a Send at
    ``-inf`` or a zero plug-in parameter) from malformed positions 12..15.
    Normal track Pan ignores the dark-ring sentinel.
    """
    position = int(value) & 0x0F
    if position == 0:
        return -1
    if not 1 <= position <= 11:
        return None
    return int(round((position - 1) * 127 / 10))

def parse_message(msg: mido.Message) -> Optional[MCUEvent]:
    if msg.type == "pitchwheel" and 0 <= msg.channel < 8:
        value14 = msg.pitch + 8192
        return MCUEvent("fader", msg.channel, max(0, min(16383, value14)))

    if msg.type in ("note_on", "note_off"):
        velocity = 0 if msg.type == "note_off" else msg.velocity
        on = velocity > 0
        note = msg.note
        if NOTE_REC <= note <= NOTE_REC + 7:
            return MCUEvent("rec", note - NOTE_REC, on)
        if NOTE_MUTE <= note <= NOTE_MUTE + 7:
            return MCUEvent("mute", note - NOTE_MUTE, on)
        if NOTE_SOLO <= note <= NOTE_SOLO + 7:
            return MCUEvent("solo", note - NOTE_SOLO, on)
        if NOTE_SELECT <= note <= NOTE_SELECT + 7:
            return MCUEvent("select", note - NOTE_SELECT, on)

    if msg.type == "control_change" and CC_VPOT <= msg.control <= CC_VPOT + 7:
        return MCUEvent("vpot", msg.control - CC_VPOT, msg.value)
    if msg.type == "control_change" and CC_VPOT_LED <= msg.control <= CC_VPOT_LED + 7:
        pan = vpot_led_to_pan(msg.value)
        if pan is not None:
            return MCUEvent("vpot_led", msg.control - CC_VPOT_LED, pan)

    return None


def is_scribble_sysex(msg: mido.Message) -> bool:
    if msg.type != "sysex":
        return False
    data = list(msg.data)
    # 00 00 66 14 12 is Mackie Control; 00 00 66 15 12 is often extender.
    return len(data) >= 6 and data[0:3] == [0x00, 0x00, 0x66] and data[4] == 0x12


def parse_scribble_sysex(msg: mido.Message) -> tuple[int, str] | None:
    """Return (offset, text) from MCU scribble strip sysex, if present."""
    if not is_scribble_sysex(msg):
        return None
    data = list(msg.data)
    offset = data[5]
    text = "".join(chr(b) if 32 <= b <= 126 else " " for b in data[6:])
    return offset, text


class ScribbleBuffer:
    """Keeps a 56-char MCU scribble line per port and extracts 8 names."""

    def __init__(self) -> None:
        self._chars = [" "] * 56

    def update(self, offset: int, text: str) -> list[str]:
        for i, ch in enumerate(text):
            pos = offset + i
            if 0 <= pos < len(self._chars):
                self._chars[pos] = ch
        return self.names()

    def names(self) -> list[str]:
        return ["".join(self._chars[i * 7:(i + 1) * 7]).strip() or f"Ch {i+1}" for i in range(8)]

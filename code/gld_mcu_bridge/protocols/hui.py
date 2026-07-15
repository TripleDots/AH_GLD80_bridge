"""Mackie HUI protocol helpers used for Pro Tools compatibility.

The implementation intentionally focuses on the 8-channel HUI core needed by
this bridge: motor faders, fader touch, Pan V-Pots, Select, Mute, Solo, display
names and the HUI ping/reply.  Four independent MIDI port pairs provide 32
tracks.

HUI switch messages use a channel-strip zone (0..7) and a port within that
zone.  Relevant channel-strip ports are:

- 0: fader touch
- 1: select
- 2: mute
- 3: solo
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import mido

HUI_CHANNELS_PER_PORT = 8
CC_TX_ZONE = 0x0F       # surface -> host zone select
CC_TX_PORT = 0x2F       # surface -> host switch press/release
CC_RX_ZONE = 0x0C       # host -> surface zone/LED select
CC_RX_PORT = 0x2C       # host -> surface LED state
CC_VPOT_RING_BASE = 0x10
CC_VPOT_DELTA_BASE = 0x40

PORT_FADER_TOUCH = 0
PORT_SELECT = 1
PORT_MUTE = 2
PORT_SOLO = 3


@dataclass(frozen=True)
class HUIEvent:
    kind: Literal["ping", "fader", "mute", "solo", "select", "vpot_led"]
    track: int = 0
    value: int | bool = 0


def clamp7(value: int) -> int:
    return max(0, min(127, int(value)))


def value7_to_fader14(value: int) -> int:
    return int(round(clamp7(value) * 16383 / 127))


def fader14_to_value7(value: int) -> int:
    value = max(0, min(16383, int(value)))
    return clamp7(int(round(value * 127 / 16383)))


def make_ping_reply() -> mido.Message:
    return mido.Message("note_on", channel=0, note=0, velocity=127)


def make_fader(track: int, value14: int) -> list[mido.Message]:
    track = int(track) % HUI_CHANNELS_PER_PORT
    value14 = max(0, min(16383, int(value14)))
    hi = (value14 >> 7) & 0x7F
    lo = value14 & 0x7F
    return [
        mido.Message("control_change", channel=0, control=track, value=hi),
        mido.Message("control_change", channel=0, control=0x20 + track, value=lo),
    ]


def make_switch(track: int, port: int, on: bool) -> list[mido.Message]:
    """Create a surface-to-host HUI zone/port switch message."""
    track = int(track) % HUI_CHANNELS_PER_PORT
    port = max(0, min(7, int(port)))
    return [
        mido.Message("control_change", channel=0, control=CC_TX_ZONE, value=track),
        mido.Message(
            "control_change",
            channel=0,
            control=CC_TX_PORT,
            value=(0x40 if on else 0x00) | port,
        ),
    ]


def make_switch_click(track: int, port: int) -> list[mido.Message]:
    return [*make_switch(track, port, True), *make_switch(track, port, False)]


def make_fader_touch(track: int, on: bool) -> list[mido.Message]:
    return make_switch(track, PORT_FADER_TOUCH, on)


def make_pan_relative(track: int, ticks: int) -> list[mido.Message]:
    """Create signed relative HUI V-Pot ticks."""
    ticks = max(-127, min(127, int(ticks)))
    if ticks == 0:
        return []
    messages: list[mido.Message] = []
    remaining = abs(ticks)
    while remaining:
        chunk = min(0x2D, remaining)
        # HUI: values > 0x40 are positive, values < 0x40 are negative.
        encoded = 0x40 + chunk if ticks > 0 else chunk
        messages.append(
            mido.Message(
                "control_change",
                channel=0,
                control=CC_VPOT_DELTA_BASE + (int(track) % HUI_CHANNELS_PER_PORT),
                value=encoded,
            )
        )
        remaining -= chunk
    return messages


def make_pan_delta(track: int, old_value7: int, new_value7: int, sensitivity: int = 1) -> list[mido.Message]:
    """Translate an absolute UI Pan change into relative HUI ticks."""
    delta = int(new_value7) - int(old_value7)
    if delta == 0:
        return []
    return make_pan_relative(track, delta * max(1, int(sensitivity)))


def vpot_ring_to_pan(value: int) -> int | None:
    """Decode the common 11-LED HUI ring (positions 0..10)."""
    position = int(value) & 0x0F
    if not 0 <= position <= 10:
        return None
    return int(round(position * 127 / 10))


def _display_char(value: int) -> str:
    value = int(value) & 0x7F
    return chr(value) if 32 <= value <= 126 else " "


def parse_display_sysex(msg: mido.Message) -> list[tuple[int, str]]:
    """Return ``[(local_track, name), ...]`` from HUI display SysEx."""
    if msg.type != "sysex":
        return []
    data = list(msg.data)
    if len(data) < 7 or data[:5] != [0x00, 0x00, 0x66, 0x05, 0x00]:
        return []
    command = data[5]
    payload = data[6:]
    if command == 0x10 and len(payload) >= 5:
        track = payload[0]
        if 0 <= track < 8:
            name = "".join(_display_char(v) for v in payload[1:5]).strip()
            return [(track, name or f"Ch {track + 1}")]
        return []
    if command == 0x12:
        output: list[tuple[int, str]] = []
        # Large-display packets consist of one or more 11-byte zones:
        # zone number followed by ten characters.
        for start in range(0, len(payload) - 10, 11):
            zone = payload[start]
            if not 0 <= zone < 8:
                continue
            text = "".join(_display_char(v) for v in payload[start + 1:start + 11]).strip()
            output.append((zone, text[:8] or f"Ch {zone + 1}"))
        return output
    return []


class HUIParser:
    """Stateful parser for messages sent from a HUI host to one surface."""

    def __init__(self) -> None:
        self.selected_zone: int | None = None
        self.fader_hi = [0] * HUI_CHANNELS_PER_PORT

    def parse(self, msg: mido.Message) -> HUIEvent | None:
        if msg.type == "note_on" and msg.channel == 0 and msg.note == 0 and msg.velocity == 0:
            return HUIEvent("ping")
        if msg.type != "control_change" or msg.channel != 0:
            return None

        control = int(msg.control)
        value = int(msg.value) & 0x7F
        if control == CC_RX_ZONE:
            self.selected_zone = value
            return None
        if control == CC_RX_PORT:
            zone = self.selected_zone
            if zone is None or not 0 <= zone < 8:
                return None
            port = value & 0x07
            on = bool(value & 0x40)
            kind = {
                PORT_SELECT: "select",
                PORT_MUTE: "mute",
                PORT_SOLO: "solo",
            }.get(port)
            return HUIEvent(kind, zone, on) if kind is not None else None
        if 0x00 <= control <= 0x07:
            self.fader_hi[control] = value
            return None
        if 0x20 <= control <= 0x27:
            track = control - 0x20
            value14 = (self.fader_hi[track] << 7) | value
            return HUIEvent("fader", track, value14)
        if CC_VPOT_RING_BASE <= control <= CC_VPOT_RING_BASE + 7:
            pan = vpot_ring_to_pan(value)
            return HUIEvent("vpot_led", control - CC_VPOT_RING_BASE, pan) if pan is not None else None
        return None

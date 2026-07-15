"""Allen & Heath GLD MIDI/TCP protocol helpers.

The defaults implemented here follow the GLD Template Show MIDI Strip
messages documented by Allen & Heath:

- Fader:       B1 00..1F <VAR>
- Rotary Pan:  B2 20..3F <VAR>
- Mute key:    91 00..1F <VAR>
- Mix key:     91 20..3F <VAR>
- PAFL key:    91 40..5F <VAR>

Mido uses zero-based MIDI channel numbers, so hex B1/91 is channel=1.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

import mido

GLD_MIDI_STRIPS = 32
GLD_MIDI_STRIP_CC_CHANNEL = 1   # hex B1: MIDI channel 2, zero-based in mido
GLD_MIDI_STRIP_ROT_CHANNEL = 2  # hex B2: MIDI channel 3, zero-based in mido
GLD_MIDI_STRIP_NOTE_CHANNEL = 1 # hex 91: MIDI channel 2, zero-based in mido

# Optional bridge convention for the ten physical GLD-80 SoftKeys. GLD
# SoftKeys have no fixed factory MIDI addresses, but can transmit user-defined
# press/release strings. Configure them on MIDI channel 16 as notes 0..9:
#   Press   9F <00..09> 7F
#   Release 9F <00..09> 00  (or 8F <00..09> 00)
# This channel is deliberately separate from the MIDI Strip channels.
GLD_BRIDGE_SOFTKEY_CHANNEL = 15
GLD_BRIDGE_SOFTKEY_COUNT = 10

# Native GLD channel addresses from the protocol table.
CH_FX_SEND_1 = 0x00
CH_FX_RETURN_1 = 0x08
CH_DCA_1 = 0x10
CH_INPUT_1 = 0x20
CH_MIX_1 = 0x60

COLOURS = {
    "off": 0x00,
    "red": 0x01,
    "green": 0x02,
    "yellow": 0x03,
    "blue": 0x04,
    "purple": 0x05,
    "light_blue": 0x06,
    "white": 0x07,
}

COLOUR_NAMES = {value: key for key, value in COLOURS.items()}


# Reverse-engineered GLD Editor protocol frames for MIDI Strip labels.
# Captured from GLD Editor V1.61 talking to a GLD on TCP port 51321.
# This is separate from the public MIDI-over-TCP protocol on port 51325.
EDITOR_LABEL_TCP_PORT = 51321
EDITOR_LABEL_HEADER = bytes((0xF0, 0x00, 0x01, 0x06, 0xD1, 0x06, 0xD1, 0x10))


def make_editor_midi_strip_name(strip: int, name: str) -> bytes:
    """Build the raw GLD Editor frame for MIDI Strip 1-32 name.

    Captured mapping:
    - address 0x00..0x1F = MIDI Strip 1..32 name
    - payload length 0x09 = 8 ASCII bytes plus a NUL terminator
    """
    if not 0 <= int(strip) < GLD_MIDI_STRIPS:
        raise ValueError("strip must be 0..31")
    safe = "".join(c if 32 <= ord(c) <= 126 else " " for c in str(name))[:8]
    payload = safe.encode("ascii", errors="replace") + (b"\x00" * (9 - len(safe)))
    return EDITOR_LABEL_HEADER + bytes((int(strip), 0x00, 0x09)) + payload + b"\xF7"


def make_editor_midi_strip_colour(strip: int, colour: int | str) -> bytes:
    """Build the raw GLD Editor frame for MIDI Strip 1-32 colour.

    Captured mapping:
    - address 0x20..0x3F = MIDI Strip 1..32 colour
    - colour values match the GLD table: 0 off, 1 red ... 7 white
    """
    if not 0 <= int(strip) < GLD_MIDI_STRIPS:
        raise ValueError("strip must be 0..31")
    if isinstance(colour, str):
        value = COLOURS.get(colour.lower().replace(" ", "_"), 0)
    else:
        value = int(colour)
    value = max(0, min(7, value))
    return EDITOR_LABEL_HEADER + bytes((0x20 + int(strip), 0x00, 0x01, value, 0xF7))


# Reverse-engineered GLD Editor Pan frames captured while moving MIDI Strip 1
# and MIDI Strip 32 in GLD Editor. Both per-strip identifier bytes advance by
# one for each strip:
#
#   Strip 1:  F0 00 01 06 DD 09 3D 10 38 00 02 00 <value> F7
#   Strip 32: F0 00 01 06 FC 09 5C 10 38 00 02 00 <value> F7
#
# Unlike the public B2 rotary message, this Editor write also forces the
# selected Pan bar on the physical scribble-strip LCD to redraw immediately.
EDITOR_PAN_OBJECT_BASE = 0xDD
EDITOR_PAN_PARAMETER_BASE = 0x3D


def make_editor_midi_strip_pan(strip: int, value: int) -> bytes:
    """Build the raw GLD Editor frame for MIDI Strip 1-32 Pan.

    The value is an absolute 7-bit Pan position (0=left, 64=centre, 127=right).
    This frame was verified at the two mapping endpoints, Strip 1 and Strip 32,
    from a user-supplied packet capture of GLD Editor V1.61.
    """
    strip = int(strip)
    if not 0 <= strip < GLD_MIDI_STRIPS:
        raise ValueError("strip must be 0..31")
    value = max(0, min(127, int(value)))
    return bytes((
        0xF0, 0x00, 0x01, 0x06,
        EDITOR_PAN_OBJECT_BASE + strip,
        0x09,
        EDITOR_PAN_PARAMETER_BASE + strip,
        0x10, 0x38, 0x00, 0x02, 0x00, value, 0xF7,
    ))




@dataclass(frozen=True)
class GLDNativeMuteEvent:
    channel_address: int
    on: bool

@dataclass(frozen=True)
class GLDEvent:
    kind: Literal["fader", "rotary_gain", "pan", "custom1", "custom2", "mute", "mix", "pafl"]
    strip: int  # 0..31
    value: int  # 0..127


@dataclass(frozen=True)
class GLDSoftKeyEvent:
    key: int  # 0..9
    pressed: bool


def parse_bridge_softkey_message(msg: mido.Message) -> Optional[GLDSoftKeyEvent]:
    """Parse the optional custom-MIDI convention for GLD SoftKeys 1-10."""
    if msg.type not in ("note_on", "note_off"):
        return None
    if int(msg.channel) != GLD_BRIDGE_SOFTKEY_CHANNEL:
        return None
    note = int(msg.note)
    if not 0 <= note < GLD_BRIDGE_SOFTKEY_COUNT:
        return None
    velocity = 0 if msg.type == "note_off" else int(msg.velocity)
    return GLDSoftKeyEvent(note, velocity > 0)


def clamp7(value: int) -> int:
    return max(0, min(127, int(value)))


def parse_midi_strip_message(msg: mido.Message) -> Optional[GLDEvent]:
    """Parse factory-template GLD MIDI Strip messages.

    Returns None for messages not part of the default MIDI Strip map.
    """
    if msg.type == "control_change":
        if msg.channel == GLD_MIDI_STRIP_CC_CHANNEL and 0x00 <= msg.control <= 0x1F:
            return GLDEvent("fader", msg.control, msg.value)
        if msg.channel == GLD_MIDI_STRIP_ROT_CHANNEL:
            if 0x00 <= msg.control <= 0x1F:
                return GLDEvent("rotary_gain", msg.control, msg.value)
            if 0x20 <= msg.control <= 0x3F:
                return GLDEvent("pan", msg.control - 0x20, msg.value)
            if 0x40 <= msg.control <= 0x5F:
                return GLDEvent("custom1", msg.control - 0x40, msg.value)
            if 0x60 <= msg.control <= 0x7F:
                return GLDEvent("custom2", msg.control - 0x60, msg.value)

    if msg.type in ("note_on", "note_off") and msg.channel == GLD_MIDI_STRIP_NOTE_CHANNEL:
        velocity = 0 if msg.type == "note_off" else msg.velocity
        if 0x00 <= msg.note <= 0x1F:
            return GLDEvent("mute", msg.note, velocity)
        if 0x20 <= msg.note <= 0x3F:
            return GLDEvent("mix", msg.note - 0x20, velocity)
        if 0x40 <= msg.note <= 0x5F:
            return GLDEvent("pafl", msg.note - 0x40, velocity)

    return None


def make_midi_strip_fader(strip: int, value: int) -> mido.Message:
    return mido.Message(
        "control_change",
        channel=GLD_MIDI_STRIP_CC_CHANNEL,
        control=int(strip) & 0x1F,
        value=clamp7(value),
    )


def make_midi_strip_rotary(kind: Literal["gain", "pan", "custom1", "custom2"], strip: int, value: int) -> mido.Message:
    base = {"gain": 0x00, "pan": 0x20, "custom1": 0x40, "custom2": 0x60}[kind]
    return mido.Message(
        "control_change",
        channel=GLD_MIDI_STRIP_ROT_CHANNEL,
        control=(base + (int(strip) & 0x1F)) & 0x7F,
        value=clamp7(value),
    )


def make_midi_strip_key(kind: Literal["mute", "mix", "pafl"], strip: int, on: bool) -> mido.Message:
    """Build the state-bearing part of a MIDI Strip key feedback message.

    The GLD treats values 0x40..0x7F as ON and 0x01..0x3F as OFF.  Use the
    conventional 0x7F/0x3F values; a complete feedback transaction must be
    followed by a Note Off/zero release (see :func:`make_midi_strip_key_feedback`).
    """
    base = {"mute": 0x00, "mix": 0x20, "pafl": 0x40}[kind]
    return mido.Message(
        "note_on",
        channel=GLD_MIDI_STRIP_NOTE_CHANNEL,
        note=(base + (int(strip) & 0x1F)) & 0x7F,
        velocity=0x7F if on else 0x3F,
    )


def make_midi_strip_key_feedback(
    kind: Literal["mute", "mix", "pafl"], strip: int, on: bool
) -> list[mido.Message]:
    """Build one complete GLD MIDI Strip key-state transaction.

    Sending only the state-bearing Note On can leave the GLD switch input in a
    held state.  The surface expects the state packet followed by a release,
    matching the documented/field-tested GLD behaviour.
    """
    state = make_midi_strip_key(kind, strip, on)
    release = mido.Message(
        "note_off",
        channel=state.channel,
        note=state.note,
        velocity=0,
    )
    return [state, release]


def parse_native_mute_message(msg: mido.Message, midi_channel: int = 0) -> Optional[GLDNativeMuteEvent]:
    """Parse native GLD mute feedback.

    GLD sends a Note On with velocity 01..3F for off or 40..7F for on,
    followed by a zero-velocity message. The trailing zero is ignored.
    """
    if msg.type != "note_on" or msg.channel != (midi_channel & 0x0F):
        return None
    if msg.velocity == 0:
        return None
    return GLDNativeMuteEvent(msg.note & 0x7F, msg.velocity >= 0x40)


def make_native_mute(ch: int, on: bool, midi_channel: int = 0) -> list[mido.Message]:
    """Native GLD mute message, followed by note-off as per protocol."""
    velocity = 0x7F if on else 0x3F
    return [
        mido.Message("note_on", channel=midi_channel, note=ch & 0x7F, velocity=velocity),
        mido.Message("note_on", channel=midi_channel, note=ch & 0x7F, velocity=0x00),
    ]


def make_native_fader_nrpn(ch: int, value: int, midi_channel: int = 0) -> list[mido.Message]:
    """Native GLD fader NRPN: BN 63 CH, BN 62 17, BN 06 LV."""
    return [
        mido.Message("control_change", channel=midi_channel, control=0x63, value=ch & 0x7F),
        mido.Message("control_change", channel=midi_channel, control=0x62, value=0x17),
        mido.Message("control_change", channel=midi_channel, control=0x06, value=clamp7(value)),
    ]



# MIDI Strip custom-fader taper used by the GLD surface.
#
# Important: the public protocol's LV table applies to native GLD channel
# faders sent as NRPN parameter 0x17.  A factory MIDI Strip instead transmits
# ``B1 <strip> <VAR>``, where <VAR> is the physical control position.  Applying
# the native LV table to that custom CC value made a physical 0 dB position
# display as roughly -4.5 dB in the bridge.
#
# These control-position anchors were verified on a physical GLD-80.  Values
# between markings are interpolated so the on-screen readout follows the
# console's printed fader scale.
GLD_MIDI_STRIP_FADER_DB_TABLE = [
    (0, float("-inf")),
    (17, -40.0),
    (32, -30.0),
    (47, -20.0),
    (62, -10.0),
    (77, -5.0),
    (98, 0.0),
    (116, 5.0),
    (127, 10.0),
]


def fader_value_to_db(value: int) -> str:
    """Format a GLD MIDI Strip custom-fader CC value as console-scale dB."""
    v = clamp7(value)
    if v == 0:
        return "-∞"

    table = GLD_MIDI_STRIP_FADER_DB_TABLE
    for (x0, y0), (x1, y1) in zip(table, table[1:]):
        if x0 <= v <= x1:
            if y0 == float("-inf"):
                # The GLD has no numeric markings between -inf and -40 dB.
                # The upper anchor itself is the printed -40 dB position.
                return "-40.0 dB" if v == x1 else "<-40 dB"
            ratio = (v - x0) / (x1 - x0)
            y = y0 + ratio * (y1 - y0)
            # Avoid displaying a distracting signed zero around the 0 dB mark.
            if abs(y) < 0.05:
                return "0.0 dB"
            return f"{y:+.1f} dB"
    return "+10.0 dB"

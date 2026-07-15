"""Optional REAPER display, exact-Pan and plug-in companion integration.

The normal GLD/DAW route remains standard MCU.  When the bundled REAPER Lua
script is explicitly enabled, this module adds the pieces that stock REAPER
MCU does not expose reliably: exact Pan values, insert/parameter pages, selected-Send fader flip, Send-slot navigation, and high-quality names/colours.
Normal faders, Bank/Channel, transport and track buttons remain on MCU.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import colorsys
import os
import tempfile

SNAPSHOT_FILENAME = "gld80_mcu_bridge_reaper.tsv"
SNAPSHOT_MAGIC = "GLD80_REAPER_SYNC"
SNAPSHOT_VERSION = 17

BANK_COMMAND_FILENAME = "gld80_mcu_bridge_reaper_bank.tsv"
BANK_COMMAND_MAGIC = "GLD80_REAPER_BANK"
BANK_COMMAND_VERSION = 1

PAN_COMMAND_PREFIX = "gld80_mcu_bridge_reaper_pan_"
PAN_COMMAND_MAGIC = "GLD80_REAPER_PAN"
PAN_COMMAND_VERSION = 1

PLUGIN_COMMAND_FILENAME = "gld80_mcu_bridge_reaper_plugin.tsv"
PLUGIN_COMMAND_MAGIC = "GLD80_REAPER_PLUGIN"
PLUGIN_COMMAND_VERSION = 1

PLUGIN_PARAMETER_COMMAND_PREFIX = "gld80_mcu_bridge_reaper_plugin_param_"
PLUGIN_PARAMETER_COMMAND_MAGIC = "GLD80_REAPER_PLUGIN_PARAM"
PLUGIN_PARAMETER_COMMAND_VERSION = 1

SEND_DELTA_COMMAND_PREFIX = "gld80_mcu_bridge_reaper_send_delta_"
SEND_DELTA_COMMAND_MAGIC = "GLD80_REAPER_SEND_DELTA"
SEND_DELTA_COMMAND_VERSION = 2

SEND_FADER_COMMAND_PREFIX = "gld80_mcu_bridge_reaper_send_fader_"
SEND_FADER_COMMAND_MAGIC = "GLD80_REAPER_SEND_FADER"
SEND_FADER_COMMAND_VERSION = 1

TRACK_FADER_COMMAND_PREFIX = "gld80_mcu_bridge_reaper_track_fader_"
TRACK_FADER_COMMAND_MAGIC = "GLD80_REAPER_TRACK_FADER"
TRACK_FADER_COMMAND_VERSION = 1


@dataclass(frozen=True)
class ReaperTrackSnapshot:
    track: int  # zero-based physical surface strip
    name: str
    red: int
    green: int
    blue: int
    has_custom_colour: bool
    value: int | None = None  # exact Pan, Send-fader or plug-in value, depending on mode
    fader: int | None = None  # exact track-fader fallback for bank refreshes
    send: int | None = None  # exact selected-Send value for fader flip


@dataclass(frozen=True)
class ReaperSnapshotMetadata:
    offset: int = 0
    total_tracks: int = 0
    version: int = 1
    mode: str = "tracks"  # tracks | send_fader | plugin_list | plugin_params
    selected_track: int = -1
    selected_fx: int = -1
    fx_page: int = 0
    param_page: int = 0
    send_slot: int = 0  # zero-based selected Send slot
    bank_sequence: int = 0
    plugin_sequence: int = 0
    send_sequence: int = 0  # legacy header compatibility only
    instance_id: str = ""
    snapshot_sequence: int = 0
    project_id: str = ""


def snapshot_path() -> Path:
    return Path(tempfile.gettempdir()) / SNAPSHOT_FILENAME


def _atomic_write(path: Path, payload: str) -> bool:
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, path)
        return True
    except OSError:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def bank_command_path() -> Path:
    return Path(tempfile.gettempdir()) / BANK_COMMAND_FILENAME


def write_bank_offset(offset: int, sequence: int) -> bool:
    """Tell the optional Lua display page which standard-MCU page is visible."""
    offset = max(0, int(offset))
    payload = f"{BANK_COMMAND_MAGIC}\t{BANK_COMMAND_VERSION}\t{int(sequence)}\t{offset}\n"
    return _atomic_write(bank_command_path(), payload)


def pan_command_path(track: int) -> Path:
    track = max(0, min(31, int(track)))
    return Path(tempfile.gettempdir()) / f"{PAN_COMMAND_PREFIX}{track + 1:02d}.tsv"


def write_pan_command(track: int, value7: int, sequence: int) -> bool:
    """Publish one exact visible-track Pan value to REAPER."""
    track = max(0, min(31, int(track)))
    value7 = max(0, min(127, int(value7)))
    payload = f"{PAN_COMMAND_MAGIC}\t{PAN_COMMAND_VERSION}\t{int(sequence)}\t{value7}\n"
    return _atomic_write(pan_command_path(track), payload)


def plugin_command_path() -> Path:
    return Path(tempfile.gettempdir()) / PLUGIN_COMMAND_FILENAME


def write_plugin_action(action: str, value: int, sequence: int) -> bool:
    """Publish one low-rate plug-in page action to the optional Lua script."""
    action = str(action).strip().lower()
    if action not in {
        "plugin", "toggle", "exit", "select", "vpot_push",
        "bank_left", "bank_right", "channel_left", "channel_right",
        "send_flip_on", "send_flip_off", "send_prev", "send_next",
    }:
        return False
    payload = (
        f"{PLUGIN_COMMAND_MAGIC}\t{PLUGIN_COMMAND_VERSION}\t{int(sequence)}"
        f"\t{action}\t{int(value)}\n"
    )
    return _atomic_write(plugin_command_path(), payload)


def plugin_parameter_command_path(parameter: int) -> Path:
    parameter = max(0, min(7, int(parameter)))
    return Path(tempfile.gettempdir()) / (
        f"{PLUGIN_PARAMETER_COMMAND_PREFIX}{parameter + 1:02d}.tsv"
    )


def write_plugin_parameter_command(parameter: int, value7: int, sequence: int) -> bool:
    """Publish one normalized 0..127 plug-in parameter target."""
    parameter = max(0, min(7, int(parameter)))
    value7 = max(0, min(127, int(value7)))
    payload = (
        f"{PLUGIN_PARAMETER_COMMAND_MAGIC}\t{PLUGIN_PARAMETER_COMMAND_VERSION}"
        f"\t{int(sequence)}\t{value7}\n"
    )
    return _atomic_write(plugin_parameter_command_path(parameter), payload)


def send_delta_command_path(track: int) -> Path:
    track = max(0, min(31, int(track)))
    return Path(tempfile.gettempdir()) / (
        f"{SEND_DELTA_COMMAND_PREFIX}{track + 1:02d}.tsv"
    )


def write_send_delta_command(
    track: int, cumulative_steps: int, delta_steps: int, sequence: int, session: int
) -> bool:
    """Publish a lossless cumulative Send movement for one visible track.

    The helper compares ``cumulative_steps`` with the last value seen for the
    same session. Replacing a file between two Lua polls therefore cannot lose
    fast rotary detents. ``delta_steps`` safely seeds the first command after a
    companion restart, so an old cumulative position is never replayed.
    """
    track = max(0, min(31, int(track)))
    payload = (
        f"{SEND_DELTA_COMMAND_MAGIC}\t{SEND_DELTA_COMMAND_VERSION}"
        f"\t{int(session)}\t{int(sequence)}\t{int(cumulative_steps)}"
        f"\t{int(delta_steps)}\n"
    )
    return _atomic_write(send_delta_command_path(track), payload)




def send_fader_command_path(track: int) -> Path:
    track = max(0, min(31, int(track)))
    return Path(tempfile.gettempdir()) / (
        f"{SEND_FADER_COMMAND_PREFIX}{track + 1:02d}.tsv"
    )


def write_send_fader_command(track: int, value7: int, sequence: int) -> bool:
    """Publish one absolute Send-fader target for the optional REAPER helper."""
    track = max(0, min(31, int(track)))
    value7 = max(0, min(127, int(value7)))
    payload = (
        f"{SEND_FADER_COMMAND_MAGIC}\t{SEND_FADER_COMMAND_VERSION}"
        f"\t{int(sequence)}\t{value7}\n"
    )
    return _atomic_write(send_fader_command_path(track), payload)


def track_fader_command_path(track: int) -> Path:
    track = max(0, min(31, int(track)))
    return Path(tempfile.gettempdir()) / (
        f"{TRACK_FADER_COMMAND_PREFIX}{track + 1:02d}.tsv"
    )


def write_track_fader_command(track: int, value7: int, sequence: int) -> bool:
    """Publish one exact normal-track-volume target while Send flip is active."""
    track = max(0, min(31, int(track)))
    value7 = max(0, min(127, int(value7)))
    payload = (
        f"{TRACK_FADER_COMMAND_MAGIC}\t{TRACK_FADER_COMMAND_VERSION}"
        f"\t{int(sequence)}\t{value7}\n"
    )
    return _atomic_write(track_fader_command_path(track), payload)


def _clamp_byte(value: int) -> int:
    return max(0, min(255, int(value)))


def rgb_to_gld_colour(red: int, green: int, blue: int, has_custom_colour: bool = True) -> str:
    """Reduce a REAPER RGB colour to a readable GLD LCD colour family."""
    if not has_custom_colour:
        return "white"

    r = _clamp_byte(red) / 255.0
    g = _clamp_byte(green) / 255.0
    b = _clamp_byte(blue) / 255.0
    hue, saturation, value = colorsys.rgb_to_hsv(r, g, b)
    degrees = hue * 360.0
    if saturation < 0.16 or value < 0.055:
        return "white"
    if degrees < 20.0 or degrees >= 345.0:
        return "red"
    if degrees < 75.0:
        return "yellow"
    if degrees < 165.0:
        return "green"
    if degrees < 205.0:
        return "light_blue"
    if degrees < 260.0:
        return "blue"
    return "purple"


def parse_snapshot_metadata(text: str) -> ReaperSnapshotMetadata:
    lines = text.splitlines()
    if not lines:
        return ReaperSnapshotMetadata()
    header = lines[0].split("\t")
    if len(header) < 2 or header[0] != SNAPSHOT_MAGIC:
        return ReaperSnapshotMetadata()
    try:
        version = int(header[1])
    except ValueError:
        return ReaperSnapshotMetadata()

    metadata = ReaperSnapshotMetadata(version=version)
    if version >= 3 and len(header) >= 5:
        try:
            metadata = ReaperSnapshotMetadata(
                offset=max(0, int(header[3])),
                total_tracks=max(0, int(header[4])),
                version=version,
            )
        except ValueError:
            return ReaperSnapshotMetadata(version=version)

    if version >= 4 and len(header) >= 10:
        mode = str(header[5]).strip().lower()
        if mode not in {"tracks", "send", "send_fader", "plugin_list", "plugin_params"}:
            mode = "tracks"
        try:
            metadata = ReaperSnapshotMetadata(
                offset=metadata.offset,
                total_tracks=metadata.total_tracks,
                version=version,
                mode=mode,
                selected_track=int(header[6]),
                selected_fx=int(header[7]),
                fx_page=max(0, int(header[8])),
                param_page=max(0, int(header[9])),
                send_slot=max(0, int(header[10])) if len(header) >= 11 else 0,
                bank_sequence=max(0, int(header[11])) if len(header) >= 12 else 0,
                plugin_sequence=max(0, int(header[12])) if len(header) >= 13 else 0,
                send_sequence=max(0, int(header[13])) if len(header) >= 14 else 0,
                instance_id=str(header[14]).strip() if len(header) >= 15 else "",
                snapshot_sequence=max(0, int(header[15])) if len(header) >= 16 else 0,
                project_id=str(header[16]).strip() if len(header) >= 17 else "",
            )
        except ValueError:
            pass
    return metadata


def parse_snapshot(text: str, max_tracks: int = 32) -> list[ReaperTrackSnapshot]:
    """Parse legacy rows plus v15 exact Pan, fader and Send fields safely."""
    lines = text.splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    if len(header) < 2 or header[0] != SNAPSHOT_MAGIC:
        return []
    try:
        version = int(header[1])
    except ValueError:
        return []
    if version < 1 or version > SNAPSHOT_VERSION:
        return []

    result: list[ReaperTrackSnapshot] = []
    seen: set[int] = set()
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) not in (6, 7, 8, 9):
            continue
        try:
            one_based = int(parts[0])
            red = _clamp_byte(int(parts[2]))
            green = _clamp_byte(int(parts[3]))
            blue = _clamp_byte(int(parts[4]))
            custom = bool(int(parts[5]))
            value = max(0, min(127, int(parts[6]))) if len(parts) >= 7 else None
            fader = max(0, min(127, int(parts[7]))) if len(parts) >= 8 else None
            send = max(0, min(127, int(parts[8]))) if len(parts) >= 9 else None
        except ValueError:
            continue
        track = one_based - 1
        if not 0 <= track < max_tracks or track in seen:
            continue
        seen.add(track)
        name = parts[1].replace("\r", " ").replace("\n", " ").replace("\t", " ")
        result.append(ReaperTrackSnapshot(track, name, red, green, blue, custom, value, fader, send))
    return result

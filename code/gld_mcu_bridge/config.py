from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .control_mapping import DEFAULT_CONTROL_MAPPINGS, normalise_control_mappings

CONFIG_DIR = Path.home() / ".gld80_mcu_bridge"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "config_version": 28,
    "tracks": 32,
    "gld_connection_mode": "tcp",
    "daw_protocol": "mcu",
    "gld_host": "192.168.1.50",
    "gld_tcp_port": 51325,
    "editor_labels_enabled": True,
    "gld_editor_port": 51321,
    "pan_sensitivity": 1.0,
    "echo_daw_feedback_to_gld_midi_strips": True,
    "send_names_to_gld": True,
    "send_colours_to_gld": True,
    "vegas_colours_enabled": True,
    "record_arm_blink_enabled": True,
    "close_to_tray": False,
    "reaper_sync_enabled": True,
    "reaper_sync_names": True,
    "reaper_sync_colours": True,
    "reaper_sync_pan": True,
    "reaper_sync_plugins": True,
    "send_fader_flip_softkey8": False,
    "custom_navigation_enabled": True,
    "custom_navigation_strip": 32,
    "manual_names": [f"MIDI {index + 1:02d}" for index in range(32)],
    "manual_colours": ["white"] * 32,
    "gld_midi_in_port": "",
    "gld_midi_out_port": "",
    "daw_in_ports": ["", "", "", ""],
    "daw_out_ports": ["", "", "", ""],
    "use_virtual_daw_ports": False,
    "control_mappings": normalise_control_mappings(DEFAULT_CONTROL_MAPPINGS),
}


def _migrate(user_cfg: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(user_cfg)
    version = int(migrated.get("config_version", 1))
    if version < 4:
        migrated.setdefault("gld_connection_mode", "tcp")
        migrated.setdefault("gld_host", "192.168.1.50")
        migrated.setdefault("gld_tcp_port", 51325)
        migrated.setdefault("vegas_softkeys_enabled", True)
        migrated.setdefault("vegas_softkey_start_dca", 1)
        migrated.setdefault("vegas_softkey_count", 8)
    if version < 5:
        migrated.setdefault("manual_names", [f"MIDI {index + 1:02d}" for index in range(32)])
        migrated.setdefault("manual_colours", ["off"] * 32)
    if version < 6:
        migrated.setdefault("vegas_colours_enabled", True)
        migrated.setdefault("close_to_tray", False)
    if version < 7:
        # v0.5 could write app labels to native Input/DCA/FX channels. Keep the
        # obsolete native target disabled during migration.
        migrated["send_names_to_gld"] = False
        migrated["send_colours_to_gld"] = False
        migrated["gld_label_target"] = "off"
    if version < 8:
        # v0.5.2 uses the captured GLD Editor protocol on TCP 51321. It
        # addresses MIDI Strip 1-32 directly and never native channels.
        migrated["editor_labels_enabled"] = True
        migrated["gld_editor_port"] = 51321
        migrated["send_names_to_gld"] = True
        migrated["send_colours_to_gld"] = True
        migrated["gld_label_target"] = "midi_strips_editor"
    if version < 10:
        # v0.5.5 removes the complete smoothing subsystem. Delete old keys so
        # saved configurations cannot expose or reactivate obsolete behaviour.
        for key in (
            "smoothing_enabled",
            "smoothing_mode",
            "smoothing_ms",
            "smoothing_live_ms",
            "smoothing_mix_ms",
            "smoothing_linear_ms",
            "smoothing_feedback_to_gld",
        ):
            migrated.pop(key, None)
    if version < 11:
        migrated.setdefault("daw_protocol", "mcu")
    if version < 12:
        migrated.setdefault("reaper_sync_enabled", True)
        migrated.setdefault("reaper_sync_names", True)
        migrated.setdefault("reaper_sync_colours", True)
        colours = list(migrated.get("manual_colours", []))
        # Earlier releases created every strip as Off. Convert only that exact
        # untouched/default pattern; mixed user colour choices remain intact.
        if len(colours) == 32 and all(str(colour).lower() == "off" for colour in colours):
            migrated["manual_colours"] = ["white"] * 32
    if version < 13:
        # v0.6.2 removes the DCA/SoftKey Vegas workaround and stores the four
        # explicit DAW port pairs. Obsolete native-label and DCA keys must not
        # survive in a saved configuration.
        for key in (
            "gld_native_midi_channel",
            "gld_label_target",
            "vegas_softkeys_enabled",
            "vegas_softkey_start_dca",
            "vegas_softkey_count",
        ):
            migrated.pop(key, None)
        migrated.setdefault("gld_midi_in_port", "")
        migrated.setdefault("gld_midi_out_port", "")
        migrated.setdefault("daw_in_ports", ["", "", "", ""])
        migrated.setdefault("daw_out_ports", ["", "", "", ""])
        migrated.setdefault("use_virtual_daw_ports", False)
    if version < 14:
        # v0.6.3 changed Pan sensitivity into a simple speed multiplier
        # (1 = normal).
        migrated["pan_sensitivity"] = 1.0
    if version < 15:
        # v0.6.4 removes the re-centering Pan experiment. The GLD Pan value is
        # now kept absolute and the REAPER companion provides exact two-way Pan.
        migrated.pop("endless_pan_enabled", None)
        migrated.setdefault("reaper_sync_pan", True)
    if version < 16:
        # MCU/HUI motor faders, key tallies and Pan feedback require the DAW to
        # GLD return path. Older saved settings could leave it disabled.
        migrated["echo_daw_feedback_to_gld_midi_strips"] = True
    if version < 17:
        # v0.6.15 adds standard MCU Bank/Channel navigation from one selected
        # MIDI Strip Custom rotary pair. User-facing strip numbering is 1..32.
        migrated.setdefault("custom_navigation_enabled", True)
        migrated.setdefault("custom_navigation_strip", 32)
    if version < 18:
        # v0.6.19 adds separate MCU/HUI control mapping profiles. The default
        # profile exactly matches the fixed mappings from previous releases.
        migrated.setdefault("control_mappings", normalise_control_mappings(None))
    if version < 19:
        # v0.6.26+ supports a non-destructive REC-arm pulse on the strip colour.
        migrated.setdefault("record_arm_blink_enabled", True)
    if version < 20:
        migrated.setdefault("send_levels_on_faders", True)
    if version < 21:
        # v0.6.29 returns Send control to the Gain rotaries. v0.6.28 was the
        # only release that migrated this option on by default, so upgrading
        # that configuration should restore the established rotary workflow.
        migrated["send_levels_on_faders"] = False
    if version < 22:
        # v0.6.30 removes the experimental fader-owned Send path completely.
        # A saved True value could otherwise steal normal track faders after
        # entering Send mode even though rotary control was expected.
        migrated.pop("send_levels_on_faders", None)
    if version < 23:
        # v0.6.31 makes standard MCU the sole control and value path.
        migrated["reaper_sync_enabled"] = False
    if version < 24:
        # v0.6.31 temporarily reduced the companion to labels only.
        migrated.pop("reaper_sync_pan", None)
        migrated["reaper_sync_enabled"] = False
    if version < 25:
        # Historical v0.6.34 migration: exact Pan and FX pages became optional
        # while faders and Bank/Channel remained standard MCU.
        migrated.setdefault("reaper_sync_pan", True)
        migrated.setdefault("reaper_sync_plugins", True)
    if version < 26:
        # Stock REAPER MCU ignores the Send assignment note. Enable companion
        # detection when its fresh snapshot is present; other DAWs remain on
        # standard MCU because no REAPER snapshot is detected.
        migrated["reaper_sync_enabled"] = True

        # v0.6.35 makes the physical layer contract unambiguous on upgrade:
        # GAIN is Send and PAN is Pan. Older saved/custom profiles could retain
        # GAIN=track_pan (or PAN=context_send), which would make the corrected
        # transport code appear broken even though it never received a Send
        # gesture. Users can still customise the profile again after migration.
        mappings = normalise_control_mappings(migrated.get("control_mappings"))
        mappings["mcu"]["controls"]["gain"] = "context_send"
        if mappings["mcu"]["controls"]["pan"] == "context_send":
            mappings["mcu"]["controls"]["pan"] = "context_pan"
        migrated["control_mappings"] = mappings
    if version < 27:
        # v0.6.36 adds an explicit opt-in workflow; no existing SoftKey 8
        # mapping is changed during upgrade.
        migrated.setdefault("send_fader_flip_softkey8", False)
    if version < 28:
        # v0.6.41 stops using the bounded GLD GAIN accumulator as a level
        # control. Selecting/turning GAIN now opens the standard MCU Send +
        # Flip page, puts the selected Send on the motor faders and uses GAIN
        # only to choose the previous/next Send. PAN returns to track faders.
        mappings = normalise_control_mappings(migrated.get("control_mappings"))
        if mappings["mcu"]["controls"].get("gain") == "context_send":
            mappings["mcu"]["controls"]["gain"] = "send_fader_select"
        migrated["control_mappings"] = mappings
    migrated["control_mappings"] = normalise_control_mappings(migrated.get("control_mappings"))
    migrated["config_version"] = 28
    return migrated


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    # Copy lists so callers cannot mutate DEFAULT_CONFIG through a shared list.
    cfg["manual_names"] = list(DEFAULT_CONFIG["manual_names"])
    cfg["manual_colours"] = list(DEFAULT_CONFIG["manual_colours"])
    cfg["daw_in_ports"] = list(DEFAULT_CONFIG["daw_in_ports"])
    cfg["daw_out_ports"] = list(DEFAULT_CONFIG["daw_out_ports"])
    cfg["control_mappings"] = normalise_control_mappings(DEFAULT_CONFIG["control_mappings"])
    try:
        if CONFIG_PATH.exists():
            user_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update(_migrate(user_cfg))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    merged = dict(DEFAULT_CONFIG)
    merged["manual_names"] = list(DEFAULT_CONFIG["manual_names"])
    merged["manual_colours"] = list(DEFAULT_CONFIG["manual_colours"])
    merged["daw_in_ports"] = list(DEFAULT_CONFIG["daw_in_ports"])
    merged["daw_out_ports"] = list(DEFAULT_CONFIG["daw_out_ports"])
    merged["control_mappings"] = normalise_control_mappings(DEFAULT_CONFIG["control_mappings"])
    merged.update(cfg)
    for key in (
        "smoothing_enabled",
        "smoothing_mode",
        "smoothing_ms",
        "smoothing_live_ms",
        "smoothing_mix_ms",
        "smoothing_linear_ms",
        "smoothing_feedback_to_gld",
        "gld_native_midi_channel",
        "gld_label_target",
        "vegas_softkeys_enabled",
        "vegas_softkey_start_dca",
        "vegas_softkey_count",
        "endless_pan_enabled",
        "send_levels_on_faders",
    ):
        merged.pop(key, None)
    merged["control_mappings"] = normalise_control_mappings(merged.get("control_mappings"))
    merged["config_version"] = 28
    CONFIG_PATH.write_text(json.dumps(merged, indent=2), encoding="utf-8")

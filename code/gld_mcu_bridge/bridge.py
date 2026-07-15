from __future__ import annotations

import math
import time
from collections import deque
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from .control_mapping import DEFAULT_CONTROL_MAPPINGS, normalise_control_mappings
from .model import ChannelState
from .midi_io import MidiRouter
from .protocols import gld, hui, mcu
from .reaper_sync import (
    parse_snapshot,
    parse_snapshot_metadata,
    rgb_to_gld_colour,
    snapshot_path,
    write_bank_offset,
    write_pan_command,
    write_plugin_action,
    write_plugin_parameter_command,
    write_send_delta_command,
    write_send_fader_command,
    write_track_fader_command,
)


class BridgeEngine(QObject):
    channel_changed = Signal(int, object)
    labels_changed = Signal()
    reaper_sync_status = Signal(str)
    channels_reset = Signal()
    log = Signal(str)

    VALID_PROTOCOLS = {"mcu", "hui", "raw"}

    def __init__(self, tracks: int = 32) -> None:
        super().__init__()
        self.tracks = tracks
        self.channels = [ChannelState(i) for i in range(tracks)]
        self.router: Optional[MidiRouter] = None
        self.daw_protocol = "mcu"
        # Shared speed multiplier for every continuous physical rotary action.
        # Values below 1 slow the control down; values above 1 accelerate it.
        self.pan_sensitivity = 1.0
        self.echo_daw_feedback_to_gld = True
        self.send_names_to_gld = False
        self.send_colours_to_gld = False
        self.vegas_colours_enabled = True
        # Optional low-duty REC indication on the MIDI-strip LCD colour. The
        # base DAW colour is restored after every pulse, so Mute/MIX/PAFL LEDs
        # and the channel name remain fully available.
        self.record_arm_blink_enabled = True
        # The default MCU workflow mirrors a hardware fader flip rather than
        # forcing the bounded GLD GAIN accumulator to behave like a relative
        # level encoder. Selecting/turning GAIN opens Send + Flip: motor faders
        # control the chosen Send and GAIN chooses the previous/next Send slot.
        # PAN returns to the normal track-fader/Pan page. REAPER's optional
        # companion supplies only the missing selected-Send fader page; the
        # physical interaction remains the same as a generic MCU host.
        self.reaper_sync_enabled = False
        self.reaper_sync_names = True
        self.reaper_sync_colours = True
        self.reaper_sync_pan = True
        self.reaper_sync_plugins = True
        self.standard_mcu_first = True
        # Optional SoftKey 8 remains as a manual toggle, but the default GAIN
        # mapping enters the same Send-fader page automatically.
        self.send_fader_flip_softkey8 = False
        self._send_fader_flip_active = False
        self._gain_send_fader_active = False
        self._selected_send_slot = 0
        self._send_select_last_at = 0.0
        self._send_select_cooldown_seconds = 0.10
        # Optional physical MCU navigation. On the selected GLD MIDI Strip,
        # Custom 1 becomes Bank Left/Right and Custom 2 becomes Channel
        # Left/Right. The DAW receives only standard MCU global button notes.
        self.custom_navigation_enabled = True
        self.custom_navigation_strip = 31
        # User-editable per-protocol mappings. Defaults are byte-for-byte the
        # same behaviour used by v0.6.18 and earlier.
        self.control_mappings = normalise_control_mappings(DEFAULT_CONTROL_MAPPINGS)

        banks = max(1, math.ceil(tracks / 8))
        self._mcu_scribble = [mcu.ScribbleBuffer() for _ in range(banks)]
        self._hui_parsers = [hui.HUIParser() for _ in range(banks)]
        # REAPER and some other hosts expose the Universal and Extenders as
        # separate MIDI endpoints. Assignment notes sent only to the Universal
        # can therefore race with, or fail to reach, a V-Pot on an Extender.
        # Remember which endpoints have explicitly received the Send assignment
        # so the assignment click and the first V-Pot movement share one ordered
        # MIDI stream on every bank that is actually used.
        self._mcu_send_ready_banks: set[int] = set()
        # Hosts can silently return a V-Pot to Pan after a page refresh. Reassert
        # Send once at the start of each physical gesture instead of trusting a
        # session-long flag. This remains standard MCU for generic hosts.
        self._mcu_send_assignment_at = [0.0] * banks
        self._send_gesture_last_at = [0.0] * tracks
        self._send_gesture_idle_seconds = 0.55
        # Debounce the final host refresh burst, but never let continuous DAW
        # automation keep a bank transition open forever.
        self._surface_page_max_wait_seconds = 1.25
        self._hui_touch_active = [False] * tracks
        self._hui_touch_timers: list[QTimer] = []
        for track in range(tracks):
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(240)
            timer.timeout.connect(lambda t=track: self._end_hui_touch(t))
            self._hui_touch_timers.append(timer)

        # GAIN on a GLD MIDI Strip is an endless physical encoder backed by a
        # bounded 0..127 software accumulator. Writing exact DAW values into
        # that accumulator fights the next physical detent and can pin it at
        # the top. Re-centre only after the user stops turning (or at an end).
        self._gain_recentre_timers: list[QTimer] = []
        for track in range(tracks):
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(220)
            timer.timeout.connect(lambda t=track: self._recenter_gain_after_idle(t))
            self._gain_recentre_timers.append(timer)

        self._vegas_timer: Optional[QTimer] = None
        self._vegas_phase = 0.0
        self._vegas_tick = 0
        self._vegas_bpm = 120
        self._vegas_active = False
        self._vegas_saved_states: list[tuple[int, bool, bool, bool, str]] = []
        self._vegas_last_colour_beat = -1
        self._vegas_input_guard_until = 0.0
        self._surface_reset_in_progress = False
        self._surface_reset_guard_until = 0.0

        self._daw_fader_out_history = [deque(maxlen=96) for _ in range(tracks)]
        self._daw_echo_guard_seconds = 0.25
        self._gld_key_pressed_at: dict[tuple[str, int], float] = {}
        # GLD MIDI Strip switches are buttons, not authoritative state packets.
        # Zero is only the release; every accepted non-zero packet is one click.
        # A short press latch rejects immediate duplicate packets without tying
        # a future real press to an unmatched outbound LED update.
        self._gld_key_press_latch_seconds = 0.08
        # Soft 9 is the MCU Plug-in assignment key. A normal press sends the
        # assignment button; holding it provides the missing physical way back
        # to the standard MCU Pan assignment without consuming another SoftKey.
        self._gld_softkey_pressed_at: dict[int, float] = {}
        self._plugin_exit_hold_seconds = 0.70
        self._gld_key_last_sent: dict[tuple[str, int], bool] = {}
        # LED/state writes sent to the GLD can be echoed back by the desk or
        # MIDI-over-TCP path. A one-shot, short-lived value match prevents that
        # echo from being mistaken for a fresh physical button press. This is
        # deliberately not a persistent state filter: a later real press with
        # the same value must still work normally.
        self._gld_key_out_expected: dict[
            tuple[str, int], deque[tuple[int, float]]
        ] = {}
        self._gld_key_echo_guard_seconds = 0.20

        # The app sends MCU buttons as a standard momentary press/release pair.
        # Some virtual MIDI backends reflect that pair into the bridge input.
        # Consume only the matching, short-lived echo transaction; all actual
        # DAW LED tallies remain authoritative after the local settle window.
        banks = max(1, math.ceil(tracks / 8))
        self._daw_switch_out_expected = [deque(maxlen=32) for _ in range(banks)]
        self._daw_switch_echo_guard_seconds = 0.20
        # MCU/HUI switch messages are momentary clicks, while messages coming
        # back from the DAW are state tallies. Around a local click some hosts
        # briefly publish the previous tally (and virtual MIDI routing can also
        # reflect our press/release pair). Hold the requested target for a short
        # settling window so that stale opposite packets cannot undo the click.
        self._daw_key_pending: dict[tuple[str, int], tuple[bool, float]] = {}
        self._daw_key_settle_seconds = 0.30
        self._pan_surface_origin_at = [0.0] * tracks
        self._pan_feedback_guard_seconds = 0.35
        # The physical rotary is mechanically endless, but the MIDI Strip Pan
        # parameter itself is an absolute bounded 0..127 value. Keep that value
        # aligned with the DAW instead of re-centring it after every movement.
        self._gld_pan_raw: list[int | None] = [None] * tracks
        # Keep the last value actually observed from the desk separate from the
        # value most recently written back to it.  The GLD may or may not rebase
        # its active rotary accumulator immediately after remote feedback.
        self._gld_pan_input_raw: list[int | None] = [None] * tracks
        self._gld_pan_out_expected = [deque(maxlen=16) for _ in range(tracks)]
        self._gld_pan_last_output_at = [0.0] * tracks
        # The GLD does not expose the global GAIN/PAN selector buttons as
        # separate MIDI messages. Some firmware nevertheless republishes a
        # short multi-strip rotary snapshot when a layer is selected. Track
        # those late repeated reports so a layer click can switch the fader
        # page before the user turns an encoder, without confusing immediate
        # echoes of our own feedback writes for a physical layer change.
        self._rotary_layer_burst_started_at = {"gain": 0.0, "pan": 0.0}
        self._rotary_layer_burst_strips = {"gain": set(), "pan": set()}
        self._rotary_layer_burst_window_seconds = 0.10
        self._rotary_layer_late_echo_seconds = 0.18
        # Gain is repurposed as the standard MCU Send-level V-Pot layer. It has
        # its own absolute accumulator and echo guard so DAW feedback can update
        # the GLD without being mistaken for another physical rotation.
        self._gld_gain_raw: list[int | None] = [None] * tracks
        self._gld_gain_input_raw: list[int | None] = [None] * tracks
        self._gld_gain_out_expected = [deque(maxlen=16) for _ in range(tracks)]
        self._gld_gain_last_sent: list[int | None] = [None] * tracks
        self._gld_gain_last_output_at = [0.0] * tracks
        # Track what has actually been transmitted to the desk so repeated
        # standard MCU LED-ring tallies do not create duplicate GLD writes.
        self._gld_pan_last_sent: list[int | None] = [None] * tracks

        # Fractional carry makes sub-1.0 rotary speeds useful: for example, at
        # 0.25x four physical detents produce one value step instead of every
        # detent being rounded away. The context is part of the key so residue
        # cannot leak between Pan, Send and Plug-in parameter modes.
        self._rotary_fraction: dict[tuple[str, str, int], float] = {}

        # Custom rotary navigation state. The first received value arms the
        # selected rotary; after each accepted step it is returned to centre so
        # the absolute 0..127 GLD parameter can behave like an endless MCU
        # Bank/Channel control. A short rate limit turns a deliberate twist into
        # one navigation press instead of dozens of DAW button presses.
        self._custom_rotary_raw = {
            "custom1": [None] * tracks,
            "custom2": [None] * tracks,
        }
        self._custom_navigation_last_at = {"custom1": 0.0, "custom2": 0.0}
        self._custom_navigation_cooldown_seconds = 0.18

        # Optional REAPER metadata page tracking. Faders, Pan, Sends, plug-in
        # parameters and all buttons remain owned exclusively by standard MCU.
        self._surface_track_offset = 0
        self._reaper_reported_offset = 0
        self._reaper_total_tracks = 0
        self._reaper_companion_version = 0
        self._reaper_bank_sequence = 0
        # A Bank/Channel command and the companion snapshot are asynchronous.
        # Keep the requested page authoritative until the companion echoes the
        # same offset and the same metadata-page command sequence.
        # This prevents one old snapshot from repainting names/colours between
        # the physical MCU move and the companion acknowledgement.
        self._reaper_bank_pending: tuple[int, int] | None = None
        # Snapshot v10 identifies the active metadata script and numbers every
        # publication. This prevents a delayed or duplicate script from
        # repainting names/colours for the wrong page.
        self._reaper_companion_instance_id = ""
        self._reaper_snapshot_sequence = -1
        # Local context only: every Plug-in/Send action sent to the host is a
        # standard MCU button or V-Pot message.
        self._plugin_mode = "tracks"  # tracks | send | send_fader | plugin_list | plugin_params
        self._plugin_selected_track = -1
        self._plugin_selected_fx = -1
        self._plugin_fx_page = 0
        self._plugin_param_page = 0
        # Metadata banking uses a restart-safe sequence so a newly started
        # optional script can acknowledge only the latest visible page.
        sequence_floor = int(time.time_ns() // 1_000_000)
        self._reaper_bank_sequence = sequence_floor
        self._reaper_pan_sequence = sequence_floor
        self._reaper_plugin_sequence = sequence_floor
        self._reaper_plugin_parameter_sequence = sequence_floor
        # Stock REAPER's MCU implementation does not expose the standard Send
        # assignment page. Companion v1.23 mirrors the selected-Send fader page.
        # The cumulative legacy delta mailbox remains only for custom profiles
        # that explicitly choose the old relative GAIN-to-Send mapping.
        self._reaper_send_session = sequence_floor
        self._reaper_send_sequence = sequence_floor
        self._reaper_send_position = [0] * tracks
        # Snapshot v17 publishes exact selected-Send and track-volume values
        # for every visible track. The default path uses selected-Send values on
        # the motor faders; neither value is written into the bounded GAIN accumulator.
        self._reaper_send_values: list[int | None] = [None] * tracks
        # Normal track-volume values remain available for page restoration and
        # for the explicitly selectable legacy rotary mapping. In the default
        # v0.6.41 workflow, GAIN chooses the Send slot and never owns volume.
        self._reaper_track_fader_values: list[int | None] = [None] * tracks
        self._reaper_pending_track_fader: list[tuple[int, float] | None] = [None] * tracks
        self._reaper_track_fader_last_write = [0.0] * tracks
        self._reaper_send_fader_sequence = sequence_floor
        self._reaper_track_fader_sequence = sequence_floor
        self._reaper_pending_send_fader: list[tuple[int, float] | None] = [None] * tracks
        self._reaper_send_fader_last_write = [0.0] * tracks
        # One exact companion repaint after Bank/Channel or leaving Send flip
        # repairs any motor whose stock-MCU refresh packet was missed.
        self._force_reaper_faders_on_next_snapshot = False
        self._reaper_session_detected = False
        self._reaper_send_upgrade_warned = False
        self._reaper_send_unavailable_warned = False
        self._reaper_pending_pan: list[tuple[int, float] | None] = [None] * tracks
        self._reaper_pan_last_write = [0.0] * tracks
        self._reaper_plugin_values: list[int | None] = [None] * 8
        self._reaper_pending_plugin_values: list[tuple[int, float] | None] = [None] * 8
        self._reaper_plugin_parameter_last_write = [0.0] * 8
        self._reaper_plugin_pending: tuple[str, int, float] | None = None

        # Fader motor-command tracking. GLD position reports caused by a DAW
        # motor command are not sent back to the DAW. GLD-originated values are
        # protected with a short value-aware echo history instead of a blanket
        # time lock, so genuine DAW automation can still reach the motors.
        self._gld_motor_target: list[int | None] = [None] * tracks
        self._gld_motor_until = [0.0] * tracks
        self._gld_motor_guard_seconds = 1.0
        self._gld_motor_last_distance: list[int | None] = [None] * tracks
        self._last_gld_fader: list[int | None] = [None] * tracks
        self._fader_surface_origin_at = [0.0] * tracks
        self._fader_startup_baseline_seconds = 2.0
        self._transport_reset_at = time.monotonic()

        # A full MCU Bank move often produces a short burst containing the old
        # page, a host clear sweep, and finally the new page. Applying every
        # intermediate packet directly makes faders and LEDs briefly (or, when
        # the host publishes only one tally, permanently) fall back to stale
        # strip state. During a page transition keep only the newest value per
        # physical strip/control and commit the settled snapshot in one pass.
        self._surface_page_feedback: dict[tuple[str, int], int | bool] = {}
        self._surface_page_expected_faders: set[int] = set()
        self._surface_page_transition_active = False
        self._surface_page_transition_started_at = 0.0
        self._surface_page_settle_timer = QTimer(self)
        self._surface_page_settle_timer.setSingleShot(True)
        self._surface_page_settle_timer.setInterval(220)
        self._surface_page_settle_timer.timeout.connect(self._flush_surface_page_feedback)
        # A motor can occasionally miss one otherwise valid GLD fader write
        # during a dense four-endpoint bank refresh. Repaint the settled page a
        # few times at a low duty cycle. A generation token prevents an older
        # page's delayed retry from ever touching the newly selected page.
        self._surface_repaint_generation = 0
        self._surface_repaint_delays_ms = (90, 240, 520)
        # After an acknowledged REAPER bank snapshot, reject the late clear
        # sweep that stock MCU can still emit for the previous/intermediate
        # page. Exact companion targets remain authoritative for a short window
        # so occupied faders move directly to the new bank instead of dipping
        # to -inf first.
        self._reaper_bank_fader_targets: list[int | None] = [None] * tracks
        self._reaper_bank_old_fader_values: list[int | None] = [None] * tracks
        self._reaper_bank_fader_guard_until = 0.0
        self._surface_page_waiting_for_reaper_exact = False
        self._surface_page_exact_wait_until = 0.0

        self._reaper_sync_path = snapshot_path()
        self._reaper_sync_last_mtime_ns = -1
        self._reaper_sync_last_text = ""
        self._reaper_sync_last_status = ""
        self._reaper_companion_connected = False
        self._reaper_track_count = 0
        self._reaper_last_valid_snapshot_at = 0.0
        self._reaper_snapshot_grace_seconds = 1.0
        self._reaper_sync_timer = QTimer(self)
        self._reaper_sync_timer.setInterval(10)
        self._reaper_sync_timer.timeout.connect(self._poll_reaper_companion)
        self._reaper_sync_timer.start()

        # A GLD can temporarily reject the proprietary Editor socket when all
        # remote-control slots are occupied. MidiRouter reconnects that socket
        # automatically. When it comes back, pace a full Pan/name/colour refresh
        # so changes made while disconnected are not lost.
        # Coalesced, paced Editor writes. Sending a 32-strip name page
        # as one burst can overrun the GLD Editor socket, while a short paced cadence
        # is fast enough to redraw a full bank without blocking control input.
        self._editor_sync_queue: deque[tuple[str, int]] = deque()
        self._editor_sync_pending: dict[tuple[str, int], bytes] = {}
        self._editor_sync_timer = QTimer(self)
        self._editor_sync_timer.setInterval(4)
        self._editor_sync_timer.timeout.connect(self._drain_editor_sync_queue)
        # A page change can interrupt an in-flight REC pulse or leave one old
        # Editor packet already on the wire. Force one complete label repaint
        # after the companion acknowledges the new page, even when a new track
        # happens to have the same model colour/name as the previous strip.
        self._force_editor_labels_on_next_snapshot = False

        # REC-arm colour pulse: a short red (or white for an already-red track)
        # overlay once per second, followed by restoration of the true DAW
        # colour. It is suspended during page changes and plug-in pages.
        self._record_blink_active_tracks: set[int] = set()
        self._record_blink_suppress_until = 0.0
        self._record_blink_timer = QTimer(self)
        self._record_blink_timer.setInterval(1000)
        self._record_blink_timer.timeout.connect(self._start_record_arm_blink)
        self._record_blink_timer.start()
        self._record_blink_restore_timer = QTimer(self)
        self._record_blink_restore_timer.setSingleShot(True)
        self._record_blink_restore_timer.setInterval(300)
        self._record_blink_restore_timer.timeout.connect(self._restore_record_arm_blink)

    @property
    def vegas_active(self) -> bool:
        return self._vegas_active or self._vegas_timer is not None

    def attach_router(self, router: MidiRouter) -> None:
        """Attach each router signal exactly once.

        The UI normally attaches one long-lived router. Keeping this method
        idempotent and disconnecting signals independently prevents a failed
        disconnect on one optional signal from leaving another signal connected
        twice after a router replacement.
        """
        if self.router is router:
            return
        if self.router is not None:
            old_router = self.router
            bindings = [
                (old_router.gld_message, self.handle_gld_message),
                (old_router.daw_message, self.handle_daw_message),
                (old_router.log, self.log.emit),
            ]
            if hasattr(old_router, "editor_connection_changed"):
                bindings.append(
                    (old_router.editor_connection_changed, self._on_editor_connection_changed)
                )
            for signal, callback in bindings:
                try:
                    signal.disconnect(callback)
                except Exception:
                    # A signal may already have been disconnected during UI
                    # teardown. Continue with the remaining bindings instead of
                    # abandoning the cleanup half-way through.
                    pass
        self.router = router
        router.gld_message.connect(self.handle_gld_message)
        router.daw_message.connect(self.handle_daw_message)
        router.log.connect(self.log.emit)
        if hasattr(router, "editor_connection_changed"):
            router.editor_connection_changed.connect(self._on_editor_connection_changed)

    def reset_transport_state(self) -> None:
        """Reset session-only echo and rotary baselines before reconnecting."""
        self._gld_pan_raw = [None] * self.tracks
        self._gld_pan_input_raw = [None] * self.tracks
        self._gld_pan_out_expected = [deque(maxlen=16) for _ in range(self.tracks)]
        self._gld_pan_last_sent = [None] * self.tracks
        self._gld_pan_last_output_at = [0.0] * self.tracks
        self._gld_gain_raw = [None] * self.tracks
        self._gld_gain_input_raw = [None] * self.tracks
        self._gld_gain_out_expected = [deque(maxlen=16) for _ in range(self.tracks)]
        self._gld_gain_last_sent = [None] * self.tracks
        self._gld_gain_last_output_at = [0.0] * self.tracks
        self._rotary_layer_burst_started_at = {"gain": 0.0, "pan": 0.0}
        self._rotary_layer_burst_strips = {"gain": set(), "pan": set()}
        self._pan_surface_origin_at = [0.0] * self.tracks
        self._custom_rotary_raw = {
            "custom1": [None] * self.tracks,
            "custom2": [None] * self.tracks,
        }
        self._custom_navigation_last_at = {"custom1": 0.0, "custom2": 0.0}
        self._mcu_send_ready_banks.clear()
        self._mcu_send_assignment_at = [0.0] * len(self._mcu_send_assignment_at)
        self._send_gesture_last_at = [0.0] * self.tracks
        for timer in self._gain_recentre_timers:
            timer.stop()
        self._surface_page_settle_timer.stop()
        self._surface_page_feedback.clear()
        self._surface_page_expected_faders.clear()
        self._surface_page_transition_active = False
        self._surface_page_transition_started_at = 0.0
        self._surface_repaint_generation += 1
        self._gld_motor_target = [None] * self.tracks
        self._gld_motor_until = [0.0] * self.tracks
        self._gld_motor_last_distance = [None] * self.tracks
        self._last_gld_fader = [None] * self.tracks
        self._fader_surface_origin_at = [0.0] * self.tracks
        self._transport_reset_at = time.monotonic()
        self._gld_key_pressed_at.clear()
        self._gld_key_last_sent.clear()
        self._gld_key_out_expected.clear()
        for history in self._daw_switch_out_expected:
            history.clear()
        self._daw_key_pending.clear()
        self._vegas_input_guard_until = 0.0
        self._surface_reset_guard_until = 0.0
        self._rotary_fraction.clear()
        self._reaper_bank_pending = None
        self._reaper_companion_instance_id = ""
        self._reaper_snapshot_sequence = -1
        self._reaper_pending_pan = [None] * self.tracks
        self._reaper_pan_last_write = [0.0] * self.tracks
        self._reaper_plugin_values = [None] * 8
        self._reaper_pending_plugin_values = [None] * 8
        self._reaper_plugin_parameter_last_write = [0.0] * 8
        self._reaper_send_session = int(time.time_ns() // 1_000_000)
        self._reaper_send_sequence = self._reaper_send_session
        self._reaper_send_position = [0] * self.tracks
        self._reaper_send_values = [None] * self.tracks
        self._reaper_track_fader_values = [None] * self.tracks
        self._reaper_pending_track_fader = [None] * self.tracks
        self._reaper_track_fader_last_write = [0.0] * self.tracks
        self._reaper_send_fader_sequence = self._reaper_send_session
        self._reaper_track_fader_sequence = self._reaper_send_session
        self._reaper_pending_send_fader = [None] * self.tracks
        self._reaper_send_fader_last_write = [0.0] * self.tracks
        self._force_reaper_faders_on_next_snapshot = False
        self._reaper_bank_fader_targets = [None] * self.tracks
        self._reaper_bank_old_fader_values = [None] * self.tracks
        self._reaper_bank_fader_guard_until = 0.0
        self._surface_page_waiting_for_reaper_exact = False
        self._surface_page_exact_wait_until = 0.0
        self._send_fader_flip_active = False
        self._gain_send_fader_active = False
        self._selected_send_slot = 0
        self._send_select_last_at = 0.0
        self._reaper_session_detected = False
        self._reaper_send_upgrade_warned = False
        self._reaper_send_unavailable_warned = False
        self._reaper_plugin_pending = None
        self._record_blink_active_tracks.clear()
        self._record_blink_restore_timer.stop()
        self._record_blink_suppress_until = time.monotonic() + 0.5
        # Force a fresh companion snapshot through the output path after every
        # GLD/DAW reconnect, even if the file itself has not changed.
        self._reaper_sync_last_mtime_ns = -1
        self._reaper_sync_last_text = ""
        self._plugin_mode = "tracks"
        self._mcu_send_ready_banks.clear()
        self._plugin_selected_track = -1
        self._plugin_selected_fx = -1
        self._plugin_fx_page = 0
        self._plugin_param_page = 0
        self._editor_sync_queue.clear()
        self._editor_sync_pending.clear()
        self._editor_sync_timer.stop()
        self._force_editor_labels_on_next_snapshot = False
        for history in self._daw_fader_out_history:
            history.clear()

    def configure(self, **kwargs) -> None:
        for key, value in kwargs.items():
            if key == "daw_protocol":
                self.set_daw_protocol(str(value))
            elif key == "control_mappings":
                self.control_mappings = normalise_control_mappings(value)
            elif key == "pan_sensitivity":
                try:
                    self.pan_sensitivity = max(0.10, min(16.0, float(value)))
                except (TypeError, ValueError):
                    self.pan_sensitivity = 1.0
            elif key == "send_fader_flip_softkey8":
                enabled = bool(value)
                if self.send_fader_flip_softkey8 and not enabled and self._send_fader_flip_active:
                    self._set_send_fader_flip(False)
                self.send_fader_flip_softkey8 = enabled
            elif key == "record_arm_blink_enabled":
                enabled = bool(value)
                if self.record_arm_blink_enabled and not enabled:
                    self._restore_record_arm_blink()
                self.record_arm_blink_enabled = enabled
            elif hasattr(self, key):
                setattr(self, key, value)

    def mapped_control_action(self, control: str, protocol: str | None = None) -> str:
        protocol = str(protocol or self.daw_protocol).lower()
        if protocol not in {"mcu", "hui"}:
            return "disabled"
        profile = self.control_mappings.get(protocol, {})
        controls = profile.get("controls", {}) if isinstance(profile, dict) else {}
        return str(controls.get(str(control), "disabled"))

    def mapped_softkey_action(self, index: int, protocol: str | None = None) -> str:
        protocol = str(protocol or self.daw_protocol).lower()
        if protocol not in {"mcu", "hui"} or not 0 <= int(index) < 10:
            return "disabled"
        profile = self.control_mappings.get(protocol, {})
        values = profile.get("softkeys", []) if isinstance(profile, dict) else []
        if not isinstance(values, list) or int(index) >= len(values):
            return "disabled"
        return str(values[int(index)])

    def set_daw_protocol(self, protocol: str) -> None:
        protocol = str(protocol).lower()
        if protocol not in self.VALID_PROTOCOLS:
            protocol = "mcu"
        if protocol == self.daw_protocol:
            return
        if self._send_fader_flip_active:
            self._set_send_fader_flip(False)
        if self.daw_protocol == "hui":
            for track in range(self.tracks):
                self._end_hui_touch(track)
        self.daw_protocol = protocol
        self._mcu_send_ready_banks.clear()
        self._surface_page_settle_timer.stop()
        self._surface_page_feedback.clear()
        self._surface_page_transition_active = False
        self.log.emit(f"DAW protocol set to {protocol.upper()}")

    # ------------------------------------------------------------------
    # Incoming GLD messages
    # ------------------------------------------------------------------
    def handle_gld_message(self, msg) -> None:
        # Vegas is a self-contained surface test. Ignore motor/LED echoes from
        # the GLD while it runs, otherwise those echoes are mistaken for human
        # fader and key gestures and can change the DAW.
        guard_now = time.monotonic()
        if (
            self._surface_reset_in_progress
            or self.vegas_active
            or guard_now < self._vegas_input_guard_until
            or guard_now < self._surface_reset_guard_until
        ):
            return

        # Raw mode is deliberately transparent. Forward every MIDI message,
        # including non-strip messages, unchanged to the first raw port.
        if self.daw_protocol == "raw":
            self._send_to_daw(0, msg)

        # GLD SoftKeys do not have fixed factory MIDI addresses. The bridge
        # supports an optional custom-MIDI convention on channel 16 (notes
        # 0..9). Most keys act on press. Soft 9 also uses release timing: a
        # short press selects MCU Plug-in, while a hold returns to MCU Pan.
        # Raw mode remains completely transparent.
        softkey = gld.parse_bridge_softkey_message(msg)
        if softkey is not None:
            if self.daw_protocol == "raw":
                return
            action = self.mapped_softkey_action(softkey.key)
            if self.daw_protocol == "mcu" and action == "plugin_toggle":
                now = time.monotonic()
                if softkey.pressed:
                    self._gld_softkey_pressed_at[softkey.key] = now
                else:
                    started = self._gld_softkey_pressed_at.pop(softkey.key, None)
                    if started is not None and now - started >= self._plugin_exit_hold_seconds:
                        self.manual_plugin_exit()
                    elif started is not None:
                        self.manual_plugin_assignment()
                return
            if softkey.pressed:
                self.manual_softkey(softkey.key)
            return

        event = gld.parse_midi_strip_message(msg)
        if event is None or not 0 <= event.strip < self.tracks:
            return
        track = event.strip
        state = self.channels[track]

        if event.kind == "fader":
            value = max(0, min(127, int(event.value)))
            now = time.monotonic()
            previous = self._last_gld_fader[track]
            self._last_gld_fader[track] = value
            state.fader = value
            self.channel_changed.emit(track, state)

            # A recent DAW->GLD command makes the following GLD position
            # reports motor feedback, not a new surface gesture. Suppress that
            # return path while the fader is moving toward the target. If the
            # position moves clearly away from the target, treat it as a human
            # override and hand control back to the surface immediately.
            target = self._gld_motor_target[track]
            motor_active = target is not None and now < self._gld_motor_until[track]
            if motor_active:
                distance = abs(value - int(target))
                last_distance = self._gld_motor_last_distance[track]
                moving_away = last_distance is not None and distance > last_distance + 2
                self._gld_motor_last_distance[track] = distance
                if distance <= 1:
                    self._clear_motor_target(track)
                    return
                elif moving_away and previous is not None:
                    self._clear_motor_target(track)
                else:
                    return
            else:
                self._clear_motor_target(track)
                # The first GLD fader report after connecting is a baseline,
                # not proof of a physical gesture. Forwarding it would make the
                # DAW echo stale GLD state and could suppress the DAW's initial
                # motor-fader snapshot.
                if (
                    previous is None
                    and now - self._transport_reset_at <= self._fader_startup_baseline_seconds
                ):
                    return

            # From here on this is a genuine surface gesture, not motor
            # feedback or a startup baseline. Remember it so a one-count
            # round-trip quantisation difference cannot nudge the motor back.
            self._fader_surface_origin_at[track] = now

            if self.daw_protocol != "raw" and self.mapped_control_action("fader") == "track_fader":
                if self._reaper_send_fader_control_active():
                    # The REAPER companion is the sole owner of fader values while
                    # REAPER's optional Send page is active. A parallel stock
                    # MCU fader packet would otherwise change track volume.
                    self._send_reaper_send_fader(track, value)
                elif self._reaper_send_fader_must_not_fallback():
                    if not self._reaper_send_unavailable_warned:
                        self._reaper_send_unavailable_warned = True
                        self.log.emit(
                            "REAPER companion v1.23 is unavailable or stale; "
                            "Send-fader movement blocked instead of changing track volume"
                        )
                else:
                    self._send_daw_fader(track, value, touch=True)
            return

        if event.kind == "rotary_gain":
            raw = max(0, min(127, int(event.value)))
            now = time.monotonic()
            previous_input = self._gld_gain_input_raw[track]
            feedback_reference = self._gld_gain_raw[track]

            if self._consume_gld_gain_echo(track, raw, now):
                # An echoed feedback/re-centre value is also the best known
                # physical baseline.  Keeping only ``_gld_gain_raw`` here left
                # ``_gld_gain_input_raw`` one detent ahead, so the next turn
                # could look like delta 0 and Send control appeared dead.
                self._gld_gain_input_raw[track] = raw
                self._gld_gain_raw[track] = raw
                # A layer-selection refresh can repeat the same stored value as
                # an older centre write. Treat only a late, multi-strip repeat
                # as a GAIN click; immediate one-strip echoes remain ignored.
                if (
                    self.mapped_control_action("gain") == "send_fader_select"
                    and not self._gain_send_fader_active
                    and not self._plugin_surface_active()
                    and self._observe_rotary_layer_refresh("gain", track, now)
                ):
                    self._handle_gain_send_fader_select(track, raw, previous_input)
                return
            self._gld_gain_input_raw[track] = raw
            self._gld_gain_raw[track] = raw
            if self.daw_protocol == "raw":
                return

            action = self.mapped_control_action("gain")
            if action == "disabled":
                return
            if action == "track_pan":
                self._handle_track_pan_rotary(
                    "gain", track, raw, previous_input, feedback_reference
                )
                return
            if action == "send_fader_select":
                if self.daw_protocol == "mcu" and not self._plugin_surface_active():
                    self._handle_gain_send_fader_select(track, raw, previous_input)
                return
            if action != "context_send" or self.daw_protocol != "mcu":
                return
            if self._plugin_surface_active():
                return
            self._handle_send_rotary(
                "gain", track, raw, previous_input, feedback_reference
            )
            return

        if event.kind == "pan":
            raw = max(0, min(127, int(event.value)))
            now = time.monotonic()
            previous_input = self._gld_pan_input_raw[track]
            feedback_reference = self._gld_pan_raw[track]
            if feedback_reference is None:
                feedback_reference = state.pan
            if self._consume_gld_pan_echo(track, raw, now):
                # Keep the observed-input baseline aligned with accepted Pan
                # feedback.  This matters most at fractional rotary speeds,
                # where a feedback rebase can otherwise make the next physical
                # detent repeat the previous raw value and be discarded.
                self._gld_pan_input_raw[track] = raw
                self._gld_pan_raw[track] = raw
                if state.pan != raw and self.mapped_control_action("pan") in {"track_pan", "context_pan"}:
                    state.pan = raw
                    self.channel_changed.emit(track, state)
                # As with GAIN, some desks publish an unchanged multi-strip Pan
                # snapshot when the global PAN selector is pressed. A late
                # refresh is enough to restore the normal volume-fader page
                # without requiring the first physical Pan detent.
                if (
                    self._gain_send_fader_active
                    and self._observe_rotary_layer_refresh("pan", track, now)
                ):
                    self._leave_gain_send_fader_mode()
                return
            self._gld_pan_input_raw[track] = raw
            self._gld_pan_raw[track] = raw
            if self.daw_protocol == "raw":
                state.pan = raw
                self.channel_changed.emit(track, state)
                return

            # The default GAIN mapping owns the Send-fader page. The first
            # genuine PAN movement is the layer-change signal available through
            # the public GLD MIDI Strip protocol, so restore normal track faders
            # before applying that same Pan movement.
            if self._gain_send_fader_active:
                self._leave_gain_send_fader_mode()

            action = self.mapped_control_action("pan")
            if action == "disabled":
                return
            if action == "context_send":
                if self.daw_protocol == "mcu" and not self._plugin_surface_active():
                    self._handle_send_rotary(
                        "pan", track, raw, previous_input, feedback_reference
                    )
                return
            if action == "track_pan":
                self._handle_track_pan_rotary(
                    "pan", track, raw, previous_input, feedback_reference
                )
                return
            if action != "context_pan":
                return

            if (
                self.daw_protocol == "mcu"
                and self._plugin_mode == "send"
                and not self._send_fader_flip_active
            ):
                self._activate_pan_assignment(track)
            self._handle_track_pan_rotary(
                "pan",
                track,
                raw,
                previous_input,
                feedback_reference,
                context_sensitive=True,
            )
            return

        if event.kind in {"custom1", "custom2"}:
            self._handle_custom_navigation(event.kind, track, int(event.value))
            return

        if event.kind not in {"mute", "mix", "pafl"}:
            return

        # Raw data has already been forwarded above. We only update the local
        # UI state here, avoiding any transformed duplicate.
        if self.daw_protocol == "raw":
            if event.value == 0:
                return
            if event.kind == "mute":
                state.mute = not state.mute
            elif event.kind == "mix":
                state.select = not state.select
            else:
                state.solo = not state.solo
            self.channel_changed.emit(track, state)
            return

        value = int(event.value) & 0x7F
        key = (event.kind, track)
        now = time.monotonic()
        if value == 0:
            return
        if self._consume_gld_key_feedback_echo(event.kind, track, value, now):
            return
        last_press = self._gld_key_pressed_at.get(key, 0.0)
        if now - last_press < self._gld_key_press_latch_seconds:
            return
        self._gld_key_pressed_at[key] = now
        self._handle_surface_button_click(event.kind, track)

    def _observe_rotary_layer_refresh(self, kind: str, track: int, now: float) -> bool:
        """Recognise a layer-select snapshot that repeats an older feedback value.

        The physical GLD GAIN/PAN selector buttons have no dedicated public
        MIDI message. On firmware that republishes the selected rotary layer,
        two or more strips arrive almost together. Immediate echoes of our own
        output are excluded; only a late repeated value can contribute to the
        burst. The method returns True once per detected layer click.
        """
        if kind not in {"gain", "pan"} or not 0 <= track < self.tracks:
            return False
        last_output = (
            self._gld_gain_last_output_at[track]
            if kind == "gain"
            else self._gld_pan_last_output_at[track]
        )
        if now - float(last_output) < self._rotary_layer_late_echo_seconds:
            return False

        started = float(self._rotary_layer_burst_started_at[kind])
        if started <= 0.0 or now - started > self._rotary_layer_burst_window_seconds:
            self._rotary_layer_burst_started_at[kind] = now
            self._rotary_layer_burst_strips[kind].clear()
        self._rotary_layer_burst_strips[kind].add(int(track))
        if len(self._rotary_layer_burst_strips[kind]) < 2:
            return False

        self._rotary_layer_burst_started_at[kind] = 0.0
        self._rotary_layer_burst_strips[kind].clear()
        other = "pan" if kind == "gain" else "gain"
        self._rotary_layer_burst_started_at[other] = 0.0
        self._rotary_layer_burst_strips[other].clear()
        return True

    def _physical_rotary_delta(
        self,
        raw: int,
        previous_input: int | None,
        feedback_reference: int | None,
    ) -> int | None:
        """Translate the GLD absolute rotary into a conservative MCU delta.

        Standard MCU owns the controlled value.  Remote LED-ring feedback is
        written to the GLD and consumed as an echo before reaching this method,
        so the only safe movement reference is the last value actually reported
        by the desk.  A large discontinuity is a layer/pickup change and is used
        as a new baseline rather than being sent to the DAW.
        """
        if previous_input is None:
            return None
        delta = int(raw) - int(previous_input)
        if abs(delta) > 12:
            return None
        return delta

    def _scaled_rotary_delta(
        self, source: str, context: str, track: int, delta: int
    ) -> int:
        """Apply the shared rotary speed while preserving fractional movement."""
        if delta == 0:
            return 0
        speed = max(0.10, min(16.0, float(self.pan_sensitivity)))
        key = (str(source), str(context), int(track))
        residue = float(self._rotary_fraction.get(key, 0.0))
        # Do not make a direction reversal fight an old fractional remainder.
        if residue and ((residue > 0) != (delta > 0)):
            residue = 0.0
        total = residue + float(delta) * speed
        steps = math.floor(total) if total >= 0 else math.ceil(total)
        self._rotary_fraction[key] = total - steps
        return max(-127, min(127, int(steps)))

    def _handle_gain_send_fader_select(
        self, track: int, raw: int, previous_input: int | None
    ) -> None:
        """Enter Send fader flip and use GAIN only to choose a Send slot.

        This avoids the unsolved absolute-GAIN/relative-V-Pot feedback loop.
        The motor fader is the level control; GAIN sends one previous/next Send
        selection command per accepted direction change.
        """
        if not 0 <= track < self.tracks or self.daw_protocol != "mcu":
            return

        if not self._gain_send_fader_active:
            flip_was_active = self._send_fader_flip_active
            if not self._set_send_fader_flip(True):
                return
            self._gain_send_fader_active = True
            # Selecting the GLD GAIN layer can publish the stored value of many
            # strips at once. Treat that complete activation burst as state, not
            # navigation. _set_send_fader_flip() already centres every GAIN
            # accumulator when it changes mode; do it here only when the user had
            # already enabled the same flip page manually with SoftKey 8.
            if flip_was_active:
                self.centre_send_rotaries()
            self._send_select_last_at = time.monotonic()
            self.log.emit(
                f"GAIN layer active — Send {self._selected_send_slot + 1} is on the motor faders"
            )
            return

        # A strip that did not participate in the activation burst still needs
        # one baseline before it can be interpreted as previous/next Send.
        if previous_input is None:
            self._return_send_control_to_centre("gain", track)
            return

        delta = ((int(raw) - int(previous_input) + 64) % 128) - 64
        if delta == 0:
            return
        if abs(delta) > 12:
            self._return_send_control_to_centre("gain", track)
            return

        now = time.monotonic()
        if now - self._send_select_last_at < self._send_select_cooldown_seconds:
            self._return_send_control_to_centre("gain", track)
            return
        self._send_select_last_at = now
        direction = "next" if delta > 0 else "previous"
        self._select_send_slot(direction)
        self._return_send_control_to_centre("gain", track)

    def _select_send_slot(self, direction: str) -> None:
        """Select the previous/next Send using host-standard MCU semantics.

        Generic hosts receive Cursor Up/Down while Send+Flip is active. REAPER
        receives the same logical action through the optional helper because
        stock REAPER MCU does not implement the Send assignment page.
        """
        direction = str(direction).strip().lower()
        if direction not in {"previous", "next"}:
            return
        if direction == "previous" and self._selected_send_slot <= 0:
            self.log.emit("Send 1 is already selected")
            return

        known_reaper = (
            self.reaper_sync_enabled
            and (self._reaper_session_detected or self._reaper_companion_connected)
        )
        if known_reaper:
            if not (
                self._reaper_companion_connected
                and self._reaper_companion_version >= 17
            ):
                if not self._reaper_send_upgrade_warned:
                    self._reaper_send_upgrade_warned = True
                    self.log.emit(
                        "REAPER Send selection needs companion v1.23 or newer"
                    )
                return
            action = "send_next" if direction == "next" else "send_prev"
            if not self._write_reaper_plugin_action(action, 0):
                return
            # Optimistic UI state; the next snapshot confirms/clamps it.
            self._selected_send_slot = max(
                0,
                min(31, self._selected_send_slot + (1 if direction == "next" else -1)),
            )
        else:
            for msg in mcu.make_send_navigation_click(direction):
                self._send_to_daw(0, msg)
            self._selected_send_slot = max(
                0,
                min(31, self._selected_send_slot + (1 if direction == "next" else -1)),
            )
        self.log.emit(f"Send {self._selected_send_slot + 1} selected")

    def _leave_gain_send_fader_mode(self) -> None:
        """Restore the normal track-fader page after leaving the GAIN layer."""
        if not self._gain_send_fader_active:
            return
        self._gain_send_fader_active = False
        if self._send_fader_flip_active:
            self._set_send_fader_flip(False)
        self.log.emit("PAN layer active — normal track faders restored")

    def _handle_send_rotary(
        self,
        source: str,
        track: int,
        raw: int,
        previous_input: int | None,
        feedback_reference: int | None,
    ) -> None:
        """Use a GLD rotary as a relative Send/volume movement control.

        The public GLD MIDI Strip GAIN message contains a stored absolute
        0..127 accumulator value, not a native relative detent. Feeding exact
        DAW Send snapshots back into that accumulator creates a closed loop:
        delayed feedback can pull the control upward, reverse a detent or pin
        it at 127. Exact REAPER values therefore remain in the bridge cache and
        on the motor-fader flip page; GAIN is used only for direction/delta.
        """
        if previous_input is None:
            self._send_gesture_last_at[track] = time.monotonic()
            if source == "gain":
                self._schedule_gain_recentre(track)
            if not self._send_fader_flip_active and not self._reaper_send_must_not_fallback():
                self._activate_send_assignment(track, force=True)
            return

        delta = self._physical_rotary_delta(raw, previous_input, feedback_reference)
        if delta is None:
            if source == "gain":
                self._schedule_gain_recentre(track)
            return
        context = "track_volume_flip" if self._send_fader_flip_active else "send"
        ticks = self._scaled_rotary_delta(source, context, track, delta)
        if not ticks:
            if source == "gain":
                self._schedule_gain_recentre(track)
            return

        now = time.monotonic()
        new_gesture = now - self._send_gesture_last_at[track] > self._send_gesture_idle_seconds
        self._send_gesture_last_at[track] = now

        if self._send_fader_flip_active:
            if not self._reaper_send_must_not_fallback():
                self._activate_send_assignment(track, force=new_gesture)
            self._handle_flipped_volume_rotary(track, ticks)
        elif self._reaper_send_control_active():
            if self._reaper_companion_version >= 14:
                current = self._reaper_send_values[track]
                if current is None:
                    if not self._reaper_send_unavailable_warned:
                        self._reaper_send_unavailable_warned = True
                        self.log.emit(
                            f"Waiting for REAPER Send value on strip {track + 1}; "
                            "turn GAIN again after the next companion snapshot"
                        )
                else:
                    target = max(0, min(127, int(current) + int(ticks)))
                    if target != int(current):
                        self._reaper_send_values[track] = target
                        if not self._send_reaper_send_fader(track, target):
                            self._reaper_send_values[track] = int(current)
            else:
                self._plugin_mode = "send"
                self._reaper_send_position[track] += int(ticks)
                self._reaper_send_sequence += 1
                if not write_send_delta_command(
                    track,
                    self._reaper_send_position[track],
                    int(ticks),
                    self._reaper_send_sequence,
                    self._reaper_send_session,
                ):
                    self.log.emit(f"Could not write REAPER Send movement for track {track + 1}")
        elif self._reaper_send_must_not_fallback():
            if self._reaper_companion_connected and self._reaper_companion_version < 13:
                if not self._reaper_send_upgrade_warned:
                    self._reaper_send_upgrade_warned = True
                    self.log.emit(
                        "REAPER Gain/Send needs companion v1.18 or newer; older companion "
                        "detected, so the turn was blocked instead of changing Pan"
                    )
            elif not self._reaper_send_unavailable_warned:
                self._reaper_send_unavailable_warned = True
                self.log.emit(
                    "REAPER companion is unavailable or stale; Gain turn blocked "
                    "instead of falling through to track Pan"
                )
        else:
            self._activate_send_assignment(track, force=new_gesture)
            bank, local = divmod(track, 8)
            for out_msg in mcu.make_vpot_relative(local, ticks):
                self._send_to_daw(bank, out_msg)

        if source == "gain":
            if raw in {0, 127}:
                self._return_send_control_to_centre(source, track)
            else:
                self._schedule_gain_recentre(track)

    def _handle_flipped_volume_rotary(self, track: int, ticks: int) -> None:
        """Control normal track volume while motor faders own Send.

        REAPER's companion supplies the exact hidden track-fader value in the
        Send-fader snapshot. Generic MCU hosts perform the swap when they
        receive the standard Flip button; the bridge may reassert the Send
        assignment after banking, then forwards the relative V-Pot movement.
        """
        if not 0 <= track < self.tracks or not ticks:
            return

        known_reaper = (
            self.daw_protocol == "mcu"
            and self.reaper_sync_enabled
            and (self._reaper_session_detected or self._reaper_companion_connected)
        )
        if known_reaper:
            if not self._reaper_send_fader_control_active():
                if not self._reaper_send_unavailable_warned:
                    self._reaper_send_unavailable_warned = True
                    self.log.emit(
                        "REAPER companion v1.23 is unavailable or stale; legacy flipped "
                        "volume rotary blocked until the Send-fader page reconnects"
                    )
                return
            current = self._reaper_track_fader_values[track]
            if current is None:
                if not self._reaper_send_unavailable_warned:
                    self._reaper_send_unavailable_warned = True
                    self.log.emit(
                        f"Waiting for REAPER track-volume value on strip {track + 1}; "
                        "turn the flipped rotary again after the next snapshot"
                    )
                return
            target = max(0, min(127, int(current) + int(ticks)))
            if target == int(current):
                return
            self._reaper_track_fader_values[track] = target
            self._reaper_pending_track_fader[track] = (
                target, time.monotonic() + 1.5
            )
            self._reaper_track_fader_last_write[track] = time.monotonic()
            # Stock MCU remains the owner of normal track volume.  Incoming
            # track-fader feedback is already suppressed while Send flip owns
            # the physical motors, so this cannot pull a motor off its Send.
            self._send_daw_fader(track, target, touch=False)
            return

        bank, local = divmod(track, 8)
        for out_msg in mcu.make_vpot_relative(local, ticks):
            self._send_to_daw(bank, out_msg)

    def _handle_track_pan_rotary(
        self,
        source: str,
        track: int,
        raw: int,
        previous_input: int | None,
        feedback_reference: int | None,
        *,
        context_sensitive: bool = False,
    ) -> None:
        """Translate one GLD rotary without creating competing control owners."""
        delta = self._physical_rotary_delta(raw, previous_input, feedback_reference)

        # REAPER FX parameters are the one optional non-MCU value path. It is
        # active only while the explicitly enabled companion reports a locked
        # parameter page; stock MCU remains the fallback for every other host.
        if (
            context_sensitive
            and self._reaper_plugin_control_active()
            and self._plugin_mode in {"plugin_list", "plugin_params"}
        ):
            if self._plugin_mode != "plugin_params" or not 0 <= track < 8:
                return
            current = self._reaper_plugin_values[track]
            if delta is None or current is None:
                if current is not None:
                    self._send_rotary_feedback(source, track, current, force=True)
                return
            ticks = self._scaled_rotary_delta(source, "plugin_parameter", track, delta)
            if ticks == 0:
                return
            target = max(0, min(127, int(current) + ticks))
            if target == current:
                return
            self._reaper_plugin_values[track] = target
            self.channels[track].pan = target
            self._mark_pan_surface_origin(track)
            self.channel_changed.emit(track, self.channels[track])
            self._send_reaper_plugin_parameter(track, target)
            self._send_rotary_feedback(source, track, target, force=True)
            return

        if delta is None:
            return
        context = "track_pan"
        ticks = self._scaled_rotary_delta(source, context, track, delta)
        if ticks == 0:
            return

        # Exact Pan is opt-in and REAPER-only. It replaces, rather than
        # duplicates, the stock relative MCU Pan message while the helper is
        # confirmed online.
        if self._reaper_exact_pan_active():
            state = self.channels[track]
            target = max(0, min(127, int(state.pan) + ticks))
            if target == int(state.pan):
                return
            state.pan = target
            self._mark_pan_surface_origin(track)
            self.channel_changed.emit(track, state)
            self._send_reaper_pan(track, target)
            self._send_pan_feedback_to_mapped_rotaries(track, target, force=True)
            return

        bank, local = divmod(track, 8)
        if self.daw_protocol == "mcu":
            for out_msg in mcu.make_vpot_relative(local, ticks):
                self._send_to_daw(bank, out_msg)
        elif self.daw_protocol == "hui":
            for out_msg in hui.make_pan_relative(local, ticks):
                self._send_to_daw(bank, out_msg)

        if not self._plugin_surface_active():
            state = self.channels[track]
            target = max(0, min(127, int(state.pan) + ticks))
            if target != state.pan:
                state.pan = target
                self._mark_pan_surface_origin(track)
                self.channel_changed.emit(track, state)

    def _send_track_pan_to_daw(self, track: int, value: int, previous: int) -> None:
        if self._reaper_exact_pan_active():
            self._send_reaper_pan(track, value)
            return
        ticks = max(-127, min(127, int(value) - int(previous)))
        if ticks == 0:
            return
        bank, local = divmod(track, 8)
        if self.daw_protocol == "mcu":
            for out_msg in mcu.make_vpot_relative(local, ticks):
                self._send_to_daw(bank, out_msg)
        elif self.daw_protocol == "hui":
            for out_msg in hui.make_pan_relative(local, ticks):
                self._send_to_daw(bank, out_msg)

    @staticmethod
    def _logical_kind_for_action(action: str) -> str | None:
        return {
            "track_mute": "mute",
            "track_solo": "pafl",
            "track_select": "mix",
            "track_record": "rec",
        }.get(str(action))

    def _resolved_button_action(self, source: str, track: int) -> str:
        action = self.mapped_control_action(source)
        if action == "context_select":
            if self._plugin_surface_active() and 0 <= track < 8:
                return "vpot_push"
            return "track_select"
        return action

    def _state_for_kind(self, track: int, kind: str) -> bool:
        state = self.channels[track]
        return {
            "mute": bool(state.mute),
            "pafl": bool(state.solo),
            "mix": bool(state.select),
            "rec": bool(getattr(state, "record", False)),
        }.get(kind, False)

    def _set_state_for_kind(self, track: int, kind: str, on: bool) -> bool:
        state = self.channels[track]
        on = bool(on)
        if kind == "mute":
            changed = state.mute != on
            state.mute = on
        elif kind == "pafl":
            changed = state.solo != on
            state.solo = on
        elif kind == "mix":
            changed = state.select != on
            state.select = on
        elif kind == "rec":
            changed = bool(getattr(state, "record", False)) != on
            state.record = on
        else:
            return False
        if changed:
            self.channel_changed.emit(track, state)
            if kind == "rec" and not on:
                self._restore_record_arm_track(track)
        return changed

    def _surface_sources_for_kind(self, kind: str, track: int) -> list[str]:
        sources: list[str] = []
        for source in ("mute", "mix", "pafl"):
            action = self._resolved_button_action(source, track)
            if self._logical_kind_for_action(action) == kind:
                sources.append(source)
        return sources

    def _handle_surface_button_click(self, source: str, track: int) -> None:
        action = self._resolved_button_action(source, track)
        if action == "disabled":
            return
        if action == "vpot_push":
            if self.daw_protocol == "mcu" and 0 <= track < 8:
                self._send_plugin_vpot_push(track)
            return
        kind = self._logical_kind_for_action(action)
        if kind is None:
            return
        target = not self._state_for_kind(track, kind)
        self._set_state_for_kind(track, kind, target)
        self._arm_daw_key_target(kind, track, target)
        self._send_track_action_click(action, track)
        self._send_gld_key_state(source, track, target)

    def _send_track_action_click(self, action: str, track: int) -> None:
        bank, local = divmod(track, 8)
        if self.daw_protocol == "mcu":
            factory = {
                "track_mute": mcu.make_mute_click,
                "track_solo": mcu.make_solo_click,
                "track_select": mcu.make_select_click,
                "track_record": mcu.make_record_click,
                "vpot_push": mcu.make_vpot_push_click,
            }.get(action)
            if factory is None:
                return
            for msg in factory(local):
                self._record_daw_switch_out(bank, msg)
                self._send_to_daw(bank, msg)
            return
        if self.daw_protocol == "hui":
            port = {
                "track_mute": hui.PORT_MUTE,
                "track_solo": hui.PORT_SOLO,
                "track_select": hui.PORT_SELECT,
            }.get(action)
            if port is None:
                return
            for msg in hui.make_switch_click(local, port):
                self._send_to_daw(bank, msg)

    # ------------------------------------------------------------------
    # Incoming DAW messages
    # ------------------------------------------------------------------
    def handle_daw_message(self, bank: int, msg) -> None:
        # Keep Vegas isolated from live DAW tallies and automation. The exact
        # pre-test surface state is restored on stop, then the REAPER snapshot
        # is force-polled again so any changes made during the test catch up.
        if (
            self.vegas_active
            or self._surface_reset_in_progress
            or time.monotonic() < self._surface_reset_guard_until
        ):
            return

        if self.daw_protocol == "raw":
            self._send_to_gld(msg)
            self._observe_raw_daw_message(msg)
            return
        if self.daw_protocol == "hui":
            self._handle_hui_message(bank, msg)
            return
        self._handle_mcu_message(bank, msg)

    def _handle_mcu_message(self, bank: int, msg) -> None:
        if self._consume_daw_switch_echo(bank, msg):
            return

        parsed_scribble = mcu.parse_scribble_sysex(msg)
        if parsed_scribble is not None:
            # REAPER temporarily reuses the MCU scribble display for parameter
            # values while a V-Pot is being moved. Treating those transient
            # strings as track names makes every GLD strip title flash and then
            # get restored by the companion snapshot. While the companion is
            # online, its track-name snapshot is the single authoritative name
            # source; MCU scribble traffic remains available as the fallback
            # when the companion is stopped or stale.
            if self._reaper_names_are_authoritative():
                return
            offset, text = parsed_scribble
            if 0 <= bank < len(self._mcu_scribble):
                names = self._mcu_scribble[bank].update(offset, text)
                for local_track, name in enumerate(names):
                    self._apply_daw_name(bank * 8 + local_track, name)
            return

        event = mcu.parse_message(msg)
        if event is None:
            return
        track = bank * 8 + event.track
        if not 0 <= track < self.tracks:
            return
        state = self.channels[track]

        # Stock REAPER still publishes track-volume faders while the optional
        # companion Send page is visible. Ignore those packets so they cannot
        # pull a physical fader away from its Send value.
        if event.kind == "fader" and self._reaper_send_fader_control_active():
            return

        # After the 220 ms coalescing timer expires, stock REAPER can still
        # emit one more all-down sweep before the companion publishes the exact
        # destination page. Keep holding those packets until the atomic snapshot
        # arrives; otherwise all motors visibly dip to -inf and then rise again.
        if (
            event.kind == "fader"
            and self._surface_page_waiting_for_reaper_exact
            and not self._surface_page_transition_active
        ):
            now = time.monotonic()
            if now <= self._surface_page_exact_wait_until:
                return
            # A missing companion snapshot must not freeze the old page forever.
            # Consume this final likely-clear packet, release the wait and ask the
            # next valid snapshot to repaint the page once.
            self._surface_page_waiting_for_reaper_exact = False
            self._surface_page_exact_wait_until = 0.0
            self._force_reaper_faders_on_next_snapshot = True
            self.log.emit(
                "REAPER bank snapshot wait timed out; retained motor positions "
                "until the next exact companion refresh"
            )
            return

        if event.kind == "fader" and self._consume_late_reaper_bank_fader(
            track, int(event.value)
        ):
            return

        if self._surface_page_transition_active and event.kind in {
            "fader", "mute", "solo", "select", "rec", "vpot_led"
        }:
            self._surface_page_feedback[(event.kind, track)] = event.value
            if event.kind == "fader":
                self._surface_page_expected_faders.discard(track)
            # Settle after the *last* packet, not a fixed delay after the Bank
            # button. Large projects and four separate MCU endpoints can easily
            # deliver the authoritative fader sweep later than 140 ms. A hard
            # upper bound prevents continuous automation from postponing the
            # refresh forever and leaving motors apparently stuck.
            if (
                self._surface_page_transition_started_at > 0.0
                and time.monotonic() - self._surface_page_transition_started_at
                >= self._surface_page_max_wait_seconds
            ):
                self._surface_page_settle_timer.stop()
                self._flush_surface_page_feedback()
            else:
                self._surface_page_settle_timer.start()
            return

        if event.kind == "fader":
            self._apply_daw_fader(track, int(event.value), mcu.pitch14_to_value7)
        elif event.kind == "mute":
            self._apply_daw_key(track, "mute", bool(event.value))
        elif event.kind == "solo":
            self._apply_daw_key(track, "pafl", bool(event.value))
        elif event.kind == "select":
            self._apply_daw_key(track, "mix", bool(event.value))
        elif event.kind == "rec":
            self._apply_daw_key(track, "rec", bool(event.value))
        elif event.kind == "vpot_led":
            self._apply_mcu_vpot_led(track, int(event.value))
        elif event.kind == "vpot":
            # A DAW should send LED-ring feedback to a surface, not V-Pot
            # rotation messages. Receiving one here is normally MIDI Thru or
            # loopback, so ignore it to prevent acceleration to an end stop.
            return

    def _apply_mcu_vpot_led(self, track: int, value: int) -> None:
        if not 0 <= track < self.tracks:
            return
        state = self.channels[track]
        # The optional REAPER helper owns exact FX parameter and Pan values.
        # Ignore the coarse stock-MCU ring in those two opt-in contexts so two
        # feedback sources cannot pull the same GLD rotary around.
        if self._reaper_plugin_control_active() and self._plugin_surface_active():
            return
        if self._reaper_exact_pan_active() and self._plugin_mode == "tracks":
            return
        if self._plugin_surface_active():
            if value < 0:
                value = 0
            if self.echo_daw_feedback_to_gld:
                self._send_pan_feedback_to_mapped_rotaries(track, value)
            return
        if self._plugin_mode == "send":
            # Sends are intentionally a pure relative MCU V-Pot path. The GLD
            # Gain parameter is an absolute accumulator; writing the 11-step MCU
            # ring back into it caused direction flips and sticking.
            return
        # A dark ring is not a valid absolute track-Pan position.
        if value < 0 or self._is_recent_pan_surface_origin(track):
            return
        value = max(0, min(127, int(value)))
        state.pan = value
        self.channel_changed.emit(track, state)
        if self.echo_daw_feedback_to_gld:
            self._send_pan_feedback_to_mapped_rotaries(track, state.pan)

    def _invalidate_surface_page_guards(self) -> None:
        """Forget transient ownership that belonged to the previous track page."""
        # These guards are indexed by physical strip, but after navigation that
        # strip represents a different project track. Keeping them would make a
        # recent movement/click on the old page suppress the new page's one and
        # only fader or LED tally.
        for history in self._daw_fader_out_history:
            history.clear()
        self._daw_key_pending.clear()
        for history in self._daw_switch_out_expected:
            history.clear()
        self._gld_key_pressed_at.clear()
        self._gld_key_last_sent.clear()
        self._fader_surface_origin_at = [0.0] * self.tracks
        self._pan_surface_origin_at = [0.0] * self.tracks
        self._rotary_fraction.clear()
        self._reaper_pending_pan = [None] * self.tracks
        self._reaper_pan_last_write = [0.0] * self.tracks
        self._reaper_pending_send_fader = [None] * self.tracks
        self._reaper_send_fader_last_write = [0.0] * self.tracks
        self._reaper_pending_track_fader = [None] * self.tracks
        self._reaper_track_fader_last_write = [0.0] * self.tracks
        self._reaper_track_fader_values = [None] * self.tracks
        self._reaper_send_values = [None] * self.tracks
        self._reaper_bank_fader_targets = [None] * self.tracks
        self._reaper_bank_old_fader_values = [None] * self.tracks
        self._reaper_bank_fader_guard_until = 0.0
        self._surface_page_waiting_for_reaper_exact = False
        self._surface_page_exact_wait_until = 0.0

    def _reaper_exact_bank_faders_available(self) -> bool:
        """Return whether the helper can replace the MCU bank clear sweep.

        Snapshot v13 added an exact normal track-fader field and an exact Send
        fader field.  When that source is live, retaining the old motor page for
        a few milliseconds is safer than applying REAPER's transient all-down
        sweep and then moving every fader a second time.
        """
        return (
            self.daw_protocol == "mcu"
            and self.reaper_sync_enabled
            and self._reaper_companion_connected
            and self._reaper_companion_version >= 13
            and self._plugin_mode in {"tracks", "send_fader"}
        )

    def _begin_surface_page_transition(self) -> None:
        """Coalesce a full Bank refresh and apply only its final strip state."""
        self._surface_page_feedback.clear()
        self._surface_page_expected_faders = set(range(self.tracks))
        self._surface_repaint_generation += 1
        self._surface_page_transition_active = True
        self._surface_page_transition_started_at = time.monotonic()
        self._surface_page_settle_timer.start()
        old_fader_values = [int(state.fader) for state in self.channels]
        self._invalidate_surface_page_guards()
        self._reaper_bank_old_fader_values = old_fader_values
        self._surface_page_waiting_for_reaper_exact = (
            self._reaper_exact_bank_faders_available()
        )
        self._surface_page_exact_wait_until = (
            time.monotonic() + self._surface_page_max_wait_seconds + 0.75
            if self._surface_page_waiting_for_reaper_exact
            else 0.0
        )
        # The physical motor can have missed an equal-valued write during the
        # host's clear/final sweep. Invalidate the output cache so every fader
        # present in the settled page is driven once, even when its numeric
        # value happens to match the previous page.
        self._last_gld_fader = [None] * self.tracks
        self._mcu_send_ready_banks.clear()
        self._mcu_send_assignment_at = [0.0] * len(self._mcu_send_assignment_at)
        self._send_gesture_last_at = [0.0] * self.tracks
        self.centre_send_rotaries()

    def _flush_surface_page_feedback(self) -> None:
        """Apply the final MCU state observed during a Bank/Channel refresh."""
        pending = self._surface_page_feedback
        self._surface_page_feedback = {}
        self._surface_page_transition_active = False
        self._surface_page_transition_started_at = 0.0
        missing_faders = set(self._surface_page_expected_faders)
        self._surface_page_expected_faders.clear()

        # REAPER emits a transient all-down MCU sweep during a full Bank move.
        # A live companion v13+ publishes the exact destination page, so keep
        # the motors at their previous positions until that atomic snapshot is
        # acknowledged. This makes each motor travel once, directly to its new
        # value, instead of visibly dipping to -inf first.
        hold_for_exact_reaper = bool(self._surface_page_waiting_for_reaper_exact)
        if not hold_for_exact_reaper:
            # Generic MCU hosts have no portable exact-page snapshot. Force
            # every received value once; an equal numeric value can still
            # belong to a motor that physically missed the host refresh. A
            # strip with no tally is cleared so stale positions cannot survive.
            for track in range(self.tracks):
                value = pending.get(("fader", track))
                if value is not None:
                    value7 = mcu.pitch14_to_value7(int(value))
                    self.channels[track].fader = value7
                    self.channel_changed.emit(track, self.channels[track])
                    self._drive_gld_fader(track, value7, force=True)
            for track in sorted(missing_faders):
                self.channels[track].fader = 0
                self.channel_changed.emit(track, self.channels[track])
                self._drive_gld_fader(track, 0, force=True)
            if missing_faders:
                self.log.emit(
                    f"Bank refresh repaired {len(missing_faders)} missing fader "
                    "tally/tallies by clearing stale motor positions"
                )
                self._force_reaper_faders_on_next_snapshot = True
        else:
            self._force_reaper_faders_on_next_snapshot = True
            self.log.emit(
                "REAPER bank refresh: holding motor positions until the exact "
                "destination snapshot arrives"
            )
        for kind, surface_kind in (
            ("mute", "mute"),
            ("solo", "pafl"),
            ("select", "mix"),
            ("rec", "rec"),
        ):
            for track in range(self.tracks):
                key = (kind, track)
                # A new MCU page is a complete ownership boundary. Hosts often
                # publish only lit/on switches, so absence must mean OFF rather
                # than "retain the previous bank's LED". This is the switch
                # equivalent of clearing a missing fader tally to -inf.
                self._apply_daw_key(
                    track, surface_kind, bool(pending.get(key, False))
                )
        for track in range(self.tracks):
            value = pending.get(("vpot_led", track))
            if value is not None:
                self._apply_mcu_vpot_led(track, int(value))

        if not hold_for_exact_reaper:
            self._schedule_surface_fader_repaint()

    def _arm_reaper_bank_fader_guard(self, targets: list[int | None]) -> None:
        """Make one exact REAPER snapshot authoritative over late MCU clears."""
        normalised: list[int | None] = [None] * self.tracks
        for track in range(min(self.tracks, len(targets))):
            value = targets[track]
            if value is not None:
                normalised[track] = max(0, min(127, int(value)))
        self._reaper_bank_fader_targets = normalised
        self._reaper_bank_fader_guard_until = time.monotonic() + 0.90
        self._surface_page_waiting_for_reaper_exact = False
        self._surface_page_exact_wait_until = 0.0
        self._surface_page_expected_faders.clear()
        # Drop fader packets collected before the atomic snapshot. Switch
        # tallies remain coalesced until the normal settle timer fires.
        self._surface_page_feedback = {
            key: value
            for key, value in self._surface_page_feedback.items()
            if key[0] != "fader"
        }

    def _consume_late_reaper_bank_fader(self, track: int, pitch14: int) -> bool:
        """Reject a late clear/old-page fader packet after exact bank repaint."""
        if time.monotonic() > self._reaper_bank_fader_guard_until:
            self._reaper_bank_fader_targets = [None] * self.tracks
            self._reaper_bank_old_fader_values = [None] * self.tracks
            self._reaper_bank_fader_guard_until = 0.0
            return False
        if not 0 <= track < self.tracks:
            return False
        target = self._reaper_bank_fader_targets[track]
        if target is None:
            return False
        value7 = mcu.pitch14_to_value7(int(pitch14))
        old_value = self._reaper_bank_old_fader_values[track]
        # Consume an exact duplicate, REAPER's characteristic all-down clear,
        # or a delayed value from the old page. A different non-zero value is
        # allowed through as genuine automation, even inside the guard window.
        if value7 == int(target):
            return True
        if value7 == 0 and int(target) != 0:
            return True
        if old_value is not None and value7 == int(old_value) and value7 != int(target):
            return True
        return False

    def _schedule_surface_fader_repaint(self) -> None:
        """Repeat the settled motor page without allowing stale-bank writes."""
        generation = int(self._surface_repaint_generation)
        single_shot = getattr(QTimer, "singleShot", None)
        if not callable(single_shot):
            # Lightweight test doubles do not need to emulate the Qt event
            # loop; the repaint worker is covered directly in regression tests.
            return
        for delay_ms in self._surface_repaint_delays_ms:
            single_shot(
                int(delay_ms),
                lambda generation=generation: self._repeat_surface_fader_repaint(
                    generation
                ),
            )

    def _repeat_surface_fader_repaint(self, generation: int) -> None:
        """Re-drive current fader values only when the same page is still active."""
        if (
            int(generation) != int(self._surface_repaint_generation)
            or self._surface_page_transition_active
            or self._surface_reset_in_progress
            or self.router is None
            or not getattr(self.router, "connected", False)
            or not self.echo_daw_feedback_to_gld
            or self.mapped_control_action("fader") != "track_fader"
        ):
            return
        for track, state in enumerate(self.channels):
            self._drive_gld_fader(track, state.fader, force=True)

    def _handle_hui_message(self, bank: int, msg) -> None:
        if not 0 <= bank < len(self._hui_parsers):
            return
        for local_track, name in hui.parse_display_sysex(msg):
            self._apply_daw_name(bank * 8 + local_track, name)

        event = self._hui_parsers[bank].parse(msg)
        if event is None:
            return
        if event.kind == "ping":
            self._send_to_daw(bank, hui.make_ping_reply())
            return
        track = bank * 8 + event.track
        if not 0 <= track < self.tracks:
            return
        if event.kind == "fader":
            self._apply_daw_fader(track, int(event.value), hui.fader14_to_value7)
        elif event.kind == "mute":
            self._apply_daw_key(track, "mute", bool(event.value))
        elif event.kind == "solo":
            self._apply_daw_key(track, "pafl", bool(event.value))
        elif event.kind == "select":
            self._apply_daw_key(track, "mix", bool(event.value))
        elif event.kind == "rec":
            self._apply_daw_key(track, "rec", bool(event.value))
        elif event.kind == "vpot_led":
            if self._is_recent_pan_surface_origin(track):
                return
            state = self.channels[track]
            state.pan = max(0, min(127, int(event.value)))
            self.channel_changed.emit(track, state)
            if self.echo_daw_feedback_to_gld:
                self._send_pan_feedback_to_mapped_rotaries(track, state.pan)

    def _observe_raw_daw_message(self, msg) -> None:
        event = gld.parse_midi_strip_message(msg)
        if event is None or not 0 <= event.strip < self.tracks:
            return
        state = self.channels[event.strip]
        if event.kind == "fader":
            state.fader = event.value
        elif event.kind == "pan":
            state.pan = event.value
        elif event.kind == "mute":
            state.mute = event.value >= 0x40
        elif event.kind == "mix":
            state.select = event.value >= 0x40
        elif event.kind == "pafl":
            state.solo = event.value >= 0x40
        self.channel_changed.emit(event.strip, state)

    def _apply_daw_name(self, track: int, name: str) -> None:
        if not 0 <= track < self.tracks:
            return
        name = str(name)[:8]
        if self.channels[track].name == name:
            return
        self.channels[track].name = name
        self.channel_changed.emit(track, self.channels[track])
        if self.send_names_to_gld:
            self._queue_editor_update("name", track, gld.make_editor_midi_strip_name(track, name))

    def _apply_daw_fader(self, track: int, value14: int, to_value7) -> None:
        value14 = max(0, min(16383, int(value14)))

        # Suppress only values that match a very recent GLD-originated fader
        # message. The previous blanket ownership timer also discarded genuine
        # DAW automation and, during connection startup, could discard the only
        # initial fader snapshot that a host sends.
        if self._is_recent_daw_fader_echo(track, value14):
            return

        state = self.channels[track]
        state.fader = to_value7(value14)
        self.channel_changed.emit(track, state)
        if (
            self.echo_daw_feedback_to_gld
            and self.mapped_control_action("fader") == "track_fader"
        ):
            self._drive_gld_fader(track, state.fader)

    def _apply_daw_key(self, track: int, kind: str, on: bool) -> None:
        on = bool(on)
        if self._is_stale_daw_key_feedback(kind, track, on):
            return
        self._set_state_for_kind(track, kind, on)
        if self.echo_daw_feedback_to_gld:
            for source in self._surface_sources_for_kind(kind, track):
                self._send_gld_key_state(source, track, on)

    def _arm_daw_key_target(self, kind: str, track: int, target: bool) -> None:
        if kind not in {"mute", "mix", "pafl", "rec"} or not 0 <= track < self.tracks:
            return
        self._daw_key_pending[(kind, track)] = (
            bool(target),
            time.monotonic() + self._daw_key_settle_seconds,
        )

    def _is_stale_daw_key_feedback(self, kind: str, track: int, on: bool) -> bool:
        """Reject the old/opposite DAW tally during a local-button settle window.

        Keep the target armed for the full window even after a matching packet:
        several hosts send a correct tally followed by one last stale poll. Once
        the window expires, direct DAW changes are authoritative again.
        """
        key = (kind, track)
        pending = self._daw_key_pending.get(key)
        if pending is None:
            return False
        target, expires = pending
        if time.monotonic() > expires:
            self._daw_key_pending.pop(key, None)
            return False
        return bool(on) != bool(target)

    # ------------------------------------------------------------------
    # UI controls
    # ------------------------------------------------------------------
    def manual_fader(self, track: int, value: int) -> None:
        if not 0 <= track < self.tracks:
            return
        value = max(0, min(127, int(value)))
        self.channels[track].fader = value
        self.channel_changed.emit(track, self.channels[track])
        if self.daw_protocol == "raw":
            self._send_to_daw(0, gld.make_midi_strip_fader(track, value))
        elif self.mapped_control_action("fader") == "track_fader":
            if self._reaper_send_fader_control_active():
                self._send_reaper_send_fader(track, value)
            elif not self._reaper_send_fader_must_not_fallback():
                self._send_daw_fader(track, value, touch=True)
        self._set_motor_target(track, value)
        self._send_gld_fader(track, value)

    def manual_pan(self, track: int, value: int) -> None:
        if not 0 <= track < self.tracks:
            return
        if (
            self.daw_protocol == "mcu"
            and self._plugin_mode == "send"
            and not self._send_fader_flip_active
        ):
            self._activate_pan_assignment(track)
        value = max(0, min(127, int(value)))
        old = int(self.channels[track].pan)
        self.channels[track].pan = value
        self._gld_pan_raw[track] = value
        self._mark_pan_surface_origin(track)
        self.channel_changed.emit(track, self.channels[track])

        if self.daw_protocol == "raw":
            self._send_to_daw(0, gld.make_midi_strip_rotary("pan", track, value))
        else:
            self._send_track_pan_to_daw(track, value, old)
        self._send_gld_pan(track, value)

    def manual_mute(self, track: int, on: bool) -> None:
        self._manual_surface_button_state("mute", track, on)

    def manual_select(self, track: int, on: bool) -> None:
        self._manual_surface_button_state("mix", track, on)

    def manual_solo(self, track: int, on: bool) -> None:
        self._manual_surface_button_state("pafl", track, on)

    def _manual_surface_button_state(self, source: str, track: int, on: bool) -> None:
        if source not in {"mute", "mix", "pafl"} or not 0 <= track < self.tracks:
            return
        if self.daw_protocol == "raw":
            state = self.channels[track]
            on = bool(on)
            if source == "mute":
                state.mute = on
            elif source == "mix":
                state.select = on
            else:
                state.solo = on
            self.channel_changed.emit(track, state)
            self._send_to_daw(0, gld.make_midi_strip_key(source, track, on))
            self._send_gld_key_state(source, track, on)
            return
        action = self._resolved_button_action(source, track)
        if action == "disabled":
            self.channel_changed.emit(track, self.channels[track])
            return
        if action == "vpot_push":
            if bool(on) and self.daw_protocol == "mcu" and track < 8:
                self._send_plugin_vpot_push(track)
            self.channel_changed.emit(track, self.channels[track])
            return
        kind = self._logical_kind_for_action(action)
        if kind is None:
            return
        on = bool(on)
        current = self._state_for_kind(track, kind)
        if current == on:
            self._send_gld_key_state(source, track, on)
            return
        self._set_state_for_kind(track, kind, on)
        self._arm_daw_key_target(kind, track, on)
        self._send_track_action_click(action, track)
        self._send_gld_key_state(source, track, on)

    def _companion_identity_snapshot_is_current(self, metadata) -> bool:
        """Accept only monotonic snapshots from one active companion instance.

        Snapshot v7 carries a per-ReaScript instance token and a strictly
        increasing publication sequence. Older snapshot versions remain
        readable for compatibility, but cannot provide this stronger stale
        writer protection.
        """
        if int(getattr(metadata, "version", 0)) < 7:
            # Preserve legacy companion compatibility until a v7 owner appears.
            # Once a modern metadata script has published, an accidentally
            # still-running legacy copy must never take ownership of the shared
            # snapshot file or repaint an old name/colour page.
            return not bool(self._reaper_companion_instance_id)
        instance_id = str(getattr(metadata, "instance_id", "")).strip()
        sequence = int(getattr(metadata, "snapshot_sequence", 0))
        if not instance_id or sequence <= 0:
            return False

        current = self._reaper_companion_instance_id
        new_instance = False
        if current and instance_id != current:
            def instance_rank(value: str) -> tuple[float, float]:
                parts = str(value).split(":")
                try:
                    if len(parts) >= 2 and float(parts[0]) > 1_000_000_000:
                        return float(parts[0]), float(parts[1])
                    return 0.0, float(parts[0])
                except (TypeError, ValueError):
                    return -1.0, -1.0

            # Modern instance tokens start with wall-clock time and then
            # time_precise(), so a REAPER restart can take over immediately. A
            # delayed older writer cannot.
            if instance_rank(instance_id) <= instance_rank(current):
                return False
            self._reaper_companion_instance_id = instance_id
            self._reaper_snapshot_sequence = -1
            new_instance = True
        elif not current:
            self._reaper_companion_instance_id = instance_id
            new_instance = True

        if sequence <= self._reaper_snapshot_sequence:
            return False
        self._reaper_snapshot_sequence = sequence
        if new_instance:
            self._resync_new_reaper_companion_instance()
        return True

    def _companion_bank_snapshot_is_current(self, metadata) -> bool:
        """Reject page rows until they acknowledge the last navigation command."""
        pending = self._reaper_bank_pending
        if pending is None:
            return True
        target, sequence = pending
        offset_matches = int(metadata.offset) == int(target)
        sequence_matches = (
            int(metadata.version) < 6
            or int(getattr(metadata, "bank_sequence", 0)) >= int(sequence)
        )
        if offset_matches and sequence_matches:
            self._reaper_bank_pending = None
            self._force_editor_labels_on_next_snapshot = True
            self._force_reaper_faders_on_next_snapshot = True
            self._record_blink_suppress_until = time.monotonic() + 0.75
            return True
        return False

    def _resync_new_reaper_companion_instance(self) -> None:
        """A restarted optional helper starts from deterministic ownership."""
        self._reaper_plugin_pending = None
        self._plugin_mode = "tracks"
        self._send_fader_flip_active = False
        self._mcu_send_ready_banks.clear()
        self._reaper_plugin_values = [None] * 8
        self._reaper_pending_send_fader = [None] * self.tracks
        self._reaper_send_fader_last_write = [0.0] * self.tracks
        self._reaper_pending_track_fader = [None] * self.tracks
        self._reaper_track_fader_last_write = [0.0] * self.tracks
        self._reaper_track_fader_values = [None] * self.tracks
        self._force_reaper_faders_on_next_snapshot = True

        # Start a fresh cumulative Send domain for the new Lua instance. The
        # helper deletes old mailbox files on startup; resetting both session
        # and position here ensures its first accepted movement can neither
        # replay pre-restart detents nor lose a burst behind an old baseline.
        self._reaper_send_session = int(time.time_ns() // 1_000_000)
        self._reaper_send_sequence = self._reaper_send_session
        self._reaper_send_position = [0] * self.tracks
        self._reaper_send_values = [None] * self.tracks
        self._reaper_send_fader_sequence = self._reaper_send_session
        self._reaper_bank_fader_targets = [None] * self.tracks
        self._reaper_bank_old_fader_values = [None] * self.tracks
        self._reaper_bank_fader_guard_until = 0.0
        self._surface_page_waiting_for_reaper_exact = False
        self._surface_page_exact_wait_until = 0.0

    def _reaper_exact_pan_active(self) -> bool:
        return (
            self.daw_protocol == "mcu"
            and self.reaper_sync_enabled
            and self.reaper_sync_pan
            and self._reaper_companion_connected
            and self._reaper_companion_version >= 11
            and self._plugin_mode == "tracks"
        )

    def _reaper_plugin_control_active(self) -> bool:
        return (
            self.daw_protocol == "mcu"
            and self.reaper_sync_enabled
            and self.reaper_sync_plugins
            and self._reaper_companion_connected
            and self._reaper_companion_version >= 11
        )

    def _write_reaper_plugin_action(self, action: str, value: int = 0, *, target: str | None = None) -> bool:
        self._reaper_plugin_sequence += 1
        ok = write_plugin_action(action, value, self._reaper_plugin_sequence)
        if not ok:
            self.log.emit(f"Could not write REAPER plug-in action: {action}")
            return False
        if target is not None:
            self._reaper_plugin_pending = (
                str(target), self._reaper_plugin_sequence, time.monotonic() + 2.0
            )
            # Do not let old track-page colours queue in front of the FX page.
            # The acknowledged helper snapshot repaints the requested page.
            self._editor_sync_timer.stop()
            self._editor_sync_queue.clear()
            self._editor_sync_pending.clear()
            self._force_editor_labels_on_next_snapshot = True
            self._record_blink_suppress_until = time.monotonic() + 0.25
        return True

    def _send_reaper_pan(self, track: int, value: int, *, preserve_expiry: float | None = None) -> bool:
        if not 0 <= track < self.tracks:
            return False
        value = max(0, min(127, int(value)))
        self._reaper_pan_sequence += 1
        self._reaper_pan_last_write[track] = time.monotonic()
        ok = write_pan_command(track, value, self._reaper_pan_sequence)
        if ok:
            expiry = preserve_expiry or (time.monotonic() + 1.5)
            self._reaper_pending_pan[track] = (value, expiry)
        else:
            self.log.emit(f"Could not write exact REAPER Pan for track {track + 1}")
        return ok

    def _send_reaper_plugin_parameter(
        self, parameter: int, value: int, *, preserve_expiry: float | None = None
    ) -> bool:
        if not 0 <= parameter < 8:
            return False
        value = max(0, min(127, int(value)))
        self._reaper_plugin_parameter_sequence += 1
        self._reaper_plugin_parameter_last_write[parameter] = time.monotonic()
        ok = write_plugin_parameter_command(
            parameter, value, self._reaper_plugin_parameter_sequence
        )
        if ok:
            expiry = preserve_expiry or (time.monotonic() + 1.5)
            self._reaper_pending_plugin_values[parameter] = (value, expiry)
        else:
            self.log.emit(
                f"Could not write REAPER plug-in parameter {parameter + 1}"
            )
        return ok

    def _flush_reaper_control_commands(self) -> None:
        now = time.monotonic()
        for track, pending in enumerate(self._reaper_pending_pan):
            if pending is None:
                continue
            value, expiry = pending
            if now > expiry:
                self._reaper_pending_pan[track] = None
            elif now - self._reaper_pan_last_write[track] >= 0.12:
                self._send_reaper_pan(track, value, preserve_expiry=expiry)
        for track, pending in enumerate(self._reaper_pending_send_fader):
            if pending is None:
                continue
            value, expiry = pending
            if now > expiry:
                self._reaper_pending_send_fader[track] = None
            elif now - self._reaper_send_fader_last_write[track] >= 0.12:
                self._send_reaper_send_fader(track, value, preserve_expiry=expiry)
        for track, pending in enumerate(self._reaper_pending_track_fader):
            if pending is None:
                continue
            value, expiry = pending
            if now > expiry:
                self._reaper_pending_track_fader[track] = None
            elif now - self._reaper_track_fader_last_write[track] >= 0.12:
                if self._reaper_companion_version >= 15:
                    self._send_reaper_track_fader(track, value, preserve_expiry=expiry)
                else:
                    self._reaper_track_fader_last_write[track] = now
                    self._send_daw_fader(track, value, touch=False)
        for parameter, pending in enumerate(self._reaper_pending_plugin_values):
            if pending is None:
                continue
            value, expiry = pending
            if now > expiry:
                self._reaper_pending_plugin_values[parameter] = None
            elif now - self._reaper_plugin_parameter_last_write[parameter] >= 0.12:
                self._send_reaper_plugin_parameter(
                    parameter, value, preserve_expiry=expiry
                )

    def _reaper_host_detected(self) -> bool:
        """Return whether a live REAPER companion owns this surface session."""
        return (
            self.daw_protocol == "mcu"
            and self.reaper_sync_enabled
            and self._reaper_companion_connected
            and not self._plugin_surface_active()
        )

    def _reaper_send_control_active(self) -> bool:
        """Return whether a compatible companion owns REAPER Send movement."""
        return self._reaper_host_detected() and self._reaper_companion_version >= 13

    def _reaper_absolute_gain_active(self) -> bool:
        """Absolute DAW feedback is intentionally disabled for GLD GAIN.

        Pan has a reverse-engineered GLD Editor control frame. GAIN currently
        has only the public B2 CC, which changes the same bounded accumulator
        used by the physical encoder. Continuous feedback through that path
        causes the real control to jump or stick, so it is never enabled.
        """
        return False

    def _reaper_send_must_not_fallback(self) -> bool:
        """Protect a known REAPER session from accidental MCU Pan fallback."""
        return (
            self.daw_protocol == "mcu"
            and self.reaper_sync_enabled
            and (self._reaper_session_detected or self._reaper_companion_connected)
            and not self._plugin_surface_active()
        )

    def _activate_send_assignment(self, track: int = 0, *, force: bool = False) -> bool:
        """Select standard MCU Send on Universal and matching Extender.

        REAPER companion mode deliberately bypasses this method because stock
        REAPER ignores note 0x29 and would keep interpreting V-Pots as Pan.
        Other MCU hosts receive the normal assignment transaction.
        """
        if (
            self.daw_protocol != "mcu"
            or self._plugin_surface_active()
            or self._reaper_send_control_active()
        ):
            return False
        bank = max(0, min(len(self._mcu_scribble) - 1, int(track) // 8))
        entering = self._plugin_mode not in {"send", "send_fader"}
        if entering:
            self._plugin_mode = "send"
            self._mcu_send_ready_banks.clear()
            self._rotary_fraction = {
                key: value for key, value in self._rotary_fraction.items()
                if key[1] != "send"
            }

        now = time.monotonic()
        targets = {0, bank}
        sent = False
        for target_bank in sorted(targets):
            stale = now - self._mcu_send_assignment_at[target_bank] > self._send_gesture_idle_seconds
            if not (force or entering or stale or target_bank not in self._mcu_send_ready_banks):
                continue
            for msg in mcu.make_send_assignment_click():
                self._send_to_daw(target_bank, msg)
            self._mcu_send_ready_banks.add(target_bank)
            self._mcu_send_assignment_at[target_bank] = now
            sent = True

        if entering:
            self.log.emit(
                "MCU Send assignment active — Gain rotaries control Sends on Universal and Extenders"
            )
        return sent

    def _activate_pan_assignment(self, track: int | None = None) -> None:
        """Return every Send-touched MCU endpoint to Pan/Surround."""
        if (
            self.daw_protocol != "mcu"
            or self._plugin_mode not in {"send", "send_fader"}
            or self._send_fader_flip_active
        ):
            return
        banks = set(self._mcu_send_ready_banks)
        banks.add(0)
        if track is not None:
            banks.add(max(0, min(len(self._mcu_scribble) - 1, int(track) // 8)))
        for target_bank in sorted(banks):
            for msg in mcu.make_pan_assignment_click():
                self._send_to_daw(target_bank, msg)
        self._mcu_send_ready_banks.clear()
        self._mcu_send_assignment_at = [0.0] * len(self._mcu_send_assignment_at)
        self._send_gesture_last_at = [0.0] * self.tracks
        self._plugin_mode = "tracks"
        self.log.emit("MCU Send assignment closed — Pan rotaries restored")


    def _reaper_send_fader_control_active(self) -> bool:
        """Return whether companion v1.18+ owns the optional Send fader page."""
        return (
            self._send_fader_flip_active
            and self.daw_protocol == "mcu"
            and self.reaper_sync_enabled
            and self._reaper_companion_connected
            and self._reaper_companion_version >= 17
            and self._plugin_mode == "send_fader"
        )

    def _reaper_send_fader_must_not_fallback(self) -> bool:
        """Never let a known REAPER Send-fader gesture change track volume."""
        return (
            self._send_fader_flip_active
            and self.daw_protocol == "mcu"
            and self.reaper_sync_enabled
            and (self._reaper_session_detected or self._reaper_companion_connected)
        )

    def _send_reaper_send_fader(
        self, track: int, value: int, *, preserve_expiry: float | None = None
    ) -> bool:
        if not 0 <= track < self.tracks:
            return False
        value = max(0, min(127, int(value)))
        self._reaper_send_fader_sequence += 1
        self._reaper_send_fader_last_write[track] = time.monotonic()
        ok = write_send_fader_command(track, value, self._reaper_send_fader_sequence)
        if ok:
            expiry = preserve_expiry or (time.monotonic() + 1.5)
            self._reaper_pending_send_fader[track] = (value, expiry)
        else:
            self.log.emit(f"Could not write REAPER Send fader for track {track + 1}")
        return ok

    def _send_reaper_track_fader(
        self, track: int, value: int, *, preserve_expiry: float | None = None
    ) -> bool:
        """Write exact normal track volume while the Send fader page is active."""
        if not 0 <= track < self.tracks:
            return False
        value = max(0, min(127, int(value)))
        self._reaper_track_fader_sequence += 1
        self._reaper_track_fader_last_write[track] = time.monotonic()
        ok = write_track_fader_command(
            track, value, self._reaper_track_fader_sequence
        )
        if ok:
            expiry = preserve_expiry or (time.monotonic() + 1.5)
            self._reaper_pending_track_fader[track] = (value, expiry)
        else:
            self.log.emit(f"Could not write REAPER track volume for strip {track + 1}")
        return ok

    def _repaint_cached_reaper_fader_page(self, send_page: bool) -> None:
        """Move directly to the cached REAPER Send or track-volume page.

        Companion snapshots already carry both exact pages while either one is
        visible. Reusing that cache makes a detected GAIN/PAN layer click move
        the motors immediately; the following companion snapshot still
        confirms and repairs the page atomically.
        """
        if not (
            self.echo_daw_feedback_to_gld
            and self.daw_protocol == "mcu"
            and self.reaper_sync_enabled
            and self._reaper_companion_connected
            and self._reaper_companion_version >= 17
        ):
            return
        values = self._reaper_send_values if send_page else self._reaper_track_fader_values
        for track, value in enumerate(values):
            if value is None or not 0 <= track < self.tracks:
                continue
            target = max(0, min(127, int(value)))
            state = self.channels[track]
            if state.fader != target:
                state.fader = target
                self.channel_changed.emit(track, state)
            self._drive_gld_fader(track, target, force=True)

    def _set_send_fader_flip(self, enabled: bool) -> bool:
        """Toggle Send levels onto the motor faders without changing mappings."""
        enabled = bool(enabled)
        if self.daw_protocol != "mcu":
            self.log.emit("Send fader flip is available in MCU mode only")
            return False
        if enabled == self._send_fader_flip_active:
            return True

        known_reaper = (
            self.reaper_sync_enabled
            and (self._reaper_session_detected or self._reaper_companion_connected)
        )
        # Immediately before entering REAPER Send flip, the bridge model still
        # contains the visible normal track-fader page. Preserve that exact
        # page as the initial hidden rotary target so the first GAIN detent does
        # not have to wait for the next 10 ms companion snapshot.
        track_fader_seed = (
            [max(0, min(127, int(state.fader))) for state in self.channels]
            if known_reaper and enabled
            else None
        )
        # Mode transitions invalidate per-strip gesture guards, but the exact
        # companion values themselves are still valid for the same visible
        # bank. Preserve both pages so the motors can move immediately instead
        # of waiting for the next 10 ms snapshot.
        cached_send_seed = list(self._reaper_send_values) if known_reaper else None
        cached_track_seed = list(self._reaper_track_fader_values) if known_reaper else None
        if known_reaper:
            if not (self._reaper_companion_connected and self._reaper_companion_version >= 17):
                self.log.emit(
                    "REAPER Send fader flip needs companion v1.23 or newer; mode was not changed"
                )
                return False
            action = "send_flip_on" if enabled else "send_flip_off"
            target = "send_fader" if enabled else "tracks"
            if not self._write_reaper_plugin_action(action, 0, target=target):
                return False
            self._send_fader_flip_active = enabled
            self._plugin_mode = target
        else:
            if enabled:
                # Send assignment must reach every separate Extender endpoint,
                # exactly once. Flip itself is a global button on the Universal
                # surface. Reusing _activate_send_assignment() here would resend
                # the Universal assignment for every Extender.
                self._plugin_mode = "send"
                self._mcu_send_ready_banks.clear()
                self._mcu_send_assignment_at = [0.0] * len(self._mcu_send_assignment_at)
                assigned_at = time.monotonic()
                for bank in range(len(self._mcu_scribble)):
                    for msg in mcu.make_send_assignment_click():
                        self._send_to_daw(bank, msg)
                    self._mcu_send_ready_banks.add(bank)
                    self._mcu_send_assignment_at[bank] = assigned_at
                for msg in mcu.make_flip_click():
                    self._send_to_daw(0, msg)
                self._send_fader_flip_active = True
                self._plugin_mode = "send_fader"
            else:
                for msg in mcu.make_flip_click():
                    self._send_to_daw(0, msg)
                self._send_fader_flip_active = False
                # Now it is safe to return V-Pots and the host page to Pan.
                self._plugin_mode = "send"
                self._activate_pan_assignment()

        self._force_reaper_faders_on_next_snapshot = True
        self._invalidate_surface_page_guards()
        if cached_send_seed is not None:
            self._reaper_send_values = cached_send_seed
        if track_fader_seed is not None:
            self._reaper_track_fader_values = track_fader_seed
        elif cached_track_seed is not None:
            self._reaper_track_fader_values = cached_track_seed
        self._last_gld_fader = [None] * self.tracks
        for track in range(self.tracks):
            self._clear_motor_target(track)
        # GAIN needs a neutral accumulator only while it is the active Send
        # selector. Recentring it while leaving the page can swallow a quick
        # subsequent GAIN-layer refresh as an output echo.
        if enabled:
            self.centre_send_rotaries()
        else:
            for timer in self._gain_recentre_timers:
                timer.stop()
        self._repaint_cached_reaper_fader_page(send_page=enabled)
        if not enabled:
            self._gain_send_fader_active = False
        mode = "ON" if enabled else "OFF"
        self.log.emit(f"Send fader flip {mode}")
        return True

    def toggle_send_fader_flip(self) -> None:
        self._set_send_fader_flip(not self._send_fader_flip_active)

    def manual_softkey(self, index: int) -> None:
        index = int(index)
        if not 0 <= index < 10 or self.daw_protocol == "raw":
            return
        if (
            index == 7
            and self.daw_protocol == "mcu"
            and self.send_fader_flip_softkey8
        ):
            self.toggle_send_fader_flip()
            return
        action = self.mapped_softkey_action(index)
        if action == "disabled":
            return
        if self.daw_protocol == "mcu":
            if action == "plugin_toggle":
                if self._plugin_surface_active():
                    self.manual_plugin_exit()
                else:
                    self.manual_plugin_assignment()
                return
            if action == "record_selected":
                self._record_arm_selected_channels()
                return
            if action.startswith("mcu_note:"):
                try:
                    note = int(action.split(":", 1)[1], 16)
                except ValueError:
                    return
                for msg in mcu.make_global_button_click(note):
                    self._send_to_daw(0, msg)
                return
        if self.daw_protocol == "hui":
            if action.startswith("hui_selected:"):
                logical = action.split(":", 1)[1]
                mapped = {
                    "mute": "track_mute",
                    "solo": "track_solo",
                    "select": "track_select",
                }.get(logical)
                if mapped is None:
                    return
                selected = [i for i, state in enumerate(self.channels) if state.select]
                if not selected:
                    selected = [0]
                for track in selected:
                    self._send_track_action_click(mapped, track)
                return
            if action.startswith("hui_switch:"):
                try:
                    _prefix, zone_text, port_text = action.split(":", 2)
                    zone = max(0, min(7, int(zone_text)))
                    port = max(0, min(7, int(port_text)))
                except (ValueError, TypeError):
                    return
                for msg in hui.make_switch_click(zone, port):
                    self._send_to_daw(0, msg)

    def manual_plugin_assignment(self) -> None:
        """Open REAPER helper FX pages when enabled, otherwise standard MCU."""
        if self.daw_protocol != "mcu":
            self.log.emit("MCU Plug-in assignment is available in MCU mode only")
            return
        if self._send_fader_flip_active:
            self._set_send_fader_flip(False)
        if self._reaper_plugin_control_active():
            if self._write_reaper_plugin_action("plugin", 0, target="plugin_list"):
                self._plugin_mode = "plugin_list"
                self._mcu_send_ready_banks.clear()
                self._reaper_plugin_values = [None] * 8
                self.log.emit("REAPER FX insert list active")
            return
        for msg in mcu.make_softkey_click(8):
            self._send_to_daw(0, msg)
        self._plugin_mode = "plugin_list"
        self._mcu_send_ready_banks.clear()
        self.log.emit("MCU Plug-in assignment active; use MIX/V-Pot push and Pan rotaries")

    def manual_plugin_exit(self) -> None:
        """Return explicitly to the normal track/Pan page."""
        if self.daw_protocol != "mcu":
            return
        if self._reaper_plugin_control_active():
            self._write_reaper_plugin_action("exit", 0, target="tracks")
        else:
            for msg in mcu.make_plugin_exit_click():
                self._send_to_daw(0, msg)
        self._plugin_mode = "tracks"
        self._plugin_selected_track = -1
        self._plugin_selected_fx = -1
        self._plugin_fx_page = 0
        self._plugin_param_page = 0
        self._reaper_plugin_values = [None] * 8
        self.log.emit("Plug-in assignment closed; normal track Pan restored")

    def _record_arm_selected_channels(self) -> None:
        selected = [index for index, state in enumerate(self.channels) if state.select]
        if not selected:
            self.log.emit("REC selected: no MCU channel is currently selected")
            return
        for track in selected:
            bank, local = divmod(track, 8)
            for msg in mcu.make_record_click(local):
                self._record_daw_switch_out(bank, msg)
                self._send_to_daw(bank, msg)
        names = ", ".join(str(track + 1) for track in selected)
        self.log.emit(f"MCU REC/RDY toggled for selected channel(s): {names}")

    def _plugin_surface_active(self) -> bool:
        return self.daw_protocol == "mcu" and self._plugin_mode in {
            "plugin_list", "plugin_params"
        }

    def _send_plugin_vpot_push(self, track: int) -> None:
        if not 0 <= int(track) < 8:
            return
        track = int(track)
        if self._reaper_plugin_control_active():
            action = "select" if self._plugin_mode == "plugin_list" else "vpot_push"
            target = "plugin_params" if self._plugin_mode == "plugin_list" else None
            if self._write_reaper_plugin_action(action, track, target=target):
                if target is not None:
                    self._plugin_mode = target
                    self._reaper_plugin_values = [None] * 8
            return
        for msg in mcu.make_vpot_push_click(track):
            self._record_daw_switch_out(0, msg)
            self._send_to_daw(0, msg)
        if self._plugin_mode == "plugin_list":
            self._plugin_mode = "plugin_params"

    def manual_navigation(self, action: str) -> None:
        """Keep track banking canonical; only an active FX page is contextual."""
        action = str(action).lower()
        if action not in {"bank_left", "bank_right", "channel_left", "channel_right"}:
            return
        if self.daw_protocol != "mcu":
            self.log.emit("Bank/Channel navigation is available in MCU mode only")
            return

        if self._reaper_plugin_control_active() and self._plugin_surface_active():
            self._write_reaper_plugin_action(action, 0)
            return
        if self._plugin_surface_active():
            for msg in mcu.make_plugin_navigation_click(action):
                self._send_to_daw(0, msg)
            return

        # Arm the page transition and companion target before the MCU click.
        # REAPER can answer a Bank note immediately from another MIDI thread;
        # sending the click first left a small race in which its all-down sweep
        # reached the motors before the transition guard existed.
        self._advance_surface_offset(action)
        self._send_track_navigation(action)

    def _send_fixed_navigation(self, action: str) -> None:
        if self.daw_protocol != "mcu":
            return
        self._advance_surface_offset(action)
        self._send_track_navigation(action)

    def _send_track_navigation(self, action: str) -> None:
        """Send Bank/Channel once from the MCU Universal (bank 1) port.

        MCU Extenders do not own independent Bank/Channel buttons; the host
        moves the Universal and all attached Extenders as one surface. Sending
        the same click to all four ports makes REAPER process one gesture four
        times, while the companion advances only once. That is the source of
        the mismatched names and rapidly changing colours reported on v0.6.25.
        """
        for msg in mcu.make_navigation_click(action):
            self._send_to_daw(0, msg)

    def _handle_custom_navigation(self, kind: str, track: int, value: int) -> None:
        """Turn one selected GLD Custom rotary into MCU Bank/Channel buttons."""
        if self.daw_protocol == "raw":
            return
        if (
            self.daw_protocol != "mcu"
            or not self.custom_navigation_enabled
            or track != max(0, min(self.tracks - 1, int(self.custom_navigation_strip)))
            or kind not in self._custom_rotary_raw
        ):
            return
        mapping_action = self.mapped_control_action(kind)
        if mapping_action == "disabled":
            return

        value = max(0, min(127, int(value)))
        previous = self._custom_rotary_raw[kind][track]
        if previous is None:
            # Arm without guessing a direction from an unknown absolute start
            # value. Returning the parameter to centre makes the next detent
            # unambiguous and keeps the rotary effectively endless.
            self._reset_custom_navigation_rotary(kind, track)
            self.log.emit(
                f"MIDI Strip {track + 1} {kind.title()} navigation armed; turn again to navigate"
            )
            return

        self._custom_rotary_raw[kind][track] = value
        delta = ((value - int(previous) + 64) % 128) - 64
        if delta == 0:
            return

        now = time.monotonic()
        if now - self._custom_navigation_last_at[kind] < self._custom_navigation_cooldown_seconds:
            return
        self._custom_navigation_last_at[kind] = now

        if mapping_action in {"context_bank", "bank_navigation"}:
            action = "bank_right" if delta > 0 else "bank_left"
        elif mapping_action in {"context_channel", "channel_navigation"}:
            action = "channel_right" if delta > 0 else "channel_left"
        else:
            return
        if mapping_action in {"bank_navigation", "channel_navigation"}:
            self._send_fixed_navigation(action)
        else:
            self.manual_navigation(action)
        self._reset_custom_navigation_rotary(kind, track)

    def _reset_custom_navigation_rotary(self, kind: str, track: int) -> None:
        if kind not in self._custom_rotary_raw or not 0 <= track < self.tracks:
            return
        self._custom_rotary_raw[kind][track] = 64
        self._send_to_gld(gld.make_midi_strip_rotary(kind, track, 64))

    def _advance_surface_offset(self, action: str) -> None:
        old = max(0, int(self._surface_track_offset))
        total = max(0, int(self._reaper_total_tracks))
        new = old
        if action == "bank_left":
            new = max(0, old - 8)
        elif action == "bank_right":
            # REAPER's stock MCU implementation advances Bank by eight and
            # refuses another bank step once the current 32-strip group reaches
            # the project end. Other DAWs still receive the same standard MCU
            # note; this offset is only for the optional REAPER companion.
            if total <= 0 or old + self.tracks < total:
                new = old + 8
                if total > 0:
                    new = min(new, total - 1)
        elif action == "channel_left":
            new = max(0, old - 1)
        elif action == "channel_right":
            if total <= 0 or old < total - 1:
                new = old + 1

        if new == old:
            self.log.emit(f"MCU {action.replace('_', ' ').title()}: already at project boundary")
            return
        self._surface_track_offset = new
        self._force_reaper_faders_on_next_snapshot = True
        if action in {"bank_left", "bank_right"}:
            self._begin_surface_page_transition()
        else:
            self._invalidate_surface_page_guards()
        for track in range(self.tracks):
            self._clear_motor_target(track)
        self._reaper_bank_sequence += 1
        self._reaper_bank_pending = (
            (new, self._reaper_bank_sequence)
            if self.reaper_sync_enabled and self._reaper_companion_connected
            else None
        )

        # Stop repainting the previous page while REAPER and the companion are
        # moving. The next acknowledged snapshot repopulates Name/Colour pairs
        # in strip order. REC states are surface-relative, so old-page arms are
        # cleared until the DAW publishes the new tallies.
        self._record_blink_restore_timer.stop()
        self._record_blink_active_tracks.clear()
        self._record_blink_suppress_until = time.monotonic() + 0.75
        for state in self.channels:
            state.record = False
        self._editor_sync_timer.stop()
        self._editor_sync_queue.clear()
        self._editor_sync_pending.clear()
        self._force_editor_labels_on_next_snapshot = bool(self._reaper_bank_pending)

        # Never leave a bank command file behind for a companion that is not
        # running. On its next connection the snapshot handshake below aligns it
        # to the already-moved standard MCU surface.
        if self.reaper_sync_enabled and self._reaper_companion_connected:
            if not write_bank_offset(new, self._reaper_bank_sequence):
                self._reaper_bank_pending = None
                self.log.emit(f"Could not write REAPER bank offset command for track {new + 1}")
        else:
            self._reaper_bank_pending = None
        if self._reaper_bank_pending is not None:
            self.log.emit(
                f"MCU {action.replace('_', ' ').title()} — waiting for project track {new + 1} page acknowledgement"
            )
        else:
            self.log.emit(
                f"MCU {action.replace('_', ' ').title()} — visible project tracks start at {new + 1}"
            )

    # ------------------------------------------------------------------
    # Protocol output helpers
    # ------------------------------------------------------------------
    def _send_daw_fader(self, track: int, value7: int, touch: bool) -> None:
        bank, local = divmod(track, 8)
        if self.daw_protocol == "mcu":
            value14 = mcu.value7_to_pitch14(value7)
            self._record_daw_fader(track, value14)
            self._send_to_daw(bank, mcu.make_fader14(local, value14))
        elif self.daw_protocol == "hui":
            value14 = hui.value7_to_fader14(value7)
            self._record_daw_fader(track, value14)
            if touch:
                self._begin_hui_touch(track)
            for msg in hui.make_fader(local, value14):
                self._send_to_daw(bank, msg)

    def _send_daw_button_click(self, kind: str, track: int) -> None:
        bank, local = divmod(track, 8)
        if self.daw_protocol == "mcu":
            factory = {
                "mute": mcu.make_mute_click,
                "mix": mcu.make_select_click,
                "pafl": mcu.make_solo_click,
            }[kind]
            for msg in factory(local):
                self._record_daw_switch_out(bank, msg)
                self._send_to_daw(bank, msg)
        elif self.daw_protocol == "hui":
            port = {
                "mute": hui.PORT_MUTE,
                "mix": hui.PORT_SELECT,
                "pafl": hui.PORT_SOLO,
            }[kind]
            for msg in hui.make_switch_click(local, port):
                self._send_to_daw(bank, msg)

    def _begin_hui_touch(self, track: int) -> None:
        if self.daw_protocol != "hui" or not 0 <= track < self.tracks:
            return
        bank, local = divmod(track, 8)
        if not self._hui_touch_active[track]:
            self._hui_touch_active[track] = True
            for msg in hui.make_fader_touch(local, True):
                self._send_to_daw(bank, msg)
        self._hui_touch_timers[track].start()

    def _end_hui_touch(self, track: int) -> None:
        if not 0 <= track < self.tracks or not self._hui_touch_active[track]:
            return
        self._hui_touch_timers[track].stop()
        self._hui_touch_active[track] = False
        bank, local = divmod(track, 8)
        for msg in hui.make_fader_touch(local, False):
            self._send_to_daw(bank, msg)

    def _is_recent_fader_surface_origin(self, track: int) -> bool:
        return (
            0 <= track < self.tracks
            and time.monotonic() - self._fader_surface_origin_at[track]
            < self._fader_surface_guard_seconds
        )

    def _drive_gld_fader(self, track: int, value7: int, *, force: bool = False) -> bool:
        """Apply authoritative DAW fader feedback without smoothing or snapshots."""
        if not 0 <= track < self.tracks:
            return False
        value7 = max(0, min(127, int(value7)))
        current = self._last_gld_fader[track]
        if not force and current is not None and int(current) == value7:
            return False
        self._set_motor_target(track, value7)
        self._send_gld_fader(track, value7)
        return True

    def _set_motor_target(self, track: int, value7: int) -> None:
        if not 0 <= track < self.tracks:
            return
        value7 = max(0, min(127, int(value7)))
        self._gld_motor_target[track] = value7
        self._gld_motor_until[track] = time.monotonic() + self._gld_motor_guard_seconds
        current = self._last_gld_fader[track]
        self._gld_motor_last_distance[track] = abs(current - value7) if current is not None else None

    def _clear_motor_target(self, track: int) -> None:
        if not 0 <= track < self.tracks:
            return
        self._gld_motor_target[track] = None
        self._gld_motor_until[track] = 0.0
        self._gld_motor_last_distance[track] = None

    def _send_rotary_feedback(
        self, source: str, track: int, value: int, *, force: bool = False
    ) -> None:
        source = str(source)
        value = max(0, min(127, int(value)))
        if source == "pan":
            self._gld_pan_raw[track] = value
            if force:
                self._gld_pan_input_raw[track] = value
                self._gld_pan_last_sent[track] = None
            self._send_gld_pan(track, value, force=force)
        elif source == "gain":
            self._gld_gain_raw[track] = value
            if force:
                self._gld_gain_input_raw[track] = value
            self._send_gld_gain(track, value, force=force)

    def _send_pan_feedback_to_mapped_rotaries(
        self, track: int, value: int, *, force: bool = False
    ) -> None:
        for source in ("gain", "pan"):
            action = self.mapped_control_action(source)
            contextual = action == "context_pan" and (
                self._plugin_mode == "tracks"
                or (self._plugin_mode == "plugin_params" and track < 8)
            )
            if action == "track_pan" or contextual:
                self._send_rotary_feedback(source, track, value, force=force)

    def _send_send_feedback_to_mapped_rotaries(
        self, track: int, value: int, *, force: bool = False
    ) -> None:
        """Keep exact Send feedback out of the bounded GAIN accumulator."""
        _ = (track, value, force)

    def _send_flipped_volume_feedback_to_mapped_rotaries(
        self, track: int, value: int, *, force: bool = False
    ) -> None:
        """Keep hidden volume feedback out of the bounded GAIN accumulator."""
        _ = (track, value, force)

    def _schedule_gain_recentre(self, track: int) -> None:
        """Re-centre one GAIN accumulator after the physical gesture stops."""
        if not 0 <= track < self.tracks:
            return
        self._gain_recentre_timers[track].start()

    def _recenter_gain_after_idle(self, track: int) -> None:
        """Return idle Send/flip GAIN to 64 without touching the DAW value."""
        if (
            not 0 <= track < self.tracks
            or self.router is None
            or not getattr(self.router, "connected", False)
            or self._surface_reset_in_progress
            or self.mapped_control_action("gain") not in {"context_send", "send_fader_select"}
        ):
            return
        self._return_send_control_to_centre("gain", track)

    def centre_send_rotaries(self) -> None:
        """Prime all Send-mapped GAIN accumulators to a neutral midpoint."""
        if self.router is None or not getattr(self.router, "connected", False):
            return
        if self.mapped_control_action("gain") not in {"context_send", "send_fader_select"}:
            return
        for track in range(self.tracks):
            self._gld_gain_input_raw[track] = 64
            self._gld_gain_raw[track] = 64
            self._send_gld_gain(track, 64, force=True)

    def _return_send_control_to_centre(self, source: str, track: int) -> None:
        """Rebase a bounded GLD rotary without changing the DAW Send value."""
        if not 0 <= track < self.tracks:
            return
        if source == "gain":
            self._gld_gain_raw[track] = 64
            # Keep _gld_gain_input_raw at the last physical sample until the
            # expected 64 echo arrives. Fast turns before that echo therefore
            # still use the real previous physical value and remain monotonic.
            self._send_gld_gain(track, 64, force=True)
        elif source == "pan":
            self._gld_pan_raw[track] = 64
            self._gld_pan_last_sent[track] = None
            self._send_gld_pan(track, 64, force=True)

    def _send_gld_gain(self, track: int, value: int, *, force: bool = False) -> None:
        """Write the GLD Gain rotary layer, optionally repeating a rebase."""
        if not 0 <= track < self.tracks:
            return
        value = max(0, min(127, int(value)))
        if not force and self._gld_gain_last_sent[track] == value:
            return
        self._gld_gain_last_sent[track] = value
        self._gld_gain_last_output_at[track] = time.monotonic()
        self._gld_gain_out_expected[track].append(
            (value, time.monotonic() + 0.75)
        )
        self._send_to_gld(gld.make_midi_strip_rotary("gain", track, value))

    def _consume_gld_gain_echo(
        self, track: int, value: int, now: float | None = None
    ) -> bool:
        if not 0 <= track < self.tracks:
            return False
        now = time.monotonic() if now is None else now
        expected = self._gld_gain_out_expected[track]
        while expected and now > expected[0][1]:
            expected.popleft()
        for index, (expected_value, _expires) in enumerate(expected):
            if int(value) == int(expected_value):
                del expected[index]
                return True
        return False

    def _send_gld_pan(self, track: int, value: int, *, force: bool = False) -> None:
        if not 0 <= track < self.tracks:
            return
        value = max(0, min(127, int(value)))
        if not force and self._gld_pan_last_sent[track] == value:
            return
        self._gld_pan_last_sent[track] = value
        self._gld_pan_last_output_at[track] = time.monotonic()

        # Prefer the reverse-engineered GLD Editor Pan write on TCP 51321.
        # The public B2 rotary message changes the stored value but leaves the
        # currently visible Pan bar stale. The Editor frame changes the same
        # absolute value and redraws the physical scribble-strip LCD in real
        # time. Fall back to B2 when the Editor-control socket is unavailable.
        expires = time.monotonic() + 0.75
        self._gld_pan_out_expected[track].append((value, expires))
        sent_with_redraw = self._send_editor_label(
            gld.make_editor_midi_strip_pan(track, value)
        )
        if not sent_with_redraw:
            self._send_to_gld(gld.make_midi_strip_rotary("pan", track, value))

    def _consume_gld_pan_echo(self, track: int, value: int, now: float | None = None) -> bool:
        if not 0 <= track < self.tracks:
            return False
        now = time.monotonic() if now is None else now
        expected = self._gld_pan_out_expected[track]
        while expected and now > expected[0][1]:
            expected.popleft()
        for index, (expected_value, _expires) in enumerate(expected):
            if int(value) == int(expected_value):
                del expected[index]
                return True
        return False

    def _reaper_names_are_authoritative(self) -> bool:
        """Return whether MCU scribble text must not override companion names."""
        return (
            self.daw_protocol == "mcu"
            and self.reaper_sync_enabled
            and self.reaper_sync_names
            and self._reaper_companion_connected
        )

    def _record_daw_fader(self, track: int, value14: int) -> None:
        self._daw_fader_out_history[track].append((time.monotonic(), value14))

    def _is_recent_daw_fader_echo(self, track: int, value14: int) -> bool:
        if not 0 <= track < self.tracks:
            return False
        now = time.monotonic()
        history = self._daw_fader_out_history[track]
        while history and now - history[0][0] > self._daw_echo_guard_seconds:
            history.popleft()
        return any(abs(sent_value - value14) <= 48 for _, sent_value in history)

    def _mark_pan_surface_origin(self, track: int) -> None:
        if 0 <= track < self.tracks:
            self._pan_surface_origin_at[track] = time.monotonic()

    def _is_recent_pan_surface_origin(self, track: int) -> bool:
        return (
            0 <= track < self.tracks
            and time.monotonic() - self._pan_surface_origin_at[track] < self._pan_feedback_guard_seconds
        )

    # ------------------------------------------------------------------
    # REAPER companion integration
    # ------------------------------------------------------------------
    def _set_reaper_sync_status(self, text: str) -> None:
        if text == self._reaper_sync_last_status:
            return
        self._reaper_sync_last_status = text
        self.reaper_sync_status.emit(text)

    def _fail_safe_reaper_companion_mode(self) -> None:
        """Metadata loss never changes MCU mode or any control value."""
        return

    def _connected_reaper_status(self) -> str:
        count = self._reaper_track_count
        if self._reaper_total_tracks > 0:
            first = self._reaper_reported_offset + 1
            last = min(
                self._reaper_total_tracks,
                self._reaper_reported_offset + max(1, count),
            )
            tracks = f"tracks {first}–{last} of {self._reaper_total_tracks}"
        else:
            tracks = f"{count} track" if count == 1 else f"{count} tracks"
        if self._reaper_companion_version < 17:
            return (
                f"REAPER helper connected — {tracks}; update to companion v1.23 "
                "for GAIN-driven Send fader flip and Send selection"
            )
        mode = {
            "tracks": "normal track faders + exact Pan + labels",
            "send": "selected Send on motor faders + labels",
            "send_fader": f"Send {self._selected_send_slot + 1} on motor faders; GAIN selects Send",
            "plugin_list": "FX insert list",
            "plugin_params": "FX parameters",
        }.get(self._plugin_mode, "labels")
        return f"REAPER helper connected — {tracks}; {mode}; banking stays standard MCU"

    def _poll_reaper_companion(self) -> None:
        """Import optional REAPER Send/Pan/FX/labels and exact fader repair."""
        self._flush_reaper_control_commands()
        if not self.reaper_sync_enabled:
            self._reaper_companion_connected = False
            self._reaper_session_detected = False
            self._reaper_track_count = 0
            self._reaper_total_tracks = 0
            self._reaper_companion_version = 0
            self._reaper_bank_pending = None
            self._set_reaper_sync_status(
                "REAPER helper disabled — generic MCU Send + Flip only; stock REAPER may ignore the Send page"
            )
            return
        if self.vegas_active:
            self._set_reaper_sync_status("REAPER helper paused during Vegas mode")
            return

        now_mono = time.monotonic()
        def transient_snapshot_gap() -> bool:
            return (
                self._reaper_companion_connected
                and now_mono - self._reaper_last_valid_snapshot_at
                <= self._reaper_snapshot_grace_seconds
            )

        try:
            stat = self._reaper_sync_path.stat()
        except OSError:
            if transient_snapshot_gap():
                return
            self._reaper_companion_connected = False
            self._reaper_track_count = 0
            self._reaper_total_tracks = 0
            self._set_reaper_sync_status(
                "Waiting for REAPER companion v1.23 — in REAPER, wait for Connected before using GAIN Send flip"
            )
            return

        if max(0.0, time.time() - stat.st_mtime) > 5.0:
            if transient_snapshot_gap():
                return
            self._reaper_companion_connected = False
            self._set_reaper_sync_status("REAPER helper snapshot is stale; run companion v1.23")
            return

        try:
            snapshot_text = self._reaper_sync_path.read_text(encoding="utf-8")
            rows = parse_snapshot(snapshot_text, self.tracks)
            metadata = parse_snapshot_metadata(snapshot_text)
        except (OSError, UnicodeError, ValueError) as exc:
            if transient_snapshot_gap():
                return
            self._reaper_companion_connected = False
            self._set_reaper_sync_status(f"REAPER helper read error: {exc}")
            return
        if not rows:
            if transient_snapshot_gap():
                return
            self._reaper_companion_connected = False
            self._set_reaper_sync_status("REAPER helper snapshot is invalid")
            return

        previous_instance_id = self._reaper_companion_instance_id
        if not self._companion_identity_snapshot_is_current(metadata):
            # Preserve the last good status while an older duplicate briefly
            # wins the replace race. The newest active instance will overwrite it.
            return

        self._reaper_last_valid_snapshot_at = now_mono
        self._reaper_companion_connected = True
        self._reaper_session_detected = True
        self._reaper_sync_last_mtime_ns = stat.st_mtime_ns
        self._reaper_sync_last_text = snapshot_text
        self._reaper_track_count = len(rows)
        self._reaper_companion_version = int(metadata.version)
        if self._reaper_companion_version >= 13:
            self._reaper_send_upgrade_warned = False
            self._reaper_send_unavailable_warned = False
        self._reaper_reported_offset = max(0, int(metadata.offset))
        self._reaper_total_tracks = max(0, int(metadata.total_tracks))

        new_instance = (
            bool(self._reaper_companion_instance_id)
            and self._reaper_companion_instance_id != previous_instance_id
        )
        if new_instance and self._reaper_reported_offset != self._surface_track_offset:
            self._reaper_bank_sequence += 1
            target = max(0, int(self._surface_track_offset))
            self._reaper_bank_pending = (target, self._reaper_bank_sequence)
            write_bank_offset(target, self._reaper_bank_sequence)
            return

        if not self._companion_bank_snapshot_is_current(metadata):
            # Keep the last connected status stable while one metadata page
            # catches up with the already-sent standard MCU Bank/Channel click.
            return

        reported_mode = str(metadata.mode)
        pending_mode = self._reaper_plugin_pending
        if pending_mode is not None:
            target_mode, required_sequence, deadline = pending_mode
            acknowledged = (
                int(metadata.plugin_sequence) >= int(required_sequence)
                and reported_mode == target_mode
            )
            if acknowledged:
                self._reaper_plugin_pending = None
                self._plugin_mode = reported_mode
            elif now_mono <= deadline:
                reported_mode = self._plugin_mode
            else:
                self._reaper_plugin_pending = None
                self._plugin_mode = reported_mode
        elif int(metadata.version) >= 11 and (
            self.reaper_sync_plugins or reported_mode in {"tracks", "send", "send_fader"}
        ):
            self._plugin_mode = reported_mode

        if int(metadata.version) >= 13:
            self._send_fader_flip_active = self._plugin_mode == "send_fader"
            if not self._send_fader_flip_active:
                self._gain_send_fader_active = False
        if int(metadata.version) >= 17:
            self._selected_send_slot = max(0, min(31, int(metadata.send_slot)))

        self._plugin_selected_track = int(metadata.selected_track)
        self._plugin_selected_fx = int(metadata.selected_fx)
        self._plugin_fx_page = max(0, int(metadata.fx_page))
        self._plugin_param_page = max(0, int(metadata.param_page))

        force_labels = self._force_editor_labels_on_next_snapshot
        force_fader_repaint = bool(self._force_reaper_faders_on_next_snapshot)
        labels_changed = False
        seen_snapshot_tracks: set[int] = set()
        exact_bank_targets: list[int | None] | None = None
        if (
            int(metadata.version) >= 13
            and force_fader_repaint
            and self._plugin_mode in {"tracks", "send_fader"}
        ):
            exact_bank_targets = [0] * self.tracks
        for row in rows:
            if not 0 <= row.track < self.tracks:
                continue
            seen_snapshot_tracks.add(int(row.track))
            state = self.channels[row.track]
            changed = False
            if self.reaper_sync_names:
                safe_name = str(row.name).strip()[:8] or f"Ch{self._reaper_reported_offset + row.track + 1:03d}"
                if state.name != safe_name:
                    state.name = safe_name
                    changed = True
                    labels_changed = True
                if self.send_names_to_gld and (changed or force_labels):
                    self._queue_editor_update(
                        "name", row.track,
                        gld.make_editor_midi_strip_name(row.track, safe_name),
                    )
            if self.reaper_sync_colours:
                colour = rgb_to_gld_colour(
                    row.red, row.green, row.blue, row.has_custom_colour
                )
                colour_changed = state.colour != colour
                if colour_changed:
                    state.colour = colour
                    changed = True
                    labels_changed = True
                if self.send_colours_to_gld and (colour_changed or force_labels):
                    self._queue_editor_update(
                        "colour", row.track,
                        gld.make_editor_midi_strip_colour(row.track, colour),
                    )

            if (
                int(metadata.version) >= 11
                and row.value is not None
                and self._plugin_mode == "tracks"
                and self.reaper_sync_pan
            ):
                value = max(0, min(127, int(row.value)))
                pending = self._reaper_pending_pan[row.track]
                accept = True
                if pending is not None:
                    target, expires = pending
                    if value == target:
                        self._reaper_pending_pan[row.track] = None
                    elif now_mono <= expires:
                        accept = False
                    else:
                        self._reaper_pending_pan[row.track] = None
                if accept:
                    if state.pan != value:
                        state.pan = value
                        changed = True
                    if self.echo_daw_feedback_to_gld:
                        self._send_pan_feedback_to_mapped_rotaries(row.track, value)

            if int(metadata.version) >= 13 and row.fader is not None:
                track_fader_value = max(0, min(127, int(row.fader)))
                pending_track_fader = self._reaper_pending_track_fader[row.track]
                accept_track_fader = True
                if pending_track_fader is not None:
                    target, expires = pending_track_fader
                    if track_fader_value == target:
                        self._reaper_pending_track_fader[row.track] = None
                    elif now_mono <= expires:
                        accept_track_fader = False
                    else:
                        self._reaper_pending_track_fader[row.track] = None
                if accept_track_fader:
                    self._reaper_track_fader_values[row.track] = track_fader_value
                    if self.echo_daw_feedback_to_gld:
                        self._send_flipped_volume_feedback_to_mapped_rotaries(
                            row.track, track_fader_value
                        )

            if (
                int(metadata.version) >= 14
                and row.send is not None
                and self._plugin_mode in {"tracks", "send", "send_fader"}
            ):
                send_value = max(0, min(127, int(row.send)))
                pending = self._reaper_pending_send_fader[row.track]
                accept_send = True
                if pending is not None:
                    target, expires = pending
                    if send_value == target:
                        self._reaper_pending_send_fader[row.track] = None
                    elif now_mono <= expires:
                        accept_send = False
                    else:
                        self._reaper_pending_send_fader[row.track] = None
                if accept_send:
                    self._reaper_send_values[row.track] = send_value
                    if self.echo_daw_feedback_to_gld:
                        self._send_send_feedback_to_mapped_rotaries(
                            row.track, send_value
                        )

            if (
                int(metadata.version) >= 13
                and row.value is not None
                and self._plugin_mode == "send_fader"
                and self._send_fader_flip_active
            ):
                value = max(0, min(127, int(row.value)))
                pending = self._reaper_pending_send_fader[row.track]
                accept = True
                if pending is not None:
                    target, expires = pending
                    if value == target:
                        self._reaper_pending_send_fader[row.track] = None
                    elif now_mono <= expires:
                        accept = False
                    else:
                        self._reaper_pending_send_fader[row.track] = None
                if accept:
                    if state.fader != value:
                        state.fader = value
                        changed = True
                    if exact_bank_targets is not None:
                        exact_bank_targets[row.track] = value
                    if self.echo_daw_feedback_to_gld:
                        self._drive_gld_fader(row.track, value, force=True)

            # Snapshot v13 carries an exact normal track-fader value in a
            # separate field. It is used only as a one-shot repair after a page
            # move or after leaving Send flip, not as a competing continuous
            # owner of standard MCU faders.
            if (
                int(metadata.version) >= 13
                and self._plugin_mode == "tracks"
                and self._force_reaper_faders_on_next_snapshot
                and row.fader is not None
            ):
                value = max(0, min(127, int(row.fader)))
                if state.fader != value:
                    state.fader = value
                    changed = True
                if exact_bank_targets is not None:
                    exact_bank_targets[row.track] = value
                if self.echo_daw_feedback_to_gld:
                    self._drive_gld_fader(row.track, value, force=True)

            if (
                int(metadata.version) >= 11
                and row.value is not None
                and self._plugin_mode == "plugin_params"
                and self.reaper_sync_plugins
                and 0 <= row.track < 8
            ):
                value = max(0, min(127, int(row.value)))
                pending = self._reaper_pending_plugin_values[row.track]
                accept = True
                if pending is not None:
                    target, expires = pending
                    if value == target:
                        self._reaper_pending_plugin_values[row.track] = None
                    elif now_mono <= expires:
                        accept = False
                    else:
                        self._reaper_pending_plugin_values[row.track] = None
                if accept:
                    self._reaper_plugin_values[row.track] = value
                    if state.pan != value:
                        state.pan = value
                        changed = True
                    if self.echo_daw_feedback_to_gld:
                        self._send_pan_feedback_to_mapped_rotaries(
                            row.track, value, force=True
                        )

            if changed:
                self.channel_changed.emit(row.track, state)

        # The helper omits surface slots beyond the end of the project. Those
        # slots must be explicitly neutralised; otherwise the last track from a
        # previous bank can remain visible and its motor/key state appears to
        # be a randomly stuck channel. Atomic snapshots make this safe: an
        # omitted row genuinely means that no REAPER track occupies the slot.
        if (
            int(metadata.version) >= 13
            and self._plugin_mode in {"tracks", "send_fader"}
        ):
            for track in range(self.tracks):
                if track in seen_snapshot_tracks:
                    continue
                state = self.channels[track]
                changed = False
                fallback_name = f"MIDI {track + 1:02d}"[:8]
                if self.reaper_sync_names and state.name != fallback_name:
                    state.name = fallback_name
                    changed = True
                    labels_changed = True
                if self.reaper_sync_colours and state.colour != "white":
                    state.colour = "white"
                    changed = True
                    labels_changed = True
                if state.pan != 64:
                    state.pan = 64
                    changed = True
                if state.fader != 0:
                    state.fader = 0
                    changed = True
                self._reaper_track_fader_values[track] = 0
                self._reaper_send_values[track] = 0
                self._reaper_pending_track_fader[track] = None
                self._reaper_pending_send_fader[track] = None
                for attr in ("mute", "solo", "select", "record"):
                    if bool(getattr(state, attr)):
                        setattr(state, attr, False)
                        changed = True
                if self.send_names_to_gld and (changed or force_labels):
                    self._queue_editor_update(
                        "name", track,
                        gld.make_editor_midi_strip_name(track, fallback_name),
                    )
                if self.send_colours_to_gld and (changed or force_labels):
                    self._queue_editor_update(
                        "colour", track,
                        gld.make_editor_midi_strip_colour(track, "white"),
                    )
                if self.echo_daw_feedback_to_gld and (changed or force_fader_repaint):
                    self._drive_gld_fader(track, 0, force=True)
                    for surface_kind in ("mute", "mix", "pafl"):
                        self._send_gld_key_state(surface_kind, track, False, force=True)
                if changed:
                    self.channel_changed.emit(track, state)

        if labels_changed:
            self.labels_changed.emit()
        self._force_editor_labels_on_next_snapshot = False
        if exact_bank_targets is not None:
            self._arm_reaper_bank_fader_guard(exact_bank_targets)
        if self._plugin_mode in {"tracks", "send_fader"}:
            self._force_reaper_faders_on_next_snapshot = False
        if force_fader_repaint and self._plugin_mode in {"tracks", "send_fader"}:
            self._schedule_surface_fader_repaint()
        self._set_reaper_sync_status(self._connected_reaper_status())

    def _reset_channel_model(self) -> None:
        """Return every displayed strip to the neutral disconnected state."""
        for track, state in enumerate(self.channels):
            state.name = f"MIDI {track + 1:02d}"
            state.colour = "white"
            state.fader = 0
            state.pan = 64
            state.mute = False
            state.solo = False
            state.select = False
            state.record = False
            self.channel_changed.emit(track, state)
        self.labels_changed.emit()

    def reset_channels(self) -> None:
        """Neutralise all GLD MIDI Strips while preserving every connection.

        This is the user-facing soft reset: names become ``MIDI 01..32``,
        colours white, faders go to -inf, all rotary layers centre, and
        Mute/MIX/PAFL are cleared. No DAW or GLD socket is closed.
        """
        if self._send_fader_flip_active and self.daw_protocol == "mcu":
            self._set_send_fader_flip(False)

        if self.router is None or not getattr(self.router, "connected", False):
            self._reset_channel_model()
            self.reset_transport_state()
            self.channels_reset.emit()
            self.log.emit("Channels reset locally; no active GLD connection")
            return

        self.log.emit("Resetting GLD MIDI Strips while keeping connections active…")
        self._surface_reset_in_progress = True
        self._surface_page_settle_timer.stop()
        self._editor_sync_timer.stop()
        self._editor_sync_queue.clear()
        self._editor_sync_pending.clear()
        # Clear transient control ownership before transmitting the neutral
        # state. Keep the fact that this connection was identified as REAPER:
        # if its helper is temporarily stale, forgetting that identity here
        # would let the next Gain turn fall back to an MCU V-Pot and change Pan.
        # Echo expectations created by the reset packets must remain alive after
        # this method returns; otherwise a delayed GLD OFF tally can look like a
        # real button press and immediately toggle the DAW again.
        known_reaper_session = self._reaper_session_detected
        self.reset_transport_state()
        self._reaper_session_detected = known_reaper_session
        editor_connected = bool(self.router.editor_labels_connected)
        try:
            for track in range(self.tracks):
                reset_name = f"MIDI {track + 1:02d}"[:8]
                self._last_gld_fader[track] = None
                self._set_motor_target(track, 0)
                self._send_to_gld(gld.make_midi_strip_fader(track, 0))
                for kind in ("gain", "pan", "custom1", "custom2"):
                    self._send_to_gld(gld.make_midi_strip_rotary(kind, track, 64))
                for kind in ("mute", "mix", "pafl"):
                    self._send_gld_key_state(kind, track, False, force=True)
                if editor_connected:
                    # Direct frames are intentionally unslept. Blocking sleeps
                    # on the GUI thread were part of the old Disconnect freeze.
                    self._send_editor_label(
                        gld.make_editor_midi_strip_name(track, reset_name)
                    )
                    self._send_editor_label(
                        gld.make_editor_midi_strip_colour(track, "white")
                    )
                    self._send_editor_label(
                        gld.make_editor_midi_strip_pan(track, 64)
                    )
            self._reset_channel_model()
            # Ignore a short tail of already-queued DAW feedback, but keep the
            # transport connected so later genuine changes work normally.
            self._surface_reset_guard_until = time.monotonic() + 0.75
        finally:
            self._surface_reset_in_progress = False

        self.channels_reset.emit()
        if editor_connected:
            self.log.emit(
                "Channels reset: names MIDI 01–32, colours white, faders -inf, "
                "rotaries centred and Mute/MIX/PAFL off; connections preserved"
            )
        else:
            self.log.emit(
                "MIDI channels reset with connections preserved; names/colours "
                "could not be written because Editor control was unavailable"
            )

    def reset_gld_surface_for_disconnect(self) -> None:
        """Compatibility wrapper used by Disconnect and older integrations."""
        self.reset_channels()

    # ------------------------------------------------------------------
    # Labels
    # ------------------------------------------------------------------
    def set_name(self, track: int, name: str) -> None:
        if not 0 <= track < self.tracks:
            return
        safe = "".join(c if 32 <= ord(c) <= 126 else " " for c in str(name))[:8]
        self.channels[track].name = safe
        self.channel_changed.emit(track, self.channels[track])
        if self.send_names_to_gld:
            self._queue_editor_update("name", track, gld.make_editor_midi_strip_name(track, safe))

    def set_colour(self, track: int, colour: str) -> None:
        if not 0 <= track < self.tracks:
            return
        colour = str(colour).lower().replace(" ", "_")
        if colour not in gld.COLOURS:
            colour = "off"
        self.channels[track].colour = colour
        self.channel_changed.emit(track, self.channels[track])
        if self.send_colours_to_gld:
            self._queue_editor_update("colour", track, gld.make_editor_midi_strip_colour(track, colour))

    def sync_all_labels(self) -> None:
        if self.router is None or not self.router.editor_labels_connected:
            self.log.emit(
                "GLD Editor control is not connected. A reconnect has been requested; "
                "close the official GLD Editor or other unused remote clients if the desk reports that all connections are in use."
            )
            if self.router is not None and hasattr(self.router, "restart_editor_control"):
                self.router.restart_editor_control()
            return
        self._queue_editor_full_sync(include_pan=False)
        self.log.emit("Queued all physical MIDI Strip names and colours for paced GLD sync")

    def _on_editor_connection_changed(self, connected: bool) -> None:
        if not connected:
            self._record_blink_restore_timer.stop()
            self._record_blink_active_tracks.clear()
            self._editor_sync_timer.stop()
            self._editor_sync_queue.clear()
            self._editor_sync_pending.clear()
            return
        self._record_blink_suppress_until = time.monotonic() + 0.5
        # Values may have changed while the GLD had no free Editor connection.
        # Always resend the complete current surface state after reconnecting.
        self._gld_pan_last_sent = [None] * self.tracks
        self._queue_editor_full_sync(include_pan=True)
        self.log.emit("GLD Editor control connected; queued Pan/name/colour resync")

    def _queue_editor_update(
        self, kind: str, track: int, payload: bytes
    ) -> None:
        """Coalesce one GLD Editor update and transmit it at a safe cadence."""
        if self.router is None or not self.router.editor_labels_connected:
            return
        key = (str(kind), int(track))
        if key not in self._editor_sync_pending:
            # Preserve the caller's order. Companion rows enqueue Name then
            # Colour for each strip, so a bank move no longer paints all new
            # names over the previous page's colours before colours catch up.
            self._editor_sync_queue.append(key)
        self._editor_sync_pending[key] = bytes(payload)
        self._editor_sync_timer.start()

    def _queue_editor_full_sync(self, include_pan: bool) -> None:
        if self.router is None or not self.router.editor_labels_connected:
            return
        self._editor_sync_queue.clear()
        self._editor_sync_pending.clear()
        for track, state in enumerate(self.channels):
            if self.send_names_to_gld:
                self._queue_editor_update(
                    "name", track, gld.make_editor_midi_strip_name(track, state.name)
                )
            if self.send_colours_to_gld:
                self._queue_editor_update(
                    "colour", track, gld.make_editor_midi_strip_colour(track, state.colour)
                )
            if include_pan and self.echo_daw_feedback_to_gld:
                self._queue_editor_update(
                    "pan", track, gld.make_editor_midi_strip_pan(track, state.pan)
                )
                self._gld_pan_last_sent[track] = state.pan
        if self._editor_sync_queue:
            self._editor_sync_timer.start()

    def _drain_editor_sync_queue(self) -> None:
        if self.router is None or not self.router.editor_labels_connected:
            self._editor_sync_timer.stop()
            return
        # Two short Editor frames per 4 ms tick keep the socket paced while
        # cutting a full 32-strip name+colour repaint to roughly 130 ms.
        sent = 0
        while self._editor_sync_queue and sent < 2:
            key = self._editor_sync_queue.popleft()
            payload = self._editor_sync_pending.pop(key, None)
            if payload is None:
                continue
            if not self._send_editor_label(payload):
                self._editor_sync_timer.stop()
                return
            sent += 1
        if not self._editor_sync_queue:
            self._editor_sync_timer.stop()
            self.log.emit("GLD Pan/name/colour resync completed")

    def _start_record_arm_blink(self) -> None:
        """Pulse the LCD colour of REC-armed surface strips without using LEDs."""
        if (
            not self.record_arm_blink_enabled
            or not self.send_colours_to_gld
            or self.router is None
            or not self.router.editor_labels_connected
            or self.vegas_active
            or self._plugin_mode not in {"tracks", "send"}
            or self._reaper_bank_pending is not None
            or time.monotonic() < self._record_blink_suppress_until
            or bool(self._editor_sync_queue)
        ):
            return
        armed = {index for index, state in enumerate(self.channels) if state.record}
        if not armed:
            self._restore_record_arm_blink()
            return
        self._record_blink_active_tracks = armed
        for track in sorted(armed):
            base = str(self.channels[track].colour)
            pulse = "white" if base == "red" else "red"
            self._queue_editor_update(
                "colour", track, gld.make_editor_midi_strip_colour(track, pulse)
            )
        self._record_blink_restore_timer.start()

    def _restore_record_arm_blink(self) -> None:
        """Restore true DAW colours after the short REC-arm pulse."""
        tracks = sorted(self._record_blink_active_tracks)
        self._record_blink_active_tracks.clear()
        self._record_blink_restore_timer.stop()
        if not self.send_colours_to_gld:
            return
        for track in tracks:
            if 0 <= track < self.tracks:
                self._queue_editor_update(
                    "colour",
                    track,
                    gld.make_editor_midi_strip_colour(track, self.channels[track].colour),
                )

    def _restore_record_arm_track(self, track: int) -> None:
        """Immediately remove an active REC overlay from one disarmed strip."""
        if track not in self._record_blink_active_tracks:
            return
        self._record_blink_active_tracks.discard(track)
        if self.send_colours_to_gld and 0 <= track < self.tracks:
            self._queue_editor_update(
                "colour",
                track,
                gld.make_editor_midi_strip_colour(track, self.channels[track].colour),
            )

    # ------------------------------------------------------------------
    # GLD output helpers
    # ------------------------------------------------------------------
    def _send_gld_fader(self, track: int, value: int) -> None:
        self._send_to_gld(gld.make_midi_strip_fader(track, value))

    def _emit_vegas_fader_now(self, track: int, value: int) -> None:
        if not 0 <= track < self.tracks:
            return
        state = self.channels[track]
        state.fader = max(0, min(127, int(value)))
        self.channel_changed.emit(track, state)
        self._set_motor_target(track, state.fader)
        self._send_to_gld(gld.make_midi_strip_fader(track, state.fader))

    def _send_to_daw(self, bank: int, msg) -> None:
        if self.router is not None:
            self.router.send_to_daw(bank, msg)

    def _send_to_gld(self, msg) -> None:
        if self.router is not None:
            self.router.send_to_gld(msg)

    def _send_editor_label(self, payload: bytes) -> bool:
        return bool(self.router is not None and self.router.send_editor_label(payload))

    def _send_gld_key_state(self, kind: str, track: int, on: bool, force: bool = False) -> None:
        if kind not in {"mute", "mix", "pafl"} or not 0 <= track < self.tracks:
            return
        key = (kind, track)
        on = bool(on)
        if not force and self._gld_key_last_sent.get(key) == on:
            return
        messages = gld.make_midi_strip_key_feedback(kind, track, on)
        self._gld_key_last_sent[key] = on
        expected = 0x7F if on else 0x3F
        queue = self._gld_key_out_expected.setdefault(key, deque(maxlen=16))
        queue.append((expected, time.monotonic() + self._gld_key_echo_guard_seconds))
        for msg in messages:
            self._send_to_gld(msg)

    def _consume_gld_key_feedback_echo(
        self,
        kind: str,
        track: int,
        value: int,
        now: float | None = None,
    ) -> bool:
        key = (kind, track)
        queue = self._gld_key_out_expected.get(key)
        if not queue:
            return False
        now = time.monotonic() if now is None else now
        while queue and now > queue[0][1]:
            queue.popleft()
        for index, (expected, _expires) in enumerate(queue):
            if int(value) == int(expected):
                del queue[index]
                if not queue:
                    self._gld_key_out_expected.pop(key, None)
                return True
        if not queue:
            self._gld_key_out_expected.pop(key, None)
        return False

    def _record_daw_switch_out(self, bank: int, msg) -> None:
        if not 0 <= bank < len(self._daw_switch_out_expected):
            return
        if msg.type not in {"note_on", "note_off"}:
            return
        on = msg.type == "note_on" and int(msg.velocity) > 0
        self._daw_switch_out_expected[bank].append(
            (int(msg.note), bool(on), time.monotonic() + self._daw_switch_echo_guard_seconds)
        )

    def _consume_daw_switch_echo(self, bank: int, msg) -> bool:
        if not 0 <= bank < len(self._daw_switch_out_expected):
            return False
        if msg.type not in {"note_on", "note_off"}:
            return False
        note = int(msg.note)
        on = msg.type == "note_on" and int(msg.velocity) > 0
        now = time.monotonic()

        # Only classify a packet as a reflected click while the corresponding
        # local action is still settling. Once that window has expired, a DAW
        # button change must be accepted immediately even if it happens to have
        # the same bytes as an old press/release message.
        kind: str | None = None
        local = -1
        if mcu.NOTE_MUTE <= note <= mcu.NOTE_MUTE + 7:
            kind, local = "mute", note - mcu.NOTE_MUTE
        elif mcu.NOTE_SOLO <= note <= mcu.NOTE_SOLO + 7:
            kind, local = "pafl", note - mcu.NOTE_SOLO
        elif mcu.NOTE_SELECT <= note <= mcu.NOTE_SELECT + 7:
            kind, local = "mix", note - mcu.NOTE_SELECT
        if kind is None:
            return False
        pending = self._daw_key_pending.get((kind, bank * 8 + local))
        if pending is None or now > pending[1]:
            return False

        queue = self._daw_switch_out_expected[bank]
        while queue and now > queue[0][2]:
            queue.popleft()
        for index, (expected_note, expected_on, _expires) in enumerate(queue):
            if note == expected_note and bool(on) == bool(expected_on):
                del queue[index]
                return True
        return False

    # ------------------------------------------------------------------
    # Vegas test
    # ------------------------------------------------------------------
    def start_vegas(self, bpm: int = 120) -> None:
        # Restarting Vegas first restores the previous test's saved state. An
        # inactive start must not call stop_vegas(), because older versions
        # reset every key and fader before the snapshot was taken.
        if self.vegas_active:
            self.stop_vegas(immediate_reset=True, log_event=False)

        self._vegas_saved_states = [
            (state.fader, state.mute, state.select, state.solo, state.colour)
            for state in self.channels
        ]
        self._gld_key_pressed_at.clear()
        self._daw_key_pending.clear()
        self._vegas_bpm = max(20, min(300, int(bpm)))
        self._vegas_phase = 0.0
        self._vegas_tick = 0
        self._vegas_last_colour_beat = -1
        timer = QTimer(self)
        timer.setInterval(50)
        timer.timeout.connect(self._vegas_step)
        self._vegas_timer = timer
        self._vegas_active = True
        timer.start()
        colours = ("physical+app on" if self.send_colours_to_gld else "app-only on") if self.vegas_colours_enabled else "off"
        self.log.emit(
            f"Vegas mode started at {self._vegas_bpm} BPM; colours: {colours}. "
            "GLD/DAW input is isolated until the saved surface state is restored."
        )

    def stop_vegas(self, immediate_reset: bool = False, log_event: bool = True) -> None:
        was_active = self._vegas_active or self._vegas_timer is not None
        if not was_active and not self._vegas_saved_states:
            return

        if self._vegas_timer is not None:
            self._vegas_timer.stop()
            self._vegas_timer.deleteLater()
            self._vegas_timer = None
        self._vegas_active = False
        # Restoring key LEDs can produce delayed non-zero packets on the public
        # MIDI socket. Ignore those briefly so they cannot be mistaken for new
        # physical clicks after the animation has already stopped.
        self._vegas_input_guard_until = time.monotonic() + 1.0

        saved_states = self._vegas_saved_states[:]
        self._vegas_saved_states.clear()
        for track, saved in enumerate(saved_states[: self.tracks]):
            fader, mute, select, solo, colour = saved
            state = self.channels[track]
            state.fader = max(0, min(127, int(fader)))
            state.mute = bool(mute)
            state.select = bool(select)
            state.solo = bool(solo)
            state.colour = str(colour)
            self.channel_changed.emit(track, state)

            # Restore the GLD only. Vegas never sends gestures to the DAW, so
            # restoring through MCU/HUI would toggle the DAW a second time.
            self._set_motor_target(track, state.fader)
            self._send_to_gld(gld.make_midi_strip_fader(track, state.fader))
            self._send_gld_key_state("mute", track, state.mute, force=True)
            self._send_gld_key_state("mix", track, state.select, force=True)
            self._send_gld_key_state("pafl", track, state.solo, force=True)
            if self.send_colours_to_gld:
                self._send_editor_label(
                    gld.make_editor_midi_strip_colour(track, state.colour)
                )

        self._vegas_last_colour_beat = -1
        self._gld_key_pressed_at.clear()
        self._daw_key_pending.clear()
        # Process a fresh REAPER snapshot after the restore. This also catches
        # project changes made while the test input was intentionally ignored.
        self._reaper_sync_last_mtime_ns = -1
        self._reaper_sync_last_text = ""

        if log_event and was_active:
            self.log.emit(
                "Vegas mode stopped; faders, Mute/Mix/PAFL LEDs and colours "
                "were restored to their exact pre-Vegas state"
            )

    def _vegas_step(self) -> None:
        seconds_per_beat = 60.0 / self._vegas_bpm
        self._vegas_phase += 0.05 / seconds_per_beat
        beat_phase = self._vegas_phase % 1.0
        beat_on = beat_phase < 0.16
        beat_index = int(self._vegas_phase)

        if self.vegas_colours_enabled and beat_index != self._vegas_last_colour_beat:
            self._vegas_last_colour_beat = beat_index
            palette = ("red", "yellow", "green", "light_blue", "blue", "purple", "white")
            for track in range(self.tracks):
                colour = palette[(track + beat_index) % len(palette)]
                state = self.channels[track]
                if state.colour != colour:
                    state.colour = colour
                    self.channel_changed.emit(track, state)
                    if self.send_colours_to_gld:
                        self._queue_editor_update("colour", track, gld.make_editor_midi_strip_colour(track, colour))

        for track in range(self.tracks):
            wave = (math.sin(self._vegas_phase * 2 * math.pi * 0.25 + track * 0.35) + 1.0) / 2.0
            self._emit_vegas_fader_now(track, int(round(wave * 127)))

            chase_group = (track + beat_index) % 3
            mute_on = beat_on and chase_group == 0
            mix_on = beat_on and chase_group == 1
            pafl_on = beat_on and chase_group == 2
            state = self.channels[track]
            changed = False
            if state.mute != mute_on:
                self._send_gld_key_state("mute", track, mute_on)
                state.mute = mute_on
                changed = True
            if state.select != mix_on:
                self._send_gld_key_state("mix", track, mix_on)
                state.select = mix_on
                changed = True
            if state.solo != pafl_on:
                self._send_gld_key_state("pafl", track, pafl_on)
                state.solo = pafl_on
                changed = True
            if changed:
                self.channel_changed.emit(track, state)

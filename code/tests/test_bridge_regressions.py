from __future__ import annotations

import sys
import os
import tempfile
import plistlib
import time
import threading
import types
import unittest
from unittest import mock
from pathlib import Path

import mido


class _BoundSignal:
    def __init__(self) -> None:
        self._callbacks = []

    def connect(self, callback) -> None:
        self._callbacks.append(callback)

    def disconnect(self, callback) -> None:
        self._callbacks = [item for item in self._callbacks if item != callback]

    def emit(self, *args, **kwargs) -> None:
        for callback in list(self._callbacks):
            callback(*args, **kwargs)


class _SignalDescriptor:
    def __init__(self, *args, **kwargs) -> None:
        self._name = ""

    def __set_name__(self, owner, name) -> None:
        self._name = f"__signal_{name}"

    def __get__(self, instance, owner):
        if instance is None:
            return self
        signal = instance.__dict__.get(self._name)
        if signal is None:
            signal = _BoundSignal()
            instance.__dict__[self._name] = signal
        return signal


class _QObject:
    def __init__(self, *args, **kwargs) -> None:
        pass


class _QTimer:
    def __init__(self, *args, **kwargs) -> None:
        self.timeout = _BoundSignal()
        self.interval = 0
        self.start_count = 0
        self.stop_count = 0

    def setSingleShot(self, value: bool) -> None:
        pass

    def setInterval(self, value: int) -> None:
        self.interval = int(value)

    def start(self, *args, **kwargs) -> None:
        self.start_count += 1

    def stop(self) -> None:
        self.stop_count += 1

    def deleteLater(self) -> None:
        pass


qtcore = types.ModuleType("PySide6.QtCore")
qtcore.QObject = _QObject
qtcore.QTimer = _QTimer
qtcore.Signal = _SignalDescriptor
pyside = types.ModuleType("PySide6")
pyside.QtCore = qtcore
sys.modules.setdefault("PySide6", pyside)
sys.modules.setdefault("PySide6.QtCore", qtcore)

from gld_mcu_bridge.bridge import BridgeEngine
from gld_mcu_bridge.config import _migrate
from gld_mcu_bridge.control_mapping import DEFAULT_CONTROL_MAPPINGS, normalise_control_mappings
from gld_mcu_bridge import startup
from gld_mcu_bridge.midi_io import MidiRouter, TcpRawClient
from gld_mcu_bridge.protocols import gld, mcu
from gld_mcu_bridge.reaper_sync import parse_snapshot, parse_snapshot_metadata


class FakeRouter:
    def __init__(self) -> None:
        self.gld_messages = []
        self.daw_messages = []
        self.editor_payloads = []
        self.gld_message = _BoundSignal()
        self.daw_message = _BoundSignal()
        self.log = _BoundSignal()
        self.editor_connection_changed = _BoundSignal()
        self.editor_labels_connected = True
        self.editor_restart_requests = 0
        self.editor_send_enabled = True
        self.connected = True

    def send_to_gld(self, msg) -> None:
        self.gld_messages.append(msg)

    def send_to_daw(self, bank: int, msg) -> None:
        self.daw_messages.append((bank, msg))

    def send_editor_label(self, payload) -> bool:
        if not self.editor_send_enabled:
            return False
        self.editor_payloads.append(bytes(payload))
        return True

    def restart_editor_control(self) -> None:
        self.editor_restart_requests += 1


class BridgeRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = BridgeEngine(tracks=32)
        self.router = FakeRouter()
        self.engine.attach_router(self.router)
        self.engine.reaper_sync_enabled = False

    def _use_legacy_gain_send(self) -> None:
        """Select the pre-v0.6.41 relative GAIN-to-V-Pot compatibility path."""
        self.engine.control_mappings["mcu"]["controls"]["gain"] = "context_send"

    def test_first_gld_fader_report_is_baseline(self) -> None:
        self.engine.handle_gld_message(gld.make_midi_strip_fader(0, 30))
        self.assertEqual([], self.router.daw_messages)
        self.assertEqual(30, self.engine.channels[0].fader)

        self.engine.handle_gld_message(gld.make_midi_strip_fader(0, 31))
        self.assertEqual(1, len(self.router.daw_messages))
        bank, msg = self.router.daw_messages[0]
        self.assertEqual(0, bank)
        self.assertEqual("pitchwheel", msg.type)
        self.assertEqual(mcu.value7_to_pitch14(31), msg.pitch + 8192)


    def test_late_first_gld_fader_report_is_treated_as_user_movement(self) -> None:
        self.engine._transport_reset_at = time.monotonic() - 5.0
        self.engine.handle_gld_message(gld.make_midi_strip_fader(0, 45))
        self.assertEqual(1, len(self.router.daw_messages))
        self.assertEqual(mcu.value7_to_pitch14(45), self.router.daw_messages[0][1].pitch + 8192)

    def test_initial_daw_snapshot_reaches_motor_after_gld_baseline(self) -> None:
        self.engine.handle_gld_message(gld.make_midi_strip_fader(0, 20))
        self.engine.handle_daw_message(0, mcu.make_fader(0, 90))
        self.assertEqual(1, len(self.router.gld_messages))
        msg = self.router.gld_messages[0]
        self.assertEqual("control_change", msg.type)
        self.assertEqual(1, msg.channel)
        self.assertEqual(0, msg.control)
        self.assertEqual(90, msg.value)

    def test_echo_is_suppressed_but_different_automation_is_accepted(self) -> None:
        self.engine.handle_gld_message(gld.make_midi_strip_fader(0, 20))
        self.engine.handle_gld_message(gld.make_midi_strip_fader(0, 50))
        sent_pitch = self.router.daw_messages[-1][1].pitch + 8192

        self.engine.handle_daw_message(0, mcu.make_fader14(0, sent_pitch))
        self.assertEqual([], self.router.gld_messages)

        different = mcu.value7_to_pitch14(100)
        self.engine.handle_daw_message(0, mcu.make_fader14(0, different))
        self.assertEqual(1, len(self.router.gld_messages))
        self.assertEqual(100, self.router.gld_messages[0].value)

    def test_each_nonzero_gld_mute_press_toggles_and_zero_is_release(self) -> None:
        press = mido.Message("note_on", channel=1, note=0, velocity=0x7F)
        release = mido.Message("note_off", channel=1, note=0, velocity=0)

        self.engine.handle_gld_message(press)
        self.assertTrue(self.engine.channels[0].mute)
        self.assertEqual(2, len(self.router.daw_messages))
        self.assertEqual(2, len(self.router.gld_messages))
        self.assertEqual([0x91, 0x00, 0x7F], self.router.gld_messages[0].bytes())
        self.assertEqual([0x81, 0x00, 0x00], self.router.gld_messages[1].bytes())

        # The physical release is not a second action.
        self.engine.handle_gld_message(release)
        self.assertTrue(self.engine.channels[0].mute)
        self.assertEqual(2, len(self.router.daw_messages))

        # A later physical press is another click and therefore toggles OFF.
        self.engine._gld_key_pressed_at[("mute", 0)] = 0.0
        self.engine._gld_key_out_expected.clear()
        self.engine.handle_gld_message(press)
        self.assertFalse(self.engine.channels[0].mute)
        self.assertEqual(4, len(self.router.daw_messages))
        self.assertEqual(4, len(self.router.gld_messages))
        self.assertEqual([0x91, 0x00, 0x3F], self.router.gld_messages[2].bytes())
        self.assertEqual([0x81, 0x00, 0x00], self.router.gld_messages[3].bytes())

    def test_low_nonzero_gld_key_packet_also_counts_as_a_press(self) -> None:
        # Some custom strip setups report 0x3F on the alternate switch state.
        self.engine.handle_gld_message(
            mido.Message("note_on", channel=1, note=0, velocity=0x7F)
        )
        self.assertTrue(self.engine.channels[0].mute)
        self.engine._gld_key_pressed_at[("mute", 0)] = 0.0
        self.engine.handle_gld_message(
            mido.Message("note_on", channel=1, note=0, velocity=0x3F)
        )
        self.assertFalse(self.engine.channels[0].mute)

    def test_repeated_daw_mute_tally_is_not_retransmitted_forever(self) -> None:
        self.engine.handle_daw_message(0, mcu.make_mute(0, True))
        self.engine.handle_daw_message(0, mcu.make_mute(0, True))
        self.assertEqual(2, len(self.router.gld_messages))
        self.assertEqual([0x91, 0x00, 0x7F], self.router.gld_messages[0].bytes())
        self.assertEqual([0x81, 0x00, 0x00], self.router.gld_messages[1].bytes())

    def test_stale_daw_mute_tally_cannot_undo_local_off_click(self) -> None:
        self.engine.channels[0].mute = True
        press = mido.Message("note_on", channel=1, note=0, velocity=0x7F)
        self.engine.handle_gld_message(press)
        self.assertFalse(self.engine.channels[0].mute)

        # A host/virtual-port poll can briefly return the previous ON tally.
        self.engine.handle_daw_message(0, mcu.make_mute(0, True))
        self.assertFalse(self.engine.channels[0].mute)

        # The requested OFF tally is accepted and keeps the state stable.
        self.engine.handle_daw_message(0, mcu.make_mute(0, False))
        self.assertFalse(self.engine.channels[0].mute)

    def test_local_mute_target_handles_reflected_mcu_press_release_pair(self) -> None:
        self.engine.manual_mute(0, True)
        self.assertTrue(self.engine.channels[0].mute)

        # If the virtual endpoint reflects the outgoing MCU click, the press is
        # the wanted target and its release must not be mistaken for LED-off.
        self.engine.handle_daw_message(0, mcu.make_mute(0, True))
        self.engine.handle_daw_message(0, mcu.make_mute(0, False))
        self.assertTrue(self.engine.channels[0].mute)

        self.engine.manual_mute(0, False)
        self.engine.handle_daw_message(0, mcu.make_mute(0, True))
        self.engine.handle_daw_message(0, mcu.make_mute(0, False))
        self.assertFalse(self.engine.channels[0].mute)

    def test_direct_daw_mute_is_authoritative_after_settle_window(self) -> None:
        self.engine.manual_mute(0, True)
        self.engine._daw_key_pending[("mute", 0)] = (True, time.monotonic() - 0.01)
        self.engine.handle_daw_message(0, mcu.make_mute(0, False))
        self.assertFalse(self.engine.channels[0].mute)

    def test_editor_pan_frames_match_capture_endpoints(self) -> None:
        self.assertEqual(
            bytes.fromhex("F0 00 01 06 DD 09 3D 10 38 00 02 00 05 F7"),
            gld.make_editor_midi_strip_pan(0, 5),
        )
        self.assertEqual(
            bytes.fromhex("F0 00 01 06 FC 09 5C 10 38 00 02 00 7F F7"),
            gld.make_editor_midi_strip_pan(31, 127),
        )

    def test_bridge_ui_pan_uses_editor_frame_for_live_lcd_redraw(self) -> None:
        self.engine.manual_pan(0, 100)
        self.assertEqual([], self.router.gld_messages)
        self.assertEqual(
            [gld.make_editor_midi_strip_pan(0, 100)],
            self.router.editor_payloads,
        )

    def test_bridge_ui_pan_falls_back_to_public_b2_without_editor_socket(self) -> None:
        self.router.editor_send_enabled = False
        self.engine.manual_pan(31, 12)
        self.assertEqual([], self.router.editor_payloads)
        self.assertEqual(
            [[0xB2, 0x3F, 12]],
            [msg.bytes() for msg in self.router.gld_messages],
        )

    def test_vegas_does_not_reset_surface_before_start_and_restores_exact_state(self) -> None:
        self.engine.send_colours_to_gld = True
        state = self.engine.channels[0]
        state.fader = 93
        state.mute = True
        state.select = True
        state.solo = False
        state.colour = "purple"

        self.engine.start_vegas(120)
        self.assertEqual(93, self.engine._vegas_saved_states[0][0])
        self.assertTrue(self.engine._vegas_saved_states[0][1])
        self.assertEqual([], self.router.gld_messages)

        # Simulate one animation step changing the model and surface.
        self.engine._emit_vegas_fader_now(0, 12)
        state.mute = False
        state.select = False
        state.solo = True
        state.colour = "red"
        self.router.gld_messages.clear()
        self.router.editor_payloads.clear()

        self.engine.stop_vegas()
        self.assertEqual(93, state.fader)
        self.assertTrue(state.mute)
        self.assertTrue(state.select)
        self.assertFalse(state.solo)
        self.assertEqual("purple", state.colour)

        # One fader message and three complete key state transactions are sent
        # for channel 1, plus the colour restore on the Editor socket.
        self.assertEqual([0xB1, 0x00, 93], self.router.gld_messages[0].bytes())
        self.assertEqual([0x91, 0x00, 0x7F], self.router.gld_messages[1].bytes())
        self.assertEqual([0x91, 0x20, 0x7F], self.router.gld_messages[3].bytes())
        self.assertEqual([0x91, 0x40, 0x3F], self.router.gld_messages[5].bytes())
        self.assertIn(gld.make_editor_midi_strip_colour(0, "purple"), self.router.editor_payloads)

    def test_vegas_ignores_gld_echoes_and_daw_tallies(self) -> None:
        self.engine.channels[0].mute = False
        self.engine.start_vegas(120)

        self.engine.handle_gld_message(
            mido.Message("note_on", channel=1, note=0, velocity=0x7F)
        )
        self.engine.handle_gld_message(gld.make_midi_strip_fader(0, 99))
        self.engine.handle_daw_message(0, mcu.make_mute(0, True))
        self.engine.handle_daw_message(0, mcu.make_fader(0, 100))

        self.assertEqual([], self.router.daw_messages)
        self.assertEqual([], self.router.gld_messages)
        self.assertFalse(self.engine.channels[0].mute)
        self.assertEqual(0, self.engine.channels[0].fader)

    def test_post_vegas_restore_echo_cannot_toggle_mute(self) -> None:
        self.engine.channels[0].mute = True
        self.engine.start_vegas(120)
        self.engine.stop_vegas()
        self.router.daw_messages.clear()

        # Delayed feedback from the GLD restore transaction is ignored.
        self.engine.handle_gld_message(
            mido.Message("note_on", channel=1, note=0, velocity=0x7F)
        )
        self.assertTrue(self.engine.channels[0].mute)
        self.assertEqual([], self.router.daw_messages)

        # Once the short restore guard expires, a real press works normally.
        self.engine._vegas_input_guard_until = 0.0
        self.engine._gld_key_pressed_at[("mute", 0)] = 0.0
        self.engine._gld_key_out_expected.clear()
        self.engine.handle_gld_message(
            mido.Message("note_on", channel=1, note=0, velocity=0x7F)
        )
        self.assertFalse(self.engine.channels[0].mute)
        self.assertEqual(2, len(self.router.daw_messages))

    def test_stop_vegas_when_inactive_is_noop(self) -> None:
        self.engine.channels[0].fader = 88
        self.engine.channels[0].mute = True
        self.engine.stop_vegas()
        self.assertEqual(88, self.engine.channels[0].fader)
        self.assertTrue(self.engine.channels[0].mute)
        self.assertEqual([], self.router.gld_messages)

    def test_daw_mute_led_echo_from_gld_does_not_toggle_daw_back(self) -> None:
        self.engine.handle_daw_message(0, mcu.make_mute(0, True))
        self.assertTrue(self.engine.channels[0].mute)
        self.router.daw_messages.clear()

        # The GLD/MIDI-over-TCP route can echo the non-zero LED write. It is a
        # one-shot feedback transaction, not a new human button press.
        self.engine.handle_gld_message(
            mido.Message("note_on", channel=1, note=0, velocity=0x7F)
        )
        self.assertTrue(self.engine.channels[0].mute)
        self.assertEqual([], self.router.daw_messages)

        # A later real surface press with the same bytes still toggles normally.
        self.engine._gld_key_out_expected.clear()
        self.engine._gld_key_pressed_at[("mute", 0)] = 0.0
        self.engine.handle_gld_message(
            mido.Message("note_on", channel=1, note=0, velocity=0x7F)
        )
        self.assertFalse(self.engine.channels[0].mute)
        self.assertEqual(2, len(self.router.daw_messages))

    def test_reaper_companion_names_ignore_transient_mcu_scribble_text(self) -> None:
        self.engine.reaper_sync_enabled = True
        self.engine.reaper_sync_names = True
        self.engine._reaper_companion_connected = True
        self.engine.channels[0].name = "Vocals"
        self.engine.send_names_to_gld = True

        transient = mido.Message(
            "sysex",
            data=(0x00, 0x00, 0x66, 0x14, 0x12, 0x00, *map(ord, "Pan R84")),
        )
        self.engine.handle_daw_message(0, transient)

        self.assertEqual("Vocals", self.engine.channels[0].name)
        self.assertEqual([], self.router.editor_payloads)

        # If the companion is no longer connected, ordinary MCU scribble names
        # become the fallback again.
        self.engine._reaper_companion_connected = False
        fallback = mido.Message(
            "sysex",
            data=(0x00, 0x00, 0x66, 0x14, 0x12, 0x00, *map(ord, "Guitar ")),
        )
        self.engine.handle_daw_message(0, fallback)
        self.assertEqual("Guitar", self.engine.channels[0].name)

    def test_blank_reaper_name_uses_five_character_unique_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "snapshot.tsv"
            self.engine._reaper_sync_path = snapshot
            self.engine.reaper_sync_enabled = True
            self.engine.reaper_sync_names = True
            self.engine.reaper_sync_colours = False
            self.engine.reaper_sync_pan = False
            snapshot.write_text(
                "GLD80_REAPER_SYNC\t2\t1\n"
                "1\t\t255\t255\t255\t0\t64\n",
                encoding="utf-8",
            )

            self.engine._poll_reaper_companion()
            self.assertEqual("Ch001", self.engine.channels[0].name)

    def test_ui_pafl_and_mix_buttons_use_standard_mcu_and_gld_feedback(self) -> None:
        self.engine.manual_solo(0, True)
        self.assertTrue(self.engine.channels[0].solo)
        self.assertEqual([mcu.NOTE_SOLO, mcu.NOTE_SOLO], [item[1].note for item in self.router.daw_messages])
        self.assertEqual([0x91, 0x40, 0x7F], self.router.gld_messages[0].bytes())

        self.router.daw_messages.clear()
        self.router.gld_messages.clear()
        self.engine.manual_select(0, True)
        self.assertTrue(self.engine.channels[0].select)
        self.assertEqual([mcu.NOTE_SELECT, mcu.NOTE_SELECT], [item[1].note for item in self.router.daw_messages])
        self.assertEqual([0x91, 0x20, 0x7F], self.router.gld_messages[0].bytes())

    def test_softkeys_use_mcu_plugin_and_selected_record_ready(self) -> None:
        self.engine.manual_softkey(0)
        self.assertEqual([0x36, 0x36], [item[1].note for item in self.router.daw_messages])
        self.assertEqual([127, 0], [item[1].velocity for item in self.router.daw_messages])

        self.router.daw_messages.clear()
        self.engine.manual_softkey(8)
        self.assertEqual([mcu.NOTE_PLUGIN, mcu.NOTE_PLUGIN], [item[1].note for item in self.router.daw_messages])
        self.assertEqual("plugin_list", self.engine._plugin_mode)

        self.router.daw_messages.clear()
        self.engine.channels[0].select = True
        self.engine.channels[9].select = True
        self.engine.manual_softkey(9)
        self.assertEqual([0, 0, 1, 1], [item[1].note for item in self.router.daw_messages])
        self.assertEqual([0, 0, 1, 1], [item[0] for item in self.router.daw_messages])
        self.assertEqual([127, 0, 127, 0], [item[1].velocity for item in self.router.daw_messages])

    def test_plugin_list_mix_key_becomes_standard_mcu_vpot_push(self) -> None:
        self.engine._plugin_mode = "plugin_list"
        self.engine.manual_select(2, True)
        self.assertEqual([mcu.NOTE_VPOT_PUSH + 2] * 2, [item[1].note for item in self.router.daw_messages])
        self.assertFalse(self.engine.channels[2].select)
        self.assertEqual("plugin_params", self.engine._plugin_mode)

    def test_editor_busy_text_is_treated_as_a_rejected_connection(self) -> None:
        class BusySocket:
            def __init__(self) -> None:
                self.closed = False

            def settimeout(self, value) -> None:
                pass

            def recv(self, size: int) -> bytes:
                return b"All available connections are in use"

            def close(self) -> None:
                self.closed = True

        fake_socket = BusySocket()
        connection_states = []
        messages = []
        client = None

        def on_log(message: str) -> None:
            messages.append(message)
            if "all remote-control connections are in use" in message:
                client._stop.set()

        client = TcpRawClient(
            "192.0.2.1", 51321, on_log, connection_states.append
        )
        with mock.patch("gld_mcu_bridge.midi_io.socket.create_connection", return_value=fake_socket):
            client._connection_loop(0.1)

        self.assertFalse(client.connected)
        self.assertEqual([], connection_states)
        self.assertTrue(fake_socket.closed)
        self.assertTrue(any("all remote-control connections are in use" in item for item in messages))

    def test_editor_busy_uses_sixty_second_safety_cooldown(self) -> None:
        class BusySocket:
            def settimeout(self, value) -> None:
                pass

            def setsockopt(self, *args) -> None:
                pass

            def recv(self, size: int) -> bytes:
                return b"All available connections are in use"

            def shutdown(self, how) -> None:
                pass

            def close(self) -> None:
                pass

        waits = []
        messages = []
        client = TcpRawClient("192.0.2.1", 51321, messages.append)

        def record_wait(seconds: float) -> None:
            waits.append(seconds)
            client._stop.set()

        client._wait_for_retry = record_wait
        with mock.patch(
            "gld_mcu_bridge.midi_io.socket.create_connection",
            return_value=BusySocket(),
        ):
            client._connection_loop(0.1)

        self.assertEqual([client.BUSY_COOLDOWN_SECONDS], waits)
        self.assertEqual(60.0, client.BUSY_COOLDOWN_SECONDS)
        self.assertTrue(any("Automatic retry paused for 60 seconds" in item for item in messages))

    def test_manual_editor_reconnect_reuses_the_existing_worker(self) -> None:
        class FakeSocket:
            def __init__(self) -> None:
                self.closed = False

            def shutdown(self, how) -> None:
                pass

            def close(self) -> None:
                self.closed = True

        class AliveThread:
            @staticmethod
            def is_alive() -> bool:
                return True

        client = TcpRawClient("192.0.2.1", 51321, lambda _message: None)
        sock = FakeSocket()
        client._socket = sock
        client._thread = AliveThread()
        with mock.patch.object(client, "connect") as connect_mock:
            client.request_reconnect()

        connect_mock.assert_not_called()
        self.assertTrue(sock.closed)
        self.assertTrue(client._retry_wake.is_set())
        self.assertTrue(client._force_retry)

    def test_raw_capture_emits_timestamp_ready_hex_lines_only_when_enabled(self) -> None:
        router = MidiRouter()
        lines = []
        router.raw_data.connect(lines.append)
        msg = mido.Message("control_change", channel=1, control=0, value=127)

        router._raw_midi("GLD RX MIDI", msg)
        self.assertEqual([], lines)

        router.set_raw_capture(True)
        router._raw_midi("GLD RX MIDI", msg)
        router._raw_bytes("GLD EDITOR TX", bytes((0xF0, 0x01, 0xF7)))
        self.assertIn("B1 00 7F", lines[0])
        self.assertIn("F0 01 F7", lines[1])

    def test_windows_builtin_virtual_port_request_has_actionable_error(self) -> None:
        router = MidiRouter()
        with mock.patch("gld_mcu_bridge.midi_io.sys.platform", "win32"):
            with self.assertRaisesRegex(ValueError, "cannot create virtual MIDI ports"):
                router.connect_ports(
                    "",
                    "",
                    ["GLD80 MCU 1", "", "", ""],
                    ["GLD80 MCU 1", "", "", ""],
                    use_virtual_daw_ports=True,
                    gld_connection_mode="midi",
                )

    def test_optional_physical_gld_softkey_custom_midi_mapping(self) -> None:
        press = mido.Message("note_on", channel=15, note=0, velocity=127)
        release = mido.Message("note_on", channel=15, note=0, velocity=0)
        self.engine.handle_gld_message(press)
        self.engine.handle_gld_message(release)
        self.assertEqual([0x36, 0x36], [item[1].note for item in self.router.daw_messages])

    def test_physical_soft9_short_press_enters_plugin_assignment(self) -> None:
        press = mido.Message("note_on", channel=15, note=8, velocity=127)
        release = mido.Message("note_on", channel=15, note=8, velocity=0)
        with mock.patch("gld_mcu_bridge.bridge.time.monotonic", side_effect=[10.0, 10.0, 10.2, 10.2]):
            self.engine.handle_gld_message(press)
            self.engine.handle_gld_message(release)
        self.assertEqual([0x2B, 0x2B], [item[1].note for item in self.router.daw_messages])
        self.assertEqual("plugin_list", self.engine._plugin_mode)

    def test_physical_soft9_hold_returns_to_pan_assignment(self) -> None:
        self.engine._plugin_mode = "plugin_params"
        press = mido.Message("note_on", channel=15, note=8, velocity=127)
        release = mido.Message("note_on", channel=15, note=8, velocity=0)
        with mock.patch("gld_mcu_bridge.bridge.time.monotonic", side_effect=[20.0, 20.0, 20.8, 20.8]):
            self.engine.handle_gld_message(press)
            self.engine.handle_gld_message(release)
        self.assertEqual([0x2A, 0x2A], [item[1].note for item in self.router.daw_messages])
        self.assertEqual("tracks", self.engine._plugin_mode)

    def test_editor_reconnect_queues_complete_current_surface_state(self) -> None:
        self.engine.send_names_to_gld = True
        self.engine.send_colours_to_gld = True
        self.engine.channels[0].name = "Vocal"
        self.engine.channels[0].colour = "red"
        self.engine.channels[0].pan = 100
        self.router.editor_connection_changed.emit(True)
        self.assertGreaterEqual(len(self.engine._editor_sync_queue), 3)
        # Names are intentionally prioritised ahead of colour/Pan traffic.
        self.engine._drain_editor_sync_queue()
        self.assertEqual(
            gld.make_editor_midi_strip_name(0, "Vocal"),
            self.router.editor_payloads[0],
        )
        while self.engine._editor_sync_queue:
            self.engine._drain_editor_sync_queue()
        self.assertIn(gld.make_editor_midi_strip_name(0, "Vocal"), self.router.editor_payloads)
        self.assertIn(gld.make_editor_midi_strip_colour(0, "red"), self.router.editor_payloads)
        self.assertIn(gld.make_editor_midi_strip_pan(0, 100), self.router.editor_payloads)

    def test_sync_labels_requests_editor_reconnect_when_connection_is_busy(self) -> None:
        self.router.editor_labels_connected = False
        self.engine.sync_all_labels()
        self.assertEqual(1, self.router.editor_restart_requests)

    def test_fader_zero_db_anchor_is_value_98(self) -> None:
        self.assertEqual("0.0 dB", gld.fader_value_to_db(98))

    def test_mcu_navigation_uses_standard_bank_and_channel_notes(self) -> None:
        expected = {
            "bank_left": 0x2E,
            "bank_right": 0x2F,
            "channel_left": 0x30,
            "channel_right": 0x31,
        }
        for action, note in expected.items():
            messages = mcu.make_navigation_click(action)
            self.assertEqual([note, note], [msg.note for msg in messages])
            self.assertEqual([127, 0], [msg.velocity for msg in messages])

    def test_plugin_navigation_uses_standard_mcu_cursor_notes(self) -> None:
        expected = {
            "bank_left": 0x62,
            "bank_right": 0x63,
            "channel_left": 0x60,
            "channel_right": 0x61,
        }
        for action, note in expected.items():
            messages = mcu.make_plugin_navigation_click(action)
            self.assertEqual([note, note], [msg.note for msg in messages])
            self.assertEqual([127, 0], [msg.velocity for msg in messages])

    def test_generic_plugin_mode_uses_cursor_navigation_not_track_banking(self) -> None:
        self.engine._plugin_mode = "plugin_params"
        self.engine.manual_navigation("bank_right")
        self.assertEqual([0x63, 0x63], [msg.note for _, msg in self.router.daw_messages])
        self.assertEqual(0, self.engine._surface_track_offset)
        self.router.daw_messages.clear()
        self.engine.manual_navigation("channel_left")
        self.assertEqual([0x60, 0x60], [msg.note for _, msg in self.router.daw_messages])
        self.assertEqual(0, self.engine._surface_track_offset)

    def test_custom1_selected_strip_sends_mcu_bank_navigation(self) -> None:
        self.engine.custom_navigation_strip = 31
        with mock.patch("gld_mcu_bridge.bridge.write_bank_offset", return_value=True):
            # First absolute value only arms and recentres the rotary.
            self.engine.handle_gld_message(gld.make_midi_strip_rotary("custom1", 31, 70))
            self.assertEqual([], self.router.daw_messages)
            self.assertEqual(64, self.router.gld_messages[-1].value)

            self.router.daw_messages.clear()
            self.router.gld_messages.clear()
            self.engine._custom_navigation_last_at["custom1"] = 0.0
            self.engine.handle_gld_message(gld.make_midi_strip_rotary("custom1", 31, 65))
            self.assertEqual([0, 0], [bank for bank, _msg in self.router.daw_messages])
            self.assertEqual([0x2F] * 2, [msg.note for _, msg in self.router.daw_messages])
            self.assertEqual(8, self.engine._surface_track_offset)
            self.assertEqual(64, self.router.gld_messages[-1].value)

            self.router.daw_messages.clear()
            self.engine._custom_navigation_last_at["custom1"] = 0.0
            self.engine.handle_gld_message(gld.make_midi_strip_rotary("custom1", 31, 63))
            self.assertEqual([0x2E] * 2, [msg.note for _, msg in self.router.daw_messages])
            self.assertEqual(0, self.engine._surface_track_offset)

    def test_custom2_selected_strip_sends_mcu_channel_navigation(self) -> None:
        self.engine.custom_navigation_strip = 31
        with mock.patch("gld_mcu_bridge.bridge.write_bank_offset", return_value=True):
            self.engine.handle_gld_message(gld.make_midi_strip_rotary("custom2", 31, 70))
            self.router.daw_messages.clear()
            self.engine._custom_navigation_last_at["custom2"] = 0.0
            self.engine.handle_gld_message(gld.make_midi_strip_rotary("custom2", 31, 65))
            self.assertEqual([0, 0], [bank for bank, _msg in self.router.daw_messages])
            self.assertEqual([0x31] * 2, [msg.note for _, msg in self.router.daw_messages])
            self.assertEqual(1, self.engine._surface_track_offset)

            self.router.daw_messages.clear()
            self.engine._custom_navigation_last_at["custom2"] = 0.0
            self.engine.handle_gld_message(gld.make_midi_strip_rotary("custom2", 31, 63))
            self.assertEqual([0x30] * 2, [msg.note for _, msg in self.router.daw_messages])
            self.assertEqual(0, self.engine._surface_track_offset)

    def test_custom_navigation_ignores_other_strips_and_non_mcu_modes(self) -> None:
        self.engine.custom_navigation_strip = 31
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("custom1", 30, 70))
        self.assertEqual([], self.router.daw_messages)
        self.engine.daw_protocol = "hui"
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("custom1", 31, 70))
        self.assertEqual([], self.router.daw_messages)

    def test_reaper_snapshot_v3_metadata_tracks_current_surface_page(self) -> None:
        text = "GLD80_REAPER_SYNC\t3\t123\t8\t64\n1\tTrack 9\t255\t0\t0\t1\t64\n"
        metadata = parse_snapshot_metadata(text)
        self.assertEqual(3, metadata.version)
        self.assertEqual(8, metadata.offset)
        self.assertEqual(64, metadata.total_tracks)

    def test_reaper_snapshot_v4_metadata_reports_plugin_workflow(self) -> None:
        text = (
            "GLD80_REAPER_SYNC\t4\t123\t8\t64\tplugin_params\t10\t2\t1\t3\n"
            "1\tThreshold\t55\t115\t220\t1\t90\n"
        )
        metadata = parse_snapshot_metadata(text)
        self.assertEqual(4, metadata.version)
        self.assertEqual("plugin_params", metadata.mode)
        self.assertEqual(10, metadata.selected_track)
        self.assertEqual(2, metadata.selected_fx)
        self.assertEqual(1, metadata.fx_page)
        self.assertEqual(3, metadata.param_page)


    def test_parameter_view_stays_locked_until_explicit_plugin_command(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "snapshot.tsv"
            self.engine._reaper_sync_path = snapshot
            self.engine.reaper_sync_enabled = True
            self.engine.reaper_sync_names = True
            self.engine.reaper_sync_colours = False
            self.engine.reaper_sync_plugins = True
            self.engine._plugin_mode = "plugin_params"
            self.engine._plugin_selected_track = 0
            self.engine._reaper_companion_instance_id = "1000:1:test"
            self.engine._reaper_snapshot_sequence = 1
            snapshot.write_text(
                "GLD80_REAPER_SYNC\t11\t1\t0\t2\tplugin_params\t0\t0\t0\t0\t0\t0\t7\t0\t1000:1:test\t2\tproject-A\n"
                "1\tFrequency\t125\t70\t180\t1\t64\n",
                encoding="utf-8",
            )
            self.engine._poll_reaper_companion()
            self.assertEqual("plugin_params", self.engine._plugin_mode)
            self.assertEqual(0, self.engine._plugin_selected_track)

    def test_gain_rotary_enters_standard_mcu_send_assignment(self) -> None:
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 64))
        self.assertEqual("send_fader", self.engine._plugin_mode)
        self.assertTrue(self.engine._send_fader_flip_active)
        self.assertTrue(self.engine._gain_send_fader_active)
        notes = [msg.note for _bank, msg in self.router.daw_messages if msg.type == "note_on"]
        self.assertEqual(8, notes.count(mcu.NOTE_SEND_ASSIGNMENT))
        self.assertEqual(2, notes.count(mcu.NOTE_FLIP))
        self.assertFalse(any(
            msg.type == "control_change" and msg.control >= mcu.CC_VPOT
            for _bank, msg in self.router.daw_messages
        ))

        # The first real detent selects the next Send rather than changing level.
        self.router.daw_messages.clear()
        self.engine._send_select_last_at = 0.0
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 66))
        self.assertEqual(
            [mcu.NOTE_CURSOR_DOWN, mcu.NOTE_CURSOR_DOWN],
            [msg.note for _bank, msg in self.router.daw_messages],
        )
        self.assertEqual(1, self.engine._selected_send_slot)

    def test_gain_layer_refresh_burst_enters_send_page_without_turn(self) -> None:
        now = time.monotonic()
        for track in (0, 1):
            self.engine._gld_gain_out_expected[track].append((64, now + 0.75))
            self.engine._gld_gain_last_output_at[track] = now - 0.30

        self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 64))
        self.assertFalse(self.engine._send_fader_flip_active)
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 1, 64))

        self.assertTrue(self.engine._send_fader_flip_active)
        self.assertTrue(self.engine._gain_send_fader_active)
        self.assertEqual("send_fader", self.engine._plugin_mode)

    def test_immediate_gain_output_echo_does_not_fake_layer_click(self) -> None:
        now = time.monotonic()
        for track in (0, 1):
            self.engine._gld_gain_out_expected[track].append((64, now + 0.75))
            self.engine._gld_gain_last_output_at[track] = now
            self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", track, 64))

        self.assertFalse(self.engine._send_fader_flip_active)
        self.assertFalse(self.engine._gain_send_fader_active)
        self.assertEqual("tracks", self.engine._plugin_mode)

    def test_pan_layer_refresh_burst_restores_track_page_without_turn(self) -> None:
        self.engine._plugin_mode = "send_fader"
        self.engine._send_fader_flip_active = True
        self.engine._gain_send_fader_active = True
        now = time.monotonic()
        for track in (0, 1):
            self.engine._gld_pan_out_expected[track].append((64, now + 0.75))
            self.engine._gld_pan_last_output_at[track] = now - 0.30

        self.engine.handle_gld_message(gld.make_midi_strip_rotary("pan", 0, 64))
        self.assertTrue(self.engine._send_fader_flip_active)
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("pan", 1, 64))

        self.assertFalse(self.engine._send_fader_flip_active)
        self.assertFalse(self.engine._gain_send_fader_active)
        self.assertEqual("tracks", self.engine._plugin_mode)

    def test_reaper_layer_switch_repaints_cached_fader_page_immediately(self) -> None:
        self.engine.reaper_sync_enabled = True
        self.engine._reaper_session_detected = True
        self.engine._reaper_companion_connected = True
        self.engine._reaper_companion_version = 17
        self.engine._reaper_send_values[0] = 23
        self.engine._reaper_send_values[1] = 87
        self.engine.channels[0].fader = 41
        self.engine.channels[1].fader = 99
        self.engine._reaper_track_fader_values[0] = 41
        self.engine._reaper_track_fader_values[1] = 99
        with mock.patch.object(self.engine, "_write_reaper_plugin_action", return_value=True):
            self.assertTrue(self.engine._set_send_fader_flip(True))
            sent = {
                msg.control: msg.value
                for msg in self.router.gld_messages
                if msg.type == "control_change" and msg.channel == 1
            }
            self.assertEqual(23, sent[0])
            self.assertEqual(87, sent[1])

            self.router.gld_messages.clear()
            self.assertTrue(self.engine._set_send_fader_flip(False))
            sent = {
                msg.control: msg.value
                for msg in self.router.gld_messages
                if msg.type == "control_change" and msg.channel == 1
            }
            self.assertEqual(41, sent[0])
            self.assertEqual(99, sent[1])

    def test_identical_pan_feedback_is_not_rewritten_every_poll(self) -> None:
        self.engine._send_gld_pan(0, 64)
        first_editor_count = len(self.router.editor_payloads)
        self.engine._send_gld_pan(0, 64)
        self.assertEqual(first_editor_count, len(self.router.editor_payloads))
        self.engine._send_gld_pan(0, 64, force=True)
        self.assertEqual(first_editor_count + 1, len(self.router.editor_payloads))

    def test_gain_rotary_can_select_previous_send_and_pan_restores_tracks(self) -> None:
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 64))
        self.engine._selected_send_slot = 2
        self.router.daw_messages.clear()
        self.engine._send_select_last_at = 0.0
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 62))
        self.assertEqual(
            [mcu.NOTE_CURSOR_UP, mcu.NOTE_CURSOR_UP],
            [msg.note for _bank, msg in self.router.daw_messages],
        )
        self.assertEqual(1, self.engine._selected_send_slot)

        self.router.daw_messages.clear()
        self.engine._gld_pan_input_raw[0] = 64
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("pan", 0, 65))
        notes = [msg.note for _bank, msg in self.router.daw_messages if msg.type == "note_on"]
        self.assertEqual(2, notes.count(mcu.NOTE_FLIP))
        self.assertEqual(8, notes.count(mcu.NOTE_PAN_ASSIGNMENT))
        self.assertEqual("tracks", self.engine._plugin_mode)
        self.assertFalse(self.engine._send_fader_flip_active)
        self.assertFalse(self.engine._gain_send_fader_active)

    def test_reaper_gain_turn_uses_send_mailbox_and_never_mcu_vpot(self) -> None:
        self.engine.reaper_sync_enabled = True
        self.engine._reaper_session_detected = True
        self.engine._reaper_companion_connected = True
        self.engine._reaper_companion_version = 17
        self.engine._plugin_mode = "tracks"
        with mock.patch.object(
            self.engine, "_write_reaper_plugin_action", return_value=True
        ) as write:
            self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 64))
            self.engine._send_select_last_at = 0.0
            self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 66))
        self.assertEqual("send_fader", self.engine._plugin_mode)
        self.assertEqual(
            [("send_flip_on", 0), ("send_next", 0)],
            [(call.args[0], call.args[1]) for call in write.call_args_list],
        )
        self.assertEqual([], self.router.daw_messages)

    def test_old_reaper_companion_blocks_gain_instead_of_panning(self) -> None:
        self.engine.reaper_sync_enabled = True
        self.engine._reaper_session_detected = True
        self.engine._reaper_companion_connected = True
        self.engine._reaper_companion_version = 16
        logs = []
        self.engine.log.connect(logs.append)
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 64))
        self.assertEqual([], self.router.daw_messages)
        self.assertFalse(self.engine._send_fader_flip_active)
        self.assertTrue(any("companion v1.23" in line for line in logs))

    def test_known_reaper_session_never_falls_back_when_helper_goes_stale(self) -> None:
        self.engine.reaper_sync_enabled = True
        self.engine._reaper_session_detected = True
        self.engine._reaper_companion_connected = False
        self.engine._reaper_companion_version = 0
        logs = []
        self.engine.log.connect(logs.append)
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 64))
        self.assertEqual([], self.router.daw_messages)
        self.assertFalse(self.engine._send_fader_flip_active)
        self.assertTrue(any("companion v1.23" in line for line in logs))

    def test_standard_mcu_send_reasserts_assignment_after_idle(self) -> None:
        self._use_legacy_gain_send()
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 64))
        self.router.daw_messages.clear()
        self.engine._send_gesture_last_at[0] = (
            time.monotonic() - self.engine._send_gesture_idle_seconds - 0.1
        )
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 66))
        self.assertEqual(3, len(self.router.daw_messages))
        self.assertEqual(
            [mcu.NOTE_SEND_ASSIGNMENT, mcu.NOTE_SEND_ASSIGNMENT],
            [msg.note for _bank, msg in self.router.daw_messages[:2]],
        )
        self.assertEqual("control_change", self.router.daw_messages[-1][1].type)
        self.assertEqual(mcu.CC_VPOT, self.router.daw_messages[-1][1].control)

    def test_gain_rotary_on_extender_assigns_send_on_matching_port_first(self) -> None:
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 10, 64))
        assignment_banks = [
            bank for bank, msg in self.router.daw_messages
            if msg.type == "note_on" and msg.note == mcu.NOTE_SEND_ASSIGNMENT
        ]
        self.assertIn(0, assignment_banks)
        self.assertIn(1, assignment_banks)
        self.assertIn(2, assignment_banks)
        self.assertIn(3, assignment_banks)
        self.assertTrue(self.engine._send_fader_flip_active)

        self.router.daw_messages.clear()
        self.engine._send_select_last_at = 0.0
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 10, 66))
        self.assertEqual(
            [mcu.NOTE_CURSOR_DOWN, mcu.NOTE_CURSOR_DOWN],
            [msg.note for _bank, msg in self.router.daw_messages],
        )

    def test_extender_send_assignment_is_not_repeated_for_every_detent(self) -> None:
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 10, 64))
        self.router.daw_messages.clear()
        self.engine._send_select_last_at = 0.0
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 10, 66))
        self.assertFalse(any(
            msg.type == "note_on" and msg.note == mcu.NOTE_SEND_ASSIGNMENT
            for _bank, msg in self.router.daw_messages
        ))
        self.assertEqual(
            [mcu.NOTE_CURSOR_DOWN, mcu.NOTE_CURSOR_DOWN],
            [msg.note for _bank, msg in self.router.daw_messages],
        )

    def test_send_vpot_feedback_does_not_fight_absolute_gld_gain(self) -> None:
        self.engine._plugin_mode = "send"
        self.engine._gld_gain_raw[0] = 64
        feedback = mido.Message(
            "control_change", channel=0, control=mcu.CC_VPOT_LED, value=0x06
        )
        self.engine.handle_daw_message(0, feedback)
        self.assertEqual([], self.router.gld_messages)
        self.assertEqual(64, self.engine._gld_gain_raw[0])

    def test_pan_turn_exits_send_mode_with_standard_mcu_pan_assignment(self) -> None:
        self.engine._plugin_mode = "send"
        self.engine._gld_pan_raw[0] = 64
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("pan", 0, 65))
        notes = [msg.note for _, msg in self.router.daw_messages if msg.type == "note_on"]
        self.assertEqual([0x2A, 0x2A], notes[:2])
        self.assertEqual("tracks", self.engine._plugin_mode)

    def test_standard_mcu_bank_channel_note_numbers_are_canonical(self) -> None:
        expected = {
            "bank_left": 0x2E,
            "bank_right": 0x2F,
            "channel_left": 0x30,
            "channel_right": 0x31,
        }
        for action, note in expected.items():
            messages = mcu.make_navigation_click(action)
            self.assertEqual([note, note], [msg.note for msg in messages])
            self.assertEqual([127, 0], [msg.velocity for msg in messages])

    def test_mapping_ui_uses_canonical_bank_channel_labels(self) -> None:
        from gld_mcu_bridge.control_mapping import KNOWN_MCU_NOTES

        self.assertEqual("Bank Right", KNOWN_MCU_NOTES[0x2F])
        self.assertEqual("Channel Left", KNOWN_MCU_NOTES[0x30])

    def test_bank_transition_is_armed_before_mcu_navigation_click(self) -> None:
        order = []
        original_advance = self.engine._advance_surface_offset
        original_send = self.engine._send_track_navigation

        def advance(action):
            order.append("arm")
            return original_advance(action)

        def send(action):
            order.append("send")
            return original_send(action)

        with mock.patch.object(self.engine, "_advance_surface_offset", side_effect=advance), \
             mock.patch.object(self.engine, "_send_track_navigation", side_effect=send):
            self.engine.manual_navigation("bank_right")
        self.assertEqual(["arm", "send"], order)
        self.assertTrue(self.engine._surface_page_transition_active)

    def test_full_bank_refresh_coalesces_clear_sweep_and_keeps_final_state(self) -> None:
        self.engine._last_gld_fader[0] = 90
        self.engine.channels[0].fader = 90
        self.engine.channels[0].mute = True
        self.engine._record_daw_fader(0, mcu.value7_to_pitch14(70))
        self.engine._arm_daw_key_target("mute", 0, True)

        self.engine.manual_navigation("bank_right")
        self.assertTrue(self.engine._surface_page_transition_active)
        self.assertEqual([], list(self.engine._daw_fader_out_history[0]))
        self.assertNotIn(("mute", 0), self.engine._daw_key_pending)

        # REAPER can briefly clear a whole surface before publishing the real
        # bank. Neither the clear nor the final values should move hardware
        # until the refresh burst has settled.
        self.engine.handle_daw_message(0, mcu.make_fader(0, 0))
        self.engine.handle_daw_message(0, mcu.make_mute(0, False))
        self.engine.handle_daw_message(0, mcu.make_fader(0, 77))
        self.engine.handle_daw_message(0, mcu.make_mute(0, True))
        self.assertEqual(90, self.engine.channels[0].fader)
        self.assertTrue(self.engine.channels[0].mute)

        self.engine._flush_surface_page_feedback()
        self.assertEqual(77, self.engine.channels[0].fader)
        self.assertTrue(self.engine.channels[0].mute)
        self.assertIn(gld.make_midi_strip_fader(0, 77), self.router.gld_messages)
        self.assertIn(gld.make_midi_strip_key("mute", 0, True), self.router.gld_messages)

    def test_bank_refresh_restarts_settle_timer_for_every_packet(self) -> None:
        self.engine.manual_navigation("bank_right")
        starts = self.engine._surface_page_settle_timer.start_count
        self.engine.handle_daw_message(0, mcu.make_fader(0, 20))
        self.engine.handle_daw_message(0, mcu.make_fader(1, 30))
        self.assertEqual(starts + 2, self.engine._surface_page_settle_timer.start_count)

    def test_bank_refresh_forces_equal_value_back_to_motor(self) -> None:
        self.engine.channels[0].fader = 77
        self.engine._last_gld_fader[0] = 77
        self.engine.manual_navigation("bank_right")
        self.engine.handle_daw_message(0, mcu.make_fader(0, 77))
        self.engine._flush_surface_page_feedback()
        self.assertIn(gld.make_midi_strip_fader(0, 77), self.router.gld_messages)

    def test_bank_refresh_has_hard_deadline_during_continuous_automation(self) -> None:
        self.engine.manual_navigation("bank_right")
        self.engine._surface_page_transition_started_at = (
            time.monotonic() - self.engine._surface_page_max_wait_seconds - 0.1
        )
        self.engine.handle_daw_message(0, mcu.make_fader(0, 88))
        self.assertFalse(self.engine._surface_page_transition_active)
        self.assertEqual(88, self.engine.channels[0].fader)
        self.assertIn(gld.make_midi_strip_fader(0, 88), self.router.gld_messages)

    def test_channel_navigation_clears_old_track_guards_without_bank_delay(self) -> None:
        self.engine._record_daw_fader(0, mcu.value7_to_pitch14(44))
        self.engine._arm_daw_key_target("mute", 0, True)
        self.engine.manual_navigation("channel_right")

        self.assertFalse(self.engine._surface_page_transition_active)
        self.assertEqual([], list(self.engine._daw_fader_out_history[0]))
        self.assertNotIn(("mute", 0), self.engine._daw_key_pending)
        self.engine.handle_daw_message(0, mcu.make_fader(0, 44))
        self.engine.handle_daw_message(0, mcu.make_mute(0, False))
        self.assertEqual(44, self.engine.channels[0].fader)
        self.assertFalse(self.engine.channels[0].mute)

    def test_send_mode_never_repurposes_bank_or_channel_navigation(self) -> None:
        self.engine._plugin_mode = "send"
        self.engine._reaper_total_tracks = 100
        with mock.patch("gld_mcu_bridge.bridge.write_bank_offset", return_value=True):
            self.engine.manual_navigation("bank_right")
        self.assertEqual([0, 0], [bank for bank, _msg in self.router.daw_messages])
        self.assertEqual([0x2F, 0x2F], [msg.note for _, msg in self.router.daw_messages])
        self.assertEqual(8, self.engine._surface_track_offset)

        self.router.daw_messages.clear()
        with mock.patch("gld_mcu_bridge.bridge.write_bank_offset", return_value=True):
            self.engine.manual_navigation("channel_right")
        self.assertEqual([0, 0], [bank for bank, _msg in self.router.daw_messages])
        self.assertEqual([0x31, 0x31], [msg.note for _, msg in self.router.daw_messages])
        self.assertEqual(9, self.engine._surface_track_offset)

    def test_reaper_snapshot_v5_metadata_reports_send_workflow(self) -> None:
        text = (
            "GLD80_REAPER_SYNC\t5\t123\t8\t64\tsend\t-1\t-1\t0\t0\t3\n"
            "1\tTrack 9\t255\t0\t0\t1\t90\n"
        )
        metadata = parse_snapshot_metadata(text)
        self.assertEqual(5, metadata.version)
        self.assertEqual("send", metadata.mode)
        self.assertEqual(3, metadata.send_slot)

    def test_reaper_snapshot_v6_metadata_reports_bank_ack_sequence(self) -> None:
        text = (
            "GLD80_REAPER_SYNC\t6\t123\t8\t64\ttracks\t-1\t-1\t0\t0\t0\t9876\n"
            "1\tTrack 9\t255\t0\t0\t1\t64\n"
        )
        metadata = parse_snapshot_metadata(text)
        self.assertEqual(6, metadata.version)
        self.assertEqual(8, metadata.offset)
        self.assertEqual(9876, metadata.bank_sequence)

    def test_stale_bank_snapshot_cannot_repaint_names_or_colours(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "snapshot.tsv"
            self.engine._reaper_sync_path = snapshot
            self.engine.reaper_sync_enabled = True
            self.engine.reaper_sync_names = True
            self.engine.reaper_sync_colours = True
            self.engine.reaper_sync_pan = False
            self.engine.send_names_to_gld = True
            self.engine.send_colours_to_gld = True
            self.engine.channels[0].name = "Keep"
            self.engine.channels[0].colour = "green"
            self.engine._surface_track_offset = 1
            self.engine._reaper_bank_pending = (1, 42)

            snapshot.write_text(
                "GLD80_REAPER_SYNC\t6\t1\t0\t8\ttracks\t-1\t-1\t0\t0\t0\t41\n"
                "1\tOLD\t255\t0\t0\t1\t64\n",
                encoding="utf-8",
            )
            self.engine._poll_reaper_companion()
            self.assertEqual("Keep", self.engine.channels[0].name)
            self.assertEqual("green", self.engine.channels[0].colour)
            self.assertEqual((1, 42), self.engine._reaper_bank_pending)
            self.assertEqual([], list(self.engine._editor_sync_queue))

            snapshot.write_text(
                "GLD80_REAPER_SYNC\t6\t2\t1\t8\ttracks\t-1\t-1\t0\t0\t0\t42\n"
                "1\tNEW\t255\t0\t0\t1\t64\n",
                encoding="utf-8",
            )
            self.engine._poll_reaper_companion()
            self.assertIsNone(self.engine._reaper_bank_pending)
            self.assertEqual("NEW", self.engine.channels[0].name)
            self.assertEqual("red", self.engine.channels[0].colour)
            self.assertEqual(
                [("name", 0), ("colour", 0)],
                list(self.engine._editor_sync_queue),
            )

    def test_record_arm_pulses_strip_colour_and_restores_daw_colour(self) -> None:
        self.engine.send_colours_to_gld = True
        self.engine.record_arm_blink_enabled = True
        self.engine.channels[0].record = True
        self.engine.channels[0].colour = "green"
        self.engine._start_record_arm_blink()
        while self.engine._editor_sync_queue:
            self.engine._drain_editor_sync_queue()
        self.assertEqual(
            gld.make_editor_midi_strip_colour(0, "red"),
            self.router.editor_payloads[-1],
        )

        self.engine._restore_record_arm_blink()
        while self.engine._editor_sync_queue:
            self.engine._drain_editor_sync_queue()
        self.assertEqual(
            gld.make_editor_midi_strip_colour(0, "green"),
            self.router.editor_payloads[-1],
        )

    def test_record_arm_on_red_track_pulses_white(self) -> None:
        self.engine.send_colours_to_gld = True
        self.engine.channels[0].record = True
        self.engine.channels[0].colour = "red"
        self.engine._start_record_arm_blink()
        while self.engine._editor_sync_queue:
            self.engine._drain_editor_sync_queue()
        self.assertEqual(
            gld.make_editor_midi_strip_colour(0, "white"),
            self.router.editor_payloads[-1],
        )

    def test_reaper_name_change_is_seen_even_when_mtime_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "snapshot.tsv"
            self.engine._reaper_sync_path = snapshot
            self.engine.reaper_sync_enabled = True
            self.engine.reaper_sync_names = True
            self.engine.reaper_sync_colours = False
            self.engine.reaper_sync_pan = False
            first = (
                "GLD80_REAPER_SYNC\t5\t1\t0\t1\ttracks\t-1\t-1\t0\t0\t0\n"
                "1\tTrack A\t255\t255\t255\t0\t64\n"
            )
            second = first.replace("Track A", "Track B")
            snapshot.write_text(first, encoding="utf-8")
            fixed_ns = time.time_ns()
            os.utime(snapshot, ns=(fixed_ns, fixed_ns))
            self.engine._poll_reaper_companion()
            self.assertEqual("Track A", self.engine.channels[0].name)

            snapshot.write_text(second, encoding="utf-8")
            os.utime(snapshot, ns=(fixed_ns, fixed_ns))
            self.engine._poll_reaper_companion()
            self.assertEqual("Track B", self.engine.channels[0].name)

    def test_editor_name_updates_are_coalesced_and_paced(self) -> None:
        self.engine.send_names_to_gld = True
        self.engine.set_name(0, "Old")
        self.engine.set_name(0, "Newest")
        self.assertEqual([], self.router.editor_payloads)
        self.assertEqual(1, len(self.engine._editor_sync_queue))
        self.engine._drain_editor_sync_queue()
        self.assertEqual(
            [gld.make_editor_midi_strip_name(0, "Newest")],
            self.router.editor_payloads,
        )

    def test_editor_drain_sends_two_frames_per_tick(self) -> None:
        self.engine.send_names_to_gld = True
        self.engine.send_colours_to_gld = True
        self.engine.set_name(0, "Vocal")
        self.engine.set_colour(0, "red")
        self.assertEqual(2, len(self.engine._editor_sync_queue))
        self.engine._drain_editor_sync_queue()
        self.assertEqual(2, len(self.router.editor_payloads))
        self.assertEqual(0, len(self.engine._editor_sync_queue))

    def test_plugin_page_request_discards_stale_editor_packets(self) -> None:
        self.engine.send_names_to_gld = True
        self.engine.set_name(0, "OldPage")
        self.assertTrue(self.engine._editor_sync_queue)
        with mock.patch("gld_mcu_bridge.bridge.write_plugin_action", return_value=True):
            self.engine._write_reaper_plugin_action("plugin", 0, target="plugin_list")
        self.assertFalse(self.engine._editor_sync_queue)
        self.assertFalse(self.engine._editor_sync_pending)
        self.assertTrue(self.engine._force_editor_labels_on_next_snapshot)

    def test_v065_saved_feedback_off_is_migrated_on(self) -> None:
        migrated = _migrate({
            "config_version": 15,
            "echo_daw_feedback_to_gld_midi_strips": False,
        })
        self.assertEqual(28, migrated["config_version"])
        self.assertTrue(migrated["echo_daw_feedback_to_gld_midi_strips"])

    def test_config_migration_preserves_established_default_mappings(self) -> None:
        migrated = _migrate({"config_version": 17})
        self.assertEqual(28, migrated["config_version"])
        self.assertEqual(
            normalise_control_mappings(DEFAULT_CONTROL_MAPPINGS),
            migrated["control_mappings"],
        )
        self.assertEqual("send_fader_select", migrated["control_mappings"]["mcu"]["controls"]["gain"])
        self.assertEqual("plugin_toggle", migrated["control_mappings"]["mcu"]["softkeys"][8])

    def test_v0635_migration_restores_gain_send_and_pan_layers(self) -> None:
        mappings = normalise_control_mappings(DEFAULT_CONTROL_MAPPINGS)
        mappings["mcu"]["controls"]["gain"] = "track_pan"
        mappings["mcu"]["controls"]["pan"] = "context_send"
        migrated = _migrate({
            "config_version": 25,
            "control_mappings": mappings,
            "reaper_sync_enabled": False,
        })
        controls = migrated["control_mappings"]["mcu"]["controls"]
        self.assertEqual("send_fader_select", controls["gain"])
        self.assertEqual("context_pan", controls["pan"])
        self.assertTrue(migrated["reaper_sync_enabled"])

    def test_v0636_migration_keeps_send_fader_flip_opt_in(self) -> None:
        migrated = _migrate({"config_version": 26})
        self.assertEqual(28, migrated["config_version"])
        self.assertFalse(migrated["send_fader_flip_softkey8"])

    def test_mcu_mute_button_can_be_remapped_to_solo(self) -> None:
        mappings = normalise_control_mappings(DEFAULT_CONTROL_MAPPINGS)
        mappings["mcu"]["controls"]["mute"] = "track_solo"
        self.engine.configure(control_mappings=mappings)

        self.engine.handle_gld_message(
            mido.Message("note_on", channel=1, note=0, velocity=0x7F)
        )

        self.assertFalse(self.engine.channels[0].mute)
        self.assertTrue(self.engine.channels[0].solo)
        self.assertEqual([mcu.NOTE_SOLO, mcu.NOTE_SOLO], [msg.note for _bank, msg in self.router.daw_messages])
        self.assertEqual([0x91, 0x00, 0x7F], self.router.gld_messages[0].bytes())

    def test_mcu_softkey_can_send_an_arbitrary_global_note(self) -> None:
        mappings = normalise_control_mappings(DEFAULT_CONTROL_MAPPINGS)
        mappings["mcu"]["softkeys"][0] = "mcu_note:63"
        self.engine.configure(control_mappings=mappings)

        self.engine.manual_softkey(0)

        self.assertEqual(2, len(self.router.daw_messages))
        self.assertEqual([0x63, 0x63], [msg.note for _bank, msg in self.router.daw_messages])
        self.assertEqual([127, 0], [msg.velocity for _bank, msg in self.router.daw_messages])

    def test_intentional_disconnect_reset_neutralises_entire_gld_surface(self) -> None:
        self.engine.channels[0].fader = 101
        self.engine.channels[0].pan = 93
        self.engine.channels[0].name = "Kick"
        self.engine.channels[0].colour = "red"
        self.engine.channels[0].mute = True

        with mock.patch("gld_mcu_bridge.bridge.time.sleep"):
            self.engine.reset_gld_surface_for_disconnect()

        # Reset channels is a soft disconnect: the connection stays alive but
        # both the displayed model and every physical strip become neutral.
        self.assertTrue(self.router.connected)
        self.assertEqual(0, self.engine.channels[0].fader)
        self.assertEqual(64, self.engine.channels[0].pan)
        self.assertEqual("MIDI 01", self.engine.channels[0].name)
        self.assertEqual("white", self.engine.channels[0].colour)
        self.assertFalse(self.engine.channels[0].mute)
        self.assertFalse(self.engine.channels[0].solo)
        self.assertFalse(self.engine.channels[0].select)
        self.assertFalse(self.engine.channels[0].record)

        self.assertEqual(32 * 11, len(self.router.gld_messages))
        self.assertIn(gld.make_midi_strip_fader(0, 0), self.router.gld_messages)
        self.assertIn(gld.make_midi_strip_rotary("pan", 0, 64), self.router.gld_messages)
        for msg in gld.make_midi_strip_key_feedback("mute", 0, False):
            self.assertIn(msg, self.router.gld_messages)
        self.assertEqual(32 * 3, len(self.router.editor_payloads))
        self.assertIn(gld.make_editor_midi_strip_name(0, "MIDI 01"), self.router.editor_payloads)
        self.assertIn(gld.make_editor_midi_strip_colour(0, "white"), self.router.editor_payloads)
        self.assertIn(gld.make_editor_midi_strip_pan(0, 64), self.router.editor_payloads)

    def test_soft_reset_preserves_known_reaper_pan_protection(self) -> None:
        self.engine.reaper_sync_enabled = True
        self.engine._reaper_session_detected = True
        self.engine._reaper_companion_connected = False
        self.engine.reset_channels()
        self.assertTrue(self.engine._reaper_session_detected)

        # A stale helper must block Gain rather than letting stock REAPER see a
        # normal MCU V-Pot, which it would interpret as track Pan.
        self.engine._surface_reset_guard_until = 0.0
        self.engine._gld_gain_input_raw[0] = 64
        self.router.daw_messages.clear()
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 65))
        self.assertEqual([], self.router.daw_messages)

    def test_reset_off_tally_echo_cannot_toggle_daw_back_on(self) -> None:
        self.engine.reset_channels()
        self.router.daw_messages.clear()
        off_state = gld.make_midi_strip_key_feedback("mute", 0, False)[0]
        self.engine.handle_gld_message(off_state)
        self.assertFalse(self.engine.channels[0].mute)
        self.assertEqual([], self.router.daw_messages)

    def test_disconnect_reset_stays_silent_after_connection_is_already_lost(self) -> None:
        self.router.connected = False
        self.engine.channels[0].name = "Old"
        self.engine.channels[0].fader = 99
        self.engine.reset_gld_surface_for_disconnect()
        self.assertEqual([], self.router.gld_messages)
        self.assertEqual([], self.router.editor_payloads)
        self.assertEqual("MIDI 01", self.engine.channels[0].name)
        self.assertEqual(0, self.engine.channels[0].fader)

    def test_linux_startup_entry_uses_minimized_argument(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "autostart" / "gld80-mcu-bridge.desktop"
            with (
                mock.patch.object(startup, "is_windows", return_value=False),
                mock.patch.object(startup, "is_macos", return_value=False),
                mock.patch.object(startup, "is_linux", return_value=True),
                mock.patch.object(startup, "is_startup_supported", return_value=True),
                mock.patch.object(startup, "startup_location", return_value=path),
            ):
                startup.set_startup_enabled(True)
                text = path.read_text(encoding="utf-8")
                self.assertIn("--minimized", text)
                self.assertIn("X-GNOME-Autostart-enabled=true", text)
                startup.set_startup_enabled(False)
                self.assertFalse(path.exists())

    def test_macos_launch_agent_uses_minimized_argument(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "com.tripledots.gld80-mcu-bridge.plist"
            with (
                mock.patch.object(startup, "is_windows", return_value=False),
                mock.patch.object(startup, "is_macos", return_value=True),
                mock.patch.object(startup, "is_linux", return_value=False),
                mock.patch.object(startup, "is_startup_supported", return_value=True),
                mock.patch.object(startup, "startup_location", return_value=path),
            ):
                startup.set_startup_enabled(True)
                with path.open("rb") as handle:
                    payload = plistlib.load(handle)
                self.assertTrue(payload["RunAtLoad"])
                self.assertIn("--minimized", payload["ProgramArguments"])
                startup.set_startup_enabled(False)
                self.assertFalse(path.exists())


    def test_reaper_snapshot_v7_reports_instance_and_mode_ack_sequences(self) -> None:
        text = (
            "GLD80_REAPER_SYNC\t7\t123\t8\t64\tplugin_params\t10\t2\t1\t3\t4"
            "\t9876\t10001\t10002\t55.25:1:token\t19\n"
            "1\tKick\t255\t0\t0\t1\t64\n"
        )
        metadata = parse_snapshot_metadata(text)
        self.assertEqual(7, metadata.version)
        self.assertEqual(9876, metadata.bank_sequence)
        self.assertEqual(10001, metadata.plugin_sequence)
        self.assertEqual(10002, metadata.send_sequence)
        self.assertEqual("55.25:1:token", metadata.instance_id)
        self.assertEqual(19, metadata.snapshot_sequence)

    def test_newer_v7_companion_instance_can_take_over_but_older_cannot_return(self) -> None:
        newer = types.SimpleNamespace(
            version=7,
            instance_id="20.0:1:new",
            snapshot_sequence=1,
            plugin_sequence=0,
            send_sequence=0,
        )
        older = types.SimpleNamespace(
            version=7,
            instance_id="10.0:1:old",
            snapshot_sequence=99,
            plugin_sequence=0,
            send_sequence=0,
        )
        self.engine._reaper_companion_instance_id = "10.0:1:old"
        self.engine._reaper_snapshot_sequence = 50
        self.assertTrue(self.engine._companion_identity_snapshot_is_current(newer))
        self.assertEqual("20.0:1:new", self.engine._reaper_companion_instance_id)
        self.assertFalse(self.engine._companion_identity_snapshot_is_current(older))
        self.assertEqual("20.0:1:new", self.engine._reaper_companion_instance_id)

    def test_send_assignment_waits_for_rotary_preload_without_overwriting_levels(self) -> None:
        self.engine._activate_send_assignment()
        # One standard MCU assignment press/release on the Universal port only.
        self.assertEqual([0, 0], [bank for bank, _msg in self.router.daw_messages])
        self.assertEqual(
            [mcu.NOTE_SEND_ASSIGNMENT, mcu.NOTE_SEND_ASSIGNMENT],
            [msg.note for _bank, msg in self.router.daw_messages],
        )
        gain_messages = [
            msg for msg in self.router.gld_messages
            if msg.type == "control_change" and msg.channel == 2
        ]
        self.assertEqual([], gain_messages)


    def test_offline_banking_leaves_no_stale_reaper_command_file(self) -> None:
        self.engine.reaper_sync_enabled = True
        self.engine._reaper_companion_connected = False
        self.engine._reaper_total_tracks = 100
        with mock.patch("gld_mcu_bridge.bridge.write_bank_offset") as write:
            self.engine._advance_surface_offset("channel_right")
        write.assert_not_called()
        self.assertEqual(1, self.engine._surface_track_offset)
        self.assertIsNone(self.engine._reaper_bank_pending)

    def test_forced_page_repaint_resends_equal_names_and_colours(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "snapshot.tsv"
            self.engine._reaper_sync_path = snapshot
            self.engine.reaper_sync_enabled = True
            self.engine.reaper_sync_names = True
            self.engine.reaper_sync_colours = True
            self.engine.reaper_sync_pan = False
            self.engine.send_names_to_gld = True
            self.engine.send_colours_to_gld = True
            self.engine.channels[0].name = "Track 1"
            self.engine.channels[0].colour = "white"
            self.engine._force_editor_labels_on_next_snapshot = True
            snapshot.write_text(
                "GLD80_REAPER_SYNC\t2\t1\n"
                "1\tTrack 1\t255\t255\t255\t0\t64\n",
                encoding="utf-8",
            )
            self.engine._poll_reaper_companion()
            self.assertIn(("name", 0), self.engine._editor_sync_pending)
            self.assertIn(("colour", 0), self.engine._editor_sync_pending)
            self.assertFalse(self.engine._force_editor_labels_on_next_snapshot)



    def test_new_reaper_companion_instance_resets_send_accumulator_domain(self) -> None:
        self.engine._reaper_send_session = 10
        self.engine._reaper_send_sequence = 25
        self.engine._reaper_send_position[0] = 123
        with mock.patch("gld_mcu_bridge.bridge.time.time_ns", return_value=9_999_000_000):
            self.engine._resync_new_reaper_companion_instance()
        self.assertEqual(9_999, self.engine._reaper_send_session)
        self.assertEqual(9_999, self.engine._reaper_send_sequence)
        self.assertEqual([0] * self.engine.tracks, self.engine._reaper_send_position)

    def test_legacy_companion_cannot_downgrade_an_active_v7_owner(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "snapshot.tsv"
            self.engine._reaper_sync_path = snapshot
            self.engine.reaper_sync_enabled = True
            self.engine.reaper_sync_names = True
            self.engine.reaper_sync_colours = False
            self.engine.reaper_sync_pan = False
            snapshot.write_text(
                "GLD80_REAPER_SYNC\t7\t1\t0\t1\ttracks\t-1\t-1\t0\t0\t0\t0\t0\t0\t1700000000:1:a:b\t1\n"
                "1\tOwner\t255\t255\t255\t0\t64\n",
                encoding="utf-8",
            )
            self.engine._poll_reaper_companion()
            self.assertEqual(7, self.engine._reaper_companion_version)
            self.assertEqual("Owner", self.engine.channels[0].name)

            snapshot.write_text(
                "GLD80_REAPER_SYNC\t6\t2\t0\t1\tplugin_list\t0\t0\t0\t0\t0\t0\n"
                "1\tOLD FX\t255\t0\t0\t1\t64\n",
                encoding="utf-8",
            )
            self.engine._poll_reaper_companion()
            self.assertEqual(7, self.engine._reaper_companion_version)
            self.assertEqual("Owner", self.engine.channels[0].name)
            self.assertEqual("tracks", self.engine._plugin_mode)

    def test_attaching_same_router_twice_does_not_duplicate_receivers(self) -> None:
        self.engine.attach_router(self.router)
        self.assertEqual(1, len(self.router.gld_message._callbacks))
        self.assertEqual(1, len(self.router.daw_message._callbacks))
        self.assertEqual(1, len(self.router.log._callbacks))
        self.assertEqual(1, len(self.router.editor_connection_changed._callbacks))

    def test_raw_gld_packet_is_forwarded_exactly_once(self) -> None:
        self.engine.daw_protocol = "raw"
        message = gld.make_midi_strip_fader(0, 37)
        self.engine.handle_gld_message(message)
        self.assertEqual([(0, message)], self.router.daw_messages)

    def test_raw_daw_packet_is_forwarded_exactly_once(self) -> None:
        self.engine.daw_protocol = "raw"
        message = gld.make_midi_strip_fader(0, 81)
        self.engine.handle_daw_message(0, message)
        self.assertEqual([message], self.router.gld_messages)

    def test_reaper_normal_bank_uses_one_mcu_click_and_one_metadata_command(self) -> None:
        self.engine.reaper_sync_enabled = True
        self.engine._reaper_companion_connected = True
        self.engine._reaper_companion_version = 7
        self.engine._reaper_total_tracks = 100
        with mock.patch(
            "gld_mcu_bridge.bridge.write_bank_offset", return_value=True
        ) as write:
            self.engine.manual_navigation("bank_right")
        self.assertEqual([0, 0], [bank for bank, _msg in self.router.daw_messages])
        self.assertEqual(
            [mcu.NOTE_BANK_RIGHT, mcu.NOTE_BANK_RIGHT],
            [msg.note for _bank, msg in self.router.daw_messages],
        )
        write.assert_called_once()
        self.assertEqual(8, write.call_args.args[0])


    def test_send_mode_never_steals_normal_track_faders(self) -> None:
        self.engine._plugin_mode = "send"
        self.engine.manual_fader(0, 81)
        self.assertEqual(1, len(self.router.daw_messages))
        bank, msg = self.router.daw_messages[0]
        self.assertEqual(0, bank)
        self.assertEqual("pitchwheel", msg.type)
        self.assertFalse(hasattr(self.engine, "send_levels_on_faders"))

    def test_v0628_config_migrates_send_control_back_to_rotaries(self) -> None:
        migrated = _migrate({
            "config_version": 20,
            "send_levels_on_faders": True,
        })
        self.assertEqual(28, migrated["config_version"])
        self.assertNotIn("send_levels_on_faders", migrated)

    def test_snapshot_v10_parses_active_project_identity(self) -> None:
        metadata = parse_snapshot_metadata(
            "GLD80_REAPER_SYNC\t10\t123\t0\t64\ttracks\t-1\t-1\t0\t0\t0\t1\t2\t3\tinst\t9\tproject-token\n"
        )
        self.assertEqual(10, metadata.version)
        self.assertEqual("project-token", metadata.project_id)

    def test_migration_removes_experimental_send_fader_setting(self) -> None:
        migrated = _migrate({
            "config_version": 21,
            "send_levels_on_faders": True,
        })
        self.assertEqual(28, migrated["config_version"])
        self.assertNotIn("send_levels_on_faders", migrated)



    def test_standard_mcu_fader_feedback_is_always_authoritative(self) -> None:
        self.engine.reaper_sync_enabled = True
        self.engine._reaper_companion_connected = True
        self.engine._reaper_companion_version = 10
        self.engine.channels[0].fader = 98
        self.engine.handle_daw_message(0, mcu.make_fader(0, 35))
        self.assertEqual(35, self.engine.channels[0].fader)
        self.assertIn(gld.make_midi_strip_fader(0, 35), self.router.gld_messages)

    def test_metadata_snapshot_never_moves_faders_or_rotaries(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "snapshot.tsv"
            self.engine._reaper_sync_path = snapshot
            self.engine.reaper_sync_enabled = True
            self.engine.channels[0].fader = 55
            self.engine.channels[0].pan = 64
            snapshot.write_text(
                "GLD80_REAPER_SYNC\t10\t1\t0\t1\tplugin_params\t0\t0\t0\t0\t0\t0\t0\t0\t1000:1:test\t1\tproject-A\n"
                "1\tTrack 1\t10\t20\t30\t1\t127\t0\n",
                encoding="utf-8",
            )
            self.engine._poll_reaper_companion()
        self.assertEqual(55, self.engine.channels[0].fader)
        self.assertEqual(64, self.engine.channels[0].pan)
        self.assertEqual("tracks", self.engine._plugin_mode)
        self.assertFalse(any(msg.type == "pitchwheel" for msg in self.router.gld_messages))

    def test_standard_mcu_send_turn_is_relative_and_immediate(self) -> None:
        self._use_legacy_gain_send()
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 64))
        self.router.daw_messages.clear()
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 66))
        self.assertEqual(1, len(self.router.daw_messages))
        bank, msg = self.router.daw_messages[0]
        self.assertEqual(0, bank)
        self.assertEqual("control_change", msg.type)
        self.assertEqual(mcu.CC_VPOT, msg.control)
        self.assertEqual(2, msg.value)

    def test_standard_mcu_send_ring_feedback_is_not_mapped_to_gain(self) -> None:
        self.engine._plugin_mode = "send"
        self.engine.handle_daw_message(
            0, mido.Message("control_change", control=mcu.CC_VPOT_LED, value=0x07)
        )
        self.assertEqual([], self.router.gld_messages)

    def test_standard_mcu_plugin_turn_uses_vpot_not_lua(self) -> None:
        self.engine._gld_pan_input_raw[0] = 64
        self.engine.manual_plugin_assignment()
        self.engine._send_plugin_vpot_push(0)
        self.router.daw_messages.clear()
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("pan", 0, 67))
        self.assertEqual(1, len(self.router.daw_messages))
        _, msg = self.router.daw_messages[0]
        self.assertEqual(mcu.CC_VPOT, msg.control)
        self.assertEqual(3, msg.value)

    def test_standard_mcu_plugin_feedback_updates_pan_rotary(self) -> None:
        self.engine._plugin_mode = "plugin_params"
        self.engine.handle_daw_message(
            0, mido.Message("control_change", control=mcu.CC_VPOT_LED, value=0x0B)
        )
        self.assertIn(gld.make_editor_midi_strip_pan(0, 127), self.router.editor_payloads)

    def test_optional_companion_owns_exact_pan_send_fx_fader_flip_and_labels(self) -> None:
        script = (
            Path(__file__).parents[1]
            / "integrations"
            / "reaper"
            / "GLD80 Bridge - Sync REAPER track names and colours.lua"
        ).read_text(encoding="utf-8")
        self.assertIn("-- @version 1.23", script)
        self.assertIn("REAPER Send, exact Pan, FX and labels", script)
        self.assertIn("TrackFX_SetParamNormalized", script)
        self.assertIn('SetMediaTrackInfo_Value(track, "D_PAN"', script)
        self.assertIn('SetMediaTrackInfo_Value(track, "D_VOL"', script)
        self.assertIn("GetTrackSendInfo_Value", script)
        self.assertIn("SetTrackSendInfo_Value", script)
        self.assertIn("SEND_DELTA_MAGIC", script)
        self.assertIn('GetSetMediaTrackInfo_String(track, "P_NAME"', script)
        self.assertIn('string.format("Ch%03d"', script)
        self.assertIn('concise_fx_name', script)
        self.assertIn('name:gsub("^[Vv][Ss][Tt]3?[Ii]?%s*:%s*", "")', script)


    def test_standard_mcu_send_dark_ring_is_ignored_by_absolute_gain(self) -> None:
        self.engine._plugin_mode = "send"
        self.engine._gld_gain_last_sent[0] = 64
        self.engine.handle_daw_message(
            0, mido.Message("control_change", control=mcu.CC_VPOT_LED, value=0x00)
        )
        self.assertEqual([], self.router.gld_messages)

    def test_standard_mcu_fader_feedback_has_no_persistent_deadband(self) -> None:
        self.engine._last_gld_fader[0] = 64
        self.engine.handle_daw_message(0, mcu.make_fader(0, 65))
        self.assertEqual(65, self.engine.channels[0].fader)
        self.assertIn(gld.make_midi_strip_fader(0, 65), self.router.gld_messages)

    def test_reaper_sync_module_exposes_only_opt_in_pan_fx_writers(self) -> None:
        import gld_mcu_bridge.reaper_sync as reaper_sync

        self.assertTrue(hasattr(reaper_sync, "write_pan_command"))
        self.assertTrue(hasattr(reaper_sync, "write_plugin_parameter_command"))
        self.assertTrue(hasattr(reaper_sync, "write_plugin_action"))
        self.assertFalse(hasattr(reaper_sync, "write_send_level_command"))
        self.assertTrue(hasattr(reaper_sync, "write_send_delta_command"))
        self.assertTrue(hasattr(reaper_sync, "write_send_fader_command"))
        self.assertTrue(hasattr(reaper_sync, "write_track_fader_command"))
        self.assertTrue(hasattr(reaper_sync, "write_bank_offset"))


    def test_v15_snapshot_parses_exact_send_value(self) -> None:
        text = (
            "GLD80_REAPER_SYNC\t15\t1\t0\t1\ttracks\t-1\t-1\t0\t0\t0"
            "\t0\t0\t0\t2000000000:1:test\t1\tproject-A\n"
            "1\tTrack 1\t255\t255\t255\t0\t64\t93\t1\n"
        )
        rows = parse_snapshot(text)
        self.assertEqual(1, len(rows))
        self.assertEqual(93, rows[0].fader)
        self.assertEqual(1, rows[0].send)

    def test_reaper_v16_gain_send_uses_relative_steps_at_minus_inf(self) -> None:
        self._use_legacy_gain_send()
        self.engine.reaper_sync_enabled = True
        self.engine._reaper_session_detected = True
        self.engine._reaper_companion_connected = True
        self.engine._reaper_companion_version = 16
        self.engine._plugin_mode = "tracks"
        self.engine._reaper_send_values[0] = 0
        self.engine._gld_gain_input_raw[0] = 64

        with mock.patch(
            "gld_mcu_bridge.bridge.write_send_fader_command", return_value=True
        ) as write, mock.patch(
            "gld_mcu_bridge.bridge.write_send_delta_command"
        ) as delta_write:
            self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 65))
            self.assertEqual(1, self.engine._reaper_send_values[0])
            self.assertEqual((0, 1), write.call_args.args[:2])

            self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 64))
            self.assertEqual(0, self.engine._reaper_send_values[0])
            self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 65))
            self.assertEqual(1, self.engine._reaper_send_values[0])
            delta_write.assert_not_called()

    def test_reaper_absolute_gain_can_reverse_and_uses_raw_target(self) -> None:
        self._use_legacy_gain_send()
        self.engine.reaper_sync_enabled = True
        self.engine._reaper_session_detected = True
        self.engine._reaper_companion_connected = True
        self.engine._reaper_companion_version = 15
        self.engine._plugin_mode = "tracks"
        self.engine._reaper_send_values[0] = 64
        self.engine._gld_gain_input_raw[0] = 64

        with mock.patch(
            "gld_mcu_bridge.bridge.write_send_fader_command", return_value=True
        ) as write:
            self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 62))
            self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 64))

        self.assertEqual([62, 64], [call.args[1] for call in write.call_args_list])
        self.assertEqual(64, self.engine._reaper_send_values[0])

    def test_reaper_v16_send_snapshot_updates_cache_without_writing_gain(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "snapshot.tsv"
            self.engine._reaper_sync_path = snapshot
            self.engine.reaper_sync_enabled = True
            snapshot.write_text(
                "GLD80_REAPER_SYNC\t16\t1\t0\t1\ttracks\t-1\t-1\t0\t0\t0\t0\t0\t0\t2000000000:1:test\t1\tproject-A\n"
                "1\tTrack 1\t255\t255\t255\t0\t64\t93\t72\n",
                encoding="utf-8",
            )
            self.engine._poll_reaper_companion()

        self.assertNotIn(gld.make_midi_strip_rotary("gain", 0, 72), self.router.gld_messages)
        self.assertEqual(72, self.engine._reaper_send_values[0])

    def test_reaper_relative_send_rolls_back_without_gain_feedback_when_write_fails(self) -> None:
        self._use_legacy_gain_send()
        self.engine.reaper_sync_enabled = True
        self.engine._reaper_session_detected = True
        self.engine._reaper_companion_connected = True
        self.engine._reaper_companion_version = 16
        self.engine._plugin_mode = "tracks"
        self.engine._reaper_send_values[0] = 64
        self.engine._gld_gain_input_raw[0] = 64

        with mock.patch(
            "gld_mcu_bridge.bridge.write_send_fader_command", return_value=False
        ):
            self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 66))

        self.assertEqual(64, self.engine._reaper_send_values[0])
        self.assertEqual([], self.router.gld_messages)

    def test_reaper_relative_gain_recentres_at_endpoint(self) -> None:
        self._use_legacy_gain_send()
        self.engine.reaper_sync_enabled = True
        self.engine._reaper_session_detected = True
        self.engine._reaper_companion_connected = True
        self.engine._reaper_companion_version = 16
        self.engine._plugin_mode = "tracks"
        self.engine._reaper_send_values[0] = 20
        self.engine._gld_gain_input_raw[0] = 126

        with mock.patch(
            "gld_mcu_bridge.bridge.write_send_fader_command", return_value=True
        ) as write:
            self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 127))

        self.assertEqual(21, self.engine._reaper_send_values[0])
        self.assertEqual(21, write.call_args.args[1])
        self.assertIn(gld.make_midi_strip_rotary("gain", 0, 64), self.router.gld_messages)

    def test_generic_send_flip_gain_rotary_uses_flipped_vpot_not_fader(self) -> None:
        self.engine._send_fader_flip_active = True
        self.engine._plugin_mode = "send_fader"
        self.engine._gain_send_fader_active = True
        self.engine._gld_gain_input_raw[0] = 64
        self.engine._send_select_last_at = 0.0
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 66))
        self.assertEqual(
            [mcu.NOTE_CURSOR_DOWN, mcu.NOTE_CURSOR_DOWN],
            [message.note for _bank, message in self.router.daw_messages],
        )
        self.assertFalse(any(
            message.type in {"control_change", "pitchwheel"}
            for _bank, message in self.router.daw_messages
        ))

    def test_reaper_send_flip_seeds_hidden_track_volume_before_snapshot(self) -> None:
        self.engine.reaper_sync_enabled = True
        self.engine._reaper_session_detected = True
        self.engine._reaper_companion_connected = True
        self.engine._reaper_companion_version = 17
        self.engine.channels[0].fader = 79

        with mock.patch.object(
            self.engine, "_write_reaper_plugin_action", return_value=True
        ):
            self.assertTrue(self.engine._set_send_fader_flip(True))

        self.assertEqual(79, self.engine._reaper_track_fader_values[0])

    def test_reaper_send_flip_gain_rotary_controls_relative_track_volume_not_send(self) -> None:
        self.engine.reaper_sync_enabled = True
        self.engine._reaper_session_detected = True
        self.engine._reaper_companion_connected = True
        self.engine._reaper_companion_version = 17
        self.engine._send_fader_flip_active = True
        self.engine._gain_send_fader_active = True
        self.engine._plugin_mode = "send_fader"
        self.engine._gld_gain_input_raw[0] = 64
        self.engine._send_select_last_at = 0.0
        with mock.patch.object(
            self.engine, "_write_reaper_plugin_action", return_value=True
        ) as write, mock.patch(
            "gld_mcu_bridge.bridge.write_send_fader_command"
        ) as send_write, mock.patch(
            "gld_mcu_bridge.bridge.write_track_fader_command"
        ) as volume_write:
            self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 66))
        write.assert_called_once_with("send_next", 0)
        send_write.assert_not_called()
        volume_write.assert_not_called()
        self.assertEqual([], self.router.daw_messages)

    def test_send_fader_snapshot_seeds_hidden_track_volume_without_gain_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "snapshot.tsv"
            self.engine._reaper_sync_path = snapshot
            self.engine.reaper_sync_enabled = True
            self.engine._send_fader_flip_active = True
            self.engine._plugin_mode = "send_fader"
            snapshot.write_text(
                "GLD80_REAPER_SYNC\t16\t1\t0\t1\tsend_fader\t-1\t-1\t0\t0\t0\t0\t0\t0\t2000000000:1:test\t1\tproject-A\n"
                "1\tTrack 1\t255\t255\t255\t0\t82\t44\t82\n",
                encoding="utf-8",
            )
            self.engine._poll_reaper_companion()

        self.assertEqual(44, self.engine._reaper_track_fader_values[0])
        self.assertEqual(82, self.engine._reaper_send_values[0])
        self.assertNotIn(gld.make_midi_strip_rotary("gain", 0, 44), self.router.gld_messages)

    def test_reaper_late_pre_snapshot_clear_is_held_after_settle_timer(self) -> None:
        self.engine.reaper_sync_enabled = True
        self.engine._reaper_companion_connected = True
        self.engine._reaper_session_detected = True
        self.engine._reaper_companion_version = 15
        self.engine._plugin_mode = "tracks"
        self.engine.channels[0].fader = 88
        self.engine._last_gld_fader[0] = 88

        self.engine._begin_surface_page_transition()
        self.engine._surface_page_feedback[("fader", 0)] = mcu.value7_to_pitch14(0)
        self.engine._surface_page_expected_faders.discard(0)
        self.engine._flush_surface_page_feedback()
        self.assertTrue(self.engine._surface_page_waiting_for_reaper_exact)

        before = list(self.router.gld_messages)
        self.engine.handle_daw_message(0, mcu.make_fader(0, 0))
        self.assertEqual(88, self.engine.channels[0].fader)
        self.assertEqual(before, self.router.gld_messages)

    def test_reaper_companion_uses_documented_gld_fader_taper_for_send(self) -> None:
        script = (
            Path(__file__).parents[1]
            / "integrations"
            / "reaper"
            / "GLD80 Bridge - Sync REAPER track names and colours.lua"
        ).read_text(encoding="utf-8")
        self.assertIn("0 = -inf, 107 = 0 dB and 127 = +10 dB", script)
        self.assertIn("value * 64.0 / 127.0 - 54.0", script)
        self.assertIn("(db + 54.0) * 127.0 / 64.0", script)

    def test_reaper_send_mailbox_v2_contains_restart_safe_delta(self) -> None:
        import gld_mcu_bridge.reaper_sync as reaper_sync

        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            reaper_sync.tempfile, "gettempdir", return_value=folder
        ):
            self.assertTrue(
                reaper_sync.write_send_delta_command(
                    track=2, cumulative_steps=17, delta_steps=-3, sequence=99, session=42
                )
            )
            payload = reaper_sync.send_delta_command_path(2).read_text(encoding="utf-8")

        self.assertEqual(
            "GLD80_REAPER_SEND_DELTA\t2\t42\t99\t17\t-3\n", payload
        )

    def test_reaper_send_mailbox_is_persistent_and_recovers_from_low_level(self) -> None:
        script = (
            Path(__file__).parents[1]
            / "integrations"
            / "reaper"
            / "GLD80 Bridge - Sync REAPER track names and colours.lua"
        ).read_text(encoding="utf-8")
        start = script.index("local function apply_send_delta_commands()")
        end = script.index("local function apply_send_fader_commands()", start)
        send_block = script[start:end]
        self.assertNotIn("os.remove(path)", send_block)
        self.assertIn("current_db <= -90.0 and delta > 0", send_block)

    def test_reaper_send_fader_mailbox_contains_absolute_target(self) -> None:
        import gld_mcu_bridge.reaper_sync as reaper_sync

        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            reaper_sync.tempfile, "gettempdir", return_value=folder
        ):
            self.assertTrue(reaper_sync.write_send_fader_command(3, 91, 1234))
            payload = reaper_sync.send_fader_command_path(3).read_text(encoding="utf-8")
        self.assertEqual("GLD80_REAPER_SEND_FADER\t1\t1234\t91\n", payload)

    def test_reaper_track_fader_mailbox_contains_absolute_target(self) -> None:
        import gld_mcu_bridge.reaper_sync as reaper_sync

        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            reaper_sync.tempfile, "gettempdir", return_value=folder
        ):
            self.assertTrue(reaper_sync.write_track_fader_command(5, 77, 4321))
            payload = reaper_sync.track_fader_command_path(5).read_text(encoding="utf-8")
        self.assertEqual("GLD80_REAPER_TRACK_FADER\t1\t4321\t77\n", payload)

    def test_softkey8_optional_generic_mcu_send_fader_flip(self) -> None:
        self.engine.configure(send_fader_flip_softkey8=True)
        self.engine.manual_softkey(7)
        notes = [msg.note for _bank, msg in self.router.daw_messages if msg.type == "note_on"]
        self.assertIn(mcu.NOTE_SEND_ASSIGNMENT, notes)
        self.assertIn(mcu.NOTE_FLIP, notes)
        self.assertTrue(self.engine._send_fader_flip_active)
        self.assertEqual("send_fader", self.engine._plugin_mode)

    def test_reaper_softkey8_requests_companion_send_fader_mode(self) -> None:
        self.engine.configure(send_fader_flip_softkey8=True)
        self.engine.reaper_sync_enabled = True
        self.engine._reaper_session_detected = True
        self.engine._reaper_companion_connected = True
        self.engine._reaper_companion_version = 17
        with mock.patch("gld_mcu_bridge.bridge.write_plugin_action", return_value=True) as write:
            self.engine.manual_softkey(7)
        self.assertTrue(self.engine._send_fader_flip_active)
        self.assertEqual("send_fader", self.engine._plugin_mode)
        self.assertEqual("send_flip_on", write.call_args.args[0])

    def test_reaper_send_fader_move_never_sends_stock_mcu_track_fader(self) -> None:
        self.engine.reaper_sync_enabled = True
        self.engine._reaper_session_detected = True
        self.engine._reaper_companion_connected = True
        self.engine._reaper_companion_version = 17
        self.engine._send_fader_flip_active = True
        self.engine._plugin_mode = "send_fader"
        self.engine._transport_reset_at = time.monotonic() - 5.0
        with mock.patch("gld_mcu_bridge.bridge.write_send_fader_command", return_value=True) as write:
            self.engine.handle_gld_message(gld.make_midi_strip_fader(0, 73))
        write.assert_called_once()
        self.assertEqual((0, 73), write.call_args.args[:2])
        self.assertEqual([], self.router.daw_messages)

    def test_reaper_send_fader_snapshot_drives_motor_and_ignores_stock_fader(self) -> None:
        self.engine.reaper_sync_enabled = True
        self.engine._reaper_companion_connected = True
        self.engine._reaper_companion_version = 17
        self.engine._send_fader_flip_active = True
        self.engine._plugin_mode = "send_fader"
        self.engine.handle_daw_message(0, mcu.make_fader(0, 99))
        self.assertNotEqual(99, self.engine.channels[0].fader)
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "snapshot.tsv"
            self.engine._reaper_sync_path = snapshot
            snapshot.write_text(
                "GLD80_REAPER_SYNC\t13\t1\t0\t1\tsend_fader\t-1\t-1\t0\t0\t0\t0\t0\t0\t2000000000:1:test\t1\tproject-A\n"
                "1\tTrack 1\t255\t255\t255\t0\t82\t44\n",
                encoding="utf-8",
            )
            self.engine._poll_reaper_companion()
        self.assertEqual(82, self.engine.channels[0].fader)
        self.assertIn(gld.make_midi_strip_fader(0, 82), self.router.gld_messages)

    def test_v13_bank_snapshot_repairs_exact_track_fader_once(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "snapshot.tsv"
            self.engine._reaper_sync_path = snapshot
            self.engine.reaper_sync_enabled = True
            self.engine._force_reaper_faders_on_next_snapshot = True
            snapshot.write_text(
                "GLD80_REAPER_SYNC\t13\t1\t0\t1\ttracks\t-1\t-1\t0\t0\t0\t0\t0\t0\t2000000000:1:test\t1\tproject-A\n"
                "1\tTrack 1\t255\t255\t255\t0\t64\t93\n",
                encoding="utf-8",
            )
            self.engine._poll_reaper_companion()
        self.assertEqual(93, self.engine.channels[0].fader)
        self.assertIn(gld.make_midi_strip_fader(0, 93), self.router.gld_messages)
        self.assertFalse(self.engine._force_reaper_faders_on_next_snapshot)

    def test_reaper_exact_bank_snapshot_avoids_all_down_motor_sweep(self) -> None:
        self.engine.reaper_sync_enabled = True
        self.engine._reaper_companion_connected = True
        self.engine._reaper_session_detected = True
        self.engine._reaper_companion_version = 13
        self.engine._plugin_mode = "tracks"
        self.engine.channels[0].fader = 88
        self.engine._last_gld_fader[0] = 88

        self.engine._begin_surface_page_transition()
        self.assertTrue(self.engine._surface_page_waiting_for_reaper_exact)
        self.engine._surface_page_feedback[("fader", 0)] = mcu.value7_to_pitch14(0)
        self.engine._surface_page_expected_faders.discard(0)
        self.engine._flush_surface_page_feedback()

        # The stock MCU clear sweep is held; the physical fader stays where it
        # is until the exact destination snapshot can move it once.
        self.assertEqual(88, self.engine.channels[0].fader)
        self.assertNotIn(gld.make_midi_strip_fader(0, 0), self.router.gld_messages)

        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "snapshot.tsv"
            self.engine._reaper_sync_path = snapshot
            snapshot.write_text(
                "GLD80_REAPER_SYNC\t13\t1\t0\t1\ttracks\t-1\t-1\t0\t0\t0\t0\t0\t0\t2000000000:1:test\t1\tproject-A\n"
                "1\tTrack 1\t255\t255\t255\t0\t64\t73\n",
                encoding="utf-8",
            )
            self.engine._poll_reaper_companion()

        self.assertEqual(73, self.engine.channels[0].fader)
        self.assertIn(gld.make_midi_strip_fader(0, 73), self.router.gld_messages)
        before = list(self.router.gld_messages)
        self.engine.handle_daw_message(0, mcu.make_fader(0, 0))
        self.assertEqual(73, self.engine.channels[0].fader)
        self.assertEqual(before, self.router.gld_messages)

        # A different non-zero packet is not treated as a clear sweep; genuine
        # automation immediately after banking must still reach the motor.
        self.engine.handle_daw_message(0, mcu.make_fader(0, 80))
        self.assertEqual(80, self.engine.channels[0].fader)
        self.assertIn(gld.make_midi_strip_fader(0, 80), self.router.gld_messages)

    def test_bank_refresh_clears_missing_fader_tally_instead_of_leaving_stale_motor(self) -> None:
        self.engine.channels[5].fader = 88
        self.engine._last_gld_fader[5] = 88
        self.engine._begin_surface_page_transition()
        self.engine._surface_page_feedback[("fader", 0)] = mcu.value7_to_pitch14(33)
        self.engine._surface_page_expected_faders.discard(0)
        self.engine._flush_surface_page_feedback()
        self.assertEqual(0, self.engine.channels[5].fader)
        self.assertIn(gld.make_midi_strip_fader(5, 0), self.router.gld_messages)

    def test_bank_refresh_clears_missing_switch_tallies_from_previous_page(self) -> None:
        self.engine.channels[5].mute = True
        self.engine.channels[5].solo = True
        self.engine.channels[5].select = True
        self.engine._begin_surface_page_transition()
        self.engine._surface_page_feedback[("fader", 0)] = mcu.value7_to_pitch14(33)
        self.engine._surface_page_expected_faders.discard(0)
        self.engine._flush_surface_page_feedback()

        self.assertFalse(self.engine.channels[5].mute)
        self.assertFalse(self.engine.channels[5].solo)
        self.assertFalse(self.engine.channels[5].select)
        self.assertIn(gld.make_midi_strip_key("mute", 5, False), self.router.gld_messages)
        self.assertIn(gld.make_midi_strip_key("pafl", 5, False), self.router.gld_messages)
        self.assertIn(gld.make_midi_strip_key("mix", 5, False), self.router.gld_messages)

    def test_delayed_fader_repaint_is_generation_guarded(self) -> None:
        self.engine.channels[0].fader = 72
        generation = self.engine._surface_repaint_generation
        self.engine._surface_repaint_generation += 1
        self.engine._repeat_surface_fader_repaint(generation)
        self.assertEqual([], self.router.gld_messages)

        current = self.engine._surface_repaint_generation
        self.engine._repeat_surface_fader_repaint(current)
        self.assertIn(gld.make_midi_strip_fader(0, 72), self.router.gld_messages)

    def test_v13_snapshot_neutralises_omitted_surface_slots(self) -> None:
        self.engine.channels[5].name = "OLD"
        self.engine.channels[5].colour = "red"
        self.engine.channels[5].fader = 88
        self.engine.channels[5].mute = True
        self.engine.channels[5].solo = True
        self.engine.channels[5].select = True
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "snapshot.tsv"
            self.engine._reaper_sync_path = snapshot
            self.engine.reaper_sync_enabled = True
            self.engine.send_names_to_gld = True
            self.engine.send_colours_to_gld = True
            snapshot.write_text(
                "GLD80_REAPER_SYNC\t13\t1\t0\t1\ttracks\t-1\t-1\t0\t0\t0\t0\t0\t0\t2000000000:1:test\t1\tproject-A\n"
                "1\tTrack 1\t255\t255\t255\t0\t64\t93\n",
                encoding="utf-8",
            )
            self.engine._poll_reaper_companion()

        state = self.engine.channels[5]
        self.assertEqual("MIDI 06", state.name)
        self.assertEqual("white", state.colour)
        self.assertEqual(0, state.fader)
        self.assertFalse(state.mute)
        self.assertFalse(state.solo)
        self.assertFalse(state.select)
        self.assertIn(gld.make_midi_strip_fader(5, 0), self.router.gld_messages)

    def test_exact_reaper_pan_snapshot_updates_gld_without_touching_fader(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "snapshot.tsv"
            self.engine._reaper_sync_path = snapshot
            self.engine.reaper_sync_enabled = True
            self.engine.reaper_sync_pan = True
            self.engine.reaper_sync_plugins = True
            self.engine.reaper_sync_names = False
            self.engine.reaper_sync_colours = False
            snapshot.write_text(
                "GLD80_REAPER_SYNC\t11\t1\t0\t1\ttracks\t-1\t-1\t0\t0\t0\t0\t0\t0\t2000000000:1:test\t1\tproject-A\n"
                "1\tTrack 1\t255\t255\t255\t0\t101\n",
                encoding="utf-8",
            )
            self.engine._poll_reaper_companion()
        self.assertEqual(101, self.engine.channels[0].pan)
        self.assertIn(gld.make_editor_midi_strip_pan(0, 101), self.router.editor_payloads)
        self.assertFalse(any(msg.type == "pitchwheel" for msg in self.router.gld_messages))

    def test_exact_reaper_pan_turn_uses_lua_instead_of_duplicate_mcu_vpot(self) -> None:
        self.engine.reaper_sync_enabled = True
        self.engine.reaper_sync_pan = True
        self.engine._reaper_companion_connected = True
        self.engine._reaper_companion_version = 11
        self.engine._plugin_mode = "tracks"
        self.engine.channels[0].pan = 64
        self.engine._gld_pan_input_raw[0] = 64
        with mock.patch("gld_mcu_bridge.bridge.write_pan_command", return_value=True) as write:
            self.engine.handle_gld_message(gld.make_midi_strip_rotary("pan", 0, 66))
        write.assert_called_once()
        self.assertEqual((0, 66), write.call_args.args[:2])
        self.assertEqual([], self.router.daw_messages)

    def test_reaper_fx_parameter_turn_uses_lua_and_not_mcu_vpot(self) -> None:
        self.engine.reaper_sync_enabled = True
        self.engine.reaper_sync_plugins = True
        self.engine._reaper_companion_connected = True
        self.engine._reaper_companion_version = 11
        self.engine._plugin_mode = "plugin_params"
        self.engine._reaper_plugin_values[0] = 50
        self.engine._gld_pan_input_raw[0] = 64
        with mock.patch(
            "gld_mcu_bridge.bridge.write_plugin_parameter_command", return_value=True
        ) as write:
            self.engine.handle_gld_message(gld.make_midi_strip_rotary("pan", 0, 67))
        write.assert_called_once()
        self.assertEqual((0, 53), write.call_args.args[:2])
        self.assertEqual([], self.router.daw_messages)

    def test_reaper_snapshot_replace_gap_keeps_connected_status_stable(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "snapshot.tsv"
            self.engine._reaper_sync_path = snapshot
            self.engine.reaper_sync_enabled = True
            snapshot.write_text(
                "GLD80_REAPER_SYNC\t11\t1\t0\t1\ttracks\t-1\t-1\t0\t0\t0\t0\t0\t0\t2000000000:1:test\t1\tproject-A\n"
                "1\tTrack 1\t255\t255\t255\t0\t64\n",
                encoding="utf-8",
            )
            self.engine._poll_reaper_companion()
            before = self.engine._reaper_sync_last_status
            snapshot.unlink()
            self.engine._poll_reaper_companion()
        self.assertTrue(self.engine._reaper_companion_connected)
        self.assertEqual(before, self.engine._reaper_sync_last_status)

    def test_send_turns_emit_only_relative_direction_after_assignment(self) -> None:
        self._use_legacy_gain_send()
        self.engine._gld_gain_input_raw[0] = 64
        self.engine._plugin_mode = "send"
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 67))
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 69))
        values = [
            msg.value for _bank, msg in self.router.daw_messages
            if msg.type == "control_change" and msg.control == mcu.CC_VPOT
        ]
        self.assertEqual([3, 2], values)
        self.assertTrue(all(value < 0x40 for value in values))

    def test_send_normal_turn_is_not_recentred_or_reversed(self) -> None:
        self._use_legacy_gain_send()
        self.engine._gld_gain_input_raw[0] = 64
        self.engine._plugin_mode = "send"
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 67))
        values = [
            msg.value for _bank, msg in self.router.daw_messages
            if msg.type == "control_change" and msg.control == mcu.CC_VPOT
        ]
        self.assertEqual([3], values)
        self.assertNotIn(gld.make_midi_strip_rotary("gain", 0, 64), self.router.gld_messages)

    def test_send_endpoint_is_rebased_and_centre_echo_is_consumed(self) -> None:
        self._use_legacy_gain_send()
        self.engine._gld_gain_input_raw[0] = 126
        self.engine._plugin_mode = "send"
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 127))
        self.assertIn(gld.make_midi_strip_rotary("gain", 0, 64), self.router.gld_messages)
        before = list(self.router.daw_messages)
        self.engine.handle_gld_message(gld.make_midi_strip_rotary("gain", 0, 64))
        self.assertEqual(before, self.router.daw_messages)
        self.assertEqual(64, self.engine._gld_gain_input_raw[0])

    def test_midi_router_invalidates_late_callbacks_before_port_close(self) -> None:
        router = MidiRouter()
        received = []
        router.daw_message.connect(lambda bank, msg: received.append((bank, msg)))
        generation = router._generation
        callback = router._make_daw_callback(0, generation)
        message = mido.Message("note_on", note=1, velocity=127)
        callback(message)
        self.assertEqual(1, len(received))
        router._detach_connections()
        callback(message)
        self.assertEqual(1, len(received))
        router._finish_close()

    def test_midi_router_closes_multiple_hanging_ports_under_one_deadline(self) -> None:
        class HangingPort:
            def close(self) -> None:
                threading.Event().wait(1.0)

        router = MidiRouter()
        started = time.monotonic()
        router._close_ports_bounded(
            [(HangingPort(), "one"), (HangingPort(), "two")], timeout=0.05
        )
        self.assertLess(time.monotonic() - started, 0.25)

    def test_midi_router_port_close_is_bounded(self) -> None:
        class HangingPort:
            def close(self) -> None:
                threading.Event().wait(1.0)

        router = MidiRouter()
        started = time.monotonic()
        router._close_port_bounded(HangingPort(), "test", timeout=0.05)
        self.assertLess(time.monotonic() - started, 0.25)



    def test_reaper_v16_external_send_change_updates_cache_without_gain_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "snapshot.tsv"
            self.engine._reaper_sync_path = snapshot
            self.engine.reaper_sync_enabled = True
            header = (
                "GLD80_REAPER_SYNC\t16\t1\t0\t1\ttracks\t-1\t-1\t0\t0\t0"
                "\t0\t0\t0\t2000000000:1:test\t{}\tproject-A\n"
            )
            snapshot.write_text(
                header.format(1) + "1\tTrack 1\t255\t255\t255\t0\t64\t93\t72\n",
                encoding="utf-8",
            )
            self.engine._poll_reaper_companion()
            self.router.gld_messages.clear()
            snapshot.write_text(
                header.format(2) + "1\tTrack 1\t255\t255\t255\t0\t64\t93\t90\n",
                encoding="utf-8",
            )
            self.engine._poll_reaper_companion()

        self.assertEqual(90, self.engine._reaper_send_values[0])
        self.assertNotIn(gld.make_midi_strip_rotary("gain", 0, 90), self.router.gld_messages)

    def test_v14_snapshot_keeps_legacy_nonfeedback_gain_path(self) -> None:
        self.engine.reaper_sync_enabled = True
        self.engine._reaper_companion_connected = True
        self.engine._reaper_companion_version = 14
        self.engine._plugin_mode = "tracks"
        self.engine._reaper_send_values[0] = 72
        self.engine._send_send_feedback_to_mapped_rotaries(0, 72)
        self.assertEqual([], self.router.gld_messages)

    def test_reaper_companion_rounds_fader_taper_for_stable_absolute_feedback(self) -> None:
        script = (
            Path(__file__).parents[1]
            / "integrations"
            / "reaper"
            / "GLD80 Bridge - Sync REAPER track names and colours.lua"
        ).read_text(encoding="utf-8")
        self.assertIn("* 127.0 / 64.0 + 0.5", script)



if __name__ == "__main__":
    unittest.main()

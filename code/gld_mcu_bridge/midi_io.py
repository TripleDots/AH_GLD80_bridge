from __future__ import annotations

import socket
import sys
import threading
import time
from typing import Callable

import mido
from PySide6.QtCore import QObject, Signal


def _hex_bytes(payload: bytes | bytearray) -> str:
    return " ".join(f"{byte:02X}" for byte in bytes(payload))


def _midi_debug(msg) -> str:
    try:
        data = bytes(msg.bytes())
    except Exception:
        return str(msg)
    return f"{_hex_bytes(data)} | {msg}"


class MidiStreamDecoder:
    """Incremental MIDI byte-stream decoder with running-status support."""

    _SYSTEM_DATA_LENGTH = {0xF1: 1, 0xF2: 2, 0xF3: 1, 0xF6: 0}

    def __init__(self) -> None:
        self.running_status: int | None = None
        self.current_status: int | None = None
        self.data: list[int] = []
        self.needed = 0
        self.sysex: list[int] | None = None

    @staticmethod
    def _channel_data_length(status: int) -> int:
        kind = status & 0xF0
        return 1 if kind in (0xC0, 0xD0) else 2

    @staticmethod
    def _message(raw: list[int]) -> mido.Message | None:
        try:
            return mido.Message.from_bytes(raw)
        except (ValueError, TypeError):
            return None

    def feed(self, payload: bytes | bytearray) -> list[mido.Message]:
        output: list[mido.Message] = []
        for byte in payload:
            if self.sysex is not None:
                if byte >= 0xF8:
                    msg = self._message([byte])
                    if msg is not None:
                        output.append(msg)
                    continue
                self.sysex.append(byte)
                if byte == 0xF7:
                    msg = self._message(self.sysex)
                    if msg is not None:
                        output.append(msg)
                    self.sysex = None
                continue

            if byte >= 0xF8:
                msg = self._message([byte])
                if msg is not None:
                    output.append(msg)
                continue

            if byte & 0x80:
                self.data = []
                if byte == 0xF0:
                    self.sysex = [0xF0]
                    self.current_status = None
                    self.running_status = None
                    continue
                if 0x80 <= byte <= 0xEF:
                    self.current_status = byte
                    self.running_status = byte
                    self.needed = self._channel_data_length(byte)
                else:
                    self.current_status = byte
                    self.running_status = None
                    self.needed = self._SYSTEM_DATA_LENGTH.get(byte, 0)
                if self.needed == 0:
                    msg = self._message([byte])
                    if msg is not None:
                        output.append(msg)
                    self.current_status = None
                continue

            status = self.current_status if self.current_status is not None else self.running_status
            if status is None:
                continue
            self.data.append(byte & 0x7F)
            needed = self._channel_data_length(status) if status <= 0xEF else self._SYSTEM_DATA_LENGTH.get(status, 0)
            if len(self.data) >= needed:
                msg = self._message([status, *self.data[:needed]])
                if msg is not None:
                    output.append(msg)
                self.data = self.data[needed:]
                self.current_status = None
        return output


class TcpMidiClient:
    """Raw MIDI byte-stream client for Allen & Heath TCP MIDI (port 51325)."""

    def __init__(
        self,
        host: str,
        port: int,
        on_message: Callable[[mido.Message], None],
        on_log: Callable[[str], None],
        on_raw: Callable[[str, bytes], None] | None = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.on_message = on_message
        self.on_log = on_log
        self.on_raw = on_raw
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()

    def connect(self, timeout: float = 4.0) -> None:
        self.close()
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        sock.settimeout(0.5)
        self._socket = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._receive_loop, name="GLD-TCP-MIDI", daemon=True)
        self._thread.start()
        self.on_log(f"GLD TCP connected to {self.host}:{self.port}")

    def close(self) -> None:
        self._stop.set()
        sock = self._socket
        self._socket = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def send(self, msg: mido.Message) -> None:
        payload = bytes(msg.bytes())
        if self.on_raw is not None:
            self.on_raw("GLD TCP TX", payload)
        with self._send_lock:
            sock = self._socket
            if sock is None:
                return
            sock.sendall(payload)

    def _receive_loop(self) -> None:
        decoder = MidiStreamDecoder()
        try:
            while not self._stop.is_set():
                sock = self._socket
                if sock is None:
                    break
                try:
                    data = sock.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    raise ConnectionError("connection closed by GLD")
                if self.on_raw is not None:
                    self.on_raw("GLD TCP RX", data)
                for msg in decoder.feed(data):
                    self.on_message(msg)
        except (OSError, ConnectionError) as exc:
            if not self._stop.is_set():
                self.on_log(f"GLD TCP connection lost: {exc}")
        except Exception as exc:
            if not self._stop.is_set():
                self.on_log(f"Error in GLD TCP MIDI parser: {exc}")
        finally:
            sock = self._socket
            self._socket = None
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


class _EditorConnectionBusy(ConnectionError):
    """The GLD accepted TCP but refused another remote-control session."""


class TcpRawClient:
    """Persistent raw TCP client for the proprietary GLD Editor protocol.

    GLD remote-control slots are finite and some firmware keeps a rejected
    socket around for a while. Reconnecting too aggressively can therefore
    make a temporary "all connections in use" condition last longer. This
    client owns exactly one worker and one socket, applies a long cooldown
    after an explicit busy rejection, and lets the user trigger one immediate
    retry without starting overlapping workers.
    """

    BUSY_COOLDOWN_SECONDS = 60.0
    RETRY_INITIAL_SECONDS = 2.0
    RETRY_MAX_SECONDS = 30.0
    REJECTION_GRACE_SECONDS = 0.75

    def __init__(
        self,
        host: str,
        port: int,
        on_log: Callable[[str], None],
        on_connection: Callable[[bool], None] | None = None,
        on_raw: Callable[[str, bytes], None] | None = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.on_log = on_log
        self.on_connection = on_connection
        self.on_raw = on_raw
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._retry_wake = threading.Event()
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._force_retry = False

    @property
    def connected(self) -> bool:
        with self._state_lock:
            return self._socket is not None and not self._stop.is_set()

    def connect(self, timeout: float = 2.0) -> None:
        # A complete close/join before starting guarantees that repeated UI
        # Connect actions can never leave two Editor workers competing for GLD
        # remote-control slots.
        self.close()
        self._stop.clear()
        self._retry_wake.clear()
        with self._state_lock:
            self._force_retry = False
        thread = threading.Thread(
            target=self._connection_loop,
            args=(float(timeout),),
            name="GLD-Editor-Control",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        self.on_log(
            f"GLD Editor control worker started for {self.host}:{self.port} "
            "(single connection, safe retry mode)"
        )

    def request_reconnect(self) -> None:
        """Request one immediate retry without creating another worker."""
        self.on_log("Manual GLD Editor control reconnect requested")
        self._schedule_immediate_reconnect()

    def _schedule_immediate_reconnect(self) -> None:
        with self._state_lock:
            self._force_retry = True
            sock = self._socket
        self._set_connected_socket(None)
        if sock is not None:
            self._close_socket(sock)
        self._retry_wake.set()
        thread = self._thread
        if thread is None or not thread.is_alive():
            self.connect()

    def close(self) -> None:
        self._stop.set()
        self._retry_wake.set()
        with self._state_lock:
            sock = self._socket
        self._set_connected_socket(None)
        if sock is not None:
            self._close_socket(sock)
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._state_lock:
            self._force_retry = False

    @staticmethod
    def _close_socket(sock: socket.socket) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass
        try:
            sock.close()
        except (OSError, AttributeError):
            pass

    def send(self, payload: bytes | bytearray) -> None:
        data = bytes(payload)
        if not data:
            return
        if self.on_raw is not None:
            self.on_raw("GLD EDITOR TX", data)
        with self._send_lock:
            with self._state_lock:
                sock = self._socket
            if sock is None:
                raise ConnectionError("GLD Editor control connection is not open")
            try:
                sock.sendall(data)
            except OSError:
                # Wake the existing worker immediately. Do not recursively call
                # connect(), which could create a second control session.
                self._schedule_immediate_reconnect()
                raise

    def _set_connected_socket(self, sock: socket.socket | None) -> bool:
        with self._state_lock:
            previous = self._socket is not None
            self._socket = sock
            current = sock is not None
        if previous != current and self.on_connection is not None:
            self.on_connection(current)
        return current

    @staticmethod
    def _is_busy_response(payload: bytes | bytearray) -> bool:
        return b"all available connections are in use" in bytes(payload).lower()

    def _consume_force_retry(self) -> bool:
        with self._state_lock:
            forced = self._force_retry
            self._force_retry = False
        if forced:
            self._retry_wake.clear()
        return forced

    def _wait_for_retry(self, seconds: float) -> None:
        if self._stop.is_set() or self._consume_force_retry():
            return
        # Check once more after clearing the event to close the tiny race where
        # a manual reconnect arrives immediately before wait().
        self._retry_wake.clear()
        if self._consume_force_retry():
            return
        self._retry_wake.wait(max(0.0, float(seconds)))
        self._retry_wake.clear()

    def _check_initial_rejection(self, sock: socket.socket) -> bytes:
        """Wait briefly for GLD's post-connect rejection text.

        The desk can complete TCP first and report a busy control table a short
        moment later. Holding the connection in a probation state prevents the
        UI and bridge from queueing a full 96-frame resync onto a refused slot.
        """
        recent = b""
        deadline = time.monotonic() + self.REJECTION_GRACE_SECONDS
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(min(0.25, remaining))
            try:
                data = sock.recv(8192)
            except socket.timeout:
                continue
            if not data:
                raise ConnectionError("connection closed by GLD")
            if self.on_raw is not None:
                self.on_raw("GLD EDITOR RX", data)
            recent = (recent + data)[-512:]
            if self._is_busy_response(recent):
                raise _EditorConnectionBusy(
                    "all available GLD control connections are in use"
                )
        return recent

    def _connection_loop(self, timeout: float) -> None:
        retry_seconds = self.RETRY_INITIAL_SECONDS
        last_error = ""
        while not self._stop.is_set():
            # Consume a manual request before the attempt so its wake event does
            # not accidentally skip the delay after a later unrelated failure.
            self._consume_force_retry()
            sock: socket.socket | None = None
            busy_rejection = False
            next_delay = retry_seconds
            try:
                sock = socket.create_connection((self.host, self.port), timeout=timeout)
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                except (OSError, AttributeError):
                    pass

                recent = self._check_initial_rejection(sock)
                if self._stop.is_set():
                    raise ConnectionAbortedError("Editor control stopped")

                sock.settimeout(0.5)
                self._set_connected_socket(sock)
                self.on_log(f"GLD Editor control connection opened to {self.host}:{self.port}")
                retry_seconds = self.RETRY_INITIAL_SECONDS
                last_error = ""

                while not self._stop.is_set():
                    try:
                        data = sock.recv(8192)
                    except socket.timeout:
                        continue
                    if not data:
                        raise ConnectionError("connection closed by GLD")
                    if self.on_raw is not None:
                        self.on_raw("GLD EDITOR RX", data)
                    recent = (recent + data)[-512:]
                    if self._is_busy_response(recent):
                        # Some firmware leaves a rejected socket open. Mark it
                        # disconnected immediately and enter the same cooldown.
                        raise _EditorConnectionBusy(
                            "all available GLD control connections are in use"
                        )
            except _EditorConnectionBusy as exc:
                busy_rejection = True
                next_delay = self.BUSY_COOLDOWN_SECONDS
                retry_seconds = self.RETRY_INITIAL_SECONDS
                last_error = str(exc)
                if not self._stop.is_set():
                    self.on_log(
                        "GLD Editor control rejected: all remote-control connections are in use. "
                        f"Automatic retry paused for {int(self.BUSY_COOLDOWN_SECONDS)} seconds "
                        "to avoid filling the GLD connection table. Close unused GLD Editor/Remote/OneMix "
                        "clients, or use Reconnect Editor control for one immediate retry."
                    )
            except (OSError, ConnectionError) as exc:
                if not self._stop.is_set():
                    error = str(exc)
                    next_delay = retry_seconds
                    if error != last_error:
                        last_error = error
                        self.on_log(
                            f"GLD Editor control unavailable: {error}; "
                            f"next automatic retry in {next_delay:.0f} seconds"
                        )
                    retry_seconds = min(
                        self.RETRY_MAX_SECONDS,
                        max(self.RETRY_INITIAL_SECONDS, retry_seconds * 1.7),
                    )
            except Exception as exc:
                if not self._stop.is_set():
                    next_delay = retry_seconds
                    self.on_log(f"Error in GLD Editor control receiver: {exc}")
                    retry_seconds = min(
                        self.RETRY_MAX_SECONDS,
                        max(self.RETRY_INITIAL_SECONDS, retry_seconds * 1.7),
                    )
            finally:
                with self._state_lock:
                    is_current = self._socket is sock
                if is_current:
                    self._set_connected_socket(None)
                if sock is not None:
                    self._close_socket(sock)

            if not self._stop.is_set():
                # Busy rejections use a deliberately long cooldown. A manual
                # button press or a send failure wakes this single worker at
                # once; no parallel connection attempt is ever started.
                self._wait_for_retry(next_delay)


class MidiRouter(QObject):
    log = Signal(str)
    raw_data = Signal(str)
    editor_connection_changed = Signal(bool)
    closed = Signal()
    gld_message = Signal(object)
    daw_message = Signal(int, object)

    def __init__(self) -> None:
        super().__init__()
        self.gld_in = None
        self.gld_out = None
        self.gld_tcp: TcpMidiClient | None = None
        self.gld_editor: TcpRawClient | None = None
        self.daw_ins = []
        self.daw_outs = []
        self.connected = False
        self.gld_connection_mode = "tcp"
        self.raw_capture_enabled = False
        self._state_lock = threading.RLock()
        self._generation = 0
        self._closing = False
        self._close_worker: threading.Thread | None = None

    @staticmethod
    def input_names() -> list[str]:
        try:
            return mido.get_input_names()
        except Exception:
            return []

    @staticmethod
    def output_names() -> list[str]:
        try:
            return mido.get_output_names()
        except Exception:
            return []

    def set_raw_capture(self, enabled: bool) -> None:
        self.raw_capture_enabled = bool(enabled)

    def _raw_bytes(self, direction: str, payload: bytes | bytearray) -> None:
        if not self.raw_capture_enabled:
            return
        # Editor state/meter packets can arrive in multi-kilobyte TCP chunks.
        # Split them into readable bounded lines so the detached log remains
        # responsive without dropping any bytes from the captured chunk.
        data = bytes(payload)
        if not data:
            self.raw_data.emit(f"{direction:<18} <empty>")
            return
        for offset in range(0, len(data), 256):
            chunk = data[offset:offset + 256]
            self.raw_data.emit(
                f"{direction:<18} +{offset:04X}  {_hex_bytes(chunk)}"
            )

    def _raw_midi(self, direction: str, msg) -> None:
        if self.raw_capture_enabled:
            self.raw_data.emit(f"{direction:<18} {_midi_debug(msg)}")

    @staticmethod
    def _port_close_worker(port, done: threading.Event) -> None:
        try:
            port.close()
        except Exception:
            pass
        finally:
            done.set()

    def _close_port_bounded(self, port, label: str, timeout: float = 0.75) -> None:
        """Close one backend port without blocking indefinitely."""
        self._close_ports_bounded([(port, label)], timeout=timeout)

    def _close_ports_bounded(self, ports, timeout: float = 0.75) -> None:
        """Close all MIDI ports concurrently under one shared deadline.

        Four MCU input/output pairs plus the GLD MIDI pair can otherwise turn a
        750 ms per-port timeout into several seconds. Every backend close gets
        its own daemon worker, while reconnect/shutdown waits at most one global
        timeout for the complete set.
        """
        workers: list[tuple[str, threading.Event, threading.Thread]] = []
        for port, label in ports:
            if port is None:
                continue
            done = threading.Event()
            worker = threading.Thread(
                target=self._port_close_worker,
                args=(port, done),
                name=f"MIDI-close-{label}",
                daemon=True,
            )
            workers.append((str(label), done, worker))
            worker.start()

        deadline = time.monotonic() + max(0.05, float(timeout))
        for _label, _done, worker in workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(timeout=remaining)
        for label, done, _worker in workers:
            if not done.is_set():
                self.log.emit(
                    f"MIDI backend did not close {label} within {timeout:.2f}s; "
                    "cleanup continues in the background"
                )

    def _detach_connections(self):
        """Invalidate callbacks immediately and return owned resources."""
        with self._state_lock:
            self._closing = True
            self._generation += 1
            snapshot = (
                self.gld_tcp,
                self.gld_editor,
                self.gld_in,
                self.gld_out,
                list(self.daw_ins),
                list(self.daw_outs),
            )
            self.gld_tcp = None
            self.gld_editor = None
            self.gld_in = None
            self.gld_out = None
            self.daw_ins = []
            self.daw_outs = []
            self.connected = False
        return snapshot

    def _close_snapshot(self, snapshot) -> None:
        gld_tcp, gld_editor, gld_in, gld_out, daw_ins, daw_outs = snapshot
        if gld_tcp is not None:
            try:
                gld_tcp.close()
            except Exception:
                pass
        if gld_editor is not None:
            try:
                gld_editor.close()
            except Exception:
                pass
        ports = [
            (gld_in, "GLD input"),
            (gld_out, "GLD output"),
            *[(port, f"DAW input {index + 1}") for index, port in enumerate(daw_ins)],
            *[(port, f"DAW output {index + 1}") for index, port in enumerate(daw_outs)],
        ]
        self._close_ports_bounded(ports)

    def _finish_close(self) -> None:
        with self._state_lock:
            self._closing = False
        self.log.emit("Connections closed")
        self.closed.emit()

    def close(self) -> None:
        """Bounded synchronous close for reconnect and process shutdown."""
        snapshot = self._detach_connections()
        self._close_snapshot(snapshot)
        self._finish_close()

    def close_async(self) -> None:
        """Detach now and close drivers off the GUI thread.

        Some WinMM/rtmidi drivers can block forever in ``port.close()`` while a
        callback is active. Detaching increments a generation token first, so
        late callbacks become harmless; the potentially blocking backend work
        then runs in daemon workers and can never freeze the Qt event loop.
        """
        with self._state_lock:
            if self._closing:
                return
        snapshot = self._detach_connections()

        def worker() -> None:
            try:
                self._close_snapshot(snapshot)
            finally:
                self._finish_close()

        thread = threading.Thread(
            target=worker, name="MIDI-router-close", daemon=True
        )
        self._close_worker = thread
        thread.start()

    def connect_ports(
        self,
        gld_in_name: str,
        gld_out_name: str,
        daw_in_names: list[str],
        daw_out_names: list[str],
        use_virtual_daw_ports: bool = False,
        gld_connection_mode: str = "tcp",
        gld_host: str = "192.168.1.50",
        gld_tcp_port: int = 51325,
        enable_editor_labels: bool = False,
        gld_editor_port: int = 51321,
    ) -> None:
        self.close()
        with self._state_lock:
            self._closing = False
            self._generation += 1
            generation = self._generation
        if use_virtual_daw_ports and sys.platform.startswith("win"):
            raise ValueError(
                "The bundled python-rtmidi WinMM backend cannot create virtual MIDI ports on Windows. "
                "Create app-to-app/loopback endpoints in Windows MIDI Services (or another virtual MIDI driver), "
                "turn off 'Create virtual DAW ports', then select the existing endpoints in the bridge."
            )
        self.gld_connection_mode = gld_connection_mode
        daw_in_names = [str(name or "").strip() for name in list(daw_in_names)[:4]]
        daw_out_names = [str(name or "").strip() for name in list(daw_out_names)[:4]]
        daw_in_names += [""] * (4 - len(daw_in_names))
        daw_out_names += [""] * (4 - len(daw_out_names))
        for bank, (input_name, output_name) in enumerate(zip(daw_in_names, daw_out_names), start=1):
            if bool(input_name) != bool(output_name):
                raise ValueError(f"DAW bank {bank} needs both an input and an output endpoint")
        try:
            if gld_connection_mode == "tcp":
                host = gld_host.strip()
                if not host:
                    raise ValueError("Enter the GLD IP address")
                self.gld_tcp = TcpMidiClient(
                    host, int(gld_tcp_port),
                    lambda msg, gen=generation: self._on_gld_message(msg, gen),
                    self.log.emit, self._raw_bytes
                )
                self.gld_tcp.connect()
                if enable_editor_labels:
                    self.gld_editor = TcpRawClient(
                        host,
                        int(gld_editor_port),
                        self.log.emit,
                        self.editor_connection_changed.emit,
                        self._raw_bytes,
                    )
                    self.gld_editor.connect()
            else:
                self.gld_in = (
                    mido.open_input(
                        gld_in_name,
                        callback=lambda msg, gen=generation: self._on_gld_message(msg, gen),
                    )
                    if gld_in_name else None
                )
                self.gld_out = mido.open_output(gld_out_name) if gld_out_name else None

            self.daw_ins = []
            self.daw_outs = []
            for bank, name in enumerate(daw_in_names):
                if not name:
                    self.daw_ins.append(None)
                    continue
                cb = self._make_daw_callback(bank, generation)
                self.daw_ins.append(mido.open_input(name, callback=cb, virtual=use_virtual_daw_ports))
            for bank, name in enumerate(daw_out_names):
                if not name:
                    self.daw_outs.append(None)
                    continue
                self.daw_outs.append(mido.open_output(name, virtual=use_virtual_daw_ports))

            for bank, (input_name, output_name) in enumerate(zip(daw_in_names, daw_out_names)):
                if not input_name and not output_name:
                    continue
                first = bank * 8 + 1
                last = first + 7
                self.log.emit(
                    f"DAW bank {bank + 1} / tracks {first}-{last}: "
                    f"DAW→bridge='{input_name}', bridge→DAW='{output_name}'"
                )
            with self._state_lock:
                if generation != self._generation or self._closing:
                    raise RuntimeError("Connection attempt was cancelled")
                self.connected = True
            self.log.emit("GLD/DAW routing connected")
        except Exception as exc:
            self.close()
            self.log.emit(f"Connection failed: {exc}")
            raise

    def _callback_is_current(self, generation: int | None) -> bool:
        with self._state_lock:
            return (
                not self._closing
                and (generation is None or int(generation) == self._generation)
            )

    def _on_gld_message(self, msg, generation: int | None = None) -> None:
        if not self._callback_is_current(generation):
            return
        self._raw_midi("GLD RX MIDI", msg)
        self.gld_message.emit(msg)

    def _make_daw_callback(
        self, bank: int, generation: int | None = None
    ) -> Callable[[object], None]:
        def callback(msg) -> None:
            if not self._callback_is_current(generation):
                return
            self._raw_midi(f"DAW{bank + 1} RX", msg)
            self.daw_message.emit(bank, msg)
        return callback

    def send_to_gld(self, msg) -> None:
        if not self._callback_is_current(None):
            return
        self._raw_midi("GLD TX MIDI", msg)
        try:
            if self.gld_connection_mode == "tcp":
                if self.gld_tcp is not None:
                    self.gld_tcp.send(msg)
            elif self.gld_out is not None:
                self.gld_out.send(msg)
        except Exception as exc:
            self.log.emit(f"Error sending to GLD: {exc}")

    def send_editor_label(self, payload: bytes | bytearray) -> bool:
        """Send one raw MIDI Strip control frame on the GLD Editor socket.

        The historical method name is retained for source compatibility; the
        socket now carries Pan redraw, name and colour frames.
        """
        try:
            if not self._callback_is_current(None) or self.gld_editor is None:
                return False
            self.gld_editor.send(payload)
            return True
        except Exception as exc:
            self.log.emit(f"Error sending GLD Editor control frame: {exc}")
            return False

    def restart_editor_control(self) -> None:
        if self.gld_editor is None:
            self.log.emit("GLD Editor control is not enabled for this connection")
            return
        self.log.emit("Restarting GLD Editor control connection")
        self.gld_editor.request_reconnect()

    @property
    def editor_labels_connected(self) -> bool:
        return self.gld_editor is not None and self.gld_editor.connected

    def send_to_daw(self, bank: int, msg) -> None:
        if not self._callback_is_current(None):
            return
        self._raw_midi(f"DAW{bank + 1} TX", msg)
        if bank < 0 or bank >= len(self.daw_outs):
            return
        port = self.daw_outs[bank]
        if port is None:
            return
        try:
            port.send(msg)
        except Exception as exc:
            self.log.emit(f"Error sending to DAW bank {bank + 1}: {exc}")

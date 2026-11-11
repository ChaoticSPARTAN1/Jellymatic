# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""CHR1 over the console's USB serial port (launcher 1.1.2 and later).

Launcher 1.1.2 runs one CHR1 handler behind two transports (see
the remote-control notes in the Jellymatic project). On
USB the PC sends `CHR1 OPEN`; the console answers `CHR1 HELLO ...` and from then
on the stream is byte-for-byte the CHR1 of PROTOCOL.md, so the session code is
LanTransport's, unchanged. Without a key the console answers `CHR1 NOKEY` and
this PC can pair over USB: the console shows a 6-digit code and only a
physical A press on the console approves.

Safety. The stock FPGA ties the USB serial control lines to ESP32 EN and IO0:
  * the real port opens only with "serialOpenAllowed": true in
    local/chromatic/config.json, with DTR and RTS de-asserted before open and
    never toggled; no flow control, no break, no baud change (as the read-only
    reader in serial_transport.py);
  * only CHR1 lines are ever written (an allow-list checked on every write):
    `CHR1 OPEN`, `CHR1 PAIR <nonce>`, `CHR1 AUTH ...` and `REQ ...`;
  * nothing is written until the port shows a launcher that speaks CHR1:
    `PJ-LAUNCHER-READY` 1.1.2 or later, or `PJ-DIAG` lines from a running
    launcher (one `CHR1 OPEN` then; no answer means no CHR1, and the port
    stays quiet until the next launcher start). After `PJ-LAUNCHER-TO-STOCK`
    nothing is written, so stock firmware never sees a byte from Remote Pilot;
  * session bytes (raw screen frames and so on) never reach the text log, and
    after a session ends, bytes are dropped until the line has been quiet;
  * the pairing key (`PJ-PAIR` line) is never logged or shown.
Even so, opening the port has not been tried on hardware: some USB-serial
drivers pulse the control lines on open (see LAUNCHER-SIDE.md).
"""

import hashlib
import re
import secrets
import threading
import time
from collections import deque

from . import protocol as p
from .lan_transport import LanTransport
from .serial_transport import READY, TO_STOCK, _PORT_NAME, SerialTransport

OPEN_LINE = b"CHR1 OPEN\n"
ANSWER_WAIT_S = 1.0         # the spec: skip other lines for up to 1 s
PAIR_ANSWER_S = 3.0
PAIR_WAIT_S = 65.0          # the console asks the owner for 60 s
QUIET_S = 0.3               # after a session: drop bytes until the line is quiet this long
REOPEN_PORT_S = 3.0
# Patience over USB, so a console busy with its Jellyfin app keeps its session. A reply is
# waited for as long as its bytes keep coming: the console is given up on only after `timeout` (3 s) with no
# byte at all, so a real loss is still noticed in 3 s. A body may take up to its size at SLOWEST_BPS on top of
# that (launchers before 1.3.2 send about 2.5 KB/s while Jellyfin is open: a 46 KB screen takes about 18 s).
SLOWEST_BPS = 1500
SLOW_BPS = 6000             # measured below this: the console is busy (Jellyfin), polling eases off
SLOW_LOG_EVERY = 5          # while slow, the RAM log tail (4 KB) is read every 5th status poll, not every one
SLOW_NOTE = "USB is slower while Jellyfin plays: screenshots take a few seconds"
MAX_SESSION_BUFFER = p.MAX_BODY + 4 * p.MAX_LINE
# The launcher's start and hand-over lines, found even in bytes that are otherwise dropped (see UsbLink._markers).
_MARKER = re.compile(rb"(" + READY.encode() + rb" [0-9][0-9.]{0,15}|" + TO_STOCK.encode() + rb"|PJ-LAUNCHER-STOCK-INVALID)\r?\n")
MARKER_TAIL = 48
FIRST_CHR1_VERSION = (1, 1, 2)
_WRITABLE = re.compile(rb"(?:CHR1 OPEN|CHR1 PAIR [0-9a-f]{32}|CHR1 AUTH [0-9a-f]{32} [0-9a-f]{64}"
                       rb"|REQ [1-9][0-9]{0,9} [a-z-]{2,15} [!-~]{1,47} [0-9a-f]{32})\n")


def pair_code(device_nonce, pc_nonce):
    """The 6-digit code both screens show (CHROMATIC-REMOTE-CONTROL.md)."""
    digest = hashlib.sha256(f"CHR1 pair-code {device_nonce} {pc_nonce}".encode("ascii")).digest()
    return f"{int.from_bytes(digest[:4], 'big') % 1000000:06d}"


def speaks_chr1(version):
    try:
        return tuple(int(part) for part in version.split(".")) >= FIRST_CHR1_VERSION
    except (AttributeError, ValueError):
        return False


CHROMATIC_USB_VID = 0x374E  # the console's USB serial interface (ModRetro)
NO_PYSERIAL = "USB support (pyserial) is not installed in this Python"


class SerialSupportMissing(ConnectionError):
    """pyserial cannot be imported: no port can be listed or opened from this Python."""

    def __init__(self):
        super().__init__(NO_PYSERIAL)


def _list_ports():
    try:
        from serial.tools import list_ports  # pyserial, optional
    except ImportError as exc:
        raise SerialSupportMissing() from exc
    return list_ports.comports()


def pyserial_available():
    try:
        _list_ports()
    except SerialSupportMissing:
        return False
    return True


def port_present(name):
    """True when the OS lists this serial port. Lists only; never opens anything.
    Raises SerialSupportMissing when pyserial is not installed (not "unplugged")."""
    return any(item.device == name for item in _list_ports())


def find_consoles(list_fn=None):
    """The serial ports whose USB vendor ID is the Chromatic's, as [{"port": "COM5"}].
    Lists only; never opens anything. Serial numbers stay out: they identify the device.
    Raises SerialSupportMissing when pyserial is not installed."""
    found = []
    for item in (list_fn or _list_ports)():
        name = getattr(item, "device", None)
        if getattr(item, "vid", None) == CHROMATIC_USB_VID and isinstance(name, str) and _PORT_NAME.fullmatch(name):
            found.append({"port": name})
    return sorted(found, key=lambda entry: entry["port"])


class PySerialPort:
    """The real console port as raw bytes. Constructed only after the local opt-in."""

    def __init__(self, port, open_allowed=False):
        if open_allowed is not True:
            raise PermissionError("Opening the Chromatic serial port needs \"serialOpenAllowed\": true "
                                  "in local/chromatic/config.json, set by the user while present")
        if not isinstance(port, str) or not _PORT_NAME.fullmatch(port):
            raise ValueError("Serial port must look like COM5, /dev/ttyUSB0 or /dev/cu.usbserial-1")
        try:
            import serial  # pyserial, optional; imported only here
        except ImportError as exc:
            raise ValueError(NO_PYSERIAL + "; start Remote Pilot with Start-RemotePilot.cmd") from exc
        handle = serial.Serial()
        handle.port, handle.baudrate, handle.timeout, handle.write_timeout = port, 115200, 0.05, 2.0
        handle.bytesize, handle.parity, handle.stopbits = serial.EIGHTBITS, serial.PARITY_NONE, serial.STOPBITS_ONE
        handle.xonxoff = handle.rtscts = handle.dsrdtr = False
        handle.dtr = False  # de-assert before open: these lines reach ESP32_EN / ESP32_IO0
        handle.rts = False
        handle.open()
        self._port = handle

    def read(self, size):
        return self._port.read(max(1, min(size, self._port.in_waiting or 1)))

    def write(self, data):
        self._port.write(data)

    def close(self):
        self._port.close()


class UsbLink:
    """Owns the port. Outside a session the bytes are text lines: CHR1 and PJ-PAIR
    answers go to a queue, everything else to on_line. Inside a session the bytes
    are the CHR1 stream and are never logged. Writes pass the CHR1 allow-list."""

    def __init__(self, port, on_line, clock=time.monotonic):
        self._port, self._on_line, self._clock = port, on_line, clock
        self._cond = threading.Condition()
        self._capture = False
        self._buffer = bytearray()
        self._pending = b""
        self._answers = deque(maxlen=16)
        self._quiet_until = 0.0
        self._resync = False        # after a session dropped mid-reply: skip its leftover bytes up to "CHR1 "
        self._resync_tail = b""
        self._marker_tail = b""     # the end of the last dropped bytes, so a marker split across reads is found
        self._marker_end = 0
        self.last_rx = None         # clock time of the last session byte (patience while bytes keep coming)
        self.rate = None            # bytes/s of the last large reply, measured
        self.closed = False
        self.error = ""
        self.timeout = 3.0
        self._thread = threading.Thread(target=self._run, name="chromatic-usb", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def close(self):
        with self._cond:
            self.closed = True
            self._cond.notify_all()
        try:
            self._port.close()
        except Exception:
            pass
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def _run(self):
        while not self.closed:
            try:
                data = self._port.read(4096)
            except Exception as exc:
                with self._cond:
                    self.error = f"USB read stopped: {type(exc).__name__}"
                    self.closed = True
                    self._cond.notify_all()
                break
            if not data:
                continue
            lines = []
            with self._cond:
                if self._capture:
                    if len(self._buffer) + len(data) > MAX_SESSION_BUFFER:
                        self._buffer.clear()  # more than one reply is owed: framing is broken
                        self.error = "USB session overflow"
                    self._buffer += data
                    self.last_rx = self._clock()
                    self._cond.notify_all()
                    continue
                now = self._clock()
                if self._resync:
                    # A CHR1 OPEN was sent while the last session's reply was still arriving: the console stops
                    # that reply and answers. Its leftover bytes are skipped up to the answer, which is kept.
                    raw, carried = data, len(self._resync_tail)
                    data = self._resync_tail + data
                    at = data.find(b"CHR1 ")
                    if at < 0:
                        self._resync_tail = data[-4:]
                        lines = self._markers(raw)  # skipped, but a launcher start or hand-over still counts
                    else:
                        lines = self._markers(raw[:max(0, at - carried)])
                        self._resync, self._resync_tail, self._quiet_until, data = False, b"", 0.0, data[at:]
                        self._marker_tail = b""
                        lines += self._text(data)
                elif now < self._quiet_until:
                    self._quiet_until = now + QUIET_S  # the end of a session's bytes: drop
                    lines = self._markers(data)
                else:
                    lines = []
                    if self._marker_tail:  # a marker may have begun in the last dropped bytes
                        skip = len(self._marker_tail)
                        lines = self._markers(data, straddling=True)
                        data = data[max(0, self._marker_end - skip):]
                        self._marker_tail = b""
                    lines += self._text(data)
            for line in lines:
                self._on_line(line)

    def _markers(self, data, straddling=False):
        """Bytes dropped unread (the quiet window after a session, or a resync's skipped leftovers) are still searched
        for the launcher's start and hand-over markers (caller holds the lock): the console may restart or go to stock
        right after a session's last bytes, and missing that would keep Remote Pilot writing to stock firmware or
        waiting for a launcher that already started. Only the marker line itself is passed on; a marker split across
        two reads is found through a short tail kept from the last. straddling: the first bytes read as text again;
        only a marker that began in the dropped tail is taken here (one wholly in `data` is the text reader's)."""
        tail = self._marker_tail
        data = tail + data
        found, end = [], 0
        for match in _MARKER.finditer(data):
            if straddling and match.start() >= len(tail):
                break
            found.append(match.group(1))
            end = match.end()
        rest = data[end:]
        self._marker_end = end  # where the last marker ended in tail + data (the text reader skips that far)
        self._marker_tail = rest[-MARKER_TAIL:] if len(rest) > MARKER_TAIL else rest
        return found

    def _text(self, data):
        """Splits text bytes into lines (caller holds the lock); returns the log lines."""
        lines = []
        self._pending += data
        while b"\n" in self._pending:
            line, self._pending = self._pending.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if line.startswith((b"CHR1 ", b"PJ-PAIR ")):
                self._answers.append(line.decode("ascii", "replace")[:p.MAX_LINE])
                self._cond.notify_all()
            else:
                marker = None if line.startswith(b"PJ-") else _MARKER.search(line + b"\n")
                lines.append(marker.group(1) if marker else line)  # a marker behind a dropped reply's last bytes
        if len(self._pending) > 4096:
            lines.append(self._pending[:240])
            self._pending = b""
        return lines

    # ---- out of a session ----------------------------------------------------------
    def write(self, data):
        if not isinstance(data, (bytes, bytearray)) or not _WRITABLE.fullmatch(bytes(data)):
            raise PermissionError("Only CHR1 lines are ever written to the console")
        if self.closed:
            raise ConnectionError("The USB port is closed")
        self._port.write(bytes(data))

    def clear_answers(self):
        with self._cond:
            self._answers.clear()

    def expect_answer(self):
        """Before CHR1 OPEN: if the last session's bytes are still arriving (it ended mid-reply), the answer
        follows them closely, inside the quiet window that would otherwise drop it. Skip only up to "CHR1 "."""
        with self._cond:
            if not self._capture and self._clock() < self._quiet_until:
                self._resync, self._resync_tail = True, b""

    def answer(self, timeout):
        """Next CHR1/PJ-PAIR line, or None after timeout. OSError when the port closed."""
        deadline = self._clock() + timeout
        with self._cond:
            while not self._answers:
                if self.closed:
                    raise ConnectionError(self.error or "The USB port closed")
                left = deadline - self._clock()
                if left <= 0:
                    return None
                self._cond.wait(min(left, 0.1))
            return self._answers.popleft()

    def begin_session(self):
        with self._cond:
            self._capture, self._pending = True, b""
            self._resync, self._resync_tail, self._marker_tail = False, b"", b""
            self.rate = None  # measured again in this session
            self._buffer.clear()

    def end_session(self, quiet=True):
        """Back to text lines. quiet: a session ended mid-reply, so drop bytes until the
        line has been silent for QUIET_S (they belong to the session, not the log)."""
        lines = []
        with self._cond:
            self._capture, self._pending = False, b""
            self._resync, self._resync_tail = False, b""
            leftover = bytes(self._buffer)
            self._buffer.clear()
            if quiet:
                self._quiet_until = self._clock() + QUIET_S
                # The leftover is dropped, but a hand-over or launcher start printed right after the session's last
                # bytes must still count (1.3.3 review): otherwise one CHR1 OPEN could reach stock firmware.
                if leftover:
                    lines = self._markers(leftover)
            elif leftover:
                lines = self._text(leftover)  # every reply was read: what follows is text again
        for line in lines:
            self._on_line(line)

    # ---- in a session: the file-like reads protocol.read_line/read_exact use ----------
    def _wait(self, ready, size=0):
        # 115200 baud moves about 11.5 KB/s: a raw 46 KB screen takes about 4 s, and about 18 s from a launcher
        # before 1.3.2 with Jellyfin open. Patience lasts while bytes keep coming (see SLOWEST_BPS).
        start = self._clock()
        cap = start + self.timeout + size / SLOWEST_BPS
        with self._cond:
            while not ready():
                if self.closed:
                    raise ConnectionError(self.error or "The USB port closed")
                now = self._clock()
                heard = start if self.last_rx is None or self.last_rx < start else self.last_rx
                left = min(heard + self.timeout, cap) - now
                if left <= 0:
                    raise TimeoutError("The console did not answer in time over USB")
                self._cond.wait(min(left, 0.1))

    def readline(self, limit):
        self._wait(lambda: b"\n" in self._buffer[:limit] or len(self._buffer) >= limit)
        with self._cond:
            end = self._buffer.find(b"\n", 0, limit)
            size = limit if end < 0 else end + 1
            data = bytes(self._buffer[:size])
            del self._buffer[:size]
            return data

    def read(self, size):
        start = self._clock()
        self._wait(lambda: len(self._buffer) >= size, size)
        with self._cond:
            data = bytes(self._buffer[:size])
            del self._buffer[:size]
        elapsed = self._clock() - start
        if size >= 2048 and elapsed > 0:
            self.rate = size / elapsed  # the pace of the console's USB output now (for the slow-link note)
        return data


class _SessionWriter:
    """What LanTransport calls `_sock` (sendall/close) and `_stream` (readline/read/close)."""

    def __init__(self, link):
        self._link = link

    def sendall(self, data):
        self._link.write(data)

    def readline(self, limit):
        return self._link.readline(limit)

    def read(self, size):
        return self._link.read(size)

    def close(self):
        pass


class UsbTransport(LanTransport):
    """CHR1 session over USB serial, plus the launcher's text lines outside sessions."""

    kind = "usb"

    def __init__(self, port_factory, key=None, label="USB (CHR1)", simulated=False, present=None,
                 on_paired=None, timeout=3.0, clock=time.monotonic):
        self._init_state(key, label, simulated, timeout, clock)
        self.host = self.port = None
        self._factory, self._present, self._on_paired = port_factory, present, on_paired
        self._link = None
        self._link_lock = threading.Lock()
        self._last_port_try = None
        self._parser = SerialTransport(lambda: None, label=label, simulated=simulated, clock=clock)
        self._may_open = False      # a launcher that speaks CHR1 is running on this port
        self._once = False          # a single OPEN allowed (PJ-DIAG seen, or the user's Retry)
        self._usb_state = "closed"
        self._pairing = None
        self._pairing_active = False
        self._opening = False

    # ---- the port ---------------------------------------------------------------------
    @property
    def port_open(self):
        return self._link is not None and not self._link.closed

    def _ensure_link(self, force=False):
        with self._link_lock:
            if self._link is not None and not self._link.closed:
                return self._link
            if self._link is not None and self._link.error:
                self._error = self._link.error
            self._link = None
            now = self._clock()
            if not force and self._last_port_try is not None and now - self._last_port_try < REOPEN_PORT_S:
                raise ConnectionError(self._error or "The USB port is not open")
            self._last_port_try = now
            try:
                present = self._present is None or self._present()
            except SerialSupportMissing:
                self._usb_state = "no-pyserial"
                raise
            if not present:
                self._usb_state = "unplugged"
                raise ConnectionError("The console's USB serial port is not connected")
            port = self._factory()  # PermissionError unless the user allowed opening it
            self._usb_state, self._may_open, self._once = "listening", False, False  # before the reader runs
            self._link = UsbLink(port, self._ingest, clock=self._clock)
            self._link.timeout = self._timeout
            self._link.start()
            return self._link

    def _ingest(self, raw):
        """A text line outside a session (reader thread)."""
        self._parser.ingest(raw)
        text = raw.decode("utf-8", "replace").strip() if isinstance(raw, (bytes, bytearray)) else raw.strip()
        if text.startswith(READY):
            self._status = None  # a new launcher start: the last hand-over is history
            if speaks_chr1(text[len(READY):].strip()):
                self._may_open, self._usb_state = True, "launcher"
                if self._key is not None and not self._pairing_active:
                    # The countdown runs about 5 s: open now, not at the next poll.
                    threading.Thread(target=self.poll, name="chromatic-usb-open", daemon=True).start()
            else:
                self._may_open, self._usb_state = False, "old-launcher"
        elif text == TO_STOCK or text.startswith("PJ-LAUNCHER-STOCK-INVALID"):
            self._may_open, self._once, self._usb_state = False, False, "stock"
        elif text.startswith("PJ-DIAG ") and self._usb_state == "listening":
            self._once, self._usb_state = True, "diag-seen"

    # ---- session (LanTransport hooks) ---------------------------------------------------
    def _connect(self):
        link = self._ensure_link()
        if self._key is None:
            raise ValueError("This PC has no key for the console yet: pair over USB")
        if self._pairing_active:
            raise ValueError("Pairing is in progress")
        if self._status and self._status.get("phase") == "handover":
            self._may_open, self._once = False, False  # never write to the console as it goes to stock
        if not (self._may_open or self._once):
            raise ValueError({"stock": "Stock firmware is running; the launcher returns at the next power-on",
                              "old-launcher": "This launcher has no CHR1; launcher 1.1.2 or later is needed",
                              "no-answer": "No CHR1 answer on USB; waiting for the next launcher start",
                              }.get(self._usb_state, "Waiting for the launcher on USB"))
        self._once = False
        link.clear_answers()
        link.expect_answer()  # the last session may have ended mid-reply: its answer must not be dropped
        link.write(OPEN_LINE)
        line = link.answer(ANSWER_WAIT_S)
        while line is not None and not line.startswith(("CHR1 HELLO ", "CHR1 NOKEY", "CHR1 BUSY", "CHR1 WAIT")):
            line = link.answer(ANSWER_WAIT_S)  # not an answer to OPEN (a leftover byte run): keep listening
        if line is None:
            self._may_open, self._usb_state = False, "no-answer"
            raise ValueError("No CHR1 answer on USB: the launcher is not running (or is older than 1.1.2)")
        if line in ("CHR1 NOKEY", "CHR1 BUSY", "CHR1 WAIT"):
            self._usb_state = line.split(" ")[1].lower()
            raise ValueError({"nokey": "The console has no key: pair this PC over USB (press A on the console)",
                              "busy": "A Wi-Fi session holds the console",
                              "wait": "The console refused a handshake a moment ago; retrying"}[self._usb_state])
        device_nonce, version = p.parse_hello(line)
        pc_nonce = p.new_nonce()
        link.write(p.auth_line(self._key, device_nonce, pc_nonce))
        answer = link.answer(self._timeout)
        if answer is None:
            raise TimeoutError("The console did not finish the USB handshake")
        if answer == "CHR1 DENY":
            self._usb_state = "denied"
            raise ValueError("The console refused this PC's key (re-pair, or use the key Chromatic Manager wrote)")
        caps = p.parse_ok(self._key, answer, pc_nonce, device_nonce)
        link.begin_session()
        self._sock = self._stream = _SessionWriter(link)
        self._session = p.session_key(self._key, device_nonce, pc_nonce)
        self._seq, self._caps, self._version = 0, caps, version
        self._decoder.reset()
        self._may_open, self._usb_state = True, "session"
        self._note("USB session open")

    def _drop(self, clean=False):
        had = self._sock is not None
        super()._drop()
        if had and self._link is not None:
            self._link.end_session(quiet=not clean)
            if self._usb_state == "session":
                self._usb_state = "launcher"

    def _end_session(self):
        """Ends a session cleanly: bye and its answer are read, so no session bytes are left
        on the line. After a hand-over nothing is sent: the console is on its way to stock."""
        with self._lock:
            clean = True
            handover = bool(self._status and self._status.get("phase") == "handover")
            if self._sock is not None and not handover:
                try:
                    self._seq += 1
                    self._sock.sendall(p.encode_request(self._session, self._seq, "bye"))
                    p.read_response(self._session, self._stream, self._seq, "bye")
                except (OSError, p.ProtocolError):
                    clean = False
            self._drop(clean)

    def poll(self):
        try:
            self._ensure_link()
        except (OSError, ValueError) as exc:
            self._error = str(exc)[:160]
            return
        if not self._pairing_active:
            super().poll()
        if self._usb_state == "stock" or self._status and self._status.get("phase") == "handover":
            self._may_open = False

    def start(self):
        try:
            self._ensure_link(force=True)
        except (OSError, ValueError) as exc:
            self._error = str(exc)[:160]

    def stop(self):
        self._end_session()
        with self._link_lock:
            link, self._link = self._link, None
        if link is not None:
            link.close()
        self._usb_state = "closed"

    def retry(self):
        """The user's Retry: one CHR1 OPEN even without a fresh launcher marker."""
        try:
            self._ensure_link(force=True)
        except (OSError, ValueError) as exc:
            self._error = str(exc)[:160]
            return
        if self._usb_state != "stock":
            self._once = True

    # ---- pace -----------------------------------------------------------------------------
    @property
    def slow(self):
        """True while a session is up and the console's last large reply came slower than SLOW_BPS (a launcher
        before 1.3.2 with Jellyfin open). Measured, never assumed from the launcher version."""
        link = self._link
        return self._sock is not None and link is not None and link.rate is not None and link.rate < SLOW_BPS

    def _want_log(self):
        if not self.slow:
            self._log_polls = 0
            return True
        self._log_polls = getattr(self, "_log_polls", 0) + 1
        return self._log_polls % SLOW_LOG_EVERY == 1

    # ---- reads ----------------------------------------------------------------------------
    def snapshot(self):
        snap = super().snapshot()
        serial = self._parser.snapshot()
        session = snap["session"]
        link = self._link
        if session and not snap["connected"] and link is not None and link.last_rx is not None \
                and self._clock() - link.last_rx < self._timeout:
            snap["connected"] = True  # a long reply is still arriving: the link is alive
        snap["usbSlow"] = self.slow
        snap["usbNote"] = SLOW_NOTE if self.slow else ""
        pairing = self.pairing_state()
        snap.update({
            "kind": self.kind, "host": None, "portOpen": self.port_open, "usbState": self._usb_state,
            "phase": snap["phase"] if session else serial["phase"],
            "launcherVersion": snap["launcherVersion"] or serial["launcherVersion"],
            "resetReason": serial["resetReason"], "handovers": serial["handovers"],
            "lines": serial["lines"], "pairing": pairing, "pcHasKey": self._key is not None,
            "error": "" if session else (self._error or serial["error"]),
        })
        if not session and self._status and self._status.get("phase") == "handover":
            snap["phase"] = "stock"
        return snap

    def log_text(self, max_bytes=p.MAX_LOG):
        text = self._parser.log_text(max_bytes)
        if self._sock is not None and "log" in self._caps:
            text += "--- launcher RAM log (CHR1 log) ---\n" + super().log_text(max_bytes)
        return text

    def tail(self, count=40):
        if self._sock is not None and self._remote_tail:
            return self._remote_tail[-count:]
        return self._parser.tail(count)

    def clear(self):
        super().clear()
        self._parser.clear()

    # ---- USB pairing ----------------------------------------------------------------------
    def pairing_state(self):
        state = self._pairing
        if not state:
            return None
        left = None if state.get("until") is None else max(0, int(state["until"] - self._clock() + 0.999))
        return {"state": state["state"], "code": state.get("code"), "secondsLeft": left,
                "message": state.get("message", "")}

    def start_pairing(self):
        """Asks the console to pair; returns at once. A physical A press approves."""
        with self._lock:
            if self._sock is not None:
                raise ValueError("A session is open: this PC is already paired with the console")
            if self._pairing_active:
                raise ValueError("Pairing is already waiting for the console")
            link = self._ensure_link(force=True)
            if self._usb_state in ("stock", "old-launcher"):
                raise ValueError("Pairing needs launcher 1.1.2 or later running (switch the console off and on)")
            self._pairing_active = True
            self._pairing = {"state": "asking", "code": None, "until": None, "message": "Asking the console"}
        threading.Thread(target=self._pair, args=(link,), name="chromatic-usb-pair", daemon=True).start()
        return self.pairing_state()

    def _pair_result(self, state, message):
        self._pairing = {"state": state, "code": (self._pairing or {}).get("code"), "until": None,
                         "message": message}

    def _pair(self, link):
        try:
            pc_nonce = secrets.token_hex(16)
            link.clear_answers()
            link.write(f"CHR1 PAIR {pc_nonce}\n".encode("ascii"))
            line = link.answer(PAIR_ANSWER_S)
            parts = (line or "").split(" ")
            if line is None:
                return self._pair_result("failed", "The console did not answer (is launcher 1.1.2 running?)")
            if parts[:2] == ["CHR1", "PAIR-REFUSED"]:
                reason = parts[2] if len(parts) > 2 else "refused"
                return self._pair_result("refused", {
                    "key-present": "The console already has a provisioned key: Chromatic Manager wrote this PC's "
                                   "copy to local/chromatic/pairing.json",
                    "busy": "The console is busy (a session or another pairing)"}.get(reason, "Pairing refused"))
            if len(parts) != 4 or parts[:2] != ["CHR1", "PAIRING"] or not re.fullmatch(r"[0-9a-f]{32}", parts[2]) \
                    or not re.fullmatch(r"[0-9]{6}", parts[3]):
                return self._pair_result("failed", "Unexpected pairing answer")
            code = pair_code(parts[2], pc_nonce)
            if code != parts[3]:
                return self._pair_result("failed", "Pairing code mismatch: not the console this PC asked")
            self._pairing = {"state": "waiting", "code": code, "until": self._clock() + 60.0,
                             "message": "Check the code matches, then press A on the console to allow, B to refuse. "
                                        "With no cartridge the screen stays dark: press A anyway."}
            deadline = self._clock() + PAIR_WAIT_S
            while True:
                line = link.answer(max(0.05, deadline - self._clock()))
                if line is None:
                    if self._clock() >= deadline:
                        return self._pair_result("failed", "No answer from the console within 60 s")
                    continue
                if line == "CHR1 PAIR-DENIED":
                    return self._pair_result("denied", "Not allowed on the console (B, or 60 s passed)")
                match = re.fullmatch(r"PJ-PAIR ([0-9a-f]{64})", line)
                if match:
                    key_hex = match.group(1)
                    if self._on_paired is not None:
                        self._on_paired(key_hex)
                    self._key = bytes.fromhex(key_hex)
                    self._may_open = True
                    self._note("paired over USB (key kept in local/, valid until the console powers off)")
                    return self._pair_result("paired", "Paired. The key lasts until the console is switched off.")
        except OSError as exc:
            return self._pair_result("failed", f"USB link lost ({type(exc).__name__})")
        finally:
            self._pairing_active = False

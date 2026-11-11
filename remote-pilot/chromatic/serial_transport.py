# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""Phase 1 transport: read the launcher's text lines from USB serial. Read only.

The launcher prints `PJ-LAUNCHER-READY <version>` when it starts and
`PJ-LAUNCHER-TO-STOCK` just before it restarts into stock firmware. Anything
else on the port (ROM boot banner, stock firmware output) is kept as log text
after sanitizing and redaction.

Safety, because the stock FPGA drives ESP32_EN and ESP32_IO0 from the USB serial
control lines:
  * there is no write path anywhere in this module;
  * the real port is opened only when local/chromatic/config.json says
    "serialOpenAllowed": true, and only with DTR and RTS de-asserted before
    open and never toggled afterwards;
  * no flow control, no break, no baud-rate switching.
Even so, some USB-serial drivers pulse the control lines on open. Any reset
that is not a true power-on lands on stock firmware, so it cannot brick the
console, but it can interrupt a game. This has not been tried on hardware.
"""

import re
import threading
import time
from collections import deque

READY = "PJ-LAUNCHER-READY"
TO_STOCK = "PJ-LAUNCHER-TO-STOCK"
MAX_LINE_CHARS = 240
MAX_PENDING_BYTES = 4096
MAX_LINES = 2000
FRESH_SECONDS = 30.0
_MAC = re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b")
_LONG_HEX = re.compile(r"\b(?:0x)?[0-9A-Fa-f]{16,}\b")
_RESET = re.compile(r"\brst:0x[0-9a-fA-F]{1,2} \(([A-Z0-9_]{3,40})\)")
_VERSION = re.compile(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}")
_PORT_NAME = re.compile(r"(?:COM[1-9][0-9]{0,2}|/dev/tty(?:USB|ACM)[0-9]{1,3}|/dev/cu\.[A-Za-z0-9._-]{1,48})")
_WITHHELD_PREFIXES = ("PJ-PAIR", "PJ-PROVISION", "PJ-WIFI")


def sanitize(raw):
    """Return printable, bounded, redacted text for one received line."""
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", "replace")
    text = "".join(ch if ch == "\t" or " " <= ch <= "~" or ord(ch) > 0x9f else "" for ch in raw.rstrip("\r\n"))
    text = text.replace("\t", " ").strip()
    if text.startswith(_WITHHELD_PREFIXES):
        # Pairing or provisioning lines are secrets; never keep or show them.
        return "[pairing/provisioning line withheld]"
    text = _MAC.sub("[redacted]", text)
    text = _LONG_HEX.sub("[redacted]", text)
    return text[:MAX_LINE_CHARS]


class PySerialLineSource:
    """Real port reader. Constructed only after an explicit local opt-in."""

    def __init__(self, port, baudrate=115200, open_allowed=False):
        if open_allowed is not True:
            raise PermissionError("Opening the Chromatic serial port needs \"serialOpenAllowed\": true "
                                  "in local/chromatic/config.json, set by the user while present")
        if not isinstance(port, str) or not _PORT_NAME.fullmatch(port):
            raise ValueError("Serial port must look like COM5, /dev/ttyUSB0 or /dev/cu.usbserial-1")
        if baudrate != 115200:
            raise ValueError("The launcher console runs at 115200 baud")
        try:
            import serial  # pyserial, optional; imported only here
        except ImportError as exc:
            raise ValueError("Install pyserial to read a real serial port") from exc
        handle = serial.Serial()
        handle.port, handle.baudrate, handle.timeout = port, baudrate, 0.2
        handle.bytesize, handle.parity, handle.stopbits = serial.EIGHTBITS, serial.PARITY_NONE, serial.STOPBITS_ONE
        handle.xonxoff = handle.rtscts = handle.dsrdtr = False
        handle.dtr = False  # de-assert before open: these lines reach ESP32_EN / ESP32_IO0
        handle.rts = False
        handle.open()
        self._port = handle
        self._pending = b""

    def read_lines(self, timeout=0.2):
        data = self._port.read(1024)
        return self._split(data)

    def _split(self, data):
        self._pending += data
        lines = []
        while b"\n" in self._pending:
            line, self._pending = self._pending.split(b"\n", 1)
            lines.append(line)
        if len(self._pending) > MAX_PENDING_BYTES:
            lines.append(self._pending[:MAX_LINE_CHARS])
            self._pending = b""
        return lines

    def close(self):
        self._port.close()


class SerialTransport:
    """Background reader that turns launcher lines into status and a log tail."""

    kind = "serial"
    capabilities = frozenset({"status", "log"})

    def __init__(self, source_factory, label="USB serial", simulated=False,
                 clock=time.monotonic, wall=time.time):
        self._factory = source_factory
        self.label = label
        self.simulated = simulated
        self._clock, self._wall = clock, wall
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._source = None
        self._lines = deque(maxlen=MAX_LINES)
        self._phase = "unknown"
        self._version = None
        self._reset = None
        self._last_line = None
        self._handovers = 0
        self._error = ""
        self._pair_sink = None

    def arm_pairing(self, sink):
        """Hand the next `PJ-PAIR <64 hex>` line to sink(key_hex) instead of dropping it.

        The line is still never logged or shown. sink=None disarms."""
        with self._lock:
            self._pair_sink = sink

    @property
    def pairing_armed(self):
        return self._pair_sink is not None

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="chromatic-serial", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._close_source()

    def _close_source(self):
        source, self._source = self._source, None
        if source is not None:
            try:
                source.close()
            except Exception:
                pass

    def _run(self):
        while not self._stop.is_set():
            if self._source is None:
                try:
                    self._source = self._factory()
                    with self._lock:
                        self._error = ""
                except Exception as exc:
                    with self._lock:
                        self._error = f"{type(exc).__name__}: {exc}"[:160]
                    self._stop.wait(2.0)
                    continue
            try:
                for line in self._source.read_lines(0.2):
                    self.ingest(line)
            except Exception as exc:
                with self._lock:
                    self._error = f"Serial read stopped: {type(exc).__name__}"
                self._close_source()
                self._stop.wait(1.0)

    # ---- parsing ---------------------------------------------------------
    def ingest(self, raw):
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8", "replace")
        candidate = raw.strip()
        if candidate.startswith("PJ-PAIR "):
            key = candidate[len("PJ-PAIR "):]
            with self._lock:
                sink, self._pair_sink = self._pair_sink, None
            if sink is not None and re.fullmatch(r"[0-9a-f]{64}", key):
                sink(key)
        text = sanitize(raw)
        if not text:
            return
        with self._lock:
            self._last_line = self._clock()
            self._lines.append(f"{time.strftime('%H:%M:%S', time.localtime(self._wall()))} {text}")
            reset = _RESET.search(text)
            if reset:
                self._phase, self._reset = "booting", reset.group(1)
            if text.startswith(READY):
                version = text[len(READY):].strip()
                self._version = version if _VERSION.fullmatch(version) else None
                self._phase = "launcher"
            elif text == TO_STOCK:
                self._phase = "stock"
                self._handovers += 1
            elif text.startswith("mcu>") and self._phase != "launcher":
                self._phase = "stock"

    # ---- reads -----------------------------------------------------------
    def snapshot(self):
        with self._lock:
            age = None if self._last_line is None else max(0.0, self._clock() - self._last_line)
            return {
                "kind": self.kind, "label": self.label, "simulated": self.simulated,
                "open": self._source is not None,
                "connected": self._source is not None and age is not None and age < FRESH_SECONDS,
                "session": False, "phase": self._phase, "launcherVersion": self._version,
                "resetReason": self._reset, "handovers": self._handovers,
                "lastLineAgeS": None if age is None else round(age, 1),
                "lines": len(self._lines), "deviceCapabilities": sorted(self.capabilities),
                "error": self._error,
            }

    def log_text(self, max_bytes=65536):
        with self._lock:
            lines = list(self._lines)
        out, size = [], 0
        for line in reversed(lines):
            size += len(line.encode("utf-8")) + 1
            if size > max_bytes:
                break
            out.append(line)
        return "\n".join(reversed(out)) + ("\n" if out else "")

    def tail(self, count=40):
        with self._lock:
            return list(self._lines)[-count:]

    def clear(self):
        with self._lock:
            self._lines.clear()

    def screen(self):
        raise ValueError("USB serial is read-only text: launcher 1.1.0 has no screen mirror")

    def press(self, button, ms):
        raise ValueError("USB serial is read-only in phase 1: no input is ever written to the port")

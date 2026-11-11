# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""A simulated console on a simulated USB serial cable. No hardware involved.

It behaves like launcher 1.1.2's USB side (CHROMATIC-REMOTE-CONTROL.md): boot
lines and once-a-second `PJ-DIAG` lines, `CHR1 OPEN` answered with HELLO,
NOKEY, BUSY or nothing (stock), USB pairing with a 6-digit code approved by a
"physical" A press (approve() / deny(), or auto_approve_s for the viewer's demo),
one session across USB and Wi-Fi, and silence towards the PC after the hand-over
to stock. The session itself is chromatic.simulator.SimulatedLauncher's CHR1
server, run over a socket pair, so USB and Wi-Fi share one handler as on the
console. Everything it draws says SIMULATOR.

The object is a port: read(n), write(data) and close(), like usb_transport.PySerialPort;
close() is the cable coming out (the console keeps running); stop() switches it off.
"""

import secrets
import socket
import threading
import time

from . import protocol as p
from .simulator import SimulatedLauncher
from .usb_transport import pair_code


class SimulatedUsbConsole:
    def __init__(self, key=None, version="1.1.2", countdown_s=5.0, lan=False, auto_approve_s=None,
                 clock=time.monotonic):
        self.key = key
        self._clock = clock
        self.launcher = SimulatedLauncher(key or secrets.token_bytes(p.KEY_BYTES), version=version,
                                          countdown_s=countdown_s)
        if lan:
            self.launcher.start()  # its Wi-Fi listener; the key is checked per connection
        self.version = version
        self.auto_approve_s = auto_approve_s
        self._cond = threading.Condition()
        self._out = bytearray()
        self._in = b""
        self._state = "launcher"  # launcher | session | stock
        self._pc = None           # the PC end of the session socket pair
        self._pairing = None
        self.written = []         # every line the PC wrote (tests check what reached the console)
        self.closed = False
        self._started = clock()
        self._say(b"ets Jun  8 2016 00:22:57")
        self._say(b"rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)")
        self._say(f"PJ-LAUNCHER-READY {version}".encode())
        self._say(f"PJ-DIAG t=0 boot version={version} reset=poweron stock-suspect=0 free-heap=251000".encode())
        self._last_stats = clock()
        self._thread = threading.Thread(target=self._run, name="chromatic-usb-sim", daemon=True)
        self._thread.start()

    # ---- the port --------------------------------------------------------------------
    def read(self, size):
        with self._cond:
            if not self._out and not self.closed:
                self._cond.wait(0.05)
            data = bytes(self._out[:size])
            del self._out[:size]
            return data

    def write(self, data):
        if self.closed:
            raise OSError("the simulated console is off")
        self._in += bytes(data)
        while b"\n" in self._in:
            line, self._in = self._in.split(b"\n", 1)
            self.written.append(line)
            self._line(line.decode("ascii", "replace"))

    def tick(self):
        self.launcher.tick()

    def close(self):
        """The PC closed the port (or the cable came out): a USB session ends; the console
        keeps running, and its Wi-Fi listener with it."""
        self._end_session()

    def stop(self):
        """Switch the simulated console off."""
        if self.closed:
            return
        self.closed = True
        self._end_session()
        self.launcher.stop()
        with self._cond:
            self._cond.notify_all()

    # ---- "physical" buttons (the owner) ---------------------------------------------------
    def approve(self):
        self._answer_pairing(True)

    def deny(self):
        self._answer_pairing(False)

    # ---- device behaviour ------------------------------------------------------------------
    def _say(self, line):
        with self._cond:
            self._out += line + b"\n"
            self._cond.notify_all()

    def _uptime_ms(self):
        return int((self._clock() - self._started) * 1000)

    def _line(self, text):
        if self._state == "stock":
            return  # stock firmware: the launcher is gone, nothing answers
        if text == "CHR1 OPEN":
            self._end_session()  # a new OPEN ends a session, as on the console
            if self._pairing:
                return
            if self.launcher._busy:
                self._say(b"CHR1 BUSY")
            elif self.key is None:
                self._say(b"CHR1 NOKEY")
            else:
                self._begin_session()
            return
        if self._state == "session":
            try:
                self._pc.sendall(text.encode("ascii") + b"\n")
            except OSError:
                self._end_session()
            return
        if text.startswith("CHR1 PAIR "):
            nonce = text[len("CHR1 PAIR "):]
            if self.key is not None:
                self._say(b"CHR1 PAIR-REFUSED key-present")
            elif self._pairing or self.launcher._busy:
                self._say(b"CHR1 PAIR-REFUSED busy")
            elif len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce):
                self._say(b"CHR1 PAIR-REFUSED bad-nonce")
            else:
                device = secrets.token_hex(16)
                self._pairing = {"until": self._clock() + 60.0, "at": self._clock()}
                self.pair_code = pair_code(device, nonce)
                self.launcher._say("pairing: asking the owner")
                self._say(f"CHR1 PAIRING {device} {self.pair_code}".encode())
        # anything else outside a session is ignored, as on the console

    def _answer_pairing(self, allow):
        if not self._pairing:
            return
        self._pairing = None
        if allow:
            self.key = secrets.token_bytes(p.KEY_BYTES)
            self.launcher.key = self.key
            self.launcher._say("pairing: allowed with A")
            self._say(b"PJ-PAIR " + self.key.hex().encode())
        else:
            self.launcher._say("pairing: refused")
            self._say(b"CHR1 PAIR-DENIED")

    def _begin_session(self):
        self.launcher.key = self.key
        device_end, self._pc = socket.socketpair()
        self.launcher._busy = True
        self._state = "session"
        self.launcher._say("session open (usb)")
        threading.Thread(target=self.launcher._serve, args=(device_end,), daemon=True).start()
        threading.Thread(target=self._pump, args=(self._pc,), daemon=True).start()

    def _pump(self, pc):
        try:
            while True:
                data = pc.recv(65536)
                if not data:
                    break
                with self._cond:
                    self._out += data
                    self._cond.notify_all()
        except OSError:
            pass
        if self._pc is pc:
            self._end_session()

    def _end_session(self):
        pc, self._pc = self._pc, None
        if pc is not None:
            try:
                pc.close()
            except OSError:
                pass
        if self._state == "session":
            self._state = "launcher"

    def _run(self):
        while not self.closed:
            time.sleep(0.05)
            launcher = self.launcher
            launcher.tick()
            if self._pairing:
                if self.auto_approve_s is not None and self._clock() - self._pairing["at"] >= self.auto_approve_s:
                    self.approve()
                elif self._pairing and self._clock() >= self._pairing["until"]:
                    self._answer_pairing(False)
            if self._state == "stock" or self._state == "session" and not self._pc:
                continue
            if launcher.phase == "handover" and not launcher._busy:
                time.sleep(0.05)
                self._state = "stock"
                self._say(f"PJ-DIAG t={self._uptime_ms()} handover to=stock done=closed uptime-ms="
                          f"{self._uptime_ms()}".encode())
                self._say(b"PJ-LAUNCHER-TO-STOCK")
                self._say(b"ets Jun  8 2016 00:22:57")
                self._say(b"rst:0xc (SW_CPU_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)")
                self._say(b"mcu>")
                continue
            if self._state == "launcher" and self._clock() - self._last_stats >= 1.0:
                self._last_stats = self._clock()
                self._say(f"PJ-DIAG t={self._uptime_ms()} stats phase=menu uart=60 msgs=10 polls=10 pokes=2 "
                          f"frames=2 btn=0x0000 heap=250000 heap-min=248000 unsafe=0".encode())

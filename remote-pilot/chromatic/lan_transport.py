# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""CHR1 client: Remote Pilot's side of the paired local-LAN Chromatic link.

Launcher 1.1.2 or newer serves CHR1 on TCP 38771 after the owner presses A on
the Jellyfin screen; the same session code runs over USB serial in
usb_transport.py. Tests drive it against chromatic.simulator and the launcher's
PC harness.
See presets/chromatic/PROTOCOL.md and LAUNCHER-SIDE.md.

Rules kept here: private IPv4 only (loopback only for the simulator), bounded
timeouts, one request in flight, and a timed-out or failed `input`/`hold` is
never resent. Reconnection happens lazily on the next status poll. The device
releases a hold by itself when its 600 ms lease is not refreshed.
"""

import json
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import ipaddress

from . import protocol as p
from .video import Decoder

STATUS_FIELDS = {"phase": str, "target": str, "version": str, "screen": str, "selected": str,
                 "countdownS": (int, type(None)), "pcSession": bool, "held": str, "uptimeMs": int,
                 # launcher 1.1.2 adds these (CHROMATIC-REMOTE-CONTROL.md); all optional
                 "transport": str, "screenVisible": bool, "leaseLeftMs": int, "service": str,
                 "servicePct": int, "key": str}
PERF_FIELDS = ("fps", "frameMs", "workMs", "timestampMs", "heapFreeKb")


def parse_status(body):
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise p.ProtocolError("Launcher status is not JSON") from exc
    if not isinstance(value, dict):
        raise p.ProtocolError("Launcher status must be an object")
    result = {}
    for name, kind in STATUS_FIELDS.items():
        if name in value:
            item = value[name]
            if isinstance(item, bool) and kind is int or not isinstance(item, kind):
                raise p.ProtocolError(f"Launcher status field {name} has the wrong type")
            if isinstance(item, str):
                item = item[:32]
            result[name] = item
    if result.get("phase") not in p.PHASES:
        raise p.ProtocolError("Launcher status has an unknown phase")
    if result.setdefault("target", "game" if result["phase"] == "game" else "launcher") not in ("launcher", "game"):
        raise p.ProtocolError("Launcher status has an unknown target")
    if "version" in result and not p.VERSION.fullmatch(result["version"]):
        raise p.ProtocolError("Launcher status has an invalid version")
    return result


def parse_perf(body):
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise p.ProtocolError("Timing sample is not JSON") from exc
    if not isinstance(value, dict):
        raise p.ProtocolError("Timing sample must be an object")
    result = {}
    for name in PERF_FIELDS:
        number = value.get(name)
        if number is None and name == "heapFreeKb":
            continue
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not 0 <= number <= 1e13:
            raise p.ProtocolError(f"Timing field {name} is invalid")
        result[name] = number
    return result


class LanTransport:
    kind = "lan"
    speaks_chr1 = True

    def __init__(self, host, port, key, label="Wi-Fi (CHR1)", simulated=False,
                 allow_loopback=False, timeout=3.0, clock=time.monotonic):
        if not p.allowed_host(host, allow_loopback):
            raise ValueError("CHR1 connects only to a private LAN IPv4 address")
        if not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError("CHR1 port must be 1024..65535")
        if not isinstance(key, bytes) or len(key) != p.KEY_BYTES:
            raise ValueError("CHR1 needs a 32-byte pairing key")
        self.host, self.port = host, port
        self._init_state(key, label, simulated, timeout, clock)

    def _init_state(self, key, label, simulated, timeout, clock):
        """Session state shared with the USB transport (usb_transport.py)."""
        self._key = key
        self.label, self.simulated = label, simulated
        self._timeout, self._clock = timeout, clock
        self._lock = threading.Lock()
        self._sock = self._stream = self._session = None
        self._seq = 0
        self._caps = frozenset()
        self._version = None
        self._status = None
        self._perf = None
        self._last_ok = None
        self._error = ""
        self._log = []
        self._remote_tail = []
        self._decoder = Decoder()
        self._audio_seq = 0

    @property
    def capabilities(self):
        return self._caps

    # ---- connection --------------------------------------------------------
    def _connect(self):
        sock = socket.create_connection((self.host, self.port), timeout=self._timeout)
        try:
            sock.settimeout(self._timeout)
            stream = sock.makefile("rb")
            device_nonce, version = p.parse_hello(p.read_line(stream))
            pc_nonce = p.new_nonce()
            sock.sendall(p.auth_line(self._key, device_nonce, pc_nonce))
            caps = p.parse_ok(self._key, p.read_line(stream), pc_nonce, device_nonce)
        except Exception:
            if "stream" in locals():
                stream.close()
            sock.close()
            raise
        self._sock, self._stream = sock, stream
        self._session = p.session_key(self._key, device_nonce, pc_nonce)
        self._seq, self._caps, self._version = 0, caps, version
        self._decoder.reset()  # video state is per session

    def _drop(self):
        sock, stream = self._sock, self._stream
        self._sock, self._stream, self._session = None, None, None
        self._caps = frozenset()
        if stream is not None:
            stream.close()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _call(self, action, arg="-", connect=True):
        with self._lock:
            if self._sock is None:
                if not connect:
                    raise ValueError("No launcher session")
                self._connect()
            self._seq += 1
            seq = self._seq
            try:
                self._sock.sendall(p.encode_request(self._session, seq, action, arg))
                code, body = p.read_response(self._session, self._stream, seq, action)
            except (OSError, p.ProtocolError):
                self._drop()
                raise
            self._last_ok = self._clock()
            return code, body

    @staticmethod
    def _expect_ok(code, body):
        if code != "ok":
            raise ValueError(f"Device {code}: {body.decode('utf-8', 'replace')[:p.MAX_REASON]}")
        return body

    def _note(self, text):
        self._log.append(f"{time.strftime('%H:%M:%S')} [pc] {text}")
        del self._log[:-200]

    # ---- transport interface -------------------------------------------------
    def start(self):
        pass

    def stop(self):
        self._end_session()

    def _end_session(self):
        with self._lock:
            if self._sock is not None:
                try:
                    self._seq += 1
                    self._sock.sendall(p.encode_request(self._session, self._seq, "bye"))
                except (OSError, p.ProtocolError):
                    pass
            self._drop()

    def poll(self):
        """Called from the adapter's one-second status loop, never from HTTP."""
        try:
            code, body = self._call("status")
            self._status = parse_status(self._expect_ok(code, body))
            if "log" in self._caps and self._want_log():
                code, body = self._call("log", "4096", connect=False)
                self._remote_tail = self._expect_ok(code, body).decode("utf-8", "replace").splitlines()[-40:]
            if "perf" in self._caps:
                code, body = self._call("perf", connect=False)
                self._perf = parse_perf(self._expect_ok(code, body)) if code == "ok" else None
            self._error = ""
            if self._status.get("phase") == "handover":
                self._note("device reported hand-over to stock")
                self._end_session()
        except (OSError, ValueError) as exc:
            self._error = f"{type(exc).__name__}: {exc}"[:160]
            self._drop()

    def _want_log(self):
        """Whether this status poll also reads the RAM log tail (USB eases off while the console is slow)."""
        return True

    def snapshot(self):
        session = self._sock is not None
        age = None if self._last_ok is None else round(max(0.0, self._clock() - self._last_ok), 1)
        status = self._status or {}
        phase = {"menu": "launcher", "media": "launcher", "other-firmware": "launcher", "game": "game",
                 "handover": "stock"}.get(status.get("phase"), "unknown")
        return {
            "kind": self.kind, "label": self.label, "simulated": self.simulated, "open": session,
            "connected": session and age is not None and age < 5, "session": session,
            "phase": phase if session or phase == "stock" else "unknown",
            "target": status.get("target", "launcher"),
            "launcherVersion": self._version, "launcherScreen": status.get("screen"),
            "selected": status.get("selected"), "countdownS": status.get("countdownS"),
            "held": status.get("held", "NONE"),
            "resetReason": None, "handovers": 0, "lastLineAgeS": age,
            "lines": len(self._remote_tail) or len(self._log),
            "deviceCapabilities": sorted(self._caps), "error": self._error,
            "host": self.host,
            "deviceTransport": status.get("transport"), "screenVisible": status.get("screenVisible"),
            "leaseLeftMs": status.get("leaseLeftMs"), "service": status.get("service"),
            "keyKind": status.get("key"),
        }

    def ping(self):
        code, body = self._call("ping", connect=False)
        return self._expect_ok(code, body).decode("ascii", "replace")

    def perf(self):
        return dict(self._perf) if self._perf and self._sock is not None else None

    def log_text(self, max_bytes=p.MAX_LOG):
        if "log" not in self._caps:
            raise ValueError("The device did not offer its log over CHR1")
        code, body = self._call("log", str(min(max_bytes, p.MAX_LOG)), connect=False)
        return self._expect_ok(code, body).decode("utf-8", "replace")

    def tail(self, count=40):
        # Cached by poll(); HTTP threads never wait on the network for this.
        return (self._remote_tail or self._log)[-count:]

    def clear(self):
        self._log.clear()
        self._remote_tail = []

    def screen(self):
        # A screenshot only reads: if the session ended a moment ago (a launcher before 1.3.2 closes it after a reply
        # that took over 15 s), it opens a new one instead of failing with "No launcher session".
        code, body = self._call("screen", connect=True)
        return self._expect_ok(code, body)

    def video_frame(self):
        """Next frame through CHV1: deltas while the chain holds, else a keyframe."""
        code, body = self._call("video", self._decoder.want, connect=False)
        data = self._expect_ok(code, body)
        try:
            return self._decoder.feed(data)
        except ValueError:
            self._decoder.reset()
            code, body = self._call("video", "key", connect=False)
            return self._decoder.feed(self._expect_ok(code, body))

    @property
    def video_bytes(self):
        return self._decoder.bytes_in

    def _after_input(self, result, names):
        if result == "handover":  # the console restarts into stock and drops the link
            self._status = {**(self._status or {}), "phase": "handover"}
            self._note(f"{'+'.join(names)} handed the launcher over to stock")
            self._end_session()
        return result

    def press(self, buttons, ms):
        names = p.parse_buttons("+".join(buttons) if isinstance(buttons, (list, tuple)) else buttons)
        arg = p.validate_arg("input", f"{p.buttons_arg(names)}:{ms}")
        # Exactly one attempt. A timeout leaves the outcome unknown; never resend.
        code, body = self._call("input", arg, connect=False)
        return self._after_input(self._expect_ok(code, body).decode("ascii", "replace"), names)

    def hold(self, buttons):
        names = p.parse_buttons(p.buttons_arg(tuple(buttons)), allow_none=True)
        code, body = self._call("hold", p.buttons_arg(names), connect=False)
        return self._after_input(self._expect_ok(code, body).decode("ascii", "replace"), names)

    def console(self, command):
        code, body = self._call("console", p.validate_arg("console", command), connect=False)
        return self._expect_ok(code, body).decode("utf-8", "replace")

    def audio(self):
        """Newest sound chunk after the last one received, or b'' when none is new."""
        code, body = self._call("audio", str(self._audio_seq), connect=False)
        data = self._expect_ok(code, body)
        if len(data) < 4 or (len(data) - 4) % 2:
            raise ValueError("Invalid audio chunk")
        number = struct.unpack_from("<I", data)[0]
        if number <= self._audio_seq:
            return b""
        self._audio_seq = number
        return data[4:]


def probe(host, port, key, timeout=0.4, allow_loopback=False):
    """True when host completes a CHR1 handshake with this key. Closes at once."""
    link = None
    try:
        link = LanTransport(host, port, key, allow_loopback=allow_loopback, timeout=timeout)
        link._connect()
        return True
    except (OSError, ValueError):
        return False
    finally:
        if link is not None:
            link.stop()


def discover(pc_host, port, key, candidates=None, workers=32, timeout=0.4, allow_loopback=False):
    """Find the paired console on the PC's private /24 (like Remote Pilot's reverse pairing)."""
    if candidates is None:
        if not p.allowed_host(pc_host):
            raise ValueError("Discovery needs this PC's private LAN IPv4 address")
        network = ipaddress.IPv4Network(f"{pc_host}/24", strict=False)
        candidates = [str(address) for address in network.hosts() if str(address) != pc_host]
    candidates = list(candidates)[:254]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for host, ok in zip(candidates, pool.map(lambda h: probe(h, port, key, timeout, allow_loopback), candidates)):
            if ok:
                return host
    return None

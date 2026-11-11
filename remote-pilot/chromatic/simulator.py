# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""Host-side simulator of the Chromatic endpoints. No hardware involved.

SimulatedSerialDevice  a line source that replays a plausible power-on transcript
                       (ROM banner, launcher marker, hand-over, stock prompt).
SimulatedLauncher      a CHR1 server on loopback that behaves like the launcher
                       described in LAUNCHER-SIDE.md: Games / Jellyfin / Other
                       firmware menu, 5 s countdown paused by a PC session,
                       bounded button pulses and holds, a 160x144 RGB565 frame,
                       CHV1 video, timing, a read-only console and a RAM log.

game_bridge=False (default) matches what launcher work alone can deliver: picking
Games or MENU hands over to stock and the link closes. game_bridge=True simulates
a future device whose Gowin FPGA update exposes the game picture, sound and
buttons (game-* capabilities); picking Games then starts a simulated game. No
such FPGA image exists. Everything drawn says SIMULATOR or SIMULATED GAME so an
image can never be mistaken for the console.

Run `python -m chromatic.simulator --key-file K` to start one for manual tests.
"""

import json
import math
import socket
import struct
import threading
import time
from collections import deque

from . import protocol as p
from . import video

TRANSCRIPT = (
    (0.0, "ets Jun  8 2016 00:22:57"),
    (0.0, "rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)"),
    (0.1, "I (31) boot: power-on reset, starting launcher slot"),
    (0.2, ""),
    (0.0, "PJ-LAUNCHER-READY 1.1.0"),
    (1.0, "menu: countdown 5 s, default Games"),
    (5.0, "PJ-LAUNCHER-TO-STOCK"),
    (0.1, "ets Jun  8 2016 00:22:57"),
    (0.0, "rst:0xc (SW_CPU_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)"),
    (0.5, "mcu>"),
)


class SimulatedSerialDevice:
    """LineSource stand-in for PySerialLineSource. Read only, like the real one."""

    def __init__(self, transcript=TRANSCRIPT, speed=1.0, clock=time.monotonic, sleep=time.sleep):
        self._script = list(transcript)
        self._speed, self._clock, self._sleep = speed, clock, sleep
        self._due = clock()
        self._closed = False

    def read_lines(self, timeout=0.2):
        if self._closed:
            raise OSError("closed")
        lines = []
        while self._script and self._clock() >= self._due:
            _, line = self._script.pop(0)
            lines.append(line.encode("ascii"))
            if self._script:
                self._due = self._clock() + self._script[0][0] / self._speed
        if not lines:
            self._sleep(timeout)
        return lines

    def close(self):
        self._closed = True


# ---- tiny 3x5 font so mirrored frames are readable -----------------------------
_FONT = {
    "A": "010101111101101", "B": "110101110101110", "C": "011100100100011", "D": "110101101101110",
    "E": "111100110100111", "F": "111100110100100", "G": "011100101101011", "H": "101101111101101",
    "I": "111010010010111", "J": "001001001101010", "K": "101101110101101", "L": "100100100100111",
    "M": "101111111101101", "N": "110101101101101", "O": "010101101101010", "P": "110101110100100",
    "Q": "010101101110011", "R": "110101110101101", "S": "011100010001110", "T": "111010010010010",
    "U": "101101101101111", "V": "101101101101010", "W": "101101111111101", "X": "101101010101101",
    "Y": "101101010010010", "Z": "111001010100111", "0": "111101101101111", "1": "010110010010111",
    "2": "110001010100111", "3": "110001010001110", "4": "101101111001001", "5": "111100110001110",
    "6": "011100111101111", "7": "111001010010010", "8": "111101111101111", "9": "111101111001110",
    ".": "000000000000010", "-": "000000111000000", ":": "000010000010000", ">": "100010001010100",
    "+": "000010111010000", " ": "000000000000000",
}


def rgb565(hex_color):
    value = int(hex_color.lstrip("#"), 16)
    return (value >> 19 & 0x1F) << 11 | (value >> 10 & 0x3F) << 5 | (value >> 3 & 0x1F)


INK = {name: rgb565(color) for name, color in {
    "bg": "#120c22", "panel": "#22183c", "text": "#f2eefc", "muted": "#8f86aa",
    "controls": "#00ccff", "system": "#ff3399", "palette": "#924ed7", "jellyfin": "#00a4dc",
    "dark": "#05030a", "grass": "#2f6b3a", "sky": "#1b3d6e"}.items()}
ITEMS = ("games", "jellyfin", "other-firmware")
LABELS = {"games": "GAMES", "jellyfin": "JELLYFIN", "other-firmware": "OTHER FIRMWARE"}


class Frame:
    def __init__(self, color):
        self.pixels = [color] * (p.SCREEN_WIDTH * p.SCREEN_HEIGHT)

    def rect(self, x, y, w, h, color):
        for row in range(max(0, y), min(p.SCREEN_HEIGHT, y + h)):
            start = row * p.SCREEN_WIDTH
            for col in range(max(0, x), min(p.SCREEN_WIDTH, x + w)):
                self.pixels[start + col] = color

    def text(self, x, y, value, color, scale=1):
        for char in value.upper():
            glyph = _FONT.get(char, _FONT[" "])
            for index, bit in enumerate(glyph):
                if bit == "1":
                    self.rect(x + index % 3 * scale, y + index // 3 * scale, scale, scale, color)
            x += 4 * scale

    def bytes(self):
        return struct.pack(f"<{len(self.pixels)}H", *self.pixels)


class SimulatedLauncher:
    """CHR1 device side on loopback. One client at a time, like the spec."""

    def __init__(self, key, version="1.1.0", capabilities=p.LAUNCHER_CAPABILITIES, countdown_s=5.0,
                 game_bridge=False, host="127.0.0.1", port=0, clock=time.monotonic):
        if not p.allowed_host(host, allow_loopback=True):
            raise ValueError("The simulator listens on loopback or a private address only")
        self.key, self.version = key, version
        self.game_bridge = game_bridge
        self.capabilities = frozenset(capabilities) | (frozenset(p.GAME_CAPABILITIES) if game_bridge else frozenset())
        self._clock = clock
        self._started = clock()
        self._countdown_s = countdown_s
        self._countdown_end = self._started + countdown_s
        self._paused_left = None
        self._stopped = False
        self._lock = threading.RLock()
        self.screen, self.selected, self.phase = "menu", 0, "menu"
        self.sprite = [72, 64]
        self.held, self._hold_until = (), 0.0
        self.log = deque(maxlen=200)
        self.presses = []
        self.failed_auth = 0
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((host, port))
        self._server.listen(2)
        self._server.settimeout(0.2)
        self.host, self.port = self._server.getsockname()
        self._busy = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, name="chromatic-sim", daemon=True)
        self._say(f"PJ-LAUNCHER-READY {version}")

    # ---- lifecycle -----------------------------------------------------------
    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        try:
            self._server.close()
        except OSError:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def _say(self, line):
        self.log.append(f"{(self._clock() - self._started):7.2f} {line}")

    # ---- launcher behaviour -----------------------------------------------------
    def countdown_left(self):
        if self.phase != "menu" or self.screen != "menu" or self._stopped:
            return None
        if self._paused_left is not None:
            return int(self._paused_left + 0.999)
        return max(0, int(self._countdown_end - self._clock() + 0.999))

    def tick(self):
        with self._lock:
            if self.held and self._clock() >= self._hold_until:
                self.held = ()  # lease expired: release, as the device must
                self._say("hold lease expired: released")
            if self.phase == "menu" and self._paused_left is None and self.countdown_left() == 0:
                self._start_game("countdown reached 0, default Games")

    def _handover(self, reason):
        self.phase = "handover"
        self._say(reason)
        self._say("PJ-LAUNCHER-TO-STOCK")

    def _start_game(self, reason):
        if self.game_bridge:
            self.phase, self.screen = "game", "game"
            self._say(reason + " (simulated FPGA game bridge)")
        else:
            self._handover(reason)

    def _pause_countdown(self):
        if self._paused_left is None and self.phase == "menu":
            self._paused_left = max(0.0, self._countdown_end - self._clock())
            self._say("pc session: countdown paused")

    def _apply(self, button):
        """One button edge, through the same logic a physical press uses. Caller holds the lock."""
        self.presses.append(button)
        self._stopped = True  # any button stops the countdown, as on the console
        if self.phase == "game":
            step = {"UP": (0, -4), "DOWN": (0, 4), "LEFT": (-4, 0), "RIGHT": (4, 0)}.get(button)
            if step:
                self.sprite = [min(148, max(0, self.sprite[0] + step[0])),
                               min(120, max(14, self.sprite[1] + step[1]))]
            return None
        if button == "MENU":
            self._handover("menu button: hand over to stock")
            return "handover"
        if self.screen == "menu" and button in ("UP", "DOWN"):
            self.selected = (self.selected + (1 if button == "DOWN" else -1)) % len(ITEMS)
            self._say(f"menu: selected {ITEMS[self.selected]}")
        elif self.screen == "menu" and button in ("A", "START"):
            choice = ITEMS[self.selected]
            if choice == "games":
                self._start_game("menu: Games")
                return "handover" if self.phase == "handover" else None
            self.screen = choice
            self._say(f"menu: opened {choice}")
        elif self.screen != "menu" and button == "B":
            self.screen = "menu"
            self._say("back to menu")
        return None

    def press(self, buttons):
        with self._lock:
            if self.phase == "handover":
                return "refused", "launcher has handed over to stock"
            result = None
            for button in buttons:
                result = self._apply(button) or result
                if self.phase == "handover":
                    break
            return "ok", result or "queued"

    def hold(self, buttons):
        with self._lock:
            if self.phase == "handover":
                return "refused", "launcher has handed over to stock"
            for button in buttons:
                if button not in self.held:
                    self._apply(button)  # a new hold is a press edge
            self.held = tuple(buttons)
            self._hold_until = self._clock() + p.HOLD_LEASE_MS / 1000
            if self.phase == "handover":
                return "ok", "handover"
            return "ok", "held" if buttons else "released"

    def status(self):
        with self._lock:
            phase = ("handover" if self.phase == "handover" else "game" if self.phase == "game" else
                     "menu" if self.screen == "menu" else "media" if self.screen == "jellyfin" else "other-firmware")
            return {"phase": phase, "target": "game" if phase == "game" else "launcher",
                    "version": self.version, "screen": self.screen, "selected": ITEMS[self.selected],
                    "countdownS": self.countdown_left(), "pcSession": self._busy,
                    "held": "+".join(self.held) or "NONE",
                    "uptimeMs": int((self._clock() - self._started) * 1000)}

    def perf(self):
        now = self._clock() - self._started
        game = self.phase == "game"
        return {"fps": 59.7 if game else 10.0, "frameMs": 16.7 if game else 100.0,
                "workMs": round(4.0 + 1.5 * math.sin(now), 2), "timestampMs": int(now * 1000),
                "heapFreeKb": 182, "source": "simulator"}

    def console(self, command):
        status = self.status()
        return {
            "help": "commands: " + " ".join(p.CONSOLE_COMMANDS),
            "version": f"launcher {self.version} (simulator)",
            "uptime": f"{status['uptimeMs'] // 1000} s",
            "heap": "free 182 KiB (simulator)",
            "wifi": "joined: simulated network, rssi -48 dBm",
            "phase": f"{status['phase']} / {status['target']}",
            "reset-reason": "POWERON_RESET (simulator)",
        }[command]

    def audio_chunk(self, since):
        """Simulated game sound: 0.25 s chunks of a quiet tone; none outside games."""
        number = max(1, int((self._clock() - self._started) * 4))
        if number <= since:
            return struct.pack("<I", since)
        samples = 4096  # 0.25 s at 16,384 Hz = 8,192 bytes
        tone = 330 if self.held else 220
        pcm = struct.pack(f"<{samples}h", *(int(1200 * math.sin(2 * math.pi * tone * (number * samples + i) / p.AUDIO_RATE))
                                         for i in range(samples)))
        return struct.pack("<I", number) + pcm

    def frame(self):
        with self._lock:
            image = Frame(INK["bg"])
            if self.phase == "game":
                return self._game_frame(image)
            image.rect(0, 0, 160, 12, INK["panel"])
            image.text(4, 3, "SIMULATOR", INK["system"])
            image.text(156 - 4 * len(self.version), 3, self.version, INK["muted"])
            if self.screen == "menu":
                for row, item in enumerate(ITEMS):
                    y = 24 + row * 30
                    chosen = row == self.selected
                    image.rect(8, y, 144, 24, INK["controls"] if chosen else INK["panel"])
                    if item == "jellyfin":
                        image.rect(12, y + 6, 12, 12, INK["jellyfin"])
                    image.text(30, y + 7, LABELS[item], INK["dark"] if chosen else INK["text"],
                               2 if len(LABELS[item]) < 10 else 1)
                left = self.countdown_left()
                if left is not None:
                    width = int(144 * left / max(1.0, self._countdown_s))
                    image.rect(8, 126, 144, 6, INK["panel"])
                    image.rect(8, 126, width, 6, INK["palette"])
                    image.text(8, 134, f"GAMES IN {left}" if self._paused_left is None else "PC SESSION", INK["muted"])
            elif self.screen == "jellyfin":
                image.rect(8, 20, 144, 100, INK["panel"])
                image.rect(12, 26, 24, 24, INK["jellyfin"])
                image.text(42, 32, "JELLYFIN", INK["text"], 2)
                image.text(14, 60, "WI-FI: SIMULATED", INK["muted"])
                image.text(14, 72, "PC PAIRING: SIMULATED", INK["muted"])
                image.text(14, 104, "B: BACK", INK["controls"])
            else:
                image.rect(8, 20, 144, 100, INK["panel"])
                image.text(14, 28, "OTHER FIRMWARE", INK["palette"])
                image.text(14, 44, "> STOCK", INK["text"])
                image.text(14, 56, "  EMPTY SLOT", INK["muted"])
                image.text(14, 68, "  EMPTY SLOT", INK["muted"])
                image.text(14, 104, "B: BACK", INK["controls"])
            return image.bytes()

    def _game_frame(self, image):
        t = self._clock() - self._started
        image.rect(0, 0, 160, 90, INK["sky"])
        image.rect(0, 90, 160, 54, INK["grass"])
        image.rect(int(t * 20) % 180 - 20, 30, 20, 6, INK["text"])  # a moving cloud, for video deltas
        image.rect(self.sprite[0], self.sprite[1], 12, 12, INK["system"])
        image.rect(0, 0, 160, 12, INK["dark"])
        image.text(4, 3, "SIMULATED GAME", INK["controls"])
        image.text(4, 134, "HELD " + ("+".join(self.held) or "NONE"), INK["text"])
        return image.bytes()

    # ---- CHR1 server -----------------------------------------------------------
    def _accept_loop(self):
        while not self._stop.is_set():
            self.tick()
            try:
                conn, _ = self._server.accept()
            except (socket.timeout, OSError):
                continue
            if self._busy or self.phase == "handover":
                conn.close()  # one client; stock firmware has no listener
                continue
            self._busy = True
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        stream = None
        try:
            conn.settimeout(15.0)
            stream = conn.makefile("rb")
            device_nonce = p.new_nonce()
            conn.sendall(p.hello_line(device_nonce, self.version))
            try:
                pc_nonce = p.parse_auth(self.key, p.read_line(stream), device_nonce)
            except p.ProtocolError:
                self.failed_auth += 1
                conn.sendall(p.encode_line(f"{p.MAGIC} DENY"))
                self._say("pc session: pairing key mismatch, denied")
                return
            conn.sendall(p.ok_line(self.key, pc_nonce, device_nonce, self.capabilities))
            session = p.session_key(self.key, device_nonce, pc_nonce)
            with self._lock:
                self._pause_countdown()
            expected, video_state = 1, {"seq": 0, "frame": None}
            while not self._stop.is_set():
                self.tick()
                action, arg = p.parse_request(session, p.read_line(stream), expected)
                code, body = self._handle(action, arg, video_state)
                conn.sendall(p.encode_response(session, expected, action, code, body))
                expected += 1
                if action == "bye" or self.phase == "handover":
                    return
        except (OSError, p.ProtocolError):
            return
        finally:
            with self._lock:
                self.held = ()  # a closed session always releases remote input
            self._busy = False
            if stream is not None:
                stream.close()
            conn.close()

    def _target_allows(self, kind):
        """Launcher screens need launcher caps; game screens need the matching game-* cap."""
        if self.phase == "game":
            return f"game-{kind}" in self.capabilities
        return kind in self.capabilities

    def _handle(self, action, arg, video_state):
        if action == "ping":
            return "ok", b"pong"
        if action == "bye":
            return "ok", b"bye"
        if action in ("status", "log", "perf", "console"):
            if action not in self.capabilities:
                return "refused", b"not offered by this launcher"
            if action == "status":
                return "ok", json.dumps(self.status(), separators=(",", ":")).encode()
            if action == "perf":
                return "ok", json.dumps(self.perf(), separators=(",", ":")).encode()
            if action == "console":
                return "ok", self.console(arg).encode()
            with self._lock:
                text = "\n".join(self.log) + "\n"
            return "ok", text.encode("utf-8")[-int(arg):]
        if action == "audio":
            if self.phase != "game" or not self._target_allows("audio"):
                return "refused", b"no sound outside games; game sound needs an FPGA update"
            return "ok", self.audio_chunk(int(arg))
        kind = {"screen": "screen", "video": "video", "input": "input", "hold": "input"}[action]
        if not self._target_allows(kind):
            return "refused", (b"game access needs an FPGA update" if self.phase == "game" else
                               b"not offered by this launcher")
        if action == "screen":
            return "ok", self.frame()
        if action == "video":
            frame = self.frame()
            video_state["seq"] += 1
            previous = video_state["frame"] if arg == "delta" else None
            data = video.encode(frame, video_state["seq"], previous, video_state["seq"] - 1)
            video_state["frame"] = frame
            return "ok", data
        if action == "hold":
            code, message = self.hold(p.parse_buttons(arg, allow_none=True))
            return code, message.encode()
        buttons, _, _ms = arg.rpartition(":")
        code, message = self.press(p.parse_buttons(buttons))
        return code, message.encode()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Run a CHR1 launcher simulator on loopback.")
    parser.add_argument("--port", type=int, default=p.PORT)
    parser.add_argument("--key-file", required=True, help="file holding the 64-hex pairing key")
    parser.add_argument("--game-bridge", action="store_true", help="simulate a future FPGA game bridge")
    args = parser.parse_args()
    with open(args.key_file, encoding="ascii") as handle:
        key = p.parse_key(handle.read().strip())
    sim = SimulatedLauncher(key, port=args.port, countdown_s=120, game_bridge=args.game_bridge).start()
    print(f"CHR1 simulator on 127.0.0.1:{sim.port}. Ctrl+C stops it.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sim.stop()


if __name__ == "__main__":
    main()

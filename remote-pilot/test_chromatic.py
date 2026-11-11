# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""Chromatic preset, API, CHR1 protocol, CHV1 video, transports, simulator and Manager runner.

No hardware. Nothing here opens a serial port or USB device: the real-port class
is checked with a stand-in `serial` module, the Manager runner only ever starts a
stub script in a temporary folder, and everything else uses the host simulators.
"""

import http.client
import io
import json
import os
import random
import secrets
import socket
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import presets
import receiver
from chromatic import api, manager_status, protocol as p, video
from chromatic.lan_transport import LanTransport, discover, parse_perf, parse_status
from chromatic.manager_runner import ManagerRunner, required_phrase
from chromatic.runtime import ChromaticRuntime, load_config
from chromatic.screen import BMP_BYTES, frame_to_bmp, rgb565_to_rgb, valid_bmp
from chromatic.serial_transport import PySerialLineSource, SerialTransport, sanitize
from chromatic.simulator import SimulatedLauncher, SimulatedSerialDevice
from plugin_runtime import PluginRuntime

ROOT = Path(__file__).resolve().parent
MANIFEST = json.loads((ROOT / "presets" / "chromatic" / "manifest.json").read_text(encoding="utf-8"))
# The Manager's per-user folder is looked at first (chromatic/manager_home.py): point it at an
# empty temporary folder so no test ever sees, or depends on, a real Jellymatic setup.
_JELLYMATIC_TEMP = tempfile.TemporaryDirectory(prefix="rp-jellymatic-test-")
os.environ["JELLYMATIC_HOME"] = str(Path(_JELLYMATIC_TEMP.name) / "home")
os.environ["JELLYMATIC_SECOND_COPY"] = str(Path(_JELLYMATIC_TEMP.name) / "second")


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.key = p.new_key()
        self.sim = SimulatedLauncher(self.key, countdown_s=60).start()

    def tearDown(self):
        self.sim.stop()

    def raw(self):
        sock = socket.create_connection(("127.0.0.1", self.sim.port), timeout=3)
        return sock, sock.makefile("rb")

    def handshake(self, sock, stream, key=None):
        device_nonce, version = p.parse_hello(p.read_line(stream))
        pc_nonce = p.new_nonce()
        sock.sendall(p.auth_line(key or self.key, device_nonce, pc_nonce))
        caps = p.parse_ok(key or self.key, p.read_line(stream), pc_nonce, device_nonce)
        return p.session_key(self.key, device_nonce, pc_nonce), caps, version

    def link(self, sim=None, key=None):
        sim = sim or self.sim
        value = LanTransport("127.0.0.1", sim.port, key or sim.key, allow_loopback=True)
        self.addCleanup(value.stop)
        return value

    def test_mutual_handshake_status_log_screen_input(self):
        link = self.link()
        link.poll()
        snap = link.snapshot()
        self.assertTrue(snap["session"] and snap["connected"])
        self.assertEqual(snap["launcherVersion"], "1.1.0")
        self.assertEqual(snap["deviceCapabilities"], sorted(p.LAUNCHER_CAPABILITIES))
        self.assertEqual((snap["launcherScreen"], snap["target"]), ("menu", "launcher"))
        self.assertIn("PJ-LAUNCHER-READY 1.1.0", link.log_text(4096))
        self.assertEqual(len(link.screen()), p.SCREEN_BYTES)
        self.assertEqual(link.press("DOWN", 100), "queued")
        link.poll()
        self.assertEqual(link.snapshot()["selected"], "jellyfin")
        self.assertEqual(self.sim.presses, ["DOWN"])
        self.assertEqual(set(link.perf()), {"fps", "frameMs", "workMs", "timestampMs", "heapFreeKb"})
        self.assertIn("simulator", link.console("version"))

    def test_chords_holds_and_the_device_lease(self):
        clock = [100.0]
        sim = SimulatedLauncher(p.new_key(), countdown_s=60, clock=lambda: clock[0]).start()
        self.addCleanup(sim.stop)
        link = self.link(sim)
        link.poll()
        self.assertEqual(link.press(("UP", "B"), 60), "queued")
        self.assertEqual(sim.presses, ["UP", "B"])
        self.assertEqual(link.hold(("DOWN",)), "held")
        self.assertEqual(sim.held, ("DOWN",))
        self.assertEqual(link.hold(("DOWN",)), "held")  # a refresh is not a new press
        self.assertEqual(sim.presses, ["UP", "B", "DOWN"])
        clock[0] += 0.7  # no refresh for longer than the 600 ms lease
        sim.tick()
        self.assertEqual(sim.held, ())
        self.assertEqual(link.hold(("LEFT", "B")), "held")  # A here would pick Games
        self.assertEqual(link.hold(()), "released")
        self.assertEqual(sim.held, ())
        self.assertEqual(link.hold(("RIGHT",)), "held")
        link.stop()  # a closed session releases everything
        self.assertTrue(wait_for(lambda: sim.held == ()))
        for bad in (("MENU",), ("A", "A"), ("A", "B", "UP", "DOWN", "LEFT"), ("HOME",)):
            with self.assertRaises(ValueError, msg=bad):
                link.hold(bad)
        with self.assertRaises(ValueError):
            link.press(("MENU", "A"), 100)

    def test_wrong_key_is_denied_and_launcher_never_proves_a_key_it_lacks(self):
        link = self.link(key=p.new_key())
        link.poll()
        snap = link.snapshot()
        self.assertFalse(snap["session"])
        self.assertIn("refused this PC", snap["error"])
        self.assertEqual(self.sim.failed_auth, 1)
        sock, stream = self.raw()
        with closing(sock):
            device_nonce, _ = p.parse_hello(p.read_line(stream))
            pc_nonce = p.new_nonce()
            sock.sendall(p.auth_line(self.key, device_nonce, pc_nonce))
            line = p.read_line(stream)
            with self.assertRaises(p.ProtocolError):  # a forged OK tag must not verify
                p.parse_ok(p.new_key(), line, pc_nonce, device_nonce)

    def test_replayed_and_tampered_requests_close_the_session(self):
        sock, stream = self.raw()
        with closing(sock):
            session, _, _ = self.handshake(sock, stream)
            request = p.encode_request(session, 1, "ping")
            sock.sendall(request)
            self.assertEqual(p.read_response(session, stream, 1, "ping"), ("ok", b"pong"))
            sock.sendall(request)  # same seq again
            self.assertEqual(stream.read(1), b"")
        sock, stream = self.raw()
        with closing(sock):
            session, _, _ = self.handshake(sock, stream)
            line = p.encode_request(session, 1, "input", "DOWN:100")
            sock.sendall(line.replace(b"DOWN", b"MENU"))  # MAC no longer matches
            self.assertEqual(stream.read(1), b"")
        self.assertEqual(self.sim.presses, [])
        self.assertNotEqual(self.sim.phase, "handover")

    def test_bounds_and_validation(self):
        session = b"s" * 32
        for action, arg in (("input", "HOME:100"), ("input", "A:10"), ("input", "A:251"), ("input", "A+A:100"),
                            ("input", "MENU+A:100"), ("input", "A+B+UP+DOWN+LEFT:100"), ("hold", "MENU"),
                            ("hold", ""), ("log", "0"), ("log", "16385"), ("status", "x"), ("video", "all"),
                            ("console", "reboot"), ("console", "flash"), ("audio", "-1"), ("flash", "-")):
            with self.assertRaises(p.ProtocolError, msg=(action, arg)):
                p.encode_request(session, 1, action, arg)
        self.assertEqual(p.validate_arg("input", "B+A:60"), "A+B:60")
        self.assertEqual(p.validate_arg("hold", "NONE"), "NONE")
        with self.assertRaises(p.ProtocolError):
            p.encode_response(session, 1, "screen", "ok", b"x" * 10)
        with self.assertRaises(p.ProtocolError):
            p.encode_response(session, 1, "status", "ok", b"x" * (p.MAX_STATUS + 1))
        with self.assertRaises(p.ProtocolError):
            p.encode_line("x" * 300)
        with self.assertRaises(p.ProtocolError):
            p.encode_line("bad\nline")
        good = p.encode_response(session, 7, "ping", "ok", b"pong")
        self.assertEqual(p.read_response(session, io.BytesIO(good), 7, "ping"), ("ok", b"pong"))
        with self.assertRaises(p.ProtocolError):
            p.read_response(session, io.BytesIO(good[:-1] + b"x"), 7, "ping")
        with self.assertRaises(p.ProtocolError):
            p.read_response(session, io.BytesIO(good), 8, "ping")
        with self.assertRaises(p.ProtocolError):
            p.parse_key("ab" * 31)
        self.assertEqual(p.parse_key("ab" * 32), bytes([0xab]) * 32)

    def test_local_lan_only(self):
        for host in ("192.168.250.20", "10.123.45.4", "172.31.250.9"):
            self.assertTrue(p.allowed_host(host))
        for host in ("203.0.113.8", "127.0.0.1", "198.51.100.7", "example.com", "", None, "::1"):
            self.assertFalse(p.allowed_host(host), host)
        self.assertTrue(p.allowed_host("127.0.0.1", allow_loopback=True))
        with self.assertRaises(ValueError):
            LanTransport("203.0.113.8", p.PORT, p.new_key())
        with self.assertRaises(ValueError):
            LanTransport("192.168.250.2", p.PORT, b"short")
        with self.assertRaises(ValueError):
            discover("203.0.113.8", p.PORT, p.new_key())

    def test_discovery_finds_only_the_paired_console(self):
        self.assertEqual(discover(None, self.sim.port, self.key, candidates=["127.0.0.1"], allow_loopback=True),
                         "127.0.0.1")
        self.assertIsNone(discover(None, self.sim.port, p.new_key(), candidates=["127.0.0.1"], allow_loopback=True))

    def test_status_and_perf_parsers_reject_bad_fields(self):
        self.assertEqual(parse_status(b'{"phase":"menu","extra":1}'), {"phase": "menu", "target": "launcher"})
        self.assertEqual(parse_status(b'{"phase":"game"}')["target"], "game")
        for body in (b"[]", b"{}", b'{"phase":"playing"}', b'{"phase":"menu","version":"x"}',
                     b'{"phase":"menu","uptimeMs":true}', b'{"phase":"menu","target":"fpga"}', b"\xff"):
            with self.assertRaises(p.ProtocolError, msg=body):
                parse_status(body)
        for body in (b'{"fps":1,"frameMs":1,"workMs":1}', b'{"fps":-1,"frameMs":1,"workMs":1,"timestampMs":1}',
                     b'{"fps":true,"frameMs":1,"workMs":1,"timestampMs":1}', b"[]"):
            with self.assertRaises(p.ProtocolError, msg=body):
                parse_perf(body)

    def test_menu_and_games_hand_over_and_close_the_link(self):
        link = self.link()
        link.poll()
        self.assertEqual(link.press("MENU", 100), "handover")
        link.poll()
        snap = link.snapshot()
        self.assertEqual(snap["phase"], "stock")
        self.assertFalse(snap["session"])
        link.poll()  # stock has no listener
        self.assertFalse(link.snapshot()["session"])
        self.assertIn("PJ-LAUNCHER-TO-STOCK", "\n".join(self.sim.log))

    def test_launcher_only_device_refuses_game_features(self):
        link = self.link()
        link.poll()
        self.assertFalse(any(cap.startswith("game-") for cap in link.capabilities))
        with self.assertRaises(ValueError):
            link.audio()

    def test_fpga_bridge_simulation_serves_game_video_input_and_sound(self):
        sim = SimulatedLauncher(p.new_key(), countdown_s=60, game_bridge=True).start()
        self.addCleanup(sim.stop)
        link = self.link(sim)
        link.poll()
        self.assertTrue(set(p.GAME_CAPABILITIES) <= link.capabilities)
        self.assertEqual(link.press("A", 100), "queued")  # Games, with the bridge: stays connected
        link.poll()
        snap = link.snapshot()
        self.assertEqual((snap["phase"], snap["target"], snap["session"]), ("game", "game", True))
        first = link.video_frame()
        x = sim.sprite[0]
        self.assertEqual(link.hold(("RIGHT",)), "held")
        self.assertEqual(sim.sprite[0], x + 4)
        second = link.video_frame()
        self.assertNotEqual(first, second)
        sound = link.audio()
        self.assertEqual(len(sound), 8192)
        self.assertEqual(link.audio(), b"")  # nothing new yet
        self.assertLess(link.video_bytes, 2 * p.SCREEN_BYTES)

    def test_one_client_at_a_time(self):
        first, stream = self.raw()
        with closing(first):
            self.handshake(first, stream)
            second = socket.create_connection(("127.0.0.1", self.sim.port), timeout=3)
            with closing(second):
                self.assertEqual(second.recv(16), b"")


class VideoTests(unittest.TestCase):
    def frame(self, fill=0, pixels=None):
        values = [fill] * (160 * 144)
        for index, value in (pixels or {}).items():
            values[index] = value
        return struct.pack(f"<{len(values)}H", *values)

    def test_keyframes_deltas_and_raw_fallback_round_trip(self):
        decoder = video.Decoder()
        first = self.frame(0x1234)
        key = video.encode(first, 1)
        self.assertEqual(key[12], video.RLE_KEY)
        self.assertEqual(decoder.feed(key), first)
        same = video.encode(first, 2, first, 1)
        self.assertEqual((same[12], len(same)), (video.RLE_DELTA, 38))
        self.assertEqual(decoder.feed(same), first)
        second = self.frame(0x1234, {5 * 160 + 7: 0xF800, 100 * 160: 0x07E0})
        delta = video.encode(second, 3, first, 2)
        self.assertEqual(struct.unpack_from("<H", delta, 14)[0], 2)
        self.assertEqual(decoder.feed(delta), second)
        noise = random.Random(1).randbytes(p.SCREEN_BYTES)
        raw = video.encode(noise, 4, second, 3)
        self.assertEqual((raw[12], len(raw)), (video.RAW, p.MAX_VIDEO))
        self.assertEqual(decoder.feed(raw), noise)

    def test_decoder_rejects_broken_chains_and_malformed_frames(self):
        decoder = video.Decoder()
        first = self.frame(1)
        with self.assertRaises(ValueError):  # a delta before any keyframe
            decoder.feed(video.encode(first, 2, first, 1))
        decoder.feed(video.encode(first, 1))
        with self.assertRaises(ValueError):  # wrong base
            decoder.feed(video.encode(self.frame(2), 5, first, 3))
        self.assertEqual(decoder.want, "key")
        key = bytearray(video.encode(first, 1))
        for broken in (key[:-1], b"XXXX" + key[4:], key + b"\0", key[:20]):
            with self.assertRaises(ValueError):
                video.Decoder().feed(bytes(broken))
        overflow = bytearray(key)
        overflow[20] = 200  # first run 200 + following runs overflow the 160-pixel row
        with self.assertRaises(ValueError):
            video.Decoder().feed(bytes(overflow))


class ScreenTests(unittest.TestCase):
    def test_rgb565_frame_becomes_bottom_up_bmp(self):
        pixels = [0] * (160 * 144)
        pixels[0] = 0xF800          # top-left red
        pixels[-1] = 0x001F         # bottom-right blue
        pixels[159] = 0x07E0        # top-right green
        image = frame_to_bmp(struct.pack(f"<{len(pixels)}H", *pixels))
        self.assertTrue(valid_bmp(image))
        self.assertEqual(len(image), BMP_BYTES)
        top_row = 54 + 143 * 480
        self.assertEqual(image[top_row:top_row + 3], bytes((0, 0, 255)))         # BGR red
        self.assertEqual(image[top_row + 159 * 3:top_row + 480], bytes((0, 255, 0)))
        self.assertEqual(image[54 + 159 * 3:54 + 480], bytes((255, 0, 0)))       # bottom row first in file
        self.assertEqual(rgb565_to_rgb(0xFFFF), (255, 255, 255))
        with self.assertRaises(ValueError):
            frame_to_bmp(b"\0" * 100)


class SerialTests(unittest.TestCase):
    def test_markers_phases_and_reset_reason(self):
        link = SerialTransport(lambda: None)
        link.ingest(b"rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\n")
        self.assertEqual(link.snapshot()["phase"], "booting")
        link.ingest(b"PJ-LAUNCHER-READY 1.1.0")
        snap = link.snapshot()
        self.assertEqual((snap["phase"], snap["launcherVersion"], snap["resetReason"]),
                         ("launcher", "1.1.0", "POWERON_RESET"))
        link.ingest("PJ-LAUNCHER-TO-STOCK")
        self.assertEqual((link.snapshot()["phase"], link.snapshot()["handovers"]), ("stock", 1))
        link.ingest("PJ-LAUNCHER-READY not-a-version")
        self.assertIsNone(link.snapshot()["launcherVersion"])
        self.assertFalse(link.snapshot()["session"])

    def test_redaction_and_bounds(self):
        mac = ":".join(["a4"] * 6)
        self.assertNotIn(mac, sanitize(f"wifi: sta mac {mac} up"))
        self.assertIn("[redacted]", sanitize(f"wifi: sta mac {mac.replace(':', '-')} up"))
        self.assertNotIn("0123456789abcdef0123", sanitize("id 0123456789abcdef0123 x"))
        self.assertEqual(sanitize("PJ-PAIR " + "ab" * 32), "[pairing/provisioning line withheld]")
        self.assertEqual(sanitize("PJ-WIFI ssid pass"), "[pairing/provisioning line withheld]")
        self.assertEqual(sanitize(b"a\x1b[31mb\x00c"), "a[31mbc")
        self.assertEqual(len(sanitize("x" * 1000)), 240)
        self.assertEqual(sanitize("ELF version 1.1.0 at load:0x3fff0030"), "ELF version 1.1.0 at load:0x3fff0030")
        link = SerialTransport(lambda: None)
        for index in range(2100):
            link.ingest(f"line {index}")
        self.assertEqual(link.snapshot()["lines"], 2000)
        self.assertLessEqual(len(link.log_text(1000).encode()), 1000)
        self.assertTrue(link.log_text(1000).rstrip().endswith("line 2099"))

    def test_pairing_capture_only_when_armed_and_never_logged(self):
        link = SerialTransport(lambda: None)
        key = secrets.token_hex(32)
        got = []
        link.ingest(f"PJ-PAIR {key}")
        link.arm_pairing(got.append)
        self.assertTrue(link.pairing_armed)
        link.ingest("PJ-PAIR not-a-key")  # invalid: discarded and disarmed
        self.assertEqual(got, [])
        self.assertFalse(link.pairing_armed)
        link.arm_pairing(got.append)
        link.ingest(f"PJ-PAIR {key}\r\n")
        self.assertEqual(got, [key])
        self.assertFalse(link.pairing_armed)
        self.assertNotIn(key, link.log_text())
        self.assertNotIn(key, json.dumps(link.snapshot()))

    def test_serial_is_read_only(self):
        link = SerialTransport(lambda: None)
        for name in ("write", "send", "sendall", "flash", "hold"):
            self.assertFalse(hasattr(link, name), name)
            self.assertFalse(hasattr(PySerialLineSource, name), name)
        with self.assertRaises(ValueError):
            link.press("A", 100)
        with self.assertRaises(ValueError):
            link.screen()

    def test_real_port_needs_explicit_opt_in_and_never_asserts_dtr_rts(self):
        with mock.patch.dict(sys.modules, {"serial": None}):
            with self.assertRaises(PermissionError):
                PySerialLineSource("COM5")  # refused before any import or open
            with self.assertRaises(PermissionError):
                PySerialLineSource("COM5", open_allowed="yes")
        events = []

        class FakePort:
            def __setattr__(self, name, value):
                events.append(("set", name, value))
                object.__setattr__(self, name, value)

            def open(self):
                events.append(("open",))

            def read(self, size):
                return b"PJ-LAUNCHER-READY 1.1.0\npartial"

            def write(self, data):  # must never be called
                events.append(("write", data))

            def close(self):
                events.append(("close",))

        fake = types.SimpleNamespace(Serial=FakePort, EIGHTBITS=8, PARITY_NONE="N", STOPBITS_ONE=1)
        with mock.patch.dict(sys.modules, {"serial": fake}):
            for bad in ("COM0", "COMX", "/etc/passwd", "\\\\.\\PhysicalDrive0", ""):
                with self.assertRaises(ValueError):
                    PySerialLineSource(bad, open_allowed=True)
            with self.assertRaises(ValueError):
                PySerialLineSource("COM5", baudrate=921600, open_allowed=True)
            source = PySerialLineSource("COM5", open_allowed=True)
            self.assertEqual(source.read_lines(), [b"PJ-LAUNCHER-READY 1.1.0"])
            source.close()
        opened = events.index(("open",))
        before = {event[1]: event[2] for event in events[:opened] if event[0] == "set"}
        self.assertIs(before["dtr"], False)
        self.assertIs(before["rts"], False)
        self.assertFalse(before["rtscts"] or before["dsrdtr"] or before["xonxoff"])
        self.assertFalse([event for event in events[opened:] if event[0] == "set"])
        self.assertFalse([event for event in events if event[0] == "write"])

    def test_simulated_serial_device_end_to_end(self):
        clock = [0.0]
        device = SimulatedSerialDevice(clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
        link = SerialTransport(lambda: device, simulated=True)
        link.start()
        try:
            self.assertTrue(wait_for(lambda: link.snapshot()["phase"] == "stock" and
                                     any("mcu>" in line for line in link.tail())))
            snap = link.snapshot()
            self.assertEqual((snap["launcherVersion"], snap["handovers"], snap["simulated"]), ("1.1.0", 1, True))
            self.assertEqual(snap["resetReason"], "SW_CPU_RESET")
        finally:
            link.stop()

    def test_failing_source_reports_error_and_retries_without_crashing(self):
        def factory():
            raise PermissionError("serialOpenAllowed is not set")
        link = SerialTransport(factory)
        link.start()
        try:
            self.assertTrue(wait_for(lambda: "serialOpenAllowed" in link.snapshot()["error"]))
            self.assertFalse(link.snapshot()["connected"])
        finally:
            link.stop()


class ManagerTests(unittest.TestCase):
    STUB = ("import os, sys, time\n"
            "print('stub manager', sys.argv[1:], flush=True)\n"
            "print('console mac ' + ':'.join(['3c'] * 6), flush=True)\n"
            "time.sleep(float(os.environ.get('STUB_SLEEP', '0')))\n"
            "raise SystemExit(int(os.environ.get('STUB_EXIT', '0')))\n")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "scripts" / "chromatic_manager.py").write_text(self.STUB)
        self.config = {"managerProject": str(self.root), "managerPython": sys.executable,
                       "managerRunAllowed": True}
        self.runner = ManagerRunner(Path(self.temp.name) / "jobs")

    def tearDown(self):
        wait_for(lambda: not self.runner.busy(), 10)
        self.temp.cleanup()

    def run_job(self, command, phrase, **kwargs):
        self.runner.start(self.config, command, phrase, kwargs.get("local", True), kwargs.get("serial", False))
        self.assertTrue(wait_for(lambda: not self.runner.busy(), 15))
        return self.runner.snapshot()

    def test_file_reader_never_runs_the_manager(self):
        with mock.patch("subprocess.Popen", side_effect=AssertionError("must not run")), \
                mock.patch("subprocess.run", side_effect=AssertionError("must not run")):
            self.assertFalse(manager_status.read("")["configured"])
            self.assertFalse(manager_status.read(self.temp.name)["projectFound"])
            source = self.root / "chromatic" / "launcher" / "main"
            source.mkdir(parents=True)
            (source / "launcher_main.c").write_text('#define PJ_LAUNCHER_VERSION "1.2.3"\n')
            backups = self.root / "private" / "chromatic-manager-backups"
            for name, record in (("a", True), ("b", True), ("c", False)):
                (backups / name).mkdir(parents=True)
                if record:
                    (backups / name / "record.json").write_text("{}")
            value = manager_status.read(str(self.root))
            self.assertEqual((value["launcherSourceVersion"], value["backupFolders"], value["journalPresent"]),
                             ("1.2.3", 2, False))
            (self.root / "private" / "chromatic-manager").mkdir()
            (self.root / "private" / "chromatic-manager" / "journal.jsonl").write_text("x\n")
            value = manager_status.read(str(self.root))
            self.assertTrue(value["journalPresent"])
            self.assertIn("write flash", value["detail"])
            # An unfinished console-chip (FPGA) job counts too.
            (self.root / "private" / "chromatic-manager" / "journal.jsonl").unlink()
            self.assertFalse(manager_status.read(str(self.root))["journalPresent"])
            (self.root / "private" / "chromatic-manager" / "fpga").mkdir()
            (self.root / "private" / "chromatic-manager" / "fpga" / "journal.jsonl").write_text("x\n")
            self.assertTrue(manager_status.read(str(self.root))["journalPresent"])

    def test_guards_before_any_process_starts(self):
        popen = mock.Mock(side_effect=AssertionError("must not start"))
        runner = ManagerRunner(Path(self.temp.name) / "jobs", popen=popen)
        not_allowed = {key: value for key, value in self.config.items() if key != "managerRunAllowed"}
        cases = [({"managerRunAllowed": True}, "status", "READ CHROMATIC", True, False, "managerProject"),
                 (self.config, "status", "READ CHROMATIC", False, False, "own browser"),
                 (not_allowed, "status", "READ CHROMATIC", True, False, "managerRunAllowed"),
                 ({**self.config, "managerRunAllowed": "true"}, "status", "READ CHROMATIC", True, False,
                  "managerRunAllowed"),
                 (self.config, "status", "READ CHROMATIC", True, True, "serial"),
                 (self.config, "status", "read chromatic", True, False, "READ CHROMATIC"),
                 (self.config, "install", "READ CHROMATIC", True, False, "INSTALL CHROMATIC LAUNCHER"),
                 (self.config, "flash", "x", True, False, "must be one of"),
                 ({**self.config, "managerPython": str(self.root / "missing.exe")}, "status", "READ CHROMATIC",
                  True, False, "Python")]
        for config, command, phrase, local, serial, message in cases:
            with self.assertRaisesRegex(ValueError, message):
                runner.start(config, command, phrase, local, serial)
        popen.assert_not_called()

    def test_runs_allowlisted_commands_with_yes_only_for_writes_and_redacts(self):
        job = self.run_job("status", "READ CHROMATIC")
        self.assertEqual((job["state"], job["exitCode"], job["writes"]), ("done", 0, False))
        self.assertTrue((Path(self.temp.name) / "jobs" / job["file"]).is_file())
        self.assertIn("stub manager ['status']", job["lines"][0])
        self.assertNotIn(":".join(["3c"] * 6), "\n".join(job["lines"]))
        self.assertTrue((Path(self.temp.name) / "jobs" / job["file"]).is_file())
        job = self.run_job("install", "INSTALL CHROMATIC LAUNCHER")
        self.assertIn("['install', '--yes']", job["lines"][0])
        with mock.patch.dict(os.environ, {"STUB_EXIT": "1"}):
            self.assertEqual(self.run_job("remove", "REMOVE CHROMATIC LAUNCHER")["state"], "refused")
        with mock.patch.dict(os.environ, {"STUB_EXIT": "3"}):
            self.assertEqual(self.run_job("restore-stock", "RESTORE CHROMATIC STOCK")["state"],
                             "manual-recovery-needed")

    def test_unfinished_journal_needs_its_own_phrase_and_one_job_at_a_time(self):
        (self.root / "private" / "chromatic-manager").mkdir(parents=True)
        (self.root / "private" / "chromatic-manager" / "journal.jsonl").write_text("x\n")
        self.assertEqual(required_phrase("status", True), "FINISH CHROMATIC JOB")
        with self.assertRaisesRegex(ValueError, "FINISH CHROMATIC JOB"):
            self.runner.start(self.config, "status", "READ CHROMATIC", True, False)
        with mock.patch.dict(os.environ, {"STUB_SLEEP": "1"}):
            self.runner.start(self.config, "status", "FINISH CHROMATIC JOB", True, False)
            self.assertTrue(self.runner.busy())
            with self.assertRaisesRegex(ValueError, "already running"):
                self.runner.start(self.config, "status", "FINISH CHROMATIC JOB", True, False)
        self.assertTrue(wait_for(lambda: not self.runner.busy(), 15))
        self.assertTrue(self.runner.snapshot()["journalFirst"])
        self.assertFalse(hasattr(self.runner, "cancel"))

    def test_jellymatic_per_user_folder_is_checked_first_then_the_old_private_layout(self):
        from chromatic import manager_home
        temp = Path(self.temp.name)
        # Where the folder is: JELLYMATIC_HOME, else %LOCALAPPDATA%\Jellymatic, else XDG on other systems.
        self.assertEqual(manager_home.jellymatic_home({"JELLYMATIC_HOME": str(temp / "h")}, "win32"), temp / "h")
        self.assertEqual(manager_home.jellymatic_home({"LOCALAPPDATA": str(temp / "la")}, "win32"),
                         temp / "la" / "Jellymatic")
        self.assertEqual(manager_home.jellymatic_home({"XDG_DATA_HOME": str(temp / "x")}, "linux"),
                         temp / "x" / "jellymatic")
        self.assertEqual(manager_home.second_copy({"JELLYMATIC_SECOND_COPY": str(temp / "s")}), temp / "s")
        home, second = temp / "home", temp / "second"
        env = {"JELLYMATIC_HOME": str(home), "JELLYMATIC_SECOND_COPY": str(second)}
        with mock.patch("subprocess.Popen", side_effect=AssertionError("must not run")):
            value = manager_status.read(str(self.root), env)
            self.assertEqual((value["journalPresent"], value["backupFolders"], value["secondCopyBackupFolders"]),
                             (False, 0, 0))
            # A journal in the per-user folder (main or console-chip) is found without any private/ folder.
            for journal in (home / "chromatic-manager" / "journal.jsonl",
                            home / "chromatic-manager" / "fpga" / "journal.jsonl"):
                journal.parent.mkdir(parents=True, exist_ok=True)
                journal.write_text("x\n")
                self.assertTrue(manager_status.read(str(self.root), env)["journalPresent"])
                self.assertTrue(manager_home.journal_present(self.root, env))
                journal.unlink()
            self.assertFalse(manager_status.read(str(self.root), env)["journalPresent"])
            # Backups are counted in both places; the second copy is reported on its own.
            for folder in (home / "chromatic-manager-backups" / "a",
                           self.root / "private" / "chromatic-manager-backups" / "b",
                           second / "manager" / "a"):
                folder.mkdir(parents=True)
                (folder / "record.json").write_text("{}")
            value = manager_status.read(str(self.root), env)
            self.assertEqual((value["backupFolders"], value["secondCopyBackupFolders"]), (2, 1))

    def test_manager_python_config_then_jellymatic_venv_then_old_private_venv(self):
        from chromatic import manager_home
        from chromatic.manager_runner import manager_paths
        home = Path(self.temp.name) / "home"
        env = {"JELLYMATIC_HOME": str(home)}
        config = {"managerProject": str(self.root)}
        new = home / "venv" / "Scripts" / "python.exe"
        old = self.root / "private" / "chromatic-tools" / "venv" / "Scripts" / "python.exe"
        self.assertEqual(manager_home.manager_python_candidates(config, self.root, env, "win32"), [new, old])
        self.assertEqual(manager_home.manager_python_candidates(config, self.root, env, "linux")[0],
                         home / "venv" / "bin" / "python")
        with self.assertRaisesRegex(ValueError, "Setup-Jellymatic"):
            manager_paths(config, env, "win32")
        old.parent.mkdir(parents=True)
        old.write_text("")
        self.assertEqual(manager_paths(config, env, "win32")[2], old)  # an older setup keeps working
        new.parent.mkdir(parents=True)
        new.write_text("")
        self.assertEqual(manager_paths(config, env, "win32")[2], new)  # the published layout wins
        self.assertEqual(manager_paths({**config, "managerPython": sys.executable}, env, "win32")[2],
                         Path(sys.executable))  # an explicit setting always wins

    def test_job_file_is_written_before_the_job_shows_finished(self):
        seen = []
        self.runner.start(self.config, "status", "READ CHROMATIC", True, False)
        end = time.monotonic() + 15
        while time.monotonic() < end:
            job = self.runner.snapshot()
            if job["state"] != "running":
                seen.append(bool(job.get("file")) and (Path(self.temp.name) / "jobs" / job["file"]).is_file())
                break
        self.assertEqual(seen, [True])


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.local = Path(self.temp.name) / "chromatic"
        self.local.mkdir()
        self.owner = secrets.token_hex(16)

    def tearDown(self):
        self.temp.cleanup()

    def runtime(self, config=None, **kwargs):
        if config is not None:
            (self.local / "config.json").write_text(json.dumps(config))
        value = ChromaticRuntime(self.local, MANIFEST, **kwargs)
        self.addCleanup(value.stop)
        value.start()
        return value

    def states(self, value):
        return {item["id"]: item["state"] for item in value.chromatic_request("status", {})["features"]}

    def arm(self, value):
        value.chromatic_request("arm", {"owner": self.owner, "enabled": True})

    def test_default_off_opens_nothing(self):
        opened = []
        value = self.runtime(serial_factory=lambda *a: opened.append(a))
        status = value.status()
        self.assertEqual((status["connected"], status["session"], status["discovery"]),
                         (False, False, "Chromatic link off"))
        self.assertEqual(opened, [])
        states = self.states(value)
        for name in ("game-screen", "game-video", "game-input", "game-audio"):
            self.assertEqual(states[name], "needs-fpga", name)
        self.assertEqual(states["launcher-input"], "off")
        self.assertEqual(states["ota-launcher"], "needs-launcher")
        self.assertNotIn("touch", states)  # not listed: the Chromatic has no touch screen
        with self.assertRaises(ValueError):
            value.chromatic_request("input", {"owner": self.owner, "buttons": ["A"], "ms": 100})

    def test_paired_default_is_wifi_with_discovery(self):
        (self.local / "pairing.json").write_text(json.dumps({"key": secrets.token_hex(32)}))
        found = []

        def fake_discover(pc_host, port, key, **kwargs):
            found.append((pc_host, port, len(key)))
            return "10.123.45.50"
        value = ChromaticRuntime(self.local, MANIFEST, discover_fn=fake_discover)
        self.addCleanup(value.stop)
        value.set_network_host("10.123.45.10")
        value.start()
        self.assertTrue(wait_for(lambda: value._transport is not None))
        self.assertEqual(found, [("10.123.45.10", p.PORT, 32)])
        self.assertEqual((value._mode, value._transport.host), ("lan", "10.123.45.50"))
        self.assertEqual(json.loads((self.local / "last-device.json").read_text()), {"host": "10.123.45.50"})
        status = value.chromatic_request("status", {})
        self.assertNotIn(json.loads((self.local / "pairing.json").read_text())["key"], json.dumps(status))

    def test_failed_discovery_is_reported(self):
        (self.local / "pairing.json").write_text(json.dumps({"key": secrets.token_hex(32)}))
        value = ChromaticRuntime(self.local, MANIFEST, discover_fn=lambda *a, **k: None)
        self.addCleanup(value.stop)
        value.set_network_host("10.123.45.5")
        value.start()
        self.assertTrue(wait_for(lambda: "no paired console" in value.status()["discovery"]))
        self.assertEqual(self.states(value)["wifi-link"], "waiting")

    def test_viewer_cannot_pick_real_transports(self):
        value = self.runtime()
        for mode in ("serial", "lan", "flash", ""):
            with self.assertRaises(ValueError):
                value.chromatic_request("mode", {"mode": mode})

    def test_configured_serial_without_opt_in_never_opens(self):
        opened = []

        def factory(port, baud, allowed):
            opened.append((port, baud, allowed))
            return PySerialLineSource(port, baud, allowed)
        value = self.runtime({"mode": "serial", "serialPort": "COM7"}, serial_factory=factory)
        self.assertTrue(wait_for(lambda: "serialOpenAllowed" in value.chromatic_request("status", {})["transport"]["error"]))
        self.assertTrue(opened and all(allowed is False for _, _, allowed in opened))
        states = self.states(value)
        self.assertEqual(states["launcher-screen"], "needs-launcher")
        self.assertEqual(states["wifi-link"], "needs-launcher")
        self.assertEqual(states["game-input"], "needs-fpga")

    def test_config_is_validated(self):
        for bad in ({"mode": "flash"}, {"mode": "lan-sim"}, {"serialOpenAllowed": "true"}, {"unknown": 1}, []):
            (self.local / "config.json").write_text(json.dumps(bad))
            with self.assertRaises(ValueError, msg=bad):
                load_config(self.local)
        value = self.runtime({"mode": "lan", "lanHost": "10.123.45.30"})
        self.assertIn("pairing.json is missing", value.chromatic_request("status", {})["configError"])
        self.assertEqual(value.chromatic_request("status", {})["mode"], "off")

    def test_control_lease(self):
        value = self.runtime()
        value.chromatic_request("mode", {"mode": "lan-sim"})
        value.status()
        with self.assertRaisesRegex(ValueError, "Enable remote controls"):
            value.chromatic_request("input", {"owner": self.owner, "buttons": ["DOWN"], "ms": 100})
        self.arm(value)
        with self.assertRaisesRegex(ValueError, "another tab"):
            value.chromatic_request("arm", {"owner": secrets.token_hex(16), "enabled": True})
        with self.assertRaisesRegex(ValueError, "Enable remote controls"):
            value.chromatic_request("input", {"owner": secrets.token_hex(16), "buttons": ["DOWN"], "ms": 100})
        self.assertEqual(value.chromatic_request("input", {"owner": self.owner, "buttons": ["DOWN"], "ms": 100})["result"],
                         "queued")
        with mock.patch("chromatic.runtime.time.monotonic", return_value=time.monotonic() + 5):
            with self.assertRaisesRegex(ValueError, "Enable remote controls"):  # lease expired
                value.chromatic_request("input", {"owner": self.owner, "buttons": ["UP"], "ms": 100})
        for bad in ({"enabled": True}, {"owner": "x", "enabled": True}, {"owner": self.owner, "enabled": 1}):
            with self.assertRaises(ValueError):
                value.chromatic_request("arm", bad)

    def test_launcher_simulator_screen_input_log_captures_and_handover(self):
        value = self.runtime()
        value.chromatic_request("mode", {"mode": "lan-sim"})
        status = value.status()
        self.assertTrue(status["connected"] and status["session"])
        self.assertTrue(status["discovery"].startswith("SIMULATOR"))
        self.assertEqual(set(status["perf"]), {"fps", "frameMs", "workMs", "timestampMs", "heapFreeKb"})
        self.assertLess(len(json.dumps({"meta": status["meta"], "perf": status["perf"]})), 8192)
        states = self.states(value)
        for name in ("wifi-link", "launcher-status", "launcher-log", "launcher-screen", "launcher-video",
                     "launcher-input", "launcher-hold", "launcher-console", "launcher-perf"):
            self.assertEqual(states[name], "available", name)
        for name in ("game-screen", "game-video", "game-input", "game-audio"):
            self.assertEqual(states[name], "needs-fpga", name)
        self.arm(value)
        result = value.chromatic_request("input", {"owner": self.owner, "buttons": ["DOWN"], "ms": 100})
        self.assertEqual((result["result"], result["target"]), ("queued", "launcher"))
        with self.assertRaises(ValueError):  # bounded rate: one press, then a gap
            value.chromatic_request("input", {"owner": self.owner, "buttons": ["DOWN"], "ms": 100})
        for bad in ({"buttons": ["HOME"]}, {"buttons": ["A"], "ms": 5}, {"buttons": ["A"], "ms": "100"},
                    {"buttons": ["A"], "ms": True}, {"buttons": "A"}, {"buttons": ["MENU", "A"]}):
            time.sleep(0.16)
            with self.assertRaises(ValueError, msg=bad):
                value.chromatic_request("input", {"owner": self.owner, **bad})
        with self.assertRaises(ValueError):
            value.chromatic_request("audio", {})
        self.assertTrue(valid_bmp(value.chromatic_request("frame", {})))
        self.assertTrue(valid_bmp(value.chromatic_request("frame", {})))  # delta path
        captures = self.local.parent / "captures"
        self.assertFalse(captures.exists())  # live video is never saved
        shot = value.chromatic_request("shot", {})
        self.assertNotIn("path", shot)  # the bare file name only, no folder path
        self.assertEqual(Path(shot["file"]).name, shot["file"])
        log = value.chromatic_request("log", {})
        self.assertTrue(valid_bmp((captures / shot["file"]).read_bytes()))
        self.assertIn("menu: selected jellyfin", (captures / log["file"]).read_text())
        bundle = value.chromatic_request("bundle", {})
        manifest = json.loads((captures / bundle["file"]).read_text())
        self.assertEqual(set(manifest["files"]), {"screen", "log", "report"})
        self.assertTrue(manifest["simulated"])
        report = (captures / value.chromatic_request("report", {})["file"]).read_text()
        self.assertIn("needs-fpga", report)
        self.assertIn("[SIMULATOR]", report)
        for item in ("device-log", "status", "console", "manager-files"):
            self.assertTrue((captures / value.chromatic_request("collect", {"item": item})["file"]).is_file(), item)
        with self.assertRaises(ValueError):
            value.chromatic_request("collect", {"item": "../x"})
        self.assertIn("rssi", value.chromatic_request("console", {"command": "wifi"})["result"])
        for name in captures.iterdir():
            self.assertRegex(name.name, r"^chromatic-[a-z-]+-[0-9-]+\.(bmp|txt|json)$")
        with self.assertRaises(ValueError):
            value.request("shot", "-")
        time.sleep(0.2)
        self.assertEqual(value.chromatic_request("input", {"owner": self.owner, "buttons": ["MENU"], "ms": 100})["result"],
                         "handover")
        value.status()
        status = value.chromatic_request("status", {})
        self.assertEqual(status["transport"]["phase"], "stock")
        launcher_input = next(item for item in status["features"] if item["id"] == "launcher-input")
        self.assertEqual(launcher_input["state"], "waiting")
        self.assertIn("next power-on", launcher_input["detail"])
        actions = [entry["action"] for entry in status["activity"]]
        self.assertIn("bundle", actions)
        self.assertNotIn("status", actions)

    def test_fpga_simulator_lights_up_game_features(self):
        value = self.runtime()
        value.chromatic_request("mode", {"mode": "lan-sim-fpga"})
        value.status()
        self.arm(value)
        self.assertEqual(self.states(value)["game-video"], "waiting")
        value.chromatic_request("input", {"owner": self.owner, "buttons": ["A"], "ms": 100})  # Games
        value.status()
        states = self.states(value)
        for name in ("game-screen", "game-video", "game-input", "game-audio"):
            self.assertEqual(states[name], "available", name)
        self.assertEqual(states["launcher-screen"], "waiting")
        self.assertEqual(states["launcher-console"], "available")  # device-level, not screen-bound
        self.assertEqual(value.chromatic_request("hold", {"owner": self.owner, "buttons": ["RIGHT", "A"]})["target"], "game")
        with self.assertRaisesRegex(ValueError, "10 a second"):
            value.chromatic_request("hold", {"owner": self.owner, "buttons": ["RIGHT"]})
        time.sleep(0.1)
        value.chromatic_request("hold", {"owner": self.owner, "buttons": []})
        self.assertTrue(valid_bmp(value.chromatic_request("frame", {})))
        self.assertEqual(value.chromatic_request("shot", {})["target"], "game")
        self.assertEqual(len(value.chromatic_request("audio", {})), 8192)
        value.chromatic_request("arm", {"owner": self.owner, "enabled": False})
        self.assertFalse(value.chromatic_request("status", {})["controlsArmed"])

    def test_serial_simulator_reports_stock_and_pairs(self):
        value = self.runtime()
        value.chromatic_request("mode", {"mode": "serial-sim"})
        self.assertTrue(wait_for(lambda: value.chromatic_request("status", {})["transport"]["phase"] == "launcher"))
        states = self.states(value)
        self.assertEqual((states["launcher-status"], states["usb-serial"]), ("available", "available"))
        self.assertEqual(states["launcher-screen"], "needs-launcher")
        self.arm(value)
        with self.assertRaises(ValueError):
            value.chromatic_request("input", {"owner": self.owner, "buttons": ["A"], "ms": 100})
        key, old_key = secrets.token_hex(32), secrets.token_hex(32)
        (self.local / "pairing.json").write_text(json.dumps({"key": old_key}))
        with self.assertRaisesRegex(ValueError, "own browser"):
            value.chromatic_request("pair-capture", {"enabled": True}, local_client=False)
        value.chromatic_request("pair-capture", {"enabled": True})
        self.assertTrue(value.chromatic_request("status", {})["pairingArmed"])
        value._transport.ingest(f"PJ-PAIR {key}")
        self.assertEqual(json.loads((self.local / "pairing.json").read_text()), {"key": key})
        backups = sorted(self.local.glob("pairing-backup-*.json"))
        self.assertEqual(len(backups), 1)  # the key it replaced is kept
        self.assertEqual(json.loads(backups[0].read_text()), {"key": old_key})
        self.assertNotIn(key, json.dumps(value.chromatic_request("status", {})))
        self.assertEqual(self.states(value)["pairing"], "available")
        value.chromatic_request("mode", {"mode": "lan-sim"})
        with self.assertRaises(ValueError):  # pairing capture needs the serial line
            value.chromatic_request("pair-capture", {"enabled": True})

    def test_manager_run_through_runtime_requires_local_client(self):
        root = Path(self.temp.name) / "project"
        (root / "scripts").mkdir(parents=True)
        (root / "scripts" / "chromatic_manager.py").write_text("print('stub')\n")
        value = self.runtime({"mode": "off", "managerProject": str(root), "managerPython": sys.executable})
        self.assertEqual(self.states(value)["firmware-patch"], "off")  # opt-in missing
        with self.assertRaisesRegex(ValueError, "managerRunAllowed"):
            value.chromatic_request("manager-run", {"command": "status", "phrase": "READ CHROMATIC"})
        value = self.runtime({"mode": "off", "managerProject": str(root), "managerPython": sys.executable,
                              "managerRunAllowed": True})
        self.assertEqual(self.states(value)["firmware-patch"], "available")
        self.assertEqual(value.chromatic_request("manager-phrase", {"command": "backup"})["phrase"], "BACKUP CHROMATIC")
        with self.assertRaisesRegex(ValueError, "own browser"):
            value.chromatic_request("manager-run", {"command": "status", "phrase": "READ CHROMATIC"}, local_client=False)
        value.chromatic_request("manager-run", {"command": "status", "phrase": "READ CHROMATIC"}, local_client=True)
        self.assertTrue(wait_for(lambda: (value.chromatic_request("manager-job", {})["job"] or {}).get("state") == "done", 15))
        self.assertTrue(value.chromatic_request("collect", {"item": "manager-job"})["file"])

    def test_lan_viewers_cannot_change_the_link_pairing_or_read_manager_output(self):
        value = self.runtime()
        for action, payload in (("mode", {"mode": "lan-sim"}), ("pair-capture", {"enabled": True}),
                                ("pair-usb", {"start": True}), ("pair-usb", {}), ("manager-job", {}),
                                ("manager-run", {"command": "status", "phrase": "READ CHROMATIC"}),
                                ("collect", {"item": "manager-files"}), ("collect", {"item": "manager-job"})):
            with self.assertRaisesRegex(ValueError, "own browser", msg=action):
                value.chromatic_request(action, payload, local_client=False)
        self.assertEqual(value.chromatic_request("mode", {"mode": "lan-sim"})["mode"], "lan-sim")  # this PC
        value._runner._job = {"id": "x", "command": "status", "state": "done", "lines": ["output"]}
        self.assertIsNone(value.chromatic_request("status", {}, local_client=False)["managerJob"])
        self.assertEqual(value.chromatic_request("status", {})["managerJob"]["lines"], ["output"])
        self.assertTrue(value.chromatic_request("collect", {"item": "status"}, local_client=False)["file"])

    def test_plugin_runtime_loads_the_preset_adapter(self):
        runtime = PluginRuntime(MANIFEST, ROOT / "presets", Path(self.temp.name))
        self.assertIsNotNone(runtime.adapter)
        self.assertEqual(type(runtime.adapter).__name__, "ChromaticRuntime")
        with self.assertRaises(ValueError):
            runtime.request("input", "0001 1 0 0 0 0")


class PresetTests(unittest.TestCase):
    def test_chromatic_preset_is_listed_and_existing_order_is_unchanged(self):
        ids = [preset["id"] for preset in presets.all_presets()]
        self.assertEqual(ids[:2], ["example", "chromatic"])  # presets added in their own folders come after
        chromatic = next(preset for preset in presets.all_presets() if preset["id"] == "chromatic")
        self.assertEqual(chromatic["models"], ["chromatic"])
        self.assertEqual(chromatic["display"], {"layout": "single-screen", "width": 160, "height": 144})
        # Chromatic Manager palette: Home red, notice yellow, info cyan.
        self.assertEqual(chromatic["theme"]["accent"], "#ff0000")
        self.assertEqual(chromatic["theme"]["accent2"], "#ffcc00")
        self.assertEqual(chromatic["theme"]["glow"], "#00a2ff")
        for preset in presets.all_presets():
            if preset["id"] != "chromatic":
                self.assertNotIn("chromatic", preset["capabilities"])
                self.assertIn(preset.get("panelScript"), (None, f"/presets/{preset['id']}/panel.js"))
                self.assertTrue(set(preset["models"]) <= {"basic", "dual-stick", preset["id"]})
        self.assertNotIn("panelScript", next(preset for preset in presets.all_presets() if preset["id"] == "example"))

    def test_chromatic_preset_files_load(self):
        loaded = {preset["id"]: preset for preset in presets.all_presets()}
        preset = loaded["chromatic"]
        self.assertEqual(preset["styleSheet"], "/presets/chromatic/theme.css")
        self.assertEqual(preset["panelScript"], "/presets/chromatic/panel.js")
        self.assertTrue((ROOT / "presets" / "chromatic" / "theme.css").is_file())
        self.assertEqual([p["id"] for p in presets.public_presets()], list(loaded))

    def test_invalid_preset_is_skipped_by_the_receiver_not_fatal(self):
        with tempfile.TemporaryDirectory() as temp:
            good, bad = Path(temp) / "good", Path(temp) / "bad"
            good.mkdir(), bad.mkdir()
            manifest = {"name": "Demo", "mark": "D", "transport": "example-offline", "models": ["basic"],
                        "capabilities": [], "theme": MANIFEST["theme"]}
            (good / "manifest.json").write_text(json.dumps({**manifest, "id": "good"}))
            (bad / "manifest.json").write_text(json.dumps({**manifest, "id": "bad", "models": ["no-such-model"]}))
            with mock.patch.object(presets, "PRESETS", Path(temp)), mock.patch.object(presets, "OVERRIDES", Path(temp) / "none.json"):
                with self.assertRaisesRegex(ValueError, "Invalid preset models"):
                    presets.all_presets()
                with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                    self.assertEqual([p["id"] for p in presets.public_presets()], ["good"])
                    self.assertEqual([p["id"] for p in presets.public_presets()], ["good"])
                self.assertEqual(err.getvalue().count("Warning: skipping preset"), 1)  # warned once, not per request

    def test_panel_script_path_is_validated(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "demo"
            folder.mkdir()
            manifest = {"id": "demo", "name": "Demo", "mark": "D", "transport": "example-offline",
                        "models": ["basic"], "capabilities": [], "theme": MANIFEST["theme"]}
            with mock.patch.object(presets, "PRESETS", Path(temp)):
                for script, exists, ok in (("/presets/demo/panel.js", True, True), ("/presets/demo/panel.js", False, False),
                                           ("/presets/other/panel.js", True, False), ("https://x/panel.js", True, False)):
                    (folder / "manifest.json").write_text(json.dumps({**manifest, "panelScript": script}))
                    if exists:
                        (folder / "panel.js").write_text("//")
                    elif (folder / "panel.js").exists():
                        (folder / "panel.js").unlink()
                    if ok:
                        self.assertEqual(presets.all_presets()[0]["panelScript"], script)
                    else:
                        with self.assertRaises(ValueError, msg=script):
                            presets.all_presets()


class ReceiverRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        receiver.LOCAL = Path(self.temp.name)
        receiver.DB = receiver.LOCAL / "queue.sqlite3"
        receiver.CAPTURES = receiver.LOCAL / "captures"
        receiver.SELECTED_PRESET = receiver.LOCAL / "selected-preset.json"
        receiver.selected_preset = "example"
        receiver.link_enabled = True
        receiver.plugin_runtimes = {}
        receiver.local()
        with closing(receiver.database()):
            pass
        self.runtime = PluginRuntime(MANIFEST, receiver.PRESETS, receiver.LOCAL)
        self.runtime.adapter.start()
        receiver.plugin_runtimes["chromatic"] = self.runtime
        self.viewer = receiver.ThreadingHTTPServer(("127.0.0.1", 0), receiver.Viewer)
        self.thread = threading.Thread(target=self.viewer.serve_forever, daemon=True)
        self.thread.start()
        self.owner = secrets.token_hex(16)

    def tearDown(self):
        self.runtime.stop()
        self.viewer.shutdown()
        self.viewer.server_close()
        self.thread.join()
        self.temp.cleanup()

    def view(self, method, path, body=None, **headers):
        client = http.client.HTTPConnection("127.0.0.1", self.viewer.server_port, timeout=5)
        payload = json.dumps(body).encode() if body is not None else b""
        client.request(method, path, payload, {"Content-Type": "application/json", **headers})
        answer = client.getresponse()
        result = answer.status, answer.read(), answer.getheader("Content-Type")
        client.close()
        return result

    def post(self, action, body=None):
        return self.view("POST", f"/api/chromatic/{action}", body or {}, **{"X-Remote-Pilot-Viewer": "1"})

    def test_routes_selection_link_and_headers(self):
        status, body, _ = self.view("GET", "/api/chromatic/status")
        self.assertEqual(status, 200)
        value = json.loads(body)
        self.assertEqual((value["mode"], value["selected"], value["linkEnabled"], value["localClient"]),
                         ("off", False, True, True))
        self.assertEqual(self.post("mode", {"mode": "lan-sim-fpga"})[0], 400)  # not selected yet
        self.assertEqual(self.view("POST", "/api/chromatic/mode", {"mode": "lan-sim"})[0], 403)  # no viewer header
        self.assertEqual(self.view("POST", "/api/preset", {"id": "chromatic"}, **{"X-Remote-Pilot-Viewer": "1"})[0], 200)
        self.assertEqual(self.post("mode", {"mode": "lan-sim-fpga"})[0], 200)
        self.runtime.adapter.status()
        status, body, kind = self.view("GET", "/api/chromatic/live.bmp?n=1")
        self.assertEqual((status, kind), (200, "image/bmp"))
        self.assertTrue(valid_bmp(body))
        self.assertEqual(self.view("GET", "/api/chromatic/live.pcm?n=1")[0], 400)  # no sound in the launcher
        status, body, _ = self.post("shot")
        self.assertEqual(status, 200)
        name = json.loads(body)["file"]
        status, body, kind = self.view("GET", f"/captures/{name}")
        self.assertEqual((status, kind), (200, "image/bmp"))
        self.assertEqual(self.post("input", {"owner": self.owner, "buttons": ["A"], "ms": 60})[0], 400)  # not armed
        self.assertEqual(self.post("arm", {"owner": self.owner, "enabled": True})[0], 200)
        self.assertEqual(self.post("input", {"owner": self.owner, "buttons": ["A"], "ms": 60})[0], 200)
        self.runtime.adapter.status()
        time.sleep(0.3)
        status, body, kind = self.view("GET", "/api/chromatic/live.pcm?n=2")
        self.assertEqual((status, kind, len(body)), (200, "application/octet-stream", 8192))
        self.assertEqual(self.post("hold", {"owner": self.owner, "buttons": ["UP"]})[0], 200)
        receiver.link_enabled = False
        self.assertEqual(self.post("hold", {"owner": self.owner, "buttons": ["UP"]})[0], 400)
        self.assertEqual(self.view("GET", "/api/chromatic/live.bmp")[0], 400)
        self.assertEqual(self.post("log")[0], 200)  # saving received lines needs no device action
        self.assertEqual(self.post("report")[0], 200)
        self.assertEqual(self.post("flash")[0], 400)
        self.assertEqual(self.view("GET", "/api/chromatic/../../x")[0], 400)
        self.assertEqual(self.post("status", {"x": "y" * 600})[0], 400)  # shared 512-byte limit
        self.assertEqual(self.post("manager-phrase", {"command": "install"})[0], 200)

    def test_connect_button_lists_the_usb_port_and_allow_usb_opens_it(self):
        from chromatic.usb_sim import SimulatedUsbConsole
        adapter = self.runtime.adapter
        console = SimulatedUsbConsole(key=None, countdown_s=60)
        self.addCleanup(console.stop)
        opened = []
        adapter._usb_port_factory = lambda: opened.append(1) or console  # never the real port in tests
        adapter._usb_present = lambda: True
        config = adapter.local_dir / "config.json"
        viewer = {"X-Remote-Pilot-Viewer": "1"}
        with mock.patch("chromatic.runtime.find_consoles", return_value=[{"port": "COM7"}]):
            self.assertEqual(self.view("POST", "/api/preset", {"id": "chromatic"}, **viewer)[0], 200)
            status, body, _ = self.view("POST", "/api/link", {"connected": True}, **viewer)
            self.assertEqual(status, 200)
            answer = json.loads(body)
            self.assertTrue(answer["linkEnabled"] and answer["connect"]["ok"])
            self.assertIn("Allow USB on COM7", answer["connect"]["message"])
            self.assertEqual((opened, config.exists()), ([], False))  # listed, not opened
            self.assertEqual(json.loads(self.post("status")[1])["usbOffer"]["ports"], ["COM7"])
            receiver.link_enabled = False
            self.assertEqual(self.post("allow-usb", {"port": "COM7"})[0], 400)  # needs the PC link
            receiver.link_enabled = True
            self.assertEqual(self.post("allow-usb", {"port": "COM9"})[0], 400)  # not a listed console
            status, body, _ = self.post("allow-usb", {"port": "COM7"})
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body)["mode"], "auto")
            self.assertEqual(json.loads(config.read_text())["serialPort"], "COM7")
            self.assertEqual(opened, [1])
            self.assertEqual(adapter.status()["meta"]["mode"], "auto")
        with self.assertRaisesRegex(ValueError, "own browser"):
            api.handle(receiver.plugin_runtimes, "allow-usb", {"port": "COM7"}, "chromatic", True, local_client=False)

    def test_panel_script_is_served_only_for_its_preset(self):
        status, body, kind = self.view("GET", "/presets/chromatic/panel.js")
        self.assertEqual((status, kind), (200, "text/javascript; charset=utf-8"))
        self.assertIn(b"RemotePilotChromatic", body)
        for path in ("/presets/example/panel.js", "/presets/chromatic/adapter.py", "/presets/chromatic/../../receiver.py",
                     "/presets/chromatic/PROTOCOL.md"):
            self.assertEqual(self.view("GET", path)[0], 404, path)
        self.assertEqual(self.view("GET", "/presets/chromatic/theme.css")[0], 200)

    def test_local_pixel_fonts_are_served(self):
        for name in ("heading-PressStart2P-Regular.ttf", "body-Silkscreen-Regular.ttf"):
            status, body, kind = self.view("GET", f"/fonts/{name}")
            self.assertEqual((status, kind), (200, "font/ttf"), name)
            self.assertEqual(body, (ROOT / "fonts" / name).read_bytes())
        for path in ("/fonts/OFL-Silkscreen.txt", "/fonts/../receiver.py", "/fonts/other.ttf"):
            self.assertEqual(self.view("GET", path)[0], 404, path)

    def test_missing_chromatic_package_leaves_receiver_working(self):
        with mock.patch.object(receiver, "chromatic_api", None):
            self.assertEqual(self.view("GET", "/api/chromatic/status")[0], 400)
            self.assertEqual(self.post("status")[0], 400)
            self.assertEqual(self.view("GET", "/api/presets")[0], 200)

    def test_api_module_guards(self):
        with self.assertRaises(ValueError):
            api.handle({}, "status", {}, "chromatic", True)
        with self.assertRaises(ValueError):
            api.handle(receiver.plugin_runtimes, "status", [], "chromatic", True)
        with self.assertRaisesRegex(ValueError, "own browser|managerProject"):
            api.handle(receiver.plugin_runtimes, "manager-run", {"command": "status", "phrase": "READ CHROMATIC"},
                       "chromatic", True, local_client=False)


if __name__ == "__main__":
    unittest.main()

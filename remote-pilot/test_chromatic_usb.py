# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""Chromatic CHR1 over USB serial, USB pairing and the automatic transport choice.

Against chromatic.usb_sim (a simulated console on a simulated cable) and, when
gcc and a Jellymatic checkout are present, against the
launcher's own PC harness (check_remote.c serve-usb / serve-lan: the real
remote.h handler and menu logic). No real port, no device: nothing here opens a
serial port, and the real-port class is checked with a fake pyserial module.
"""

import hashlib
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from collections import deque
from unittest import mock
from pathlib import Path

from chromatic import protocol as p
from chromatic.auto_transport import AutoTransport
from chromatic.lan_transport import LanTransport, parse_status
from chromatic.runtime import ChromaticRuntime, load_config, save_pairing_key
from chromatic.screen import frame_to_png
from chromatic.simulator import SimulatedLauncher
from chromatic.usb_sim import SimulatedUsbConsole
from chromatic.usb_transport import (CHROMATIC_USB_VID, SerialSupportMissing, UsbLink, UsbTransport, find_consoles,
                                     pair_code, port_present, pyserial_available, speaks_chr1)

ROOT = Path(__file__).resolve().parent
MANIFEST = json.loads((ROOT / "presets" / "chromatic" / "manifest.json").read_text(encoding="utf-8"))
# Optional: the Jellymatic checkout's chromatic/launcher folder (the launcher C source), for the PC harness tests.
LAUNCHER = Path(os.environ.get("CHROMATIC_LAUNCHER_SOURCE", str(Path(__file__).resolve().parent.parent / "Jellymatic" / "chromatic" / "launcher")))
GCC = Path(os.environ.get("CHROMATIC_GCC", r"C:\msys64\ucrt64\bin\gcc.exe"))


def wait_for(predicate, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class LinePort:
    """A port that only plays back text lines and records writes."""

    def __init__(self, lines):
        self._out = deque(line + b"\n" for line in lines)
        self.written = []
        self.closed = False

    def read(self, size):
        if self._out:
            return self._out.popleft()
        time.sleep(0.02)
        return b""

    def write(self, data):
        self.written.append(bytes(data))

    def close(self):
        self.closed = True


def sim_usb(key=None, **kwargs):
    console = SimulatedUsbConsole(key=key, countdown_s=kwargs.pop("countdown_s", 30.0), **kwargs)
    keys = []
    usb = UsbTransport(lambda: console, key, simulated=True, on_paired=keys.append, timeout=2.0)
    usb.start()
    return console, usb, keys


class UsbSafetyTests(unittest.TestCase):
    def test_only_chr1_lines_can_be_written(self):
        link = UsbLink(LinePort([]), lambda line: None)
        for bad in (b"\xc0\x00\x08", b"mcu> reboot\n", b"CHR1 OPEN", b"CHR1 OPEN\nX\n", b"REQ 1 status - zz\n",
                    b"CHR1 PAIR 1234\n", "CHR1 OPEN\n"):
            with self.assertRaises(PermissionError):
                link.write(bad)
        link.write(b"CHR1 OPEN\n")
        link.write(b"CHR1 PAIR " + b"a" * 32 + b"\n")
        link.write(p.encode_request(b"k" * 32, 7, "input", "DOWN:100"))

    def test_nothing_is_written_to_stock_or_old_launchers(self):
        cases = {
            "stock": [b"ets Jun  8 2016 00:22:57", b"rst:0xc (SW_CPU_RESET),boot:0x13", b"mcu>"],
            "old launcher": [b"rst:0x1 (POWERON_RESET),boot:0x13", b"PJ-LAUNCHER-READY 1.1.0"],
            "silent": [],
        }
        for name, lines in cases.items():
            port = LinePort(lines)
            usb = UsbTransport(lambda: port, bytes(32), timeout=0.3)
            usb.start()
            time.sleep(0.15)
            for _ in range(3):
                usb.poll()
            usb.stop()
            self.assertEqual(port.written, [], name)
            self.assertTrue(port.closed, name)

    def test_diag_lines_allow_one_open_then_silence(self):
        port = LinePort([b"PJ-DIAG t=1000 stats phase=menu uart=60"])
        usb = UsbTransport(lambda: port, bytes(32), timeout=0.3)
        usb.start()
        time.sleep(0.15)
        for _ in range(4):
            usb.poll()
        self.assertEqual(port.written, [b"CHR1 OPEN\n"])  # no answer: not a CHR1 launcher
        self.assertEqual(usb.snapshot()["usbState"], "no-answer")
        usb.retry()  # the user's explicit Retry allows exactly one more
        usb.poll()
        usb.poll()
        self.assertEqual(port.written, [b"CHR1 OPEN\n"] * 2)
        usb.stop()

    def test_real_port_holds_dtr_and_rts_low_and_needs_opt_in(self):
        events = []

        class FakeSerial:
            def __init__(self):
                self.__dict__["is_open"] = False

            def __setattr__(self, name, value):
                events.append(("set", name, value, self.is_open))
                self.__dict__[name] = value

            def open(self):
                events.append(("open",))
                self.__dict__["is_open"] = True

            def read(self, size):
                return b""

            in_waiting = 0

            def write(self, data):
                events.append(("write", data))

            def close(self):
                events.append(("close",))

        fake = types.ModuleType("serial")
        fake.Serial, fake.EIGHTBITS, fake.PARITY_NONE, fake.STOPBITS_ONE = FakeSerial, 8, "N", 1
        from chromatic.usb_transport import PySerialPort
        saved = sys.modules.get("serial")
        sys.modules["serial"] = fake
        try:
            with self.assertRaises(PermissionError):
                PySerialPort("COM7", open_allowed=False)
            self.assertEqual(events, [])
            with self.assertRaises(ValueError):
                PySerialPort("COM7; rm", open_allowed=True)
            port = PySerialPort("COM7", open_allowed=True)
            port.write(b"CHR1 OPEN\n")
            port.close()
        finally:
            if saved is None:
                sys.modules.pop("serial", None)
            else:
                sys.modules["serial"] = saved
        opened = events.index(("open",))
        before = {event[1]: event[2] for event in events[:opened] if event[0] == "set"}
        self.assertIs(before["dtr"], False)
        self.assertIs(before["rts"], False)
        self.assertEqual((before["xonxoff"], before["rtscts"], before["dsrdtr"], before["baudrate"]),
                         (False, False, False, 115200))
        self.assertFalse(any(event[0] == "set" and event[1] in ("dtr", "rts", "baudrate") for event in events[opened:]))


class UsbSimulatorTests(unittest.TestCase):
    def test_pair_over_usb_then_drive_the_menu(self):
        console, usb, keys = sim_usb()
        try:
            self.assertTrue(wait_for(lambda: usb.snapshot()["usbState"] == "launcher"))
            usb.poll()  # no key on the PC: nothing is sent
            self.assertEqual(console.written, [])
            usb._key = bytes(32)  # a stale key: the console has none, so it answers NOKEY
            usb.poll()
            self.assertEqual(usb.snapshot()["usbState"], "nokey")
            usb._key = None
            state = usb.start_pairing()
            self.assertIn(state["state"], ("asking", "waiting"))
            self.assertTrue(wait_for(lambda: (usb.pairing_state() or {}).get("state") == "waiting"))
            self.assertEqual(usb.pairing_state()["code"], console.pair_code)  # both screens show the same code
            console.approve()  # the owner's physical A press
            self.assertTrue(wait_for(lambda: usb.pairing_state()["state"] == "paired"))
            self.assertEqual(keys, [console.key.hex()])
            key_hex = console.key.hex()
            usb.poll()
            snap = usb.snapshot()
            self.assertTrue(snap["session"] and snap["connected"])
            self.assertEqual((snap["kind"], snap["phase"], snap["launcherVersion"]), ("usb", "launcher", "1.1.2"))
            self.assertEqual(set(snap["deviceCapabilities"]), set(p.LAUNCHER_CAPABILITIES))
            self.assertEqual(usb.press(["DOWN"], 100), "queued")
            usb.poll()
            self.assertEqual(usb.snapshot()["selected"], "jellyfin")
            self.assertEqual(len(usb.screen()), p.SCREEN_BYTES)
            self.assertEqual(len(usb.video_frame()), p.SCREEN_BYTES)
            self.assertEqual(usb.hold(("UP",)), "held")
            self.assertEqual(usb.hold(()), "released")
            self.assertEqual(usb.ping(), "pong")
            self.assertIn("1.1.2", usb.console("version"))
            self.assertIsNotNone(usb.perf())
            log = usb.log_text()
            self.assertIn("launcher RAM log", log)
            # Secrets and session bytes never reach the text log.
            text = log + "\n".join(usb.tail(200)) + "\n".join(usb._parser.tail(2000))
            self.assertNotIn(key_hex, text)
            self.assertFalse(any(line.split(" ", 1)[-1].startswith(("RES ", "REQ ", "CHR1 ")) for line in usb._parser.tail(2000)))
        finally:
            usb.stop()
            console.stop()

    def test_denied_and_refused_pairing(self):
        console, usb, keys = sim_usb()
        try:
            self.assertTrue(wait_for(lambda: usb.snapshot()["usbState"] == "launcher"))
            usb.start_pairing()
            self.assertTrue(wait_for(lambda: (usb.pairing_state() or {}).get("state") == "waiting"))
            console.deny()
            self.assertTrue(wait_for(lambda: usb.pairing_state()["state"] == "denied"))
            self.assertEqual(keys, [])
        finally:
            usb.stop()
            console.stop()
        console, usb, keys = sim_usb(key=bytes(range(32)))
        try:
            usb._key = None
            self.assertTrue(wait_for(lambda: usb.snapshot()["usbState"] == "launcher"))
            usb.start_pairing()
            self.assertTrue(wait_for(lambda: usb.pairing_state()["state"] == "refused"))
            self.assertIn("provisioned", usb.pairing_state()["message"])
        finally:
            usb.stop()
            console.stop()

    def test_session_opens_at_the_launcher_marker_and_stops_writing_after_handover(self):
        key = bytes(range(32))
        console, usb, _ = sim_usb(key=key, countdown_s=5.0)
        try:
            # No poll here: the READY line alone opens the session (the countdown runs about 5 s).
            self.assertTrue(wait_for(lambda: usb.snapshot()["session"], 3.0))
            self.assertIsNotNone(console.launcher._paused_left)  # the session paused the countdown
            self.assertEqual(usb.press(["MENU"], 100), "handover")
            self.assertTrue(wait_for(lambda: usb.snapshot()["phase"] == "stock"))
            written = len(console.written)
            for _ in range(4):
                usb.poll()
            usb.retry()
            usb.poll()
            self.assertEqual(len(console.written), written)  # stock firmware never sees a byte
            self.assertTrue(wait_for(lambda: "PJ-LAUNCHER-TO-STOCK" in "\n".join(usb.tail(40))))
        finally:
            usb.stop()
            console.stop()

    def test_menu_chord_is_refused_before_it_is_sent(self):
        console, usb, _ = sim_usb(key=bytes(range(32)))
        try:
            self.assertTrue(wait_for(lambda: usb.snapshot()["session"]))
            with self.assertRaises(p.ProtocolError):
                usb.press(["MENU", "A"], 100)
        finally:
            usb.stop()
            console.stop()


class AutoTests(unittest.TestCase):
    def test_usb_first_then_wifi(self):
        key = bytes(range(32))
        console = SimulatedUsbConsole(key=key, countdown_s=60, lan=True)
        plugged = [True]
        usb = UsbTransport(lambda: console, key, present=lambda: plugged[0], timeout=2.0)
        lan = LanTransport("127.0.0.1", console.launcher.port, key, allow_loopback=True, timeout=2.0)
        auto = AutoTransport(usb, lan)
        try:
            auto.start()
            self.assertTrue(wait_for(lambda: usb.snapshot()["session"]))
            auto.poll()
            self.assertEqual(auto.snapshot()["route"], "usb")
            self.assertEqual(auto.ping(), "pong")
            # Unplugged: USB is gone, Wi-Fi takes over.
            plugged[0] = False
            usb.stop()
            auto.poll()
            snap = auto.snapshot()
            self.assertEqual((snap["route"], snap["session"]), ("lan", True))
            self.assertEqual(auto.press(["DOWN"], 100), "queued")
            # Plugged back while Wi-Fi holds the session: Wi-Fi is kept (USB would get BUSY).
            plugged[0] = True
            auto.poll()
            self.assertEqual(auto.snapshot()["route"], "lan")
        finally:
            auto.stop()
            console.stop()

    def test_needs_at_least_one_transport(self):
        with self.assertRaises(ValueError):
            AutoTransport(None, None)


class RuntimeUsbTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.local = Path(self.temp.name) / "chromatic"
        self.local.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_config_defaults_and_modes(self):
        self.assertEqual(load_config(self.local)["mode"], "off")
        (self.local / "config.json").write_text(json.dumps({"serialPort": "COM5", "serialOpenAllowed": True}))
        self.assertEqual(load_config(self.local)["mode"], "auto")
        (self.local / "config.json").write_text(json.dumps({"serialPort": "COM5", "serialOpenAllowed": False}))
        self.assertEqual(load_config(self.local)["mode"], "off")
        (self.local / "config.json").write_text(json.dumps({"mode": "usb"}))
        self.assertEqual(load_config(self.local)["mode"], "usb")
        (self.local / "config.json").write_text(json.dumps({"mode": "usb-sim"}))
        with self.assertRaises(ValueError):
            load_config(self.local)
        runtime = ChromaticRuntime(self.local, MANIFEST)
        for mode in ("usb", "auto"):
            with self.assertRaises(ValueError):
                runtime.set_mode(mode)  # real transports come only from the config file

    def test_usb_pairing_keeps_a_provisioned_key(self):
        provisioned = "ab" * 32
        (self.local / "pairing.json").write_text(json.dumps({"key": provisioned}))
        save_pairing_key(self.local, "cd" * 32, source="usb-pairing")
        self.assertEqual(json.loads((self.local / "pairing.json").read_text())["key"], "cd" * 32)
        self.assertEqual(json.loads((self.local / "pairing-provisioned.json").read_text())["key"], provisioned)
        save_pairing_key(self.local, "ef" * 32, source="usb-pairing")  # a later USB pairing: nothing more to keep
        self.assertEqual(json.loads((self.local / "pairing-provisioned.json").read_text())["key"], provisioned)

    def test_configured_usb_mode_pairs_and_drives_through_the_api(self):
        (self.local / "config.json").write_text(json.dumps({"mode": "usb", "serialPort": "COM9",
                                                            "serialOpenAllowed": True, "managerRunAllowed": True}))
        console = SimulatedUsbConsole(key=None, countdown_s=60)
        runtime = ChromaticRuntime(self.local, MANIFEST, usb_port_factory=lambda: console, usb_present=lambda: True)
        try:
            runtime.start()
            self.assertTrue(wait_for(lambda: runtime.chromatic_request("status", {})["transport"]["usbState"] == "launcher"))
            started = runtime.chromatic_request("pair-usb", {"start": True})
            self.assertIn(started["pairing"]["state"], ("asking", "waiting"))
            self.assertTrue(wait_for(lambda: (runtime.chromatic_request("pair-usb", {})["pairing"] or {}).get("state")
                                     == "waiting"))
            console.approve()
            self.assertTrue(wait_for(lambda: runtime.chromatic_request("pair-usb", {})["pairing"]["state"] == "paired"))
            stored = json.loads((self.local / "pairing.json").read_text())
            self.assertEqual((stored["key"], stored["source"]), (console.key.hex(), "usb-pairing"))
            runtime.status()
            status = runtime.chromatic_request("status", {})
            self.assertTrue(status["transport"]["session"])
            features = {item["id"]: item["state"] for item in status["features"]}
            self.assertEqual((features["usb-link"], features["launcher-input"], features["launcher-screen"]),
                             ("available", "available", "available"))
            self.assertEqual(features["game-input"], "needs-fpga")
            self.assertNotIn(console.key.hex(), json.dumps(status))
            shot = runtime.chromatic_request("shot", {"format": "png"})
            self.assertTrue(shot["file"].endswith(".png") and shot["via"] == "usb")
            self.assertNotIn("path", shot)  # the bare file name only
            self.assertEqual((runtime.captures / shot["file"]).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(runtime.chromatic_request("ping", {})["result"], "pong")
            self.assertIn("fps", runtime.chromatic_request("perf", {})["perf"])
            owner = "1" * 32
            runtime.chromatic_request("arm", {"owner": owner, "enabled": True})
            self.assertEqual(runtime.chromatic_request("input", {"owner": owner, "buttons": ["DOWN"], "ms": 100})["result"],
                             "queued")
            # The Manager needs the port: refused while USB holds it.
            with self.assertRaisesRegex(ValueError, "USB-serial"):
                runtime.chromatic_request("manager-run", {"command": "status", "phrase": "READ CHROMATIC"})
        finally:
            runtime.stop()
            console.stop()

    def test_simulated_modes_from_the_viewer(self):
        runtime = ChromaticRuntime(self.local, MANIFEST)
        try:
            runtime.chromatic_request("mode", {"mode": "auto-sim"})
            self.assertTrue(wait_for(lambda: (runtime.status(), runtime.chromatic_request("status", {}))[1]
                                     ["transport"]["session"], 6.0))
            status = runtime.chromatic_request("status", {})
            self.assertEqual((status["transport"]["route"], status["transport"]["simulated"]), ("usb", True))
            runtime.chromatic_request("mode", {"mode": "usb-sim"})
            self.assertTrue(wait_for(lambda: runtime.chromatic_request("status", {})["transport"]["usbState"] == "launcher"))
            runtime.status()
            runtime.chromatic_request("pair-usb", {"start": True})
            self.assertTrue(wait_for(lambda: runtime.chromatic_request("pair-usb", {})["pairing"]["state"] == "paired", 6.0))
            self.assertFalse((self.local / "pairing.json").exists())  # simulator keys stay in memory
        finally:
            runtime.stop()


class HelperTests(unittest.TestCase):
    def test_pair_code_and_versions(self):
        digest = hashlib.sha256(b"CHR1 pair-code " + b"a" * 32 + b" " + b"b" * 32).digest()
        self.assertEqual(pair_code("a" * 32, "b" * 32), f"{int.from_bytes(digest[:4], 'big') % 1000000:06d}")
        self.assertTrue(speaks_chr1("1.1.2") and speaks_chr1("1.2.0") and speaks_chr1("2.0.0"))
        self.assertFalse(speaks_chr1("1.1.1") or speaks_chr1("1.1.0") or speaks_chr1("x"))

    def test_png(self):
        image = frame_to_png(bytes(p.SCREEN_BYTES), scale=2)
        self.assertEqual(image[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(int.from_bytes(image[16:20], "big"), 320)

    def test_status_with_launcher_112_fields(self):
        body = (b'{"phase":"menu","target":"launcher","version":"1.1.2","screen":"pairing","selected":"games",'
                b'"countdownS":null,"pcSession":true,"held":"NONE","uptimeMs":900,"transport":"usb",'
                b'"screenVisible":false,"leaseLeftMs":599100,"service":"none","servicePct":0,"key":"paired"}')
        status = parse_status(body)
        self.assertEqual((status["transport"], status["screenVisible"], status["key"]), ("usb", False, "paired"))


class ConnectTests(unittest.TestCase):
    """The shared Connect button for the Chromatic: configured transport, else list
    the console's USB port (never opened) and offer one explicit "Allow USB" click."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.local = Path(self.temp.name) / "chromatic"
        self.local.mkdir()
        self.opened = []
        self.console = SimulatedUsbConsole(key=None, countdown_s=60)
        self.addCleanup(self.console.stop)

    def tearDown(self):
        self.temp.cleanup()

    def factory(self):
        self.opened.append(True)
        return self.console

    def runtime(self, ports=("COM7",), **kwargs):
        def listing():
            if isinstance(ports, Exception):
                raise ports
            return [{"port": name} for name in ports]
        value = ChromaticRuntime(self.local, MANIFEST, usb_port_factory=self.factory, usb_present=lambda: True,
                                 usb_list=listing, **kwargs)
        self.addCleanup(value.stop)
        value.start()
        return value

    def test_connect_without_config_lists_and_offers_but_never_opens(self):
        runtime = self.runtime()
        result = runtime.connect()
        self.assertEqual((result["state"], result["mode"], result["usbOffer"]["ports"]), ("found", "off", ["COM7"]))
        self.assertIn("Allow USB on COM7", result["message"])
        self.assertIn("374E", result["message"])
        self.assertEqual(self.opened, [])  # listed only
        self.assertFalse((self.local / "config.json").exists())
        status = runtime.chromatic_request("status", {})
        self.assertEqual(status["usbOffer"]["ports"], ["COM7"])
        self.assertEqual(runtime.status()["meta"]["nextStep"], "Press Allow USB on COM7")
        usb = next(item for item in status["features"] if item["id"] == "usb-link")
        self.assertIn("Allow USB on COM7", usb["detail"])

    def test_allow_usb_needs_this_pc_and_a_listed_port_then_writes_the_config_and_opens(self):
        runtime = self.runtime()
        runtime.connect()
        with self.assertRaisesRegex(ValueError, "own browser"):
            runtime.chromatic_request("allow-usb", {"port": "COM7"}, local_client=False)
        with self.assertRaisesRegex(ValueError, "not listed"):
            runtime.chromatic_request("allow-usb", {"port": "COM8"})
        with self.assertRaises(ValueError):
            runtime.chromatic_request("allow-usb", {})
        self.assertEqual(self.opened, [])
        self.assertFalse((self.local / "config.json").exists())
        result = runtime.chromatic_request("allow-usb", {"port": "COM7"})
        self.assertEqual((result["mode"], result["port"]), ("auto", "COM7"))
        self.assertEqual(json.loads((self.local / "config.json").read_text()),
                         {"mode": "auto", "serialPort": "COM7", "serialOpenAllowed": True, "lanHost": "auto"})
        self.assertEqual(self.opened, [True])  # opened only after the click
        self.assertTrue(wait_for(lambda: runtime.chromatic_request("status", {})["transport"]["usbState"] == "launcher"))
        self.assertIsNone(runtime.chromatic_request("status", {})["usbOffer"])
        self.assertEqual(load_config(self.local)["mode"], "auto")

    def test_allow_usb_keeps_other_config_fields(self):
        (self.local / "config.json").write_text(json.dumps({"mode": "off", "managerProject": "X:\\Manager",
                                                            "lanHost": "10.123.45.40"}))
        runtime = self.runtime()
        self.assertEqual(runtime.connect()["state"], "found")
        runtime.allow_usb("COM7", local_client=True)
        self.assertEqual(json.loads((self.local / "config.json").read_text()),
                         {"mode": "auto", "managerProject": "X:\\Manager", "lanHost": "10.123.45.40",
                          "serialPort": "COM7", "serialOpenAllowed": True})

    def test_connect_switches_to_the_configured_transport(self):
        (self.local / "config.json").write_text(json.dumps({"mode": "auto", "serialPort": "COM7",
                                                            "serialOpenAllowed": True, "lanHost": "auto"}))
        runtime = self.runtime()
        self.assertEqual(runtime._mode, "auto")
        runtime.chromatic_request("mode", {"mode": "off"})  # the user chose Off in the panel
        self.assertEqual(runtime.status()["meta"]["nextStep"], "Press Connect")
        opened = len(self.opened)
        result = runtime.connect()
        self.assertEqual((result["state"], result["mode"]), ("connecting", "auto"))
        self.assertIsNone(result["usbOffer"])  # USB is already allowed on the listed port
        self.assertEqual(len(self.opened), opened + 1)
        self.assertTrue(runtime.status()["session"])
        again = runtime.connect()  # pressing Connect again does not reopen the port
        self.assertEqual((again["state"], len(self.opened)), ("connecting", opened + 1))

    def test_connect_with_a_config_written_after_start(self):
        runtime = self.runtime()
        self.assertEqual(runtime._mode, "off")
        (self.local / "config.json").write_text(json.dumps({"mode": "usb", "serialPort": "COM7",
                                                            "serialOpenAllowed": True}))
        self.assertEqual(runtime.connect()["mode"], "usb")  # re-read from disk

    def test_no_console_and_no_pyserial(self):
        runtime = self.runtime(ports=())
        result = runtime.connect()
        self.assertEqual((result["state"], result["usbOffer"]["ports"]), ("none", []))
        self.assertIn("No Chromatic USB port found", result["message"])
        self.assertEqual(runtime.status()["meta"]["nextStep"], "Press Connect")
        runtime = self.runtime(ports=SerialSupportMissing())
        result = runtime.connect()
        self.assertEqual(result["state"], "no-pyserial")
        self.assertIn("USB support (pyserial) is not installed in this Python", result["message"])
        self.assertNotIn("not connected", result["message"])
        self.assertIn("pyserial", runtime.status()["meta"]["nextStep"])
        with self.assertRaisesRegex(ValueError, "pyserial"):
            runtime.allow_usb("COM7", local_client=True)
        self.assertEqual(self.opened, [])

    def test_wording_says_what_to_do_next(self):
        runtime = ChromaticRuntime(self.local, MANIFEST)
        step = runtime.next_step
        self.assertEqual(step("off", {}), "Press Connect")
        self.assertIn("press a on the console", step("usb", {"usbState": "nokey"}).lower())
        self.assertIn("press a on the console", step("auto", {"alternate": {"usbState": "denied"}}).lower())
        self.assertEqual(step("usb", {"pairing": {"state": "waiting", "code": "123456"}}),
                         "Press A on the console (code 123456)")
        self.assertIn("pyserial", step("usb", {"usbState": "no-pyserial"}))
        self.assertEqual(step("usb", {"connected": True, "session": True}), "Connected")
        self.assertEqual(step("lan", {}), "Waiting for the console")
        for text in (step("off", {}), step("usb", {"usbState": "unplugged"}), step("usb", {"usbState": "stock"})):
            self.assertNotEqual(text, "Remote session off")
            self.assertLessEqual(len(text), 80)


class PyserialMissingTests(unittest.TestCase):
    """Without pyserial nothing can be listed: say so instead of "not connected"."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.local = Path(self.temp.name) / "chromatic"
        self.local.mkdir()
        self.blocked = mock.patch.dict(sys.modules, {"serial": None, "serial.tools": None,
                                                     "serial.tools.list_ports": None})
        self.blocked.start()

    def tearDown(self):
        self.blocked.stop()
        self.temp.cleanup()

    def test_listing_raises_instead_of_reporting_unplugged(self):
        self.assertFalse(pyserial_available())
        with self.assertRaises(SerialSupportMissing) as caught:
            port_present("COM5")
        self.assertEqual(str(caught.exception), "USB support (pyserial) is not installed in this Python")
        self.assertIsInstance(caught.exception, ConnectionError)
        with self.assertRaises(SerialSupportMissing):
            find_consoles()

    def test_usb_transport_and_features_name_pyserial(self):
        opened = []
        transport = UsbTransport(lambda: opened.append(1), None, present=lambda: port_present("COM5"))
        transport.start()
        snap = transport.snapshot()
        self.assertEqual(snap["usbState"], "no-pyserial")
        self.assertIn("pyserial", snap["error"])
        self.assertEqual(opened, [])
        (self.local / "config.json").write_text(json.dumps({"mode": "usb", "serialPort": "COM5",
                                                            "serialOpenAllowed": True}))
        runtime = ChromaticRuntime(self.local, MANIFEST)  # the real port_present and find_consoles
        try:
            runtime.start()
            status = runtime.chromatic_request("status", {})
            self.assertEqual(status["transport"]["usbState"], "no-pyserial")
            usb = next(item for item in status["features"] if item["id"] == "usb-link")
            self.assertIn("USB support (pyserial) is not installed in this Python", usb["detail"])
            self.assertNotIn("not connected", usb["detail"])
            self.assertIn("pyserial", runtime.status()["meta"]["nextStep"])
            self.assertEqual(runtime.connect()["usbOffer"]["state"], "no-pyserial")
        finally:
            runtime.stop()


class PacedPort:
    """A console port that sends what it is given at a set pace (bytes per model second), with a model clock that
    runs `speed` times faster than real time, so a 46 KB reply at 2.5 KB/s takes under a second here."""

    def __init__(self, speed=40.0):
        self.speed, self.t0 = speed, time.monotonic()
        self._queue = deque()  # (bytes, rate)
        self._lock = threading.Lock()
        self._credit_at = self.clock()
        self.written = []

    def clock(self):
        return (time.monotonic() - self.t0) * self.speed

    def send(self, data, rate=11520.0):
        with self._lock:
            self._queue.append([bytearray(data), rate])

    def read(self, size):
        time.sleep(0.005)
        now = self.clock()
        with self._lock:
            elapsed, self._credit_at = now - self._credit_at, now
            if not self._queue:
                return b""
            chunk, rate = self._queue[0]
            n = min(len(chunk), size, max(0, int(elapsed * rate)))
            data = bytes(chunk[:n])
            del chunk[:n]
            if not chunk:
                self._queue.popleft()
            return data

    def write(self, data):
        self.written.append(bytes(data))

    def close(self):
        pass


class UsbPaceTests(unittest.TestCase):
    """A slow console must not drop the USB session: launchers before 1.3.2 send only about 2.5 KB/s
    while their Jellyfin app is open. These are the PC client's parts of that behaviour."""

    def link(self, port):
        link = UsbLink(port, lambda line: None, clock=port.clock)
        link.timeout = 3.0
        return link.start()

    def test_a_slow_reply_is_waited_for_while_its_bytes_keep_coming(self):
        port = PacedPort()
        link = self.link(port)
        try:
            link.begin_session()
            port.send(b"x" * 46080, rate=2500)  # a raw screen at the pace of a launcher before 1.3.2 with Jellyfin open
            start = port.clock()
            self.assertEqual(len(link.read(46080)), 46080)
            took = port.clock() - start
            self.assertGreater(took, 9.0)  # past the old fixed 9 s limit, which dropped the session
            self.assertLess(link.rate, 6000)
        finally:
            link.close()

    def test_a_silent_console_is_still_given_up_on_in_3_s(self):
        port = PacedPort(speed=5.0)  # the client's 0.1 s (real) wait steps stay small in model time
        link = self.link(port)
        try:
            link.begin_session()
            port.send(b"y" * 5000, rate=2500)  # 2 s of a reply, then nothing: the console hung or was unplugged
            start = port.clock()
            with self.assertRaises(TimeoutError):
                link.read(46080)
            self.assertLess(port.clock() - start, 2.0 + 3.0 + 0.8)
        finally:
            link.close()

    def test_the_answer_after_a_dropped_reply_is_kept(self):
        """The PC gave up mid-reply and sends CHR1 OPEN; the console stops the reply and answers right after its
        last bytes. Before, that answer fell inside the 0.3 s quiet window and was dropped: "no-answer", and USB
        stayed down until the next launcher start."""
        port = PacedPort()
        link = self.link(port)
        try:
            link.begin_session()
            port.send(b"\x00\x01binary\n" * 40, rate=11520)
            self.assertTrue(wait_for(lambda: len(link._buffer) > 100, 2))
            link.end_session(quiet=True)
            port.send(b"\x07leftover reply bytes\n\x00" * 20 + b"CHR1 HELLO " + b"a" * 32 + b" 1.3.2\n", rate=11520)
            link.expect_answer()
            line = link.answer(3.0)
            self.assertEqual(line, "CHR1 HELLO " + "a" * 32 + " 1.3.2")
        finally:
            link.close()

    def marker_link(self):
        port, seen = PacedPort(speed=1.0), []  # real time: the 0.3 s quiet window is a real 0.3 s here
        link = UsbLink(port, seen.append, clock=port.clock)
        link.timeout = 3.0
        return port, link.start(), seen

    def test_markers_in_the_quiet_window_are_not_dropped(self):
        """1.3.2 review: bytes in the 0.3 s quiet window after a session were dropped unread, so a hand-over or a
        launcher start right after a session was missed. The markers are found in them (and nothing else is)."""
        port, link, seen = self.marker_link()
        try:
            link.end_session(quiet=True)
            port.send(b"\x07rest of a reply\nPJ-DIAG t=1 handover\nPJ-LAUNCHER-TO-STOCK\n\x00more\n"
                      b"\x01junk\x02\r\nPJ-LAUNCHER-READY 1.3.3\n")
            self.assertTrue(wait_for(lambda: len(seen) >= 2, 2))
            self.assertLess(port.clock(), link._quiet_until)  # all of it inside the window
            self.assertEqual(seen, [b"PJ-LAUNCHER-TO-STOCK", b"PJ-LAUNCHER-READY 1.3.3"])  # nothing else of it
        finally:
            link.close()

    def test_a_marker_in_the_captured_leftover_is_not_dropped(self):
        """1.3.3 review: a hand-over printed while a reply was still being captured sits in the session's leftover,
        which end_session(quiet=True) dropped unscanned."""
        port, link, seen = self.marker_link()
        try:
            with link._cond:
                link._buffer.extend(b"\x07part of a reply\nPJ-DIAG t=2 handover\nPJ-LAUNCHER-TO-STOCK\n\x00")
            link.end_session(quiet=True)
            self.assertTrue(wait_for(lambda: seen, 2))
            self.assertEqual(seen, [b"PJ-LAUNCHER-TO-STOCK"])
        finally:
            link.close()

    def test_a_marker_split_across_reads_is_found_once(self):
        port, link, seen = self.marker_link()
        try:
            link.end_session(quiet=True)
            port.send(b"\x00leftover PJ-LAUNCHER-RE")
            self.assertTrue(wait_for(lambda: not port._queue, 2))
            time.sleep(0.05)
            port.send(b"ADY 1.3.3\n\x00\x01PJ-LAUNCHER-TO-")  # the window ends inside the next marker
            self.assertTrue(wait_for(lambda: seen, 2))
            self.assertTrue(wait_for(lambda: port.clock() > link._quiet_until + 0.05, 2))
            port.send(b"STOCK\nPJ-DIAG t=9 after\n")  # read as text again: the marker's rest is not a text line
            self.assertTrue(wait_for(lambda: len(seen) >= 3, 2))
            self.assertEqual(seen, [b"PJ-LAUNCHER-READY 1.3.3", b"PJ-LAUNCHER-TO-STOCK", b"PJ-DIAG t=9 after"])
        finally:
            link.close()

    def test_a_marker_behind_leftover_bytes_after_the_window_is_found(self):
        port, link, seen = self.marker_link()
        try:
            link.end_session(quiet=True)
            self.assertTrue(wait_for(lambda: port.clock() > link._quiet_until + 0.05, 2))
            port.send(b"\x00\x07tail of a reply PJ-LAUNCHER-TO-STOCK\n")
            self.assertTrue(wait_for(lambda: seen, 2))
            self.assertEqual(seen, [b"PJ-LAUNCHER-TO-STOCK"])
        finally:
            link.close()

    def test_markers_skipped_by_a_resync_are_still_seen(self):
        port, link, seen = self.marker_link()
        try:
            link.end_session(quiet=True)
            link.expect_answer()
            port.send(b"\x07leftover\nPJ-LAUNCHER-TO-ST")
            self.assertTrue(wait_for(lambda: not port._queue, 2))
            time.sleep(0.05)
            port.send(b"OCK\n\x00ets Jun  8 2016\r\nPJ-LAUNCHER-READY 1.3.3\nCHR1 HELLO " + b"b" * 32 + b" 1.3.3\n")
            self.assertEqual(link.answer(3.0), "CHR1 HELLO " + "b" * 32 + " 1.3.3")
            self.assertEqual(seen, [b"PJ-LAUNCHER-TO-STOCK", b"PJ-LAUNCHER-READY 1.3.3"])
        finally:
            link.close()

    def test_a_hand_over_found_in_the_window_stops_all_writes(self):
        """The transport takes a marker found in the window as it takes one read as text: no write to stock."""
        usb = UsbTransport(lambda: None, key=b"k" * 32)
        usb._may_open, usb._usb_state = True, "launcher"
        usb._ingest(b"PJ-LAUNCHER-TO-STOCK")
        self.assertEqual(usb._usb_state, "stock")
        self.assertFalse(usb._may_open)

    def test_slow_console_eases_polling_and_says_why(self):
        usb = UsbTransport(lambda: None, key=b"k" * 32)
        usb._sock = object()
        usb._link = types.SimpleNamespace(rate=2400.0, last_rx=None, closed=False, error="")
        self.assertTrue(usb.slow)
        reads = [usb._want_log() for _ in range(10)]
        self.assertEqual(reads.count(True), 2)  # the 4 KB log tail every 5th poll, not every one
        usb._link.rate = 10500.0  # launcher 1.3.2: the line's own pace
        self.assertFalse(usb.slow)
        self.assertTrue(all(usb._want_log() for _ in range(5)))


class ConsoleListingTests(unittest.TestCase):
    def test_find_consoles_filters_by_vendor_id_and_keeps_identifiers_out(self):
        item = types.SimpleNamespace
        listing = [item(device="COM3", vid=0x374E, pid=0x0001, serial_number="SECRET-SERIAL"),
                   item(device="COM9", vid=0x1A86, pid=0x7523, serial_number="other"),
                   item(device="COM12", vid=None, pid=None, serial_number=None),
                   item(device="COM4", vid=0x374E, pid=0x0002, serial_number=None),
                   item(device="not a port", vid=0x374E, pid=0x0001, serial_number=None)]
        found = find_consoles(lambda: listing)
        self.assertEqual(found, [{"port": "COM3"}, {"port": "COM4"}])
        self.assertNotIn("SECRET", json.dumps(found))
        self.assertEqual(CHROMATIC_USB_VID, 0x374E)


class LauncherWordingTests(unittest.TestCase):
    def test_launcher_requirement_claims_no_install_state(self):
        from chromatic import runtime as module
        self.assertIn("1.1.2 or newer", module.LAUNCHER_NOTE)
        self.assertNotIn("not installed", module.LAUNCHER_NOTE)
        with tempfile.TemporaryDirectory() as temp:
            runtime = ChromaticRuntime(Path(temp) / "chromatic", MANIFEST)
            features = runtime.chromatic_request("status", {})["features"]
        launcher_text = " ".join(item["label"] + " " + item["detail"] for item in features
                                 if item["group"] in ("connection", "launcher"))
        self.assertNotIn("not installed", launcher_text)
        wifi = next(item for item in features if item["id"] == "wifi-link")
        self.assertIn("1.1.2 or newer", wifi["detail"])


# ---- the launcher's own PC harness (check_remote.c) -------------------------------------

class HarnessPort:
    """serve-usb's stdin/stdout as a console port. The harness clock moves only when it
    reads input, so idle reads send "#wait 100" (a harness directive, not a CHR1 line,
    written straight to the harness, never through UsbLink). The harness's USB model
    starts after the boot marker, so this port first plays the one line the real
    launcher prints at power-on: PJ-LAUNCHER-READY 1.1.2."""

    def __init__(self, exe, flash, key="none"):
        env = dict(os.environ, PATH=str(GCC.parent) + os.pathsep + os.environ.get("PATH", ""))
        self.process = subprocess.Popen([str(exe), "serve-usb", str(flash), key], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, env=env)
        self.chunks = queue.Queue()
        self.boot = deque([b"PJ-LAUNCHER-READY 1.1.2\n"])
        self.written = []
        self._lock = threading.Lock()
        self._last = time.monotonic()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while True:
            data = self.process.stdout.read1(65536)
            if not data:
                break
            self.chunks.put(data)

    def _raw(self, data):
        with self._lock:
            if self.process.poll() is None:
                try:
                    self.process.stdin.write(data)
                    self.process.stdin.flush()
                except OSError:
                    pass
            self._last = time.monotonic()

    def read(self, size):
        if self.boot:
            return self.boot.popleft()
        try:
            return self.chunks.get(timeout=0.05)
        except queue.Empty:
            if time.monotonic() - self._last > 0.1:
                self._raw(b"#wait 100\n")
            return b""

    def write(self, data):
        self.written.append(bytes(data))
        self._raw(bytes(data))

    def press(self, button):
        self._raw(f"#press {button}\n".encode("ascii"))  # a physical press on the modelled console

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()


@unittest.skipUnless((LAUNCHER / "check_remote.c").is_file() and GCC.is_file(),
                     "the launcher's PC harness or gcc is not available")
class LauncherHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        folder = Path(cls.temp.name)
        cls.exe = folder / "check_remote.exe"
        env = dict(os.environ, PATH=str(GCC.parent) + os.pathsep + os.environ.get("PATH", ""))
        subprocess.run([str(GCC), "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", str(LAUNCHER / "check_remote.c"),
                        "-lws2_32", "-o", str(cls.exe)], check=True, env=env, capture_output=True)
        data = bytearray(hashlib.shake_256(b"remote-pilot-usb-test").digest(0x400000))  # synthetic flash image
        data[0x300000:] = b"\xff" * 0x100000
        cls.flash = folder / "synthetic-flash.bin"
        cls.flash.write_bytes(bytes(data))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_usb_pairing_and_menu_against_the_real_handler(self):
        port = HarnessPort(self.exe, self.flash)
        keys = []
        usb = UsbTransport(lambda: port, None, on_paired=keys.append, timeout=5.0)
        try:
            usb.start()
            self.assertTrue(wait_for(lambda: usb.snapshot()["usbState"] == "launcher"))
            usb.start_pairing()
            self.assertTrue(wait_for(lambda: (usb.pairing_state() or {}).get("state") == "waiting"))
            port.press("A")
            self.assertTrue(wait_for(lambda: usb.pairing_state()["state"] == "paired", 10))
            self.assertEqual(len(keys), 1)
            usb.poll()
            snap = usb.snapshot()
            self.assertTrue(snap["session"], snap["error"])
            self.assertEqual((snap["launcherVersion"], snap["deviceTransport"], snap["keyKind"]), ("1.1.2", "usb", "paired"))
            self.assertEqual(usb.press(["DOWN"], 100), "queued")
            usb.poll()
            self.assertEqual(usb.snapshot()["selected"], "jellyfin")
            self.assertEqual(len(usb.screen()), p.SCREEN_BYTES)
            self.assertEqual(len(usb.video_frame()), p.SCREEN_BYTES)  # keyframes; a delta request gets one too
            self.assertEqual(len(usb.video_frame()), p.SCREEN_BYTES)
            self.assertEqual(usb.hold(("UP",)), "held")
            self.assertEqual(usb.hold(()), "released")
            self.assertEqual(usb.ping(), "pong")
            self.assertEqual(usb.console("version"), "launcher 1.1.2")
            self.assertIn("fps", usb.perf())
            self.assertIn("remote input DOWN", usb.log_text())
            self.assertNotIn(keys[0], usb.log_text() + "\n".join(usb.tail(200)))
            self.assertEqual(usb.press(["MENU"], 100), "handover")
            self.assertTrue(wait_for(lambda: "PJ-LAUNCHER-TO-STOCK" in "\n".join(usb._parser.tail(20)), 10))
            written = len(port.written)
            usb.poll()
            usb.poll()
            self.assertEqual(len(port.written), written)
            self.assertEqual(usb.snapshot()["phase"], "stock")
        finally:
            usb.stop()
            port.close()

    def test_provisioned_key_session_and_refused_pairing(self):
        key = bytes(range(32))
        port = HarnessPort(self.exe, self.flash, key.hex())
        usb = UsbTransport(lambda: port, None, timeout=5.0)
        try:
            usb.start()
            self.assertTrue(wait_for(lambda: usb.snapshot()["usbState"] == "launcher"))
            usb.start_pairing()
            self.assertTrue(wait_for(lambda: usb.pairing_state()["state"] == "refused"))
            usb._key = bytes(32)  # the wrong key: denied, and the console waits a second
            usb.poll()
            self.assertEqual(usb.snapshot()["usbState"], "denied")
            usb._key = key
            time.sleep(1.2)
            usb.poll()
            snap = usb.snapshot()
            self.assertTrue(snap["session"], snap["error"])
            self.assertEqual(snap["keyKind"], "provisioned")
            usb.stop()  # bye: the console waits for CHR1 OPEN again
        finally:
            usb.stop()
            port.close()

    def test_wifi_through_the_automatic_choice(self):
        key = secrets_key = bytes(range(64, 96))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]
        env = dict(os.environ, PATH=str(GCC.parent) + os.pathsep + os.environ.get("PATH", ""))
        server = subprocess.Popen([str(self.exe), "serve-lan", str(free), str(self.flash), secrets_key.hex()],
                                  stdout=subprocess.PIPE, env=env)
        try:
            self.assertEqual(server.stdout.readline().strip(), b"READY")
            lan = LanTransport("127.0.0.1", free, key, allow_loopback=True, timeout=5.0)
            auto = AutoTransport(None, lan)
            auto.poll()
            snap = auto.snapshot()
            self.assertEqual((snap["route"], snap["session"], snap["deviceTransport"]), ("lan", True, "lan"))
            self.assertEqual(auto.press(["DOWN"], 100), "queued")
            self.assertEqual(auto.ping(), "pong")
            self.assertEqual(len(auto.screen()), p.SCREEN_BYTES)
            self.assertEqual(auto.press(["MENU"], 100), "handover")
            self.assertEqual(server.wait(timeout=15), 0)
        finally:
            if server.poll() is None:
                server.kill()
            server.wait()
            server.stdout.close()


if __name__ == "__main__":
    unittest.main()

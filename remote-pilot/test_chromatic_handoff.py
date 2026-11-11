# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""USB hand-off with Chromatic Manager (chromatic/usb_hold.py and the usb-* API actions).

Against the simulators only (auto-sim, usb-sim, serial-sim and a fake configured USB
port): no real serial port is opened and no console is touched. Checks that a hold on
disk keeps every transport away from the port, that usb-release closes it at once while
Wi-Fi keeps its session, that usb-return reopens it, and the crash-safety rules: an
unreadable hold counts as held, a restarted runtime still respects the file, a forced
return needs the holder gone and no Manager journal.
"""

import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from chromatic import api, usb_hold
from chromatic.runtime import ChromaticRuntime
from chromatic.usb_sim import SimulatedUsbConsole

ROOT = Path(__file__).resolve().parent
MANIFEST = json.loads((ROOT / "presets" / "chromatic" / "manifest.json").read_text(encoding="utf-8"))
# Keep the Manager's per-user folder (looked at first) empty and temporary in these tests.
_JELLYMATIC_TEMP = tempfile.TemporaryDirectory(prefix="rp-jellymatic-test-")
os.environ["JELLYMATIC_HOME"] = str(Path(_JELLYMATIC_TEMP.name) / "home")
os.environ["JELLYMATIC_SECOND_COPY"] = str(Path(_JELLYMATIC_TEMP.name) / "second")


def wait_for(predicate, seconds=6.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.03)
    return predicate()


def release_payload(hold_id=None, reason="Install the start-up menu"):
    return {"id": hold_id or secrets.token_hex(16), "holder": "Chromatic Manager", "reason": reason,
            "pid": os.getpid()}


class HoldFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.local = Path(self.temp.name) / "chromatic"

    def tearDown(self):
        self.temp.cleanup()

    def test_write_read_clear(self):
        self.assertIsNone(usb_hold.read_hold(self.local))
        hold_id = secrets.token_hex(16)
        hold = usb_hold.write_hold(self.local, hold_id, "Chromatic Manager", "Back up the console", os.getpid())
        self.assertEqual((hold["id"], hold["holder"], hold["unreadable"]), (hold_id, "Chromatic Manager", False))
        self.assertFalse(usb_hold.clear_hold(self.local, secrets.token_hex(16)))  # someone else's id
        self.assertIsNotNone(usb_hold.read_hold(self.local))
        self.assertTrue(usb_hold.clear_hold(self.local, hold_id))
        self.assertIsNone(usb_hold.read_hold(self.local))
        self.assertEqual(list(self.local.iterdir()), [])  # no temporary files left behind

    def test_unreadable_counts_as_held(self):
        self.local.mkdir(parents=True)
        for body in ("{", "[]", json.dumps({"format": 2}), json.dumps({"format": 1, "id": "x"}), "x" * 5000):
            usb_hold.hold_path(self.local).write_text(body, encoding="utf-8")
            hold = usb_hold.read_hold(self.local)
            self.assertTrue(hold and hold["unreadable"], body[:20])

    def test_bad_fields_refused(self):
        for args in (("short", "Chromatic Manager", "", 1), (secrets.token_hex(16), "", "", 1),
                     (secrets.token_hex(16), "Chromatic Manager", "x" * 200, 1),
                     (secrets.token_hex(16), "Chromatic Manager", "", "1"),
                     (secrets.token_hex(16), "Chromatic Manager", "<script>", 1)):
            with self.assertRaises(ValueError):
                usb_hold.write_hold(self.local, *args)

    def test_process_alive_never_signals(self):
        self.assertTrue(usb_hold.process_alive(os.getpid()))
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=20)
        self.assertFalse(usb_hold.process_alive(child.pid))
        self.assertTrue(usb_hold.process_alive(None))  # unknown counts as alive: the hold stays
        self.assertTrue(usb_hold.process_alive(os.getpid()))  # still here: nothing was sent to it

    def test_public_view_has_no_id(self):
        hold = usb_hold.write_hold(self.local, secrets.token_hex(16), "Chromatic Manager", "", os.getpid())
        view = usb_hold.public(hold)
        self.assertNotIn(hold["id"], json.dumps(view))
        self.assertTrue(view["held"] and view["holderRunning"])


class RuntimeHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.local = Path(self.temp.name) / "chromatic"
        self.local.mkdir()
        self.runtime = ChromaticRuntime(self.local, MANIFEST)

    def tearDown(self):
        self.runtime.stop()
        self.temp.cleanup()

    def request(self, action, payload=None, local_client=True):
        return self.runtime.chromatic_request(action, payload or {}, local_client=local_client)

    def snap(self):
        self.runtime.status()
        return self.request("status")["transport"]

    def test_auto_sim_release_keeps_wifi_and_return_reopens_usb(self):
        self.request("mode", {"mode": "auto-sim"})
        self.assertTrue(wait_for(lambda: self.snap()["session"] and self.snap()["route"] == "usb"))
        payload = release_payload()
        result = self.request("usb-release", payload)
        self.assertEqual((result["held"], result["portOpen"]), (True, False))
        self.assertTrue(usb_hold.read_hold(self.local))
        # Wi-Fi takes over the one CHR1 session while USB is paused.
        self.assertTrue(wait_for(lambda: self.snap()["session"] and self.snap()["route"] == "lan", 10.0))
        status = self.request("status")
        self.assertTrue(status["usbHold"]["held"] and status["usbHold"]["holder"] == "Chromatic Manager")
        self.assertNotIn(payload["id"], json.dumps(status))
        usb_link = next(item for item in status["features"] if item["id"] == "usb-link")
        self.assertIn("USB paused: Chromatic Manager is using the cable", usb_link["detail"])
        # Nothing reopens the port while the hold is on disk: not a poll, not a retry, not a start.
        transport = self.runtime._transport
        for attempt in (transport.usb.start, transport.usb.retry, transport.retry, self.runtime.retry):
            attempt()
            self.assertFalse(transport.usb.port_open)
        time.sleep(3.3)  # past the port's reopen interval
        self.snap()
        self.assertFalse(transport.usb.port_open)
        self.assertIn("paused", self.runtime.status()["meta"]["nextStep"] + transport.usb.snapshot()["error"])
        # The wrong id does not end the hold; the right one does and USB opens again.
        with self.assertRaisesRegex(ValueError, "not the current USB hold"):
            self.request("usb-return", {"id": secrets.token_hex(16)})
        self.assertFalse(transport.usb.port_open)
        back = self.request("usb-return", {"id": payload["id"]})
        self.assertEqual((back["held"], back["portOpen"]), (False, True))
        self.assertIsNone(usb_hold.read_hold(self.local))
        self.assertIsNone(self.request("status")["usbHold"])
        self.assertTrue(wait_for(lambda: self.snap()["session"]))
        # Returning again is harmless.
        self.assertFalse(self.request("usb-return", {"id": payload["id"]})["held"])

    def test_hold_survives_a_restart_and_blocks_usb_from_the_start(self):
        usb_hold.write_hold(self.local, secrets.token_hex(16), "Chromatic Manager", "Restore", os.getpid())
        self.runtime.stop()
        self.runtime = ChromaticRuntime(self.local, MANIFEST)  # as after a receiver restart mid-job
        self.request("mode", {"mode": "usb-sim"})
        time.sleep(0.5)
        transport = self.runtime._transport
        self.assertFalse(transport.port_open)
        self.assertIn("Chromatic Manager", transport.snapshot()["error"])

    def test_unreadable_hold_blocks_usb(self):
        usb_hold.hold_path(self.local).write_text("not json", encoding="utf-8")
        self.request("mode", {"mode": "usb-sim"})
        time.sleep(0.4)
        self.assertFalse(self.runtime._transport.port_open)
        with self.assertRaises(ValueError):
            self.request("usb-return", {"id": secrets.token_hex(16)})
        usb_hold.remove_any(self.local)
        self.runtime._transport.start()
        self.assertTrue(self.runtime._transport.port_open)

    def test_serial_reader_is_switched_off_and_back(self):
        self.request("mode", {"mode": "serial-sim"})
        self.assertTrue(wait_for(lambda: self.snap()["kind"] == "serial" and self.snap()["lines"] > 0))
        payload = release_payload()
        self.assertFalse(self.request("usb-release", payload)["portOpen"])
        self.assertEqual(self.request("status")["mode"], "off")
        self.request("usb-return", {"id": payload["id"]})
        self.assertEqual(self.request("status")["mode"], "serial-sim")

    def test_configured_usb_port_never_opens_while_held(self):
        (self.local / "config.json").write_text(json.dumps({"mode": "usb", "serialPort": "COM9",
                                                            "serialOpenAllowed": True}))
        opened = []
        console = SimulatedUsbConsole(key=None, countdown_s=60)

        def factory():
            opened.append(1)
            return console
        self.runtime.stop()
        self.runtime = ChromaticRuntime(self.local, MANIFEST, usb_port_factory=factory, usb_present=lambda: True)
        try:
            self.runtime.start()
            self.assertEqual(len(opened), 1)
            payload = release_payload()
            self.assertFalse(self.request("usb-release", payload)["portOpen"])
            for _ in range(3):
                self.runtime._transport.start()
                self.runtime._transport.retry()
            self.assertEqual(len(opened), 1)  # the real factory was never called again
            self.request("usb-return", {"id": payload["id"]})
            self.assertEqual(len(opened), 2)
            self.assertTrue(self.runtime._transport.port_open)
        finally:
            console.stop()

    def test_force_return_rules(self):
        payload = release_payload()
        self.request("usb-release", payload)
        with self.assertRaisesRegex(ValueError, "still running"):
            self.request("usb-return", {"force": True})  # this process is the holder and is alive
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=20)
        usb_hold.write_hold(self.local, payload["id"], "Chromatic Manager", "Install", child.pid)
        project = Path(self.temp.name) / "manager"
        (project / "scripts").mkdir(parents=True)
        (project / "scripts" / "chromatic_manager.py").write_text("# stand-in\n")
        (self.local / "config.json").write_text(json.dumps({"mode": "off", "managerProject": str(project)}))
        self.runtime._reload_config()
        journal = project / "private" / "chromatic-manager" / "journal.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text("{}")
        with self.assertRaisesRegex(ValueError, "did not finish"):
            self.request("usb-return", {"force": True})
        self.assertIsNotNone(usb_hold.read_hold(self.local))
        journal.unlink()
        self.assertFalse(self.request("usb-return", {"force": True})["held"])
        self.assertIsNone(usb_hold.read_hold(self.local))

    def test_only_this_pc_and_valid_requests(self):
        with self.assertRaisesRegex(ValueError, "this PC"):
            self.request("usb-release", release_payload(), local_client=False)
        with self.assertRaisesRegex(ValueError, "this PC"):
            self.request("usb-return", {"force": True}, local_client=False)
        for bad in ({}, {"id": "zz", "pid": 1}, {**release_payload(), "pid": -1}, {**release_payload(), "reason": "\n"}):
            with self.assertRaises(ValueError):
                self.request("usb-release", bad)
        self.assertIsNone(usb_hold.read_hold(self.local))


class ApiRouteTests(unittest.TestCase):
    """The usb-* actions need neither the Chromatic preset selected nor the PC link on."""

    def test_routes_without_selection_or_link(self):
        temp = tempfile.TemporaryDirectory()
        try:
            local = Path(temp.name) / "chromatic"
            local.mkdir()
            runtime = ChromaticRuntime(local, MANIFEST)

            class Plugin:
                capabilities = frozenset({"chromatic"})
                adapter = runtime
            runtimes = {"chromatic": Plugin()}
            payload = release_payload()
            value = api.handle(runtimes, "usb-release", payload, "example", False, local_client=True)
            self.assertTrue(value["held"])
            self.assertTrue(api.handle(runtimes, "usb-hold", {}, "example", False, local_client=True)["usbHold"]["held"])
            with self.assertRaises(ValueError):
                api.handle(runtimes, "usb-return", {"id": payload["id"]}, "example", False, local_client=False)
            self.assertFalse(api.handle(runtimes, "usb-return", {"id": payload["id"]}, "example", False,
                                        local_client=True)["held"])
            runtime.stop()
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()

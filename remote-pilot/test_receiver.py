# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""The shared receiver: local-only binding, request gates, capture files and the python-v1 contract."""

import contextlib
import http.client
import json
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock

import receiver
from plugin_runtime import PluginRuntime


def stacked_bmp(width, height):
    body = bytearray(54 + width * height * 3)
    body[:2] = b"BM"
    struct.pack_into("<I", body, 2, len(body))
    struct.pack_into("<I", body, 10, 54)
    struct.pack_into("<I", body, 14, 40)
    struct.pack_into("<II", body, 18, width, height)
    struct.pack_into("<HH", body, 26, 1, 24)
    struct.pack_into("<I", body, 34, width * height * 3)
    return bytes(body)


class FakeAdapter:
    """A minimal python-v1 adapter, as a third party would write one."""

    def __init__(self):
        self.requests = []

    def status(self):
        return {"connected": True, "session": True, "host": "", "discovery": "paired", "meta": None, "perf": None}

    def request(self, kind, payload):
        self.requests.append((kind, payload))
        if kind == "shot":
            return stacked_bmp(400, 480)
        if kind == "stream":
            return stacked_bmp(200, 240), b"\x00\x00" * 8
        if kind == "report":
            return "report text"
        return "ok"


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        local = Path(self.temp.name)
        self.patches = [mock.patch.object(receiver, name, value) for name, value in (
            ("LOCAL", local), ("DB", local / "queue.sqlite3"), ("CAPTURES", local / "captures"),
            ("SELECTED_PRESET", local / "selected-preset.json"))]
        for patch in self.patches:
            patch.start()
        receiver.selected_preset = "example"
        receiver.link_enabled = True
        receiver.plugin_runtimes = {}
        self.manifest = {"id": "sample", "name": "Sample", "mark": "S", "transport": "python-v1",
                         "models": ["basic"], "capabilities": ["screenshot", "stream", "audio", "console", "report"],
                         "remoteReport": True, "theme": {}}
        self.adapter = FakeAdapter()
        runtime = mock.Mock(spec=PluginRuntime)
        runtime.adapter = self.adapter
        runtime.status.side_effect = lambda: self.adapter.status()
        runtime.request.side_effect = self.adapter.request
        receiver.plugin_runtimes["sample"] = runtime
        original = receiver.public_presets
        self.presets = mock.patch.object(receiver, "public_presets", lambda: original() + [self.manifest])
        self.presets.start()
        self.viewer = receiver.ThreadingHTTPServer(("127.0.0.1", 0), receiver.Viewer)
        self.viewer.lan_enabled = False
        self.thread = threading.Thread(target=self.viewer.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.viewer.shutdown()
        self.viewer.server_close()
        self.thread.join()
        self.presets.stop()
        for patch in self.patches:
            patch.stop()
        receiver.plugin_runtimes = {}
        receiver.selected_preset = None
        self.temp.cleanup()

    def call(self, method, path, body=None, headers=None, host=None):
        client = http.client.HTTPConnection("127.0.0.1", self.viewer.server_port, timeout=5)
        payload = json.dumps(body).encode() if body is not None else b""
        sent = {"Content-Type": "application/json", "Host": host or f"127.0.0.1:{self.viewer.server_port}"}
        sent.update(headers or {})
        client.request(method, path, payload, sent)
        answer = client.getresponse()
        result = answer.status, answer.read(), dict(answer.getheaders())
        client.close()
        return result

    def post(self, path, body=None, **headers):
        return self.call("POST", path, body or {}, {"X-Remote-Pilot-Viewer": "1", **headers})

    def test_default_bind_is_loopback_only(self):
        source = (Path(receiver.__file__).read_text(encoding="utf-8"))
        self.assertIn('ThreadingHTTPServer(("0.0.0.0" if args.viewer_lan else "127.0.0.1", args.viewer_port), Viewer)', source)

    def test_page_and_security_headers(self):
        status, body, headers = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Remote pilot", body)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        # The preset stylesheets' inline SVG patterns load; scripts stay same-origin only.
        self.assertIn("img-src 'self' data:;", headers["Content-Security-Policy"])
        self.assertIn("script-src 'self';", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_dns_rebinding_and_cross_site_requests_are_refused(self):
        self.assertEqual(self.call("GET", "/", host=f"evil.example:{self.viewer.server_port}")[0], 403)
        self.assertEqual(self.call("GET", "/", host=f"localhost:{self.viewer.server_port}")[0], 403)
        self.assertEqual(self.call("GET", "/", host="127.0.0.1:1")[0], 403)
        self.assertEqual(self.post("/api/preset", {"id": "sample"}, Origin="http://evil.example")[0], 403)
        self.assertEqual(self.post("/api/preset", {"id": "sample"}, Origin="null")[0], 403)
        # No custom header: a plain cross-site form or fetch without a CORS preflight is refused.
        self.assertEqual(self.call("POST", "/api/preset", {"id": "sample"})[0], 403)
        self.assertEqual(self.call("OPTIONS", "/api/preset")[0], 501)
        self.assertEqual(self.post("/api/preset", {"id": "sample"})[0], 200)

    def test_lan_host_is_refused_unless_lan_mode(self):
        self.assertEqual(self.call("GET", "/", host=f"10.123.45.8:{self.viewer.server_port}")[0], 403)

    def test_example_blocks_device_actions(self):
        self.assertEqual(self.post("/api/shot")[0], 400)
        self.assertEqual(self.post("/api/console", {"text": "help"})[0], 400)
        state = json.loads(self.call("GET", "/api/state")[1])
        self.assertEqual((state["preset"], state["connected"]), ("example", False))

    def test_body_limit_and_unknown_routes(self):
        self.assertEqual(self.post("/api/preset", {"id": "x" * 600})[0], 400)
        self.assertEqual(self.post("/api/preset", {"id": "sample"})[0], 200)
        self.assertEqual(self.post("/api/nothing")[0], 404)
        self.assertEqual(self.call("GET", "/receiver.py")[0], 404)
        self.assertEqual(self.call("GET", "/presets/sample/adapter.py")[0], 404)

    def test_capture_files_cannot_escape_the_captures_folder(self):
        receiver.local()
        (receiver.CAPTURES / "ok-1.txt").write_text("fine", encoding="utf-8")
        (receiver.LOCAL / "secret.txt").write_text("pairing", encoding="utf-8")
        self.assertEqual(self.call("GET", "/captures/ok-1.txt")[0], 200)
        for name in ("../secret.txt", "..%2Fsecret.txt", "..\\secret.txt", ".hidden.txt", "ok-1.exe",
                     "%2e%2e/secret.txt", "a/b.txt", "ok-1.txt:stream"):
            self.assertEqual(self.call("GET", f"/captures/{name}")[0], 404, name)

    def test_python_v1_adapter_capture_stream_and_console(self):
        self.assertEqual(self.post("/api/preset", {"id": "sample"})[0], 200)
        status, body, _ = self.post("/api/capture")
        self.assertEqual(status, 200, body)
        for _ in range(50):
            state = json.loads(self.call("GET", "/api/state")[1])
            if state["captures"] and all(item["state"] == "done" for item in state["captures"][0]["items"]):
                break
            time.sleep(0.05)
        files = [item["file"] for item in state["captures"][0]["items"]]
        self.assertTrue(all(files), state)
        self.assertEqual(self.call("GET", f"/captures/{files[0]}")[2]["Content-Type"], "image/bmp")
        self.assertEqual(self.post("/api/stream")[0], 200)
        for _ in range(50):
            if self.call("GET", "/live.bmp")[0] == 200:
                break
            time.sleep(0.05)
        self.assertEqual(self.call("GET", "/live.bmp")[0], 200)
        self.assertEqual(len(self.call("GET", "/live-audio.pcm?since=0")[1]), 4 + 16)
        self.assertEqual(self.post("/api/console", {"text": "help"})[0], 200)
        self.assertEqual(self.post("/api/console", {"text": "bad\nline"})[0], 400)
        self.assertEqual(self.post("/api/input", {"buttons": "A"})[0], 400)  # no input capability

    def test_disconnect_stops_device_actions(self):
        self.post("/api/preset", {"id": "sample"})
        self.assertEqual(json.loads(self.post("/api/link", {"connected": False})[1]), {"linkEnabled": False})
        self.assertEqual(self.post("/api/shot")[0], 400)
        self.assertEqual(self.post("/api/link", {"connected": "yes"})[0], 400)

    def test_lan_viewer_needs_the_pairing_code(self):
        class RemoteViewer(receiver.Viewer):
            def setup(self):
                super().setup()
                self.client_address = ("10.123.45.55", 12345)

        server = receiver.ThreadingHTTPServer(("127.0.0.1", 0), RemoteViewer)
        server.lan_enabled = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))
        host = f"10.123.45.8:{server.server_port}"
        self.pairing("123456")

        def send(method, path, body=b"", **headers):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            conn.request(method, path, body, {"Host": host, **headers})
            answer = conn.getresponse()
            result = answer.status, answer.read(), answer.getheader("Set-Cookie")
            conn.close()
            return result
        self.assertIn(b"six-digit code", send("GET", "/")[1])
        self.assertEqual(send("GET", "/api/state")[0], 403)
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        self.assertEqual(send("POST", "/pair", b"pin=000000", **form)[0], 403)
        self.assertEqual(send("POST", "/pair", b"pin=%C3%A9", **form)[0], 403)  # not six ASCII digits
        status, _, cookie = send("POST", "/pair", b"pin=123456", **form)
        self.assertEqual(status, 303)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        token = cookie.split(";", 1)[0]
        self.assertEqual(send("GET", "/api/state", Cookie=token)[0], 200)

    # ---- LAN pairing code: per-address limit, total limit, expiry, single use, bounded tables ----
    def pairing(self, pin="123456"):
        """A known LAN pairing code and empty pairing tables; the console printout is captured."""
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name, value in (("viewer_pin", pin), ("viewer_pin_expires", time.monotonic() + 600),
                            ("viewer_pin_failures", 0), ("viewer_lockout_until", 0.0),
                            ("viewer_sessions", {}), ("viewer_attempts", {})):
            stack.enter_context(mock.patch.object(receiver, name, value))
        self.shown = stack.enter_context(mock.patch.object(receiver, "show_viewer_pin"))

    def pair_try(self, pin, peer="10.123.45.56"):
        body = b"pin=" + pin.encode("ascii")
        handler = mock.Mock()
        handler.client_address = (peer, 1)
        handler.headers = {"Content-Length": str(len(body)), "Content-Type": "application/x-www-form-urlencoded"}
        handler.rfile.read.return_value = body
        handler.local_client.return_value = False
        receiver.Viewer.pair_post(handler)
        return handler.response.call_args[0][0] if handler.response.called else handler.send_response.call_args[0][0]

    def test_pairing_attempts_are_rate_limited_per_address(self):
        self.pairing()
        statuses = [self.pair_try("000000") for _ in range(7)]
        self.assertEqual(statuses[:5], [403] * 5)
        self.assertEqual(statuses[5:], [429, 429])
        self.assertEqual(self.pair_try("123456"), 429)  # even the right code, until a minute has passed
        self.assertEqual(self.pair_try("123456", peer="203.0.113.57"), 303)  # another address is not blocked

    def test_malformed_codes_are_refused_before_comparing(self):
        self.pairing()
        for pin in ("%C3%A9", "%C3%A9%C3%A9%C3%A9%C3%A9%C3%A9%C3%A9", "12345", "1234567", "12345a", "%EF%BC%91" * 6,
                    "+23456", "", "123456%00"):
            with mock.patch.object(receiver.hmac, "compare_digest", side_effect=AssertionError("compared")):
                self.assertEqual(self.pair_try(pin, peer=f"203.0.113.{len(pin) + 60}"), 403, pin)
        self.assertEqual(receiver.viewer_pin, "123456")

    def test_twenty_wrong_codes_in_total_replace_the_code_and_pause_pairing(self):
        self.pairing()
        for number in range(receiver.PIN_MAX_FAILURES - 1):  # one try each from many addresses
            self.assertEqual(self.pair_try("000000", peer=f"198.51.100.{number + 1}"), 403)
        self.assertEqual(receiver.viewer_pin, "123456")
        self.shown.assert_not_called()
        self.assertEqual(self.pair_try("000000", peer="198.51.100.200"), 403)
        new_pin = receiver.viewer_pin
        self.assertNotEqual(new_pin, "123456")
        self.assertRegex(new_pin, r"^[0-9]{6}$")
        self.shown.assert_called_once()
        self.assertIn(new_pin, self.shown.call_args[0][0])  # printed on this PC's console
        self.assertEqual(self.pair_try("123456", peer="203.0.113.1"), 429)  # paused for everyone
        self.assertEqual(self.pair_try(new_pin, peer="203.0.113.2"), 429)
        later = time.monotonic() + receiver.PIN_LOCKOUT_S + 1
        with mock.patch.object(receiver.time, "monotonic", return_value=later):
            self.assertEqual(self.pair_try("123456", peer="203.0.113.3"), 403)  # the old code is gone
            self.assertEqual(self.pair_try(new_pin, peer="203.0.113.4"), 303)

    def test_code_expires_after_ten_minutes(self):
        self.pairing()
        self.assertEqual(receiver.PIN_LIFETIME_S, 600)
        later = time.monotonic() + receiver.PIN_LIFETIME_S + 1
        with mock.patch.object(receiver.time, "monotonic", return_value=later):
            self.assertEqual(self.pair_try("123456"), 403)
            self.assertNotEqual(receiver.viewer_pin, "123456")
            self.assertIn("expired", self.shown.call_args[0][0])
            self.assertEqual(self.pair_try(receiver.viewer_pin, peer="203.0.113.70"), 303)
        # The serving loop replaces an expired code without waiting for a pairing attempt.
        self.pairing("654321")
        receiver.refresh_viewer_pin()
        self.assertEqual(receiver.viewer_pin, "654321")
        with mock.patch.object(receiver.time, "monotonic", return_value=later):
            receiver.refresh_viewer_pin()
        self.assertNotEqual(receiver.viewer_pin, "654321")
        self.shown.assert_called_once()

    def test_code_works_once(self):
        self.pairing()
        self.assertEqual(self.pair_try("123456"), 303)
        new_pin = receiver.viewer_pin
        self.assertNotEqual(new_pin, "123456")
        self.assertIn(new_pin, self.shown.call_args[0][0])
        self.assertEqual(len(receiver.viewer_sessions), 1)
        self.assertEqual(self.pair_try("123456", peer="203.0.113.71"), 403)  # a second device needs the new code
        self.assertEqual(self.pair_try(new_pin, peer="203.0.113.71"), 303)
        self.assertEqual(len(receiver.viewer_sessions), 2)

    def test_session_and_attempt_tables_stay_bounded(self):
        self.pairing()
        now = time.monotonic()
        for number in range(receiver.MAX_VIEWER_SESSIONS + 40):
            receiver.viewer_sessions[f"token-{number}"] = now + 3600
        receiver.viewer_sessions["expired"] = now - 1
        for number in range(receiver.MAX_VIEWER_ATTEMPT_PEERS + 100):
            receiver.viewer_attempts[f"peer-{number}"] = [now]
        receiver.viewer_attempts["peer-old"] = [now - 120]  # older than a minute
        with receiver.viewer_auth_lock:
            receiver.prune_viewer_auth(now)
        self.assertEqual(len(receiver.viewer_sessions), receiver.MAX_VIEWER_SESSIONS)
        self.assertNotIn("expired", receiver.viewer_sessions)
        self.assertNotIn("token-0", receiver.viewer_sessions)  # oldest evicted first
        self.assertIn(f"token-{receiver.MAX_VIEWER_SESSIONS + 39}", receiver.viewer_sessions)
        self.assertEqual(len(receiver.viewer_attempts), receiver.MAX_VIEWER_ATTEMPT_PEERS)
        self.assertNotIn("peer-old", receiver.viewer_attempts)
        self.assertEqual(self.pair_try("000000", peer="203.0.113.200"), 403)
        self.assertLessEqual(len(receiver.viewer_attempts), receiver.MAX_VIEWER_ATTEMPT_PEERS)
        self.assertIn("203.0.113.200", receiver.viewer_attempts)  # the newest address is kept


class ContractTests(unittest.TestCase):
    def test_bmp_validation(self):
        self.assertTrue(receiver.valid_bmp("shot", stacked_bmp(400, 480)))
        self.assertTrue(receiver.valid_bmp("stream", stacked_bmp(200, 240)))
        self.assertFalse(receiver.valid_bmp("shot", stacked_bmp(200, 240)))
        self.assertFalse(receiver.valid_bmp("stream", b"BM" + b"\x00" * 10))

    def test_input_and_gamepad_encoding(self):
        self.assertEqual(receiver.encode_input("A,B", 2, [0, 0, 0, 0]), "3 2 0.000 0.000 0.000 0.000")
        for bad in (("Q", 1, [0] * 4), ("A", 0, [0] * 4), ("A", 1, [2, 0, 0, 0]), ("A", True, [0] * 4)):
            with self.assertRaises(ValueError):
                receiver.encode_input(*bad)
        self.assertEqual(receiver.encode_gamepad({"padMask": 1, "moveX": 0.5}),
                         "buttons=0001,lx=500,ly=0,rx=0,ry=0,lt=0,rt=0,frames=8")
        with self.assertRaises(ValueError):
            receiver.encode_gamepad({"padMask": 70000})

    def test_private_host(self):
        self.assertEqual(receiver.private_host("10.123.45.8"), "10.123.45.8")
        for host in ("203.0.113.8", "127.0.0.1", "198.51.100.7"):
            with self.assertRaises(ValueError):
                receiver.private_host(host)


if __name__ == "__main__":
    unittest.main()

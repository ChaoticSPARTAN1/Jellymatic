#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""Remote Pilot receiver: a local PC viewer for paired handhelds. Python standard library only.

The receiver serves the viewer on 127.0.0.1 (this PC only) unless --viewer-lan is given.
Device presets are python-v1 adapters (presets/<id>/adapter.py, see ADAPTER_API.md); the
Chromatic preset's routes are under /api/chromatic/ (chromatic/api.py).
"""

import argparse
from collections import deque
from contextlib import closing
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import struct
import sys
import threading
import time
from urllib.parse import parse_qs
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from presets import public_presets, set_theme_override, PRESETS
from plugin_runtime import PluginRuntime
import capture_api
import touch_api
import performance_capture
try:  # Optional: the receiver also runs without the Chromatic package.
    import chromatic.api as chromatic_api
except ImportError:
    chromatic_api = None

HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
LOCAL = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else HERE) / "local"
DB = LOCAL / "queue.sqlite3"
CAPTURES = LOCAL / "captures"
SELECTED_PRESET = LOCAL / "selected-preset.json"
MAX_REPORT = 65536
DEFAULT_VIEWER_PORT = 38764
# Generic button names for python-v1 "input" requests (ADAPTER_API.md).
KEYS = {
    "A": 0, "B": 1, "SELECT": 2, "START": 3, "RIGHT": 4, "LEFT": 5, "UP": 6,
    "DOWN": 7, "R": 8, "L": 9, "X": 10, "Y": 11, "ZL": 14, "ZR": 15,
}
PRIVATE_NETS = tuple(ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
CAPTURE_NAME = re.compile(r"[a-zA-Z0-9_-][a-zA-Z0-9_.-]{0,127}\.(bmp|txt|json)")
live_lock = threading.Lock()
link_enabled = True
# LAN viewer pairing (--viewer-lan): a one-time six-digit code shown only on this PC's console.
PIN_FORMAT = re.compile(r"[0-9]{6}")
PIN_LIFETIME_S = 600            # a code expires after 10 minutes and a new one is printed
PIN_MAX_FAILURES = 20           # wrong codes in total (all addresses) before the code is replaced
PIN_LOCKOUT_S = 60              # after that, every pairing attempt is refused for a minute
PEER_ATTEMPTS = 5               # wrong codes per address per minute
MAX_VIEWER_SESSIONS = 64
MAX_VIEWER_ATTEMPT_PEERS = 256
viewer_pin = ""
viewer_pin_expires = 0.0
viewer_pin_failures = 0
viewer_lockout_until = 0.0
viewer_sessions = {}
viewer_attempts = {}
viewer_auth_lock = threading.Lock()
selection_lock = threading.Lock()
selected_preset = None
plugin_runtimes = {}
plugin_frames = {}
plugin_versions = {}
plugin_audio_chunks = {}
plugin_audio_versions = {}
plugin_stream_busy = set()
plugin_stream_errors = {}
plugin_input_arms = {}
plugin_pad_states = {}
plugin_pad_errors = {}


def current_preset():
    global selected_preset
    with selection_lock:
        if selected_preset is None:
            ids = [preset["id"] for preset in public_presets()]
            saved = ""
            try:
                saved = json.loads(SELECTED_PRESET.read_text(encoding="utf-8"))["id"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
            selected_preset = saved if saved in ids else ids[0]
        return selected_preset


def select_preset(preset_id):
    global selected_preset
    if preset_id not in {preset["id"] for preset in public_presets()}:
        raise ValueError("Unknown preset")
    with selection_lock:
        selected_preset = preset_id
        local()
        temporary = SELECTED_PRESET.with_suffix(".tmp")
        temporary.write_text(json.dumps({"id": preset_id}) + "\n", encoding="utf-8")
        temporary.replace(SELECTED_PRESET)
    with live_lock:
        plugin_input_arms.clear()
        for state in plugin_pad_states.values():
            state['delivered'] = state['revision']
    return preset_id


def manifest_for(preset_id):
    return next((item for item in public_presets() if item["id"] == preset_id), None)


def active_plugin():
    return plugin_runtimes.get(current_preset())


def plugin_poll_active(preset_id):
    return current_preset() == preset_id


def plugin_audio_limit(preset_id):
    return (manifest_for(preset_id) or {}).get('maxLiveAudioBytes', 16384)


def input_arm_required(preset_id):
    return (manifest_for(preset_id) or {}).get('manualInputRequiresArm', False)


def require_input_arm(preset_id, owner):
    if not input_arm_required(preset_id):
        return
    with live_lock:
        lease = plugin_input_arms.get(preset_id)
        valid = bool(link_enabled and lease and lease[0] == owner and time.monotonic() < lease[1])
    if not valid:
        raise ValueError('Enable remote controls in this focused tab before sending input')


def set_input_arm(preset_id, data):
    if not input_arm_required(preset_id):
        raise ValueError('This preset does not use manual input arming')
    owner = data.get('owner')
    enabled = data.get('enabled')
    if not isinstance(owner, str) or not re.fullmatch('[0-9a-f]{32}', owner) or type(enabled) is not bool:
        raise ValueError('Invalid control lease')
    runtime = plugin_runtimes.get(preset_id)
    if enabled and (not link_enabled or not runtime or not runtime.status()['connected'] or not runtime.status()['session']):
        raise ValueError('Connect the device before enabling remote controls')
    with live_lock:
        prior = plugin_input_arms.get(preset_id)
        if enabled:
            if prior and prior[0] != owner and time.monotonic() < prior[1]:
                raise ValueError('Another tab has remote controls enabled')
            plugin_input_arms[preset_id] = (owner, time.monotonic() + 1.5)
        elif prior and prior[0] == owner:
            plugin_input_arms.pop(preset_id, None)
    return {'armed': enabled}


def plugin_action(preset_id, kind, payload, ident, input_owner=None):
    """Run one local adapter request with the same capture and queue limits."""
    try:
        if kind == 'input':
            require_input_arm(preset_id, input_owner)
        result = plugin_runtimes[preset_id].request(kind, payload)
        if kind in ("shot", "stream"):
            audio = b""
            if kind == "stream" and isinstance(result, tuple) and len(result) == 2:
                result, audio = result
                if not isinstance(audio, bytes) or len(audio) > plugin_audio_limit(preset_id) or len(audio) % 2:
                    raise ValueError("Adapter returned invalid live audio")
            if not isinstance(result, bytes) or not valid_bmp(kind, result,
                                                              (manifest_for(preset_id) or {}).get("display")):
                raise ValueError("Adapter returned an invalid stacked screen image")
            if kind == "shot":
                local()
                digest = hashlib.sha256(result).hexdigest()[:16]
                destination = CAPTURES / f"{preset_id}-shot-{ident}-{digest}.bmp"
                destination.write_bytes(result)
                with closing(database()) as db, db:
                    capture_api.record_file(db, ident, destination.name)
                result = f"Screen capture saved: {destination.name}"
            else:
                with live_lock:
                    plugin_frames[preset_id] = result
                    plugin_versions[preset_id] = plugin_versions.get(preset_id, 0) + 1
                    plugin_stream_errors.pop(preset_id, None)
                    if audio:
                        version = plugin_audio_versions.get(preset_id, 0) + 1
                        plugin_audio_versions[preset_id] = version
                        plugin_audio_chunks.setdefault(preset_id, deque(maxlen=32)).append((version, audio))
                result = "Preview updated"
        elif kind in ("report", "log"):
            if not isinstance(result, str) or len(result.encode("utf-8")) > MAX_REPORT:
                raise ValueError("Adapter returned an invalid diagnostic report")
            local()
            body = result.encode("utf-8")
            digest = hashlib.sha256(body).hexdigest()[:16]
            destination = CAPTURES / f"{preset_id}-{kind}-{ident}-{digest}.txt"
            if body:
                destination.write_bytes(body)
                with closing(database()) as db, db:
                    capture_api.record_file(db, ident, destination.name)
                result = f"{kind.title()} saved: {destination.name}"
            else:
                result = 'Build logs unavailable'
        elif not isinstance(result, str) or len(result.encode("utf-8")) > 4096:
            raise ValueError("Adapter returned an invalid action result")
        if ident:
            with closing(database()) as db, db:
                db.execute("UPDATE tasks SET state='done', result=? WHERE id=?", (result[:4096], ident))
    except Exception as exc:
        if kind == 'stream':
            with live_lock:
                plugin_stream_errors[preset_id] = str(exc)[:160]
        if ident:
            with closing(database()) as db, db:
                db.execute("UPDATE tasks SET state='failed', result=? WHERE id=?",
                           (f"{type(exc).__name__}: {exc}"[:300], ident))
    finally:
        if ident:
            with closing(database()) as db, db:
                capture_api.save_manifests(db, CAPTURES)
        if kind == "stream":
            with live_lock:
                plugin_stream_busy.discard(preset_id)


def plugin_event(preset_id, kind, sequence):
    """Turn an authenticated device-side "report this" press into visible local tasks."""
    if kind != "bug-it" or type(sequence) is not int or sequence <= 0 or \
            preset_id != current_preset() or not link_enabled:
        return

    def collect():
        # The device poll can see the event just before PluginRuntime publishes its
        # connected status; allow that a bounded moment to catch up.
        for _ in range(8):
            try:
                request_capture(preset_id, diagnostic='report')
                return
            except ValueError:
                if preset_id != current_preset() or not link_enabled:
                    return
                time.sleep(0.5)

    threading.Thread(target=collect, daemon=True).start()


def require_connected(preset_id):
    runtime = plugin_runtimes.get(preset_id)
    if not runtime or not link_enabled:
        raise ValueError("Paired device is not connected")
    status = runtime.status()
    if not status["connected"] or not status["session"]:
        raise ValueError("Paired device is not connected")
    return runtime


def request_capture(preset_id=None, diagnostic=None):
    """One capture API for every adapter: a screen plus available diagnostics."""
    preset_id = preset_id or current_preset()
    if preset_id == 'example':
        raise ValueError('Example is a local preview with no device connection')
    if preset_id != current_preset() or not link_enabled:
        raise ValueError("PC link is disconnected")
    require_connected(preset_id)
    manifest = manifest_for(preset_id) or {}
    if "screenshot" not in manifest.get("capabilities", []):
        raise ValueError("Action is unavailable for this preset")
    diagnostic = diagnostic or ('log' if manifest.get('remoteLog') else 'report' if manifest.get('remoteReport') else None)
    with closing(database()) as db, db:
        for bundle in capture_api.list_bundles(db, preset_id):
            if any(item['state'] in ('queued', 'sent') for item in bundle['items']):
                return {'capture': bundle['id'], 'id': bundle['items'][0]['task'],
                        'tasks': [item['task'] for item in bundle['items']]}
        capture, items = capture_api.create(db, preset_id, diagnostic, plugin=True)
        capture_api.save_manifests(db, CAPTURES)

    def collect():
        for ident, kind in items:
            plugin_action(preset_id, kind, '-', ident)
    threading.Thread(target=collect, daemon=True).start()
    return {'capture': capture, 'id': items[0][0], 'tasks': [ident for ident, kind in items]}


def capture_bundles(preset):
    with closing(database()) as db, db:
        capture_api.save_manifests(db, CAPTURES)
        return capture_api.list_bundles(db, preset)


def plugin_enqueue(kind, payload, input_owner=None):
    preset_id = current_preset()
    require_connected(preset_id)
    capability = "screenshot" if kind == "shot" else kind
    manifest = manifest_for(preset_id)
    if not manifest or capability not in manifest["capabilities"]:
        raise ValueError("Action is unavailable for this preset")
    if kind == 'input':
        require_input_arm(preset_id, input_owner)
    with closing(database()) as db, db:
        pending = db.execute("SELECT count(*) FROM tasks WHERE state IN ('queued', 'sent')").fetchone()[0]
        if pending >= 16:
            raise ValueError("Queue full; wait for the device")
        cur = db.execute("INSERT INTO tasks (kind, payload, state) VALUES (?, ?, 'sent')",
                         (f"plugin:{preset_id}:{kind}", payload))
        ident = cur.lastrowid
    threading.Thread(target=plugin_action, args=(preset_id, kind, payload, ident, input_owner), daemon=True).start()
    return ident


def plugin_request_stream():
    preset_id = current_preset()
    require_connected(preset_id)
    manifest = manifest_for(preset_id)
    if not manifest or "stream" not in manifest["capabilities"]:
        raise ValueError("Preview is unavailable for this preset")
    with live_lock:
        if preset_id in plugin_stream_busy:
            return
        plugin_stream_busy.add(preset_id)
    threading.Thread(target=plugin_action, args=(preset_id, "stream", "-", 0), daemon=True).start()


def encode_gamepad(data):
    """One complete standard controller snapshot, with quantized axes."""
    mask = data.get("padMask")
    frames = data.get("frames", 8)
    if type(mask) is not int or not 0 <= mask <= 0xffff:
        raise ValueError("Controller button mask must be 0..65535")
    if type(frames) is not int or not 1 <= frames <= 30:
        raise ValueError("Controller frames must be 1..30")
    values = []
    for key in ("moveX", "moveY", "lookX", "lookY", "lt", "rt"):
        value = data.get(key, 0)
        minimum = 0 if key in ("lt", "rt") else -1
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value) or not minimum <= value <= 1:
            raise ValueError(f"Invalid controller {key}")
        values.append(round(value * 1000))
    lx, ly, rx, ry, lt, rt = values
    return (f"buttons={mask:04x},lx={lx},ly={ly},rx={rx},ry={ry},"
            f"lt={lt},rt={rt},frames={frames}")


def plugin_pad_worker(preset_id, kind='gamepad', key=None):
    """Send only the freshest controller state, never retry a gameplay action."""
    key = key or preset_id
    while True:
        with live_lock:
            state = plugin_pad_states[key]
            if state["delivered"] == state["revision"] or time.monotonic() - state["updated"] > .3 \
                    or current_preset() != preset_id or not link_enabled:
                state["busy"] = False
                return
            revision, payload = state["revision"], state["payload"]
        try:
            result = plugin_runtimes[preset_id].request(kind, payload)
            if not isinstance(result, str) or len(result) > 80:
                raise ValueError("Invalid controller acknowledgment")
            plugin_pad_errors[preset_id] = ""
        except Exception as exc:
            plugin_pad_errors[preset_id] = f"{type(exc).__name__}: {exc}"[:160]
        with live_lock:
            state["delivered"] = max(state["delivered"], revision)


def plugin_set_gamepad(data, kind='gamepad'):
    preset_id = current_preset()
    manifest = manifest_for(preset_id)
    capability = 'touch' if kind == 'touch' else 'controller'
    if not manifest or capability not in manifest["capabilities"]:
        raise ValueError("Controller bridge is unavailable for this preset")
    require_connected(preset_id)
    payload = touch_api.encode_sample(data) if kind == 'touch' else encode_gamepad(data)
    key = preset_id + ':touch' if kind == 'touch' else preset_id
    with live_lock:
        state = plugin_pad_states.setdefault(key, {
            "payload": "", "revision": 0, "delivered": 0, "updated": 0.0, "busy": False})
        state["payload"] = payload
        state["revision"] += 1
        state["updated"] = time.monotonic()
        start = not state["busy"]
        state["busy"] = True
    if start:
        threading.Thread(target=plugin_pad_worker, args=(preset_id, kind, key), daemon=True).start()


def display_size(display):
    """A preset's declared full-size screen (manifest `display`), else the stacked 400x480."""
    if isinstance(display, dict):
        width, height = display.get("width"), display.get("height")
        if type(width) is int and type(height) is int and 32 <= width <= 1024 and 32 <= height <= 1024 \
                and width % 8 == 0 and height % 2 == 0:
            return width, height
    return 400, 480


def valid_bmp(kind, body, display=None):
    """Accept only exact uncompressed 24-bit BMPs: a full-size shot of the preset's display
    (400x480 stacked screens unless the manifest declares another size) or a half-size stream."""
    width, height = display_size(display)
    if kind != "shot":
        width, height = width // 2, height // 2
    expected = 54 + width * height * 3
    if not isinstance(body, bytes) or len(body) != expected or body[:2] != b"BM":
        return False
    return (struct.unpack_from("<I", body, 2)[0] == expected
            and struct.unpack_from("<I", body, 10)[0] == 54
            and struct.unpack_from("<I", body, 14)[0] == 40
            and struct.unpack_from("<II", body, 18) == (width, height)
            and struct.unpack_from("<HH", body, 26) == (1, 24)
            and struct.unpack_from("<I", body, 30)[0] == 0
            and struct.unpack_from("<I", body, 34)[0] == width * height * 3)


def local():
    LOCAL.mkdir(mode=0o700, parents=True, exist_ok=True)
    CAPTURES.mkdir(mode=0o700, exist_ok=True)
    try:
        os.chmod(LOCAL, 0o700)
        os.chmod(CAPTURES, 0o700)
    except OSError:
        pass


def database():
    local()
    db = sqlite3.connect(DB, timeout=10)
    db.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', result TEXT NOT NULL DEFAULT '')")
    db.commit()
    return db


def is_private_ipv4(ip):
    return ip.version == 4 and any(ip in net for net in PRIVATE_NETS)


def private_host(host):
    addr = ipaddress.ip_address(host)
    if not is_private_ipv4(addr):
        raise ValueError("Host must be this PC's private IPv4 address")
    return str(addr)


def local_pc_host():
    for host in socket.gethostbyname_ex(socket.gethostname())[2]:
        try:
            return private_host(host)
        except ValueError:
            pass
    raise ValueError("No private PC IPv4 address found; pass --pc-host")


def refresh_network_adapters(host):
    """Keep device adapters on the current private LAN when a laptop changes networks."""
    for runtime in plugin_runtimes.values():
        runtime.set_network_host(host)


def encode_input(buttons, frames, axes):
    if not isinstance(frames, int) or isinstance(frames, bool) or not 1 <= frames <= 30:
        raise ValueError("Frames must be 1..30")
    if not isinstance(buttons, str):
        raise ValueError("Buttons must be comma-separated names")
    names = [v.strip().upper() for v in buttons.split(",")]
    if not names or any(n not in KEYS for n in names if n != "NONE"):
        raise ValueError("Unknown button")
    if len(axes) != 4 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                             or not -1 <= v <= 1 for v in axes):
        raise ValueError("Motion axes must be -1..1")
    bits = sum(1 << KEYS[name] for name in set(names) if name != "NONE")
    return f"{bits:x} {frames} {axes[0]:.3f} {axes[1]:.3f} {axes[2]:.3f} {axes[3]:.3f}"


def show_viewer_pin(message):
    """Prints the LAN pairing code on this PC's console only (never sent to a browser)."""
    print(message, flush=True)


def rotate_viewer_pin(reason):
    """A fresh one-time LAN pairing code, valid for PIN_LIFETIME_S. Caller holds viewer_auth_lock."""
    global viewer_pin, viewer_pin_expires, viewer_pin_failures
    viewer_pin = f"{secrets.randbelow(1000000):06d}"
    viewer_pin_expires = time.monotonic() + PIN_LIFETIME_S
    viewer_pin_failures = 0
    show_viewer_pin(f"{reason} New one-time viewer pairing code: {viewer_pin} (valid for "
                    f"{PIN_LIFETIME_S // 60} minutes)")


def refresh_viewer_pin():
    """Replaces an expired LAN pairing code (called about once a second while serving)."""
    with viewer_auth_lock:
        if viewer_pin and time.monotonic() >= viewer_pin_expires:
            rotate_viewer_pin("The viewer pairing code expired.")


def prune_viewer_auth(now):
    """Keeps the LAN session and attempt tables bounded. Caller holds viewer_auth_lock."""
    for token in [token for token, expiry in viewer_sessions.items() if expiry <= now]:
        del viewer_sessions[token]
    for peer in [peer for peer, times in viewer_attempts.items() if not times or now - times[-1] >= 60]:
        del viewer_attempts[peer]
    while len(viewer_sessions) > MAX_VIEWER_SESSIONS:
        del viewer_sessions[next(iter(viewer_sessions))]  # oldest first (dicts keep insertion order)
    while len(viewer_attempts) > MAX_VIEWER_ATTEMPT_PEERS:
        del viewer_attempts[next(iter(viewer_attempts))]


def latest_capture():
    selected = current_preset()
    if selected == 'example' or selected not in plugin_runtimes:
        return None
    shots = list(CAPTURES.glob(f"{selected}-shot-*.bmp")) if CAPTURES.exists() else []
    return max(shots, key=lambda p: p.stat().st_mtime_ns) if shots else None


def viewer_state():
    selected = current_preset()
    plugin = plugin_runtimes.get(selected)
    perf_enabled = performance_capture.enabled(LOCAL / selected)
    if selected == 'example' or not plugin:
        return {'preset': selected, 'connected': False, 'transportConnected': False, 'session': False,
                'linkEnabled': False if selected == 'example' else link_enabled,
                'discovery': 'local example' if selected == 'example' else 'no device transport',
                'pairedDeviceAddress': None, 'tasks': [], 'captures': [], 'captureVersion': None,
                'captureAtMs': None, 'streamVersion': 0, 'streamError': '', 'inputArmed': False,
                'audioVersion': 0, 'streamTransferMs': None, 'streamStagesMs': None, 'perf': None,
                'perfEnabled': perf_enabled, 'deviceMeta': None, 'controllerError': ''}
    with closing(database()) as db:
        prefix = f"plugin:{selected}:"
        rows = db.execute("SELECT * FROM tasks WHERE substr(kind, 1, ?) = ? ORDER BY id DESC LIMIT 20",
                          (len(prefix), prefix))
        tasks = [dict(id=i, kind=k, payload=p, state=s, result=r) for i, k, p, s, r in rows]
    shot = latest_capture()
    adapter = plugin.status()
    with live_lock:
        frame_version = plugin_versions.get(selected, 0)
        audio_version = plugin_audio_versions.get(selected, 0)
        input_lease = plugin_input_arms.get(selected)
        input_armed = bool(input_lease and time.monotonic() < input_lease[1])
    return {
        "connected": adapter["connected"] and link_enabled,
        "transportConnected": adapter["connected"],
        "preset": selected,
        "discovery": adapter["discovery"],
        "pairedDeviceAddress": adapter["host"],
        "session": adapter["session"],
        "linkEnabled": link_enabled,
        "tasks": tasks,
        "captures": capture_bundles(selected),
        "captureVersion": shot.stat().st_mtime_ns if shot else None,
        "captureAtMs": shot.stat().st_mtime_ns // 1000000 if shot else None,
        "streamVersion": frame_version,
        "streamError": plugin_stream_errors.get(selected, ''),
        "inputArmed": input_armed,
        "audioVersion": audio_version,
        "streamTransferMs": None,
        "streamStagesMs": None,
        "perf": adapter["perf"] if perf_enabled else None,
        "perfEnabled": perf_enabled,
        "deviceMeta": adapter["meta"],
        "controllerError": plugin_pad_errors.get(selected, ""),
    }


class Viewer(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    io_timeout = 15

    def setup(self):
        super().setup()
        self.connection.settimeout(self.io_timeout)

    def log_message(self, fmt, *args):
        pass

    def local_client(self):
        return self.client_address[0] == "127.0.0.1"

    def valid_origin(self):
        # A numeric Host with this server's port and a same-origin Origin header block
        # DNS rebinding and cross-site requests. Without --viewer-lan both ends must be
        # this PC (loopback).
        port = self.server.server_port
        host = self.headers.get("Host", "")
        if not host.endswith(f":{port}"):
            return False
        try:
            host_ip = ipaddress.IPv4Address(host.rsplit(":", 1)[0])
            peer_ip = ipaddress.IPv4Address(self.client_address[0])
        except ipaddress.AddressValueError:
            return False
        allowed = lambda ip: ip.is_loopback or is_private_ipv4(ip)
        if not allowed(host_ip) or not allowed(peer_ip):
            return False
        if not getattr(self.server, "lan_enabled", False) and (not host_ip.is_loopback or not peer_ip.is_loopback):
            return False
        origin = f"http://{host}"
        return self.headers.get("Origin", origin) == origin

    def authorized_viewer(self):
        if self.local_client():
            return True
        if not getattr(self.server, "lan_enabled", False):
            return False
        cookie = self.headers.get("Cookie", "")
        token = next((part.split("=", 1)[1] for part in cookie.split("; ")
                      if part.startswith("REMOTE_PILOT_VIEWER=")), "")
        with viewer_auth_lock:
            expiry = viewer_sessions.get(token, 0)
        return bool(token and expiry > time.monotonic())

    def pair_page(self):
        body = ("<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<link rel='stylesheet' href='/viewer.css'><title>Remote Pilot Pairing</title>"
                "<main><h1>Remote Pilot</h1><p>Enter the six-digit code shown by the receiver.</p>"
                "<form method='post' action='/pair'><label>Pairing code "
                "<input name='pin' inputmode='numeric' pattern='[0-9]{6}' maxlength='6' required></label> "
                "<button type='submit'>Connect</button></form></main>").encode()
        self.response(200, body, "text/html; charset=utf-8")

    def response(self, code, body=b"", content_type="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        # data: images only: the preset stylesheets draw their pixel patterns as inline SVG
        # backgrounds (an SVG used as an image cannot run script). Scripts stay same-origin.
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if not self.valid_origin():
            self.response(403)
            return
        if self.path == "/viewer.css":
            self.response(200, (HERE / "viewer.css").read_bytes(), "text/css; charset=utf-8")
            return
        if not self.authorized_viewer():
            self.pair_page() if self.path == "/" else self.response(403)
            return
        paths = {"/": (HERE / "viewer.html", "text/html; charset=utf-8"),
                 "/viewer.js": (HERE / "viewer.js", "text/javascript; charset=utf-8"),
                 "/themes.css": (HERE / "themes.css", "text/css; charset=utf-8")}
        if self.path in paths:
            file, content_type = paths[self.path]
            self.response(200, file.read_bytes(), content_type)
        elif self.path in ("/fonts/heading-PressStart2P-Regular.ttf", "/fonts/body-Silkscreen-Regular.ttf"):
            # Local OFL pixel fonts for preset stylesheets (licences in fonts/LICENCES.txt).
            font = HERE / "fonts" / self.path.rsplit("/", 1)[1]
            self.response(200, font.read_bytes(), "font/ttf") if font.is_file() else self.response(404)
        elif self.path.startswith("/presets/") and self.path.endswith("/theme.css"):
            parts = self.path.split("/")
            manifest = next((preset for preset in public_presets() if len(parts) == 4
                             and preset["id"] == parts[2] and preset.get("styleSheet") == self.path), None)
            stylesheet = PRESETS / parts[2] / "theme.css" if manifest else None
            self.response(200, stylesheet.read_bytes(), "text/css; charset=utf-8") \
                if stylesheet and stylesheet.is_file() else self.response(404)
        elif self.path.startswith("/presets/") and self.path.endswith("/panel.js"):
            parts = self.path.split("/")
            manifest = next((preset for preset in public_presets() if len(parts) == 4
                             and preset["id"] == parts[2] and preset.get("panelScript") == self.path), None)
            script = PRESETS / parts[2] / "panel.js" if manifest else None
            self.response(200, script.read_bytes(), "text/javascript; charset=utf-8") \
                if script and script.is_file() else self.response(404)
        elif self.path.startswith("/api/chromatic/"):
            try:
                if chromatic_api is None:
                    raise ValueError("Chromatic support is not installed")
                body, content_type = chromatic_api.handle_get(plugin_runtimes, self.path, current_preset(),
                                                              link_enabled, self.local_client())
                self.response(200, body, content_type)
            except ValueError as exc:
                self.response(400, str(exc).encode())
        elif self.path == "/api/state":
            self.response(200, json.dumps(viewer_state()).encode(), "application/json; charset=utf-8")
        elif self.path == "/api/presets":
            self.response(200, json.dumps({"selected": current_preset(), "presets": public_presets()}).encode(),
                          "application/json; charset=utf-8")
        elif self.path.startswith('/captures/'):
            # A bare file name only: no slashes, no leading dot, three known types.
            name = self.path.removeprefix('/captures/')
            file = CAPTURES / name if CAPTURE_NAME.fullmatch(name) else None
            self.response(200, file.read_bytes(), 'image/bmp' if name.endswith('.bmp') else
                          'application/json; charset=utf-8' if name.endswith('.json') else 'text/plain; charset=utf-8') \
                if file and file.is_file() else self.response(404)
        elif self.path.startswith("/latest.bmp"):
            shot = latest_capture()
            self.response(200, shot.read_bytes(), "image/bmp") if shot else self.response(404)
        elif self.path.startswith("/live.bmp"):
            with live_lock:
                selected = current_preset()
                frame = plugin_frames.get(selected, b"") if selected in plugin_runtimes else b""
            self.response(200, frame, "image/bmp") if frame else self.response(404)
        elif self.path.startswith("/live.pcm"):
            with live_lock:
                chunks = plugin_audio_chunks.get(current_preset())
                samples = chunks[-1][1] if chunks else b""
            self.response(200, samples, "application/octet-stream") if samples else self.response(404)
        elif self.path.startswith("/live-audio.pcm?since="):
            try:
                since = int(self.path.partition("?since=")[2])
                if since < 0 or since > 0xffffffff:
                    raise ValueError()
            except ValueError:
                self.response(400)
                return
            with live_lock:
                selected = current_preset()
                version = plugin_audio_versions.get(selected, 0)
                chunks = b"".join(samples for number, samples in
                                  plugin_audio_chunks.get(selected, ()) if number > since)
            # Keep playback close to the device after a long network gap (about half a second).
            limit = plugin_audio_limit(selected)
            if limit == 16384:
                limit = 16364
            chunks = chunks[-limit:]
            self.response(200, struct.pack("<I", version & 0xffffffff) + chunks, "application/octet-stream")
        else:
            self.response(404)

    def do_POST(self):
        global link_enabled
        if not self.valid_origin():
            self.response(403)
            return
        if self.path == "/pair" and getattr(self.server, "lan_enabled", False):
            self.pair_post()
            return
        # The custom header cannot be sent cross-site without a CORS preflight, which this
        # server never answers, so other web pages cannot drive the device.
        if not self.authorized_viewer() or self.headers.get("X-Remote-Pilot-Viewer") != "1":
            self.response(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0 or length > 512:
                raise ValueError("Request too large")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Object expected")
            if self.path.startswith('/api/chromatic/'):
                if chromatic_api is None:
                    raise ValueError("Chromatic support is not installed")
                value = chromatic_api.handle(plugin_runtimes, self.path.removeprefix('/api/chromatic/'),
                                             data, current_preset(), link_enabled, self.local_client())
                self.response(200, json.dumps(value).encode(), 'application/json; charset=utf-8')
                return
            if current_preset() == 'example' and self.path not in ('/api/preset', '/api/theme', '/api/performance'):
                raise ValueError('Example is a local preview with no device connection')
            if self.path == "/api/preset":
                preset_id = data.get("id")
                if not isinstance(preset_id, str):
                    raise ValueError("Preset id must be a string")
                self.response(200, json.dumps({"selected": select_preset(preset_id)}).encode(),
                              "application/json; charset=utf-8")
                return
            elif self.path == "/api/theme":
                preset_id = data.get("id")
                colors = data.get("colors")
                if not isinstance(preset_id, str):
                    raise ValueError("Preset id must be a string")
                self.response(200, json.dumps({"id": preset_id, "colors": set_theme_override(preset_id, colors)}).encode(),
                              "application/json; charset=utf-8")
                return
            elif self.path == "/api/console":
                value = data.get("text", "")
                if not isinstance(value, str) or not 1 <= len(value) <= 127 or any(ord(c) < 32 or ord(c) > 126 for c in value):
                    raise ValueError("Console command must be printable ASCII, up to 127 characters")
                ident = plugin_enqueue("console", value)
            elif self.path == "/api/input":
                payload = encode_input(data.get("buttons", "NONE"), data.get("frames", 1),
                                       [data.get(k, 0) for k in ("moveX", "moveY", "lookX", "lookY")])
                manifest = manifest_for(current_preset()) or {}
                buttons = {name.strip().upper() for name in str(data.get("buttons", "NONE")).split(",")}
                if buttons - {"NONE"} - set(manifest.get("inputButtons", [])):
                    raise ValueError("Button is unavailable for this preset")
                if not manifest.get("analogInput", False) and any(float(axis) for axis in payload.split()[2:]):
                    raise ValueError("This preset accepts button input only")
                ident = plugin_enqueue("input", payload, data.get('owner'))
            elif self.path == '/api/control-arm':
                self.response(200, json.dumps(set_input_arm(current_preset(), data)).encode(),
                              'application/json; charset=utf-8')
                return
            elif self.path in ("/api/gamepad", "/api/touch"):
                plugin_set_gamepad(data, kind='touch' if self.path == '/api/touch' else 'gamepad')
                self.response(200, b'{"ok":true}', "application/json; charset=utf-8")
                return
            elif self.path == "/api/stream":
                plugin_request_stream()
                self.response(200, b'{"ok":true}', "application/json; charset=utf-8")
                return
            elif self.path == "/api/link":
                desired = data.get("connected")
                if not isinstance(desired, bool):
                    raise ValueError("connected must be true or false")
                with live_lock:
                    link_enabled = desired
                    if not desired:
                        plugin_input_arms.clear()
                        for state in plugin_pad_states.values():
                            state['delivered'] = state['revision']
                runtime = active_plugin()
                answer = {"linkEnabled": desired}
                if not desired:
                    notice = getattr(runtime.adapter, 'disconnect', None) if runtime else None
                    if callable(notice):
                        def notify_disconnect():
                            try:
                                notice()
                            except Exception:
                                pass
                        threading.Thread(target=notify_disconnect, daemon=True).start()
                else:
                    # Optional python-v1 connect(): the adapter starts its configured link.
                    start = getattr(runtime.adapter, 'connect', None) if runtime else None
                    if callable(start):
                        try:
                            result = start()
                            message = result.get("message", "") if isinstance(result, dict) else ""
                            answer["connect"] = {"ok": True, "message": message[:240] if isinstance(message, str) else ""}
                        except Exception as exc:
                            answer["connect"] = {"ok": False, "message": f"Connect failed: {exc}"[:240]}
                self.response(200, json.dumps(answer).encode(), "application/json; charset=utf-8")
                return
            elif self.path == "/api/retry":
                if not link_enabled:
                    raise ValueError("Enable the PC link before retrying")
                plugin = active_plugin()
                if not plugin:
                    raise ValueError("This preset has no device transport")
                if not plugin.status()["session"]:
                    raise ValueError("Arm the selected device session before retrying")
                plugin.retry()
                self.response(200, b'{"retryRequested":true}', "application/json; charset=utf-8")
                return
            elif self.path == "/api/shot":
                ident = plugin_enqueue("shot", "-")
            elif self.path == '/api/capture':
                self.response(200, json.dumps(request_capture()).encode(), 'application/json; charset=utf-8')
                return
            elif self.path in ("/api/report", "/api/log", "/api/perf"):
                ident = plugin_enqueue(self.path.rsplit("/", 1)[1], "-")
            elif self.path == '/api/performance':
                preset = data.get('preset')
                if preset not in {item['id'] for item in public_presets()}:
                    raise ValueError('Unknown preset')
                value = performance_capture.set_enabled(LOCAL / preset, data.get('enabled'))
                self.response(200, json.dumps({'enabled': value}).encode(), 'application/json; charset=utf-8')
                return
            else:
                self.response(404)
                return
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.response(400, str(exc).encode("utf-8", errors="replace"))
            return
        self.response(200, json.dumps({"id": ident}).encode(), "application/json; charset=utf-8")

    def pair_post(self):
        if self.local_client():
            self.response(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if not 1 <= length <= 64 or self.headers.get("Content-Type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
                raise ValueError()
            pin = parse_qs(self.rfile.read(length).decode("ascii"), strict_parsing=True).get("pin", [""])[0]
        except (ValueError, UnicodeDecodeError):
            self.response(400)
            return
        global viewer_pin_failures, viewer_lockout_until
        # Exactly six ASCII digits before any comparison (compare_digest refuses non-ASCII text).
        well_formed = isinstance(pin, str) and PIN_FORMAT.fullmatch(pin) is not None
        now = time.monotonic()
        peer = self.client_address[0]
        with viewer_auth_lock:
            prune_viewer_auth(now)
            if viewer_pin and now >= viewer_pin_expires:
                rotate_viewer_pin("The viewer pairing code expired.")
            attempts = [t for t in viewer_attempts.pop(peer, []) if now - t < 60]
            if now < viewer_lockout_until or len(attempts) >= PEER_ATTEMPTS:
                attempts.append(now)
                viewer_attempts[peer] = attempts[-(PEER_ATTEMPTS + 1):]
                self.response(429)
                return
            if not viewer_pin or not well_formed or \
                    not hmac.compare_digest(pin.encode("ascii"), viewer_pin.encode("ascii")):
                attempts.append(now)
                viewer_attempts[peer] = attempts
                if viewer_pin:
                    viewer_pin_failures += 1
                    if viewer_pin_failures >= PIN_MAX_FAILURES:
                        viewer_lockout_until = now + PIN_LOCKOUT_S
                        rotate_viewer_pin(f"{PIN_MAX_FAILURES} wrong viewer pairing codes: pairing is paused for "
                                          f"{PIN_LOCKOUT_S} seconds.")
                prune_viewer_auth(now)
                self.response(403)
                return
            token = secrets.token_urlsafe(24)
            viewer_sessions[token] = now + 12 * 3600
            prune_viewer_auth(now)
            # One code, one pairing: the next phone or tablet needs the new code from this PC.
            rotate_viewer_pin("A phone or tablet paired with the viewer.")
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", f"REMOTE_PILOT_VIEWER={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()


def submit_cli(action, payload, viewer_port=DEFAULT_VIEWER_PORT):
    """Send a CLI action to the running receiver, which owns the connection state."""
    if not 1 <= viewer_port <= 65535:
        raise ValueError("Viewer port must be between 1 and 65535")
    body = json.dumps(payload).encode("utf-8")
    request = Request(f"http://127.0.0.1:{viewer_port}/api/{action}", body,
                      {"Content-Type": "application/json", "X-Remote-Pilot-Viewer": "1"},
                      method="POST")
    try:
        with urlopen(request, timeout=5) as response:
            result = json.load(response)
    except HTTPError as exc:
        reason = exc.read(300).decode("utf-8", "replace")
        raise ValueError(reason or f"Viewer rejected request ({exc.code})") from exc
    except (URLError, TimeoutError) as exc:
        raise ValueError("Start the local viewer before sending a command") from exc
    print(f"Queued {action} #{result['id']}")
    return result["id"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("serve", help="start the viewer on this PC (http://127.0.0.1:38764/)")
    p.add_argument("--viewer-port", type=int, default=DEFAULT_VIEWER_PORT)
    p.add_argument("--pc-host", help="this PC's private IPv4 address, if it has more than one network")
    p.add_argument("--viewer-lan", action="store_true",
                   help="also let phones/tablets on your private network open the viewer (plain HTTP; needs a one-time code shown here)")
    p = sub.add_parser("setup-preset", help="make a private device pairing config for a preset adapter")
    p.add_argument("preset", help="preset id from its manifest")
    p.add_argument("--device-host", default="auto", help="device private IPv4 address, or auto")
    p.add_argument("--port", type=int, default=38765)
    p = sub.add_parser("console", help="send one console command through the running viewer")
    p.add_argument("text", help="up to 127 printable ASCII characters")
    p.add_argument("--viewer-port", type=int, default=DEFAULT_VIEWER_PORT)
    p = sub.add_parser("input", help="press listed buttons for 1..30 frames (python-v1 adapters)")
    p.add_argument("buttons", help="comma-separated A,B,X,Y,L,R,UP,DOWN,LEFT,RIGHT,START,SELECT,ZL,ZR; NONE for motion only")
    p.add_argument("--frames", type=int, default=1)
    p.add_argument("--move-x", type=float, default=0)
    p.add_argument("--move-y", type=float, default=0)
    p.add_argument("--look-x", type=float, default=0)
    p.add_argument("--look-y", type=float, default=0)
    p.add_argument("--viewer-port", type=int, default=DEFAULT_VIEWER_PORT)
    p = sub.add_parser("shot", help="request a screen capture through the running viewer")
    p.add_argument("--viewer-port", type=int, default=DEFAULT_VIEWER_PORT)
    sub.add_parser("status", help="show queued and completed requests")
    args = parser.parse_args()
    if args.command == "setup-preset":
        manifest = manifest_for(args.preset)
        if not manifest or manifest["transport"] != "python-v1":
            parser.error("The preset does not provide a local adapter")
        runtime = PluginRuntime(manifest, PRESETS, LOCAL)
        configure = getattr(runtime.adapter, "configure", None)
        if not callable(configure):
            parser.error("The preset adapter does not offer setup")
        path = configure(args.device_host, args.port)
        print(f"Private device config: {path}")
        print("Copy it as the preset documentation says. Keep pairing files private.")
    elif args.command == "serve":
        host = None
        if args.pc_host:
            host = private_host(args.pc_host)
        else:
            try:
                host = local_pc_host()
            except ValueError:
                host = None
        viewer = ThreadingHTTPServer(("0.0.0.0" if args.viewer_lan else "127.0.0.1", args.viewer_port), Viewer)
        viewer.lan_enabled = args.viewer_lan
        viewer.daemon_threads = True
        viewer_thread = threading.Thread(target=viewer.serve_forever, daemon=True)
        for manifest in public_presets():
            if manifest["transport"] == "python-v1":
                runtime = PluginRuntime(manifest, PRESETS, LOCAL, network_host=host,
                                        active=lambda preset_id=manifest["id"]: plugin_poll_active(preset_id),
                                        on_event=plugin_event)
                plugin_runtimes[manifest["id"]] = runtime
                threading.Thread(target=runtime.run_forever, daemon=True).start()
        viewer_thread.start()
        print(f"Remote Pilot viewer (this PC only): http://127.0.0.1:{args.viewer_port}/")
        if args.viewer_lan:
            print(f"Phone/tablet viewer: http://{host}:{args.viewer_port}/" if host else
                  "Phone/tablet viewer: waiting for a private network address")
            print("The LAN viewer is plain HTTP (not encrypted): only use --viewer-lan on a private network you "
                  "trust. Never forward this port to the internet.")
            with viewer_auth_lock:
                rotate_viewer_pin("Phones and tablets need a pairing code; each code works once.")
        try:
            previous_host = host
            while True:
                if args.viewer_lan:
                    refresh_viewer_pin()
                if not args.pc_host:
                    try:
                        current_host = local_pc_host()
                    except ValueError:
                        current_host = None
                    if current_host != previous_host:
                        refresh_network_adapters(current_host)
                        previous_host = current_host
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            for runtime in plugin_runtimes.values():
                runtime.stop()
            viewer.shutdown()
            viewer.server_close()
            viewer_thread.join()
    elif args.command == "console":
        value = args.text
        if not value or len(value) > 127 or any(ord(c) < 32 or ord(c) > 126 for c in value):
            parser.error("Console command must be 1..127 printable ASCII characters")
        submit_cli("console", {"text": value}, args.viewer_port)
    elif args.command == "input":
        try:
            submit_cli("input", {"buttons": args.buttons, "frames": args.frames,
                     "moveX": args.move_x, "moveY": args.move_y,
                     "lookX": args.look_x, "lookY": args.look_y}, args.viewer_port)
        except ValueError as exc:
            parser.error(str(exc))
    elif args.command == "shot":
        submit_cli("shot", {}, args.viewer_port)
    elif args.command == "status":
        with closing(database()) as db, db:
            for ident, kind, payload, state, result in db.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 30"):
                print(f"#{ident} {kind} {state}: {payload} -> {result[:200]}")


if __name__ == "__main__":
    main()

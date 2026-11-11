# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""Validated /api/chromatic/* dispatch. The receiver's only Chromatic hooks call this module.

GET routes:
  /api/chromatic/status           cached transport, features, log tail, timing, Manager, activity
  /api/chromatic/live.bmp?n=<k>   current frame (CHV1 video when offered) as a 160x144 BMP, not saved
  /api/chromatic/live.pcm?n=<k>   newest game-sound chunk, PCM16 mono 16,384 Hz (needs game-audio)

POST routes keep the shared X-Remote-Pilot-Viewer header, origin and LAN-cookie
checks, and the shared 512-byte JSON limit (mode, pair-capture, pair-usb, manager-run,
manager-job and collect of Manager items are this PC only; see runtime.LOCAL_ONLY_ACTIONS):
  status, mode {mode}, retry, log, clear-log, shot, bundle, report,
  collect {item}, console {command}, arm {owner, enabled},
  input {owner, buttons: [..], ms}, hold {owner, buttons: [..] | []},
  pair-capture {enabled}, manager, manager-phrase {command},
  manager-run {command, phrase}, manager-job,
  ping, perf, pair-usb {start: true | absent}, shot {format: bmp | png},
  connect (the configured transport; lists, never opens, the console's USB port),
  allow-usb {port} (this PC's browser only: writes local/chromatic/config.json, then connects)

USB hand-off with Chromatic Manager (this PC only; no preset selection or PC link needed,
because the Chromatic transport runs whichever preset is shown; see usb_hold.py):
  usb-release {id, holder, reason, pid}  record the hold on disk, close the USB port, keep Wi-Fi
  usb-return {id} | {force: true}         end the hold and reopen USB (force: holder gone, no journal)
  usb-hold                                the current hold, if any (no id)
"""

import json
import re

from . import PRESET_ID

ACTIONS = ("status", "mode", "retry", "log", "clear-log", "shot", "bundle", "report", "collect", "console",
           "arm", "input", "hold", "pair-capture", "manager", "manager-phrase", "manager-run", "manager-job",
           "ping", "perf", "pair-usb", "connect", "allow-usb", "usb-release", "usb-return", "usb-hold")
NEEDS_SELECTION = ("mode", "retry", "log", "clear-log", "shot", "bundle", "report", "collect", "console", "arm",
                   "input", "hold", "pair-capture", "manager-run", "frame", "audio", "ping", "perf", "pair-usb",
                   "connect", "allow-usb")
NEEDS_LINK = ("shot", "bundle", "console", "input", "hold", "frame", "audio", "ping", "pair-usb", "connect",
              "allow-usb")
_LIVE = re.compile(r"/api/chromatic/live\.(bmp|pcm)(?:\?n=[0-9]{1,12})?")


def runtime(runtimes):
    value = runtimes.get(PRESET_ID)
    if value is None or "chromatic" not in value.capabilities or value.adapter is None:
        raise ValueError("Chromatic adapter is unavailable")
    return value


def _dispatch(runtimes, action, data, selected, link_enabled, local_client):
    if not isinstance(data, dict):
        raise ValueError("Object expected")
    if action in NEEDS_SELECTION and selected != PRESET_ID:
        raise ValueError("Select the Chromatic preset first")
    if action in NEEDS_LINK and not link_enabled:
        raise ValueError("Enable the PC link before Chromatic device actions")
    request = getattr(runtime(runtimes).adapter, "chromatic_request", None)
    if not callable(request):
        raise ValueError("Chromatic adapter is unavailable")
    try:
        value = request(action, data, local_client=local_client)
    except OSError as exc:
        # Socket timeouts and resets: the receiver reports ValueErrors as 400s.
        # A timed-out input is never resent; its outcome is unknown.
        raise ValueError(f"Chromatic link error ({type(exc).__name__}); nothing was retried") from exc
    if action == "status":
        value = {**value, "selected": selected == PRESET_ID, "linkEnabled": bool(link_enabled),
                 "localClient": bool(local_client)}
    return value


def handle(runtimes, action, data, selected, link_enabled, local_client=False):
    if action not in ACTIONS:
        raise ValueError("Unknown Chromatic action")
    return _dispatch(runtimes, action, data, selected, link_enabled, local_client)


def handle_get(runtimes, path, selected, link_enabled, local_client=False):
    """Returns (body bytes, content type) for the GET routes."""
    if path == "/api/chromatic/status":
        value = _dispatch(runtimes, "status", {}, selected, link_enabled, local_client)
        return json.dumps(value).encode("utf-8"), "application/json; charset=utf-8"
    match = _LIVE.fullmatch(path)
    if match and match.group(1) == "bmp":
        return _dispatch(runtimes, "frame", {}, selected, link_enabled, local_client), "image/bmp"
    if match:
        return _dispatch(runtimes, "audio", {}, selected, link_enabled, local_client), "application/octet-stream"
    raise ValueError("Unknown Chromatic route")

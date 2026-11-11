# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""CHR1: the paired, local-LAN, bounded request protocol for the Chromatic.

The normative description is presets/chromatic/PROTOCOL.md. Both the PC client
(lan_transport.py) and the host simulator (simulator.py) use these helpers, so
the two sides cannot drift apart silently.

Shape, modelled on Remote Pilot's reverse pairing: the console listens, the PC
connects to a private IPv4 address it was paired with, both sides prove they
hold the same 32-byte pairing key (HMAC-SHA256 over fresh nonces), and every
request and response after that carries a truncated HMAC under a per-connection
session key with a strictly increasing sequence number.

The same actions address whatever owns the screen. While the launcher runs that
is the launcher's own 160x144 overlay. Game screens, game video, game audio and
game input exist only on a device that advertises the matching game-* capability,
which needs a Gowin FPGA update (see LAUNCHER-SIDE.md).
"""

import hashlib
import hmac
import ipaddress
import re
import secrets

MAGIC = "CHR1"
PORT = 38771
KEY_BYTES = 32
NONCE_HEX = 32
MAC_HEX = 32
MAX_LINE = 256
SCREEN_WIDTH, SCREEN_HEIGHT = 160, 144
SCREEN_BYTES = SCREEN_WIDTH * SCREEN_HEIGHT * 2
VIDEO_HEADER = 20
MAX_VIDEO = SCREEN_BYTES + VIDEO_HEADER
MAX_STATUS = 1024
MAX_PERF = 512
MAX_LOG = 16384
MAX_CONSOLE = 1024
MAX_AUDIO = 8192 + 4
AUDIO_RATE = 16384
MAX_REASON = 160
MAX_BODY = MAX_VIDEO
MAX_SEQ = 0xFFFFFFFF
BUTTONS = ("UP", "DOWN", "LEFT", "RIGHT", "A", "B", "START", "SELECT", "MENU")
MAX_CHORD = 4
MIN_PRESS_MS, MAX_PRESS_MS = 20, 250
HOLD_LEASE_MS = 600
ACTIONS = ("status", "log", "screen", "video", "input", "hold", "perf", "console", "audio", "ping", "bye")
CODES = ("ok", "refused", "busy", "error")
LAUNCHER_CAPABILITIES = ("status", "log", "screen", "video", "input", "hold", "perf", "console")
GAME_CAPABILITIES = ("game-screen", "game-video", "game-input", "game-audio")
CAPABILITIES = LAUNCHER_CAPABILITIES + GAME_CAPABILITIES
CONSOLE_COMMANDS = ("help", "version", "uptime", "heap", "wifi", "phase", "reset-reason")
PHASES = ("menu", "media", "other-firmware", "game", "handover")
VERSION = re.compile(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}")
_HEX = re.compile(r"[0-9a-f]+")
_PRINTABLE = re.compile(r"[\x20-\x7e]*")


class ProtocolError(ValueError):
    """Raised for anything that is not exactly CHR1. Callers close the connection."""


def new_key():
    return secrets.token_bytes(KEY_BYTES)


def parse_key(text):
    if not isinstance(text, str) or len(text) != KEY_BYTES * 2 or not _HEX.fullmatch(text):
        raise ProtocolError("Pairing key must be 64 lowercase hex digits")
    return bytes.fromhex(text)


def new_nonce():
    return secrets.token_hex(NONCE_HEX // 2)


def _hex(value, length, name):
    if not isinstance(value, str) or len(value) != length or not _HEX.fullmatch(value):
        raise ProtocolError(f"Invalid {name}")
    return value


def _tag(key, *parts):
    return hmac.new(key, " ".join(parts).encode("ascii"), hashlib.sha256).hexdigest()


def pc_auth_tag(key, device_nonce, pc_nonce):
    return _tag(key, MAGIC, "pc-auth", device_nonce, pc_nonce)


def device_auth_tag(key, pc_nonce, device_nonce):
    return _tag(key, MAGIC, "device-auth", pc_nonce, device_nonce)


def session_key(key, device_nonce, pc_nonce):
    return bytes.fromhex(_tag(key, MAGIC, "session", device_nonce, pc_nonce))


def request_mac(session, seq, action, arg):
    return hmac.new(session, f"REQ {seq} {action} {arg}".encode("ascii"),
                    hashlib.sha256).hexdigest()[:MAC_HEX]


def response_mac(session, seq, code, body):
    message = f"RES {seq} {code} {len(body)}\n".encode("ascii") + body
    return hmac.new(session, message, hashlib.sha256).hexdigest()[:MAC_HEX]


def encode_line(text):
    if not isinstance(text, str) or len(text) >= MAX_LINE or not _PRINTABLE.fullmatch(text):
        raise ProtocolError("CHR1 lines are printable ASCII under 256 bytes")
    return text.encode("ascii") + b"\n"


def read_line(stream):
    """Read one bounded line from a binary file-like object (socket.makefile('rb'))."""
    data = stream.readline(MAX_LINE)
    if not data:
        raise ProtocolError("Connection closed")
    if not data.endswith(b"\n"):
        raise ProtocolError("Line too long or truncated")
    try:
        text = data[:-1].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ProtocolError("Line is not ASCII") from exc
    if not _PRINTABLE.fullmatch(text):
        raise ProtocolError("Line has control characters")
    return text


def read_exact(stream, length):
    if not 0 <= length <= MAX_BODY:
        raise ProtocolError("Body length out of range")
    data = stream.read(length) if length else b""
    if len(data) != length:
        raise ProtocolError("Body truncated")
    return data


# ---- handshake ---------------------------------------------------------------

def hello_line(device_nonce, version):
    _hex(device_nonce, NONCE_HEX, "device nonce")
    if not VERSION.fullmatch(version):
        raise ProtocolError("Invalid launcher version")
    return encode_line(f"{MAGIC} HELLO {device_nonce} {version}")


def parse_hello(line):
    parts = line.split(" ")
    if len(parts) != 4 or parts[:2] != [MAGIC, "HELLO"] or not VERSION.fullmatch(parts[3]):
        raise ProtocolError("Expected CHR1 HELLO")
    return _hex(parts[2], NONCE_HEX, "device nonce"), parts[3]


def auth_line(key, device_nonce, pc_nonce):
    return encode_line(f"{MAGIC} AUTH {pc_nonce} {pc_auth_tag(key, device_nonce, pc_nonce)}")


def parse_auth(key, line, device_nonce):
    parts = line.split(" ")
    if len(parts) != 4 or parts[:2] != [MAGIC, "AUTH"]:
        raise ProtocolError("Expected CHR1 AUTH")
    pc_nonce = _hex(parts[2], NONCE_HEX, "PC nonce")
    if pc_nonce == device_nonce:
        raise ProtocolError("Reflected nonce")
    if not hmac.compare_digest(_hex(parts[3], 64, "PC tag"), pc_auth_tag(key, device_nonce, pc_nonce)):
        raise ProtocolError("Pairing key mismatch")
    return pc_nonce


def ok_line(key, pc_nonce, device_nonce, capabilities):
    caps = ",".join(cap for cap in CAPABILITIES if cap in capabilities) or "status"
    return encode_line(f"{MAGIC} OK {device_auth_tag(key, pc_nonce, device_nonce)} {caps}")


def parse_ok(key, line, pc_nonce, device_nonce):
    parts = line.split(" ")
    if parts == [MAGIC, "DENY"]:
        raise ProtocolError("The launcher refused this PC")
    if len(parts) != 4 or parts[:2] != [MAGIC, "OK"]:
        raise ProtocolError("Expected CHR1 OK")
    if not hmac.compare_digest(_hex(parts[2], 64, "device tag"), device_auth_tag(key, pc_nonce, device_nonce)):
        raise ProtocolError("Launcher did not prove the pairing key")
    caps = parts[3].split(",")
    if any(cap not in CAPABILITIES for cap in caps) or len(set(caps)) != len(caps):
        raise ProtocolError("Unknown launcher capability")
    return frozenset(caps)


# ---- buttons -----------------------------------------------------------------

def parse_buttons(text, allow_none=False):
    """'A', 'A+B', or 'NONE' (hold release). MENU only ever alone and never held."""
    if allow_none and text == "NONE":
        return ()
    names = str(text).split("+")
    if not 1 <= len(names) <= MAX_CHORD or len(set(names)) != len(names) \
            or any(name not in BUTTONS for name in names):
        raise ProtocolError(f"Buttons must be 1..{MAX_CHORD} of {', '.join(BUTTONS)} joined by +")
    if "MENU" in names and (len(names) > 1 or allow_none):
        raise ProtocolError("MENU hands over to stock: it is a single press, never chorded or held")
    return tuple(sorted(names, key=BUTTONS.index))


def buttons_arg(names):
    return "+".join(names) if names else "NONE"


# ---- requests and responses --------------------------------------------------

def validate_arg(action, arg):
    """Return the canonical argument; reject anything outside the action's bounds."""
    if action not in ACTIONS:
        raise ProtocolError("Unknown action")
    arg = "-" if arg is None else str(arg)
    if action == "log":
        if not re.fullmatch(r"[1-9][0-9]{0,4}", arg) or int(arg) > MAX_LOG:
            raise ProtocolError(f"Log size must be 1..{MAX_LOG}")
        return str(int(arg))
    if action == "input":
        buttons, _, ms = arg.rpartition(":")
        if not re.fullmatch(r"[0-9]{2,3}", ms) or not MIN_PRESS_MS <= int(ms) <= MAX_PRESS_MS:
            raise ProtocolError(f"Input needs BUTTONS:ms with ms {MIN_PRESS_MS}..{MAX_PRESS_MS}")
        return f"{buttons_arg(parse_buttons(buttons))}:{int(ms)}"
    if action == "hold":
        return buttons_arg(parse_buttons(arg, allow_none=True))
    if action == "video":
        if arg not in ("key", "delta"):
            raise ProtocolError("Video takes key or delta")
        return arg
    if action == "console":
        if arg not in CONSOLE_COMMANDS:
            raise ProtocolError("Console command is not on the read-only list")
        return arg
    if action == "audio":
        if not re.fullmatch(r"[0-9]{1,10}", arg) or int(arg) > MAX_SEQ:
            raise ProtocolError("Audio takes the last received chunk number")
        return str(int(arg))
    if arg != "-":
        raise ProtocolError("This action takes no argument")
    return "-"


def encode_request(session, seq, action, arg="-"):
    if not 1 <= seq <= MAX_SEQ:
        raise ProtocolError("Sequence out of range")
    arg = validate_arg(action, arg)
    return encode_line(f"REQ {seq} {action} {arg} {request_mac(session, seq, action, arg)}")


def parse_request(session, line, expected_seq):
    parts = line.split(" ")
    if len(parts) != 5 or parts[0] != "REQ" or not re.fullmatch(r"[1-9][0-9]{0,9}", parts[1]):
        raise ProtocolError("Malformed request")
    seq, action, arg, mac = int(parts[1]), parts[2], parts[3], parts[4]
    if action not in ACTIONS:
        raise ProtocolError("Unknown action")
    if not hmac.compare_digest(_hex(mac, MAC_HEX, "request MAC"), request_mac(session, seq, action, arg)):
        raise ProtocolError("Request MAC mismatch")
    if seq != expected_seq:
        raise ProtocolError("Replayed or out-of-order request")
    return action, validate_arg(action, arg)


def body_limit(action, code):
    if code != "ok":
        return MAX_REASON
    return {"status": MAX_STATUS, "log": MAX_LOG, "screen": SCREEN_BYTES, "video": MAX_VIDEO,
            "perf": MAX_PERF, "console": MAX_CONSOLE, "audio": MAX_AUDIO}.get(action, 16)


def encode_response(session, seq, action, code, body=b""):
    if code not in CODES:
        raise ProtocolError("Unknown response code")
    if isinstance(body, str):
        body = body.encode("utf-8")
    if len(body) > body_limit(action, code):
        raise ProtocolError("Response body too large")
    if code == "ok" and action == "screen" and len(body) != SCREEN_BYTES:
        raise ProtocolError("Screen body must be exactly one 160x144 RGB565 frame")
    return encode_line(f"RES {seq} {code} {len(body)} {response_mac(session, seq, code, body)}") + body


def read_response(session, stream, expected_seq, action):
    parts = read_line(stream).split(" ")
    if len(parts) != 5 or parts[0] != "RES" or not re.fullmatch(r"[0-9]{1,10}", parts[1]) \
            or parts[2] not in CODES or not re.fullmatch(r"[0-9]{1,5}", parts[3]):
        raise ProtocolError("Malformed response")
    seq, code, length, mac = int(parts[1]), parts[2], int(parts[3]), parts[4]
    if seq != expected_seq:
        raise ProtocolError("Response for a different request")
    if length > body_limit(action, code):
        raise ProtocolError("Response body exceeds the action limit")
    body = read_exact(stream, length)
    if not hmac.compare_digest(_hex(mac, MAC_HEX, "response MAC"), response_mac(session, seq, code, body)):
        raise ProtocolError("Response MAC mismatch")
    if code == "ok" and action == "screen" and length != SCREEN_BYTES:
        raise ProtocolError("Screen frame has the wrong size")
    return code, body


# ---- addresses ---------------------------------------------------------------

def allowed_host(host, allow_loopback=False):
    """CHR1 is local-LAN only: RFC 1918 IPv4, or loopback for the host simulator."""
    try:
        address = ipaddress.IPv4Address(host)
    except (ipaddress.AddressValueError, TypeError, ValueError):
        return False
    if allow_loopback and address.is_loopback:
        return True
    return any(address in ipaddress.IPv4Network(net)
               for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))

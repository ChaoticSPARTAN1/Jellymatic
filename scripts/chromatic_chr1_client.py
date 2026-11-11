# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""CHR1 PC client for the Chromatic launcher, over USB serial or the LAN: a reference.

Specification: docs/Design-notes.md. Device side: chromatic/launcher/shared/remote.h.
Wire format and handshake are Remote Pilot's CHR1 (presets/chromatic/PROTOCOL.md there);
this module adds the USB opening ("CHR1 OPEN"), USB pairing, and Chromatic Manager's
read-only service actions (svc-*).

It opens no port by itself. `Stream` wraps anything with `write(bytes)` and
`read(n) -> bytes` (b'' on timeout): a pyserial port opened with DTR and RTS de-asserted
and left alone, a socket's makefile, or a test pipe. Keys come from the caller; never log
them, the PJ-PAIR line, or svc-read bodies (flash contents, including the owner's settings).
"""
import hashlib
import hmac
import re
import secrets
import time

MAGIC = 'CHR1'
SCREEN_BYTES = 160 * 144 * 2
FLASH_BYTES = 0x400000
READ_MAX = 0x4000
LAN_PORT = 38771


class Chr1Error(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class Stream:
    """Line and exact reads over a byte transport with an overall deadline per call."""

    def __init__(self, transport):
        self.transport = transport
        self.buffer = bytearray()

    def write(self, data):
        self.transport.write(data)

    def _fill(self, deadline):
        chunk = self.transport.read(4096)
        if chunk:
            self.buffer += chunk
        elif time.monotonic() > deadline:
            raise Chr1Error('The console did not answer in time.', 'timeout')

    def line(self, timeout):
        deadline = time.monotonic() + timeout
        while b'\n' not in self.buffer:
            if len(self.buffer) > 4096:
                raise Chr1Error('Line too long.')
            self._fill(deadline)
        at = self.buffer.index(b'\n')
        line, self.buffer = bytes(self.buffer[:at]), self.buffer[at + 1:]
        return line.rstrip(b'\r').decode('ascii', 'replace')

    def exact(self, size, timeout):
        deadline = time.monotonic() + timeout
        while len(self.buffer) < size:
            self._fill(deadline)
        data, self.buffer = bytes(self.buffer[:size]), self.buffer[size:]
        return data


def _tag(key, text):
    return hmac.new(key, text.encode('ascii'), hashlib.sha256).hexdigest()


class Session:
    """An authenticated CHR1 session. One request at a time."""

    def __init__(self, stream, key, hello_line, transport):
        parts = hello_line.split(' ')
        if len(parts) != 4 or parts[:2] != [MAGIC, 'HELLO'] or not re.fullmatch('[0-9a-f]{32}', parts[2]):
            raise Chr1Error(f'Expected CHR1 HELLO, got {hello_line[:40]!r}')
        self.stream, self.key, self.transport = stream, key, transport
        self.device_nonce, self.version = parts[2], parts[3]
        pc_nonce = secrets.token_hex(16)
        stream.write(f'{MAGIC} AUTH {pc_nonce} {_tag(key, f"CHR1 pc-auth {self.device_nonce} {pc_nonce}")}\n'.encode())
        answer = stream.line(5).split(' ')
        if answer == [MAGIC, 'DENY']:
            raise Chr1Error('The console refused this key.', 'deny')
        if len(answer) != 4 or answer[:2] != [MAGIC, 'OK'] or not hmac.compare_digest(
                answer[2], _tag(key, f'CHR1 device-auth {pc_nonce} {self.device_nonce}')):
            raise Chr1Error('The console did not prove the key.')
        self.capabilities = answer[3].split(',')
        self.session = hmac.new(key, f'CHR1 session {self.device_nonce} {pc_nonce}'.encode(), hashlib.sha256).digest()
        self.seq = 1

    def request(self, action, arg='-', timeout=5.0):
        head = f'REQ {self.seq} {action} {arg}'
        mac = hmac.new(self.session, head.encode(), hashlib.sha256).hexdigest()[:32]
        self.stream.write(f'{head} {mac}\n'.encode())
        parts = self.stream.line(timeout).split(' ')
        if len(parts) != 5 or parts[0] != 'RES' or int(parts[1]) != self.seq or not parts[3].isdigit():
            raise Chr1Error(f'Malformed response {" ".join(parts)[:60]!r}')
        code, length = parts[2], int(parts[3])
        body = self.stream.exact(length, timeout)
        expect = hmac.new(self.session, f'RES {self.seq} {code} {length}\n'.encode() + body, hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(parts[4], expect):
            raise Chr1Error('Response MAC mismatch.')
        self.seq += 1
        return code, body

    def ok(self, action, arg='-', timeout=5.0):
        code, body = self.request(action, arg, timeout)
        if code != 'ok':
            raise Chr1Error(f'{action} {code}: {body.decode("utf-8", "replace")}', code)
        return body

    # ---- Chromatic Manager's service actions (read only) ---------------------------
    def svc_begin(self, op, total):
        """Shows the service screen. total: bytes to hash plus read (0: no percentage)."""
        return self.ok('svc-begin', f'{op}:{total:x}').decode()

    def svc_identity(self):
        """(nonce_hex, answer): compare with identity_digest() from the backup record."""
        nonce = secrets.token_hex(8)
        return nonce, dict(item.split('=', 1) for item in self.ok('svc-id', nonce).decode().split(' '))

    def svc_read(self, offset, size):
        data = self.ok('svc-read', f'{offset:x}:{size:x}', timeout=10.0)
        if len(data) != size:
            raise Chr1Error('Short read.')
        return data

    def svc_hash(self, offset, size, seconds_per_mib=12.0):
        text = self.ok('svc-hash', f'{offset:x}:{size:x}', timeout=5.0 + seconds_per_mib * size / 0x100000).decode()
        return dict(item.split('=', 1) for item in text.split(' '))

    def svc_pct(self, pct):
        self.ok('svc-pct', str(int(pct)))

    def svc_freeze(self, kind):
        """Still "don't switch off" screen. Reset into the bootloader within wait-ms of the answer."""
        return dict(item.split('=', 1) for item in self.ok('svc-freeze', kind, timeout=3.0).decode().split(' '))

    def svc_end(self, ok):
        return self.ok('svc-end', 'ok' if ok else 'fail').decode()


def identity_digest(nonce_hex, mac, flash_id):
    """What svc-id returns for a unit, from the backup record's MAC ('aa:bb:..') and flash ID."""
    raw = bytes(int(part, 16) for part in mac.split(':'))
    return hashlib.sha256(b'PJ-SVC-ID1' + bytes.fromhex(nonce_hex) + raw + flash_id.to_bytes(3, 'big')).hexdigest()


def _chr1_line(stream, timeout):
    """Next CHR1 line on USB; boot output and PJ-DIAG lines in between are skipped."""
    deadline = time.monotonic() + timeout
    while True:
        line = stream.line(max(0.1, deadline - time.monotonic()))
        if line.startswith(('CHR1 ', 'PJ-PAIR ')):
            return line


def open_usb(stream, key, timeout=3.0):
    """Starts a session on the console UART (the launcher must be running)."""
    stream.write(b'CHR1 OPEN\n')
    line = _chr1_line(stream, timeout)
    if line in ('CHR1 NOKEY', 'CHR1 BUSY', 'CHR1 WAIT'):
        raise Chr1Error(line, line.split(' ')[1].lower())
    return Session(stream, key, line, 'usb')


def connect_lan(sock_stream, key):
    """Starts a session on an open TCP connection to the console's port 38771."""
    return Session(sock_stream, key, sock_stream.line(5), 'lan')


def pair_usb(stream, show_code, timeout=65.0):
    """USB pairing: the console shows a code and asks the owner to press A. Calls
    show_code(code) so the PC can show the same code. Returns the 32-byte key, or raises."""
    pc_nonce = secrets.token_hex(16)
    stream.write(f'CHR1 PAIR {pc_nonce}\n'.encode())
    line = _chr1_line(stream, 3.0)
    parts = line.split(' ')
    if parts[:2] == ['CHR1', 'PAIR-REFUSED']:
        raise Chr1Error(line, parts[2] if len(parts) > 2 else 'refused')
    if len(parts) != 4 or parts[:2] != ['CHR1', 'PAIRING']:
        raise Chr1Error(f'Unexpected pairing answer {line[:40]!r}')
    digest = hashlib.sha256(f'CHR1 pair-code {parts[2]} {pc_nonce}'.encode()).digest()
    code = f'{int.from_bytes(digest[:4], "big") % 1000000:06d}'
    if code != parts[3]:
        raise Chr1Error('Pairing code mismatch: not the console this PC asked.')
    show_code(code)
    line = _chr1_line(stream, timeout)
    if line == 'CHR1 PAIR-DENIED':
        raise Chr1Error('Pairing was not allowed on the console.', 'denied')
    match = re.fullmatch('PJ-PAIR ([0-9a-f]{64})', line)
    if not match:
        raise Chr1Error('Unexpected pairing result.')
    return bytes.fromhex(match.group(1))

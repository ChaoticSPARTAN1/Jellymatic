# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Remote control end to end on the PC, through both transports of the one CHR1 handler:
  - USB: the console model (check_remote.c serve-usb, the launcher's own remote.h, menu
    logic and service code) on stdin/stdout, i.e. a serial loopback;
  - LAN: the same model on loopback TCP (serve-lan).
Clients: the reference client (chromatic_chr1_client.py) and Remote Pilot's own protocol
functions, unchanged, over the serial stream. Synthetic flash image; no port, no device."""
import hashlib
import os
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time

from chromatic_chr1_client import (FLASH_BYTES, READ_MAX, Chr1Error, Stream, connect_lan, identity_digest, open_usb,
                                   pair_usb)
from connection_config import ROOT

REMOTE_PILOT = Path(os.environ.get('REMOTE_PILOT_HOME') or (ROOT / 'remote-pilot'))
SOURCE = ROOT / 'chromatic/launcher'
OUT = ROOT / 'staging/chromatic-launcher'
OUT.mkdir(parents=True, exist_ok=True)  # build output, ignored by Git
TEST_MAC = '24:6f:28:10:20:30'  # made up in check_remote.c; not the owner's unit
PORT = 38798


def env():
    e = dict(os.environ)
    e['PATH'] = 'C:/msys64/ucrt64/bin;' + e['PATH']
    return e


class Pipe:
    """The console model's UART: write() to its RX, read() what it sent (0.1 s at most)."""

    def __init__(self, exe, flash, key='none', cart=True):
        args = [str(exe), 'serve-usb', str(flash), key] + ([] if cart else ['nocart'])
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env())
        self.chunks, self.received = queue.Queue(), 0
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while True:
            data = self.process.stdout.read1(65536)
            if not data:
                break
            self.received += len(data)
            self.chunks.put(data)

    def write(self, data):
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def read(self, size):
        try:
            return self.chunks.get(timeout=0.1)
        except queue.Empty:
            return b''

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=30)


class SocketTransport:
    def __init__(self, sock):
        self.sock = sock
        sock.settimeout(0.1)

    def write(self, data):
        self.sock.sendall(data)

    def read(self, size):
        try:
            return self.sock.recv(size)
        except socket.timeout:
            return b''


class RemotePilotFile:
    """Remote Pilot's protocol functions read a binary file; give them the serial stream."""

    def __init__(self, stream):
        self.stream = stream

    def readline(self, limit=-1):
        return self.stream.line(5).encode() + b'\n'

    def read(self, size):
        return self.stream.exact(size, 5)


def build():
    exe = OUT / 'check_remote.exe'
    subprocess.run(['C:/msys64/ucrt64/bin/gcc.exe', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                    str(SOURCE / 'check_remote.c'), '-lws2_32', '-o', str(exe)], check=True, env=env(), cwd=SOURCE)
    print(subprocess.run([str(exe), str(OUT)], check=True, env=env(), capture_output=True, text=True).stdout.strip())
    ffmpeg = shutil.which('ffmpeg')
    for name in ('service-working', 'service-still', 'service-done', 'service-stopped', 'remote-pairing'):
        if ffmpeg:
            subprocess.run([ffmpeg, '-loglevel', 'error', '-y', '-i', str(OUT / f'ui-{name}.ppm'), '-vf',
                            'scale=640:576:flags=neighbor', str(OUT / f'ui-{name}.png')], check=True)
    return exe


def synthetic_flash():
    folder = OUT / 'remote-test'
    folder.mkdir(parents=True, exist_ok=True)
    data = bytearray(hashlib.shake_256(b'remote-test-usb').digest(FLASH_BYTES))
    data[0x300000:] = b'\xff' * 0x100000
    path = folder / 'synthetic-flash-usb.bin'
    path.write_bytes(bytes(data))
    return path, bytes(data)


def usb_pairing_and_remote_pilot(exe, flash):
    """No key on the console: pair over USB with the owner's A press, then drive the menu
    with Remote Pilot's own protocol functions over the serial stream."""
    sys.path.insert(0, str(REMOTE_PILOT))
    from chromatic import protocol as p
    pipe = Pipe(exe, flash)
    stream = Stream(pipe)
    stream.write(b'CHR1 OPEN\n')
    assert stream.line(3) == 'CHR1 NOKEY'
    shown = []
    key = pair_usb(stream, lambda code: (shown.append(code), pipe.write(b'#press A\n')))
    assert len(shown) == 1 and len(key) == 32
    # Remote Pilot's code, unchanged, after the USB opening line.
    stream.write(b'CHR1 OPEN\n')
    file = RemotePilotFile(stream)
    hello = p.parse_hello(stream.line(3)) if hasattr(p, 'parse_hello') else None
    device_nonce = hello[0] if isinstance(hello, tuple) else hello
    pc_nonce = p.new_nonce()
    stream.write(p.auth_line(key, device_nonce, pc_nonce))
    caps = p.parse_ok(key, stream.line(3), pc_nonce, device_nonce)
    assert {'status', 'screen', 'input', 'hold', 'video'} <= caps
    session = p.session_key(key, device_nonce, pc_nonce)
    seq = 1
    for action, arg in (('status', '-'), ('input', 'DOWN:100'), ('screen', '-'), ('video', 'key'), ('hold', 'UP'),
                        ('log', '2048'), ('console', 'phase'), ('perf', '-')):
        stream.write(p.encode_request(session, seq, action, arg))
        code, body = p.read_response(session, file, seq, action)
        assert code == 'ok', (action, body)
        seq += 1
    stream.write(p.encode_request(session, seq, 'bye'))
    assert p.read_response(session, file, seq, 'bye') == ('ok', b'bye')
    pipe.close()
    return key


def usb_backup(exe, flash, image):
    """Chromatic Manager's backup as CHR1 service actions over USB, with the console's
    percentage, then stock."""
    key = bytes(range(32))
    pipe = Pipe(exe, flash, key.hex())
    session = open_usb(Stream(pipe), key)
    readable = [(0, 0x9000), (0xF000, 0x3FE000)]  # NVS and the provisioning sectors are never served
    hashed = (0xF000, 0x3FE000 - 0xF000)
    total = sum(end - start for start, end in readable) + hashed[1]
    assert session.svc_begin('backup', total) == 'screen=shown'
    nonce, ident = session.svc_identity()
    assert ident['digest'] == identity_digest(nonce, TEST_MAC, 0x204016) and ident['flash-id'] == '204016'
    start = pipe.received
    data = bytearray()
    for begin, end in readable:
        for offset in range(begin, end, READ_MAX):
            data += session.svc_read(offset, min(READ_MAX, end - offset))
    wire = pipe.received - start
    assert bytes(data) == image[:0x9000] + image[0xF000:0x3FE000], 'USB read differs from the image'
    for offset, size in ((0x9000, 0x10), (0x8000, 0x2000), (0x3FE000, 0x2000), (0x3FF000, 0x10), (0, FLASH_BYTES)):
        for action in ('svc-read', 'svc-hash'):
            if action == 'svc-read' and size > READ_MAX:
                continue
            code, body = session.request(action, f'{offset:x}:{size:x}')
            assert code == 'refused' and body.startswith(b'protected'), (action, offset, size, code, body)
    whole = session.svc_hash(*hashed)
    piece = image[hashed[0]:hashed[0] + hashed[1]]
    assert whole['sha256'] == hashlib.sha256(piece).hexdigest() and whole['md5'] == hashlib.md5(piece).hexdigest()
    status = session.ok('status').decode()
    assert '"service":"active"' in status and '"servicePct":99' in status, status
    assert session.svc_end(True) == 'next=stock'
    session.ok('bye')
    pipe.write(b'#wait 3000\n')
    tail = b''
    deadline = time.monotonic() + 10
    while b'PJ-LAUNCHER-TO-STOCK' not in tail and time.monotonic() < deadline:
        tail += pipe.read(4096)
    assert b'PJ-LAUNCHER-TO-STOCK' in tail, 'no hand-over to stock after svc-end'
    pipe.close()
    return wire


def usb_no_cartridge(exe, flash):
    key = bytes(range(32))
    pipe = Pipe(exe, flash, key.hex(), cart=False)
    session = open_usb(Stream(pipe), key)
    pipe.write(b'#wait 8000\n')  # the opening attempts run out: no cartridge, held for the session
    assert '"screenVisible":false' in session.ok('status').decode()
    assert session.svc_begin('install', 0) == 'screen=hidden'
    assert session.svc_freeze('write')['screen'] == 'hidden'
    try:
        session.svc_read(0, 16)
    except Chr1Error as refusal:
        assert refusal.code == 'refused'
    else:
        raise SystemExit('read accepted while frozen')
    pipe.close()


def lan_service(exe, flash, image):
    key = bytes(range(32, 64))
    server = subprocess.Popen([str(exe), 'serve-lan', str(PORT), str(flash), key.hex()], stdout=subprocess.PIPE, env=env())
    try:
        assert server.stdout.readline().strip() == b'READY'
        sock = socket.create_connection(('127.0.0.1', PORT), timeout=3)
        session = connect_lan(Stream(SocketTransport(sock)), key)
        assert '"transport":"lan"' in session.ok('status').decode()
        # Service actions are USB only: over the LAN each is refused and nothing is read.
        for action, arg in (('svc-begin', 'status:0'), ('svc-read', '1000:10'), ('svc-hash', '10000:1000'),
                            ('svc-id', '0011223344556677')):
            code, body = session.request(action, arg)
            assert code == 'refused' and b'USB only' in body, (action, code, body)
        assert session.ok('screen') and len(session.ok('screen')) == 160 * 144 * 2
        session.ok('input', 'B:100')  # B on the menu: Games, a hand-over
        sock.close()
        assert server.wait(timeout=15) == 0, 'the console did not go on to stock'
    finally:
        if server.poll() is None:
            server.kill()
        server.wait()


if __name__ == '__main__':
    exe = build()
    flash, image = synthetic_flash()
    try:
        usb_pairing_and_remote_pilot(exe, flash)
        wire = usb_backup(exe, flash, image)
        usb_no_cartridge(exe, flash)
        lan_service(exe, flash, image)
    finally:
        flash.unlink(missing_ok=True)
    print(f'PASS: USB serial loopback: pairing confirmed by A, Remote Pilot\'s own protocol code over the serial stream '
          f'(status, input, screen, video, hold, log, console, perf, bye), whole-flash backup through svc-read matches '
          f'byte for byte except the protected ranges ({wire:,} bytes, about {wire * 10 / 115200 / 60:.1f} min at 115200 baud, modelled), svc-hash, '
          'svc-end then stock, NVS and provisioning refused to read and hash; no cartridge: hidden screen and immediate '
          'FREEZE answer. LAN loopback: the same handler, svc-* refused there, B hands over to stock.')

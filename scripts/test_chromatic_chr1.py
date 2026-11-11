# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""The launcher's real CHR1 handler (chromatic/launcher/shared/remote.h, driving the real
menu logic against a modelled FPGA) against Remote Pilot's real PC client
(RemotePilot/chromatic/lan_transport.py) over loopback TCP. Also a wrong key, a forged
request and a replay. No hardware; a random key and a synthetic flash image per run."""
import hashlib
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

from connection_config import ROOT

REMOTE_PILOT = Path(os.environ.get('REMOTE_PILOT_HOME') or (ROOT / 'remote-pilot'))
SOURCE = ROOT / 'chromatic/launcher'
OUT = ROOT / 'staging/chromatic-launcher'
OUT.mkdir(parents=True, exist_ok=True)  # build output, ignored by Git
PORT = 38799


def check(condition, message):
    if not condition:
        raise SystemExit(f'FAIL: {message}')


def env():
    e = dict(os.environ)
    e['PATH'] = 'C:/msys64/ucrt64/bin;' + e['PATH']
    return e


def build():
    exe = OUT / 'check_remote.exe'
    subprocess.run(['C:/msys64/ucrt64/bin/gcc.exe', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                    str(SOURCE / 'check_remote.c'), '-lws2_32', '-o', str(exe)], check=True, env=env(), cwd=SOURCE)
    return exe


def synthetic_flash():
    folder = OUT / 'remote-test'
    folder.mkdir(parents=True, exist_ok=True)
    data = bytearray(hashlib.shake_256(b'remote-test').digest(0x400000))
    data[0x300000:] = b'\xff' * 0x100000
    path = folder / 'synthetic-flash.bin'
    path.write_bytes(bytes(data))
    return path, bytes(data)


def main():
    sys.path.insert(0, str(REMOTE_PILOT))
    from chromatic import lan_transport as lan, protocol as p
    exe = build()
    flash, _ = synthetic_flash()
    key = secrets.token_bytes(32)
    server = subprocess.Popen([str(exe), 'serve-lan', str(PORT), str(flash), key.hex()], stdout=subprocess.PIPE, env=env())
    try:
        check(server.stdout.readline().strip() == b'READY', 'server started')
        client = lan.LanTransport('127.0.0.1', PORT, key, simulated=True, allow_loopback=True)
        code, body = client._call('status')
        status = lan.parse_status(body)
        check(code == 'ok' and status['phase'] == 'menu' and status['screen'] == 'menu', 'status')
        code, screen = client._call('screen')
        check(code == 'ok' and len(screen) == p.SCREEN_BYTES, 'screen')
        check(client._call('input', 'DOWN:100')[1] == b'queued', 'input moves the real menu')
        check(lan.parse_status(client._call('status')[1])['selected'] == 'jellyfin', 'menu moved to Jellyfin')
        check(client._call('input', 'UP+LEFT:50')[1] == b'queued', 'chord')
        check(client._call('hold', 'UP')[1] == b'held', 'hold')
        check(client._call('hold', 'NONE')[1] == b'released', 'release')
        code, body = client._call('log', '4096')
        check(code == 'ok' and b'input DOWN' in body and b'session open (lan)' in body, 'log has the presses')
        check(client._call('perf')[0] == 'ok' and client._call('console', 'version')[1] == b'launcher 1.1.2', 'perf, console')
        code, frame = client._call('video', 'key')
        check(code == 'ok' and frame[:4] == b'CHV1' and frame[12] in (0, 1) and len(frame) <= 20 + p.SCREEN_BYTES,
              'video keyframe')
        check(client._call('ping')[1] == b'pong', 'ping')
        check(client._call('bye')[1] == b'bye', 'bye')

        # Wrong key: refused at the handshake.
        wrong = lan.LanTransport('127.0.0.1', PORT, secrets.token_bytes(32), simulated=True, allow_loopback=True)
        try:
            wrong._call('status')
            check(False, 'wrong key accepted')
        except (OSError, p.ProtocolError, ValueError):
            pass
        time.sleep(1.2)  # the console allows one failed handshake a second

        # Forged MAC and replayed sequence: the connection is closed, nothing answered.
        for forge in ('mac', 'replay'):
            raw = socket.create_connection(('127.0.0.1', PORT), timeout=3)
            stream = raw.makefile('rb')
            hello = stream.readline().decode().split()
            pc_nonce = secrets.token_hex(16)
            raw.sendall(p.auth_line(key, hello[2], pc_nonce))
            stream.readline()
            session = p.session_key(key, hello[2], pc_nonce)
            if forge == 'mac':
                raw.sendall(b'REQ 1 ping - ' + b'0' * 32 + b'\n')
            else:
                raw.sendall(p.encode_request(session, 1, 'ping'))
                stream.readline(); stream.read(4)
                raw.sendall(p.encode_request(session, 1, 'ping'))   # same sequence again
            check(stream.readline() == b'', f'{forge} closed the connection')
            raw.close()
            time.sleep(0.1)
        # MENU is a hand-over, never chorded; afterwards the console runs stock.
        client = lan.LanTransport('127.0.0.1', PORT, key, simulated=True, allow_loopback=True)
        try:
            client._call('input', 'MENU+A:100')
            check(False, 'MENU chord accepted')
        except (ValueError, p.ProtocolError):
            pass
        check(client._call('input', 'MENU:100')[1] == b'handover', 'MENU hands over')
        check(server.wait(timeout=10) == 0, 'the modelled console went on to stock')
    finally:
        if server.poll() is None:
            server.kill()
        server.wait()
        flash.unlink(missing_ok=True)
    print('PASS: the launcher\'s real CHR1 handler and menu against Remote Pilot\'s LAN client: status, screen, '
          'input moving the real menu, chord, hold/release, log, perf, console, RLE video keyframe, ping, bye; wrong '
          'key, forged MAC, replay and MENU chord refused; MENU hands over to stock. Loopback TCP only.')


if __name__ == '__main__':
    main()

# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Read-only, full-chip Chromatic backup. No firmware/cart programming commands.

Run with the private esptool venv; all device identifiers and diagnostics stay
inside the fresh ignored backup directory. Console output is progress only.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from user_paths import PRIVATE, TOOLS  # noqa: E402  (per-user folder, see user_paths.py)
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
SIZE = 4 * 1024 * 1024
CHUNK = 256 * 1024
CONSOLE = sys.stdout


def progress(message):
    print(message, file=CONSOLE, flush=True)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save_json(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2)


def identity():
    from serial.tools import list_ports
    ports = [p for p in list_ports.comports() if p.vid == 0x374e and p.pid == 0x0101]
    if len(ports) != 1:
        raise RuntimeError('Expected exactly one stock Chromatic serial interface')
    command = "@(Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB\\VID_33AA&PID_0120*' -and $_.Status -eq 'OK' } | Select-Object InstanceId,Status) | ConvertTo-Json -Compress"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command],
                            check=True, capture_output=True, text=True, timeout=30)
    devices = json.loads(result.stdout)
    if isinstance(devices, dict):
        devices = [devices]
    if len(devices) != 1:
        raise RuntimeError('Expected exactly one healthy GWU2X interface')
    return ports[0], devices


def mcu_backup(folder):
    import esptool
    port, jtag = identity()
    save_json(folder / 'windows-identity.json', {
        'port': port.device, 'hwid': port.hwid, 'location': port.location,
        'jtag': jtag,
    })
    esp = None
    try:
        progress('MCU: entering temporary readback session (no flash writes).')
        # Instantiate explicitly so even a failed connect leaves a closable handle.
        esp = esptool.CHIP_DEFS['esp32'](port.device, 115200, False)
        esp.connect('default_reset', 3)
        description = esp.get_chip_description()
        revision = esp.get_chip_revision()
        mac = ':'.join(f'{b:02x}' for b in esp.read_mac())
        if not description.startswith('ESP32-U4WDH') or revision // 100 != 3:
            raise RuntimeError('Unexpected MCU model/revision')
        esp = esp.run_stub()  # RAM only; no erase/program command.
        flash_id = esp.flash_id()
        if flash_id != 0x164020:
            raise RuntimeError('Unexpected MCU flash ID/capacity')
        save_json(folder / 'mcu-identity.json', {
            'chip': description, 'revision': revision, 'mac': mac,
            'flash_id': '204016', 'size': SIZE,
        })
        hashes = []
        for pass_number in (1, 2):
            path = folder / f'mcu-read-{pass_number}.bin'
            digest = hashlib.sha256()
            with path.open('xb') as stream:
                for offset in range(0, SIZE, CHUNK):
                    data = esp.read_flash(offset, CHUNK)
                    if len(data) != CHUNK:
                        raise RuntimeError('Short MCU read')
                    if hashlib.md5(data).hexdigest() != esp.flash_md5sum(offset, CHUNK).lower():
                        raise RuntimeError('MCU on-chip chunk checksum mismatch')
                    stream.write(data)
                    digest.update(data)
                    progress(f'MCU read {pass_number}/2: {(offset + CHUNK) * 100 // SIZE}% (chunk verified).')
                stream.flush()
                os.fsync(stream.fileno())
            if sha(path.read_bytes()) != digest.hexdigest():
                raise RuntimeError('MCU saved-file checksum mismatch')
            hashes.append(digest.hexdigest())
        if hashes[0] != hashes[1]:
            raise RuntimeError('Full MCU readbacks differ')
        save_json(folder / 'mcu-verified.json', {'size': SIZE, 'sha256': hashes[0],
                  'reads': 2, 'chunk_md5_verified': True, 'saved_files_rehashed': True})
        progress('MCU: two complete reads match; every chunk and both saved files verified.')
    finally:
        if esp is not None:
            try:
                esp.hard_reset()
            finally:
                esp._port.close()
            progress('MCU: normal boot requested and serial port released.')


def fpga_backup(folder):
    identity()  # Refuse ambiguous/unavailable physical interface.
    exe = Path(os.environ.get('JELLYMATIC_OPENFPGALOADER') or (TOOLS / 'openFPGALoader/openFPGALoader.exe'))
    env = dict(os.environ)
    env['PATH'] = str(exe.parent) + ';C:\\msys64\\ucrt64\\bin;' + env['PATH']

    def loader(args, logfile, observe_serial=False):
        from serial.tools import list_ports
        disappeared = False
        with (folder / logfile).open('xb') as log:
            proc = subprocess.Popen([str(exe), '--cable', 'gwu2x', *args],
                                    stdout=log, stderr=subprocess.STDOUT, env=env)
            deadline = time.monotonic() + 240
            try:
                while proc.poll() is None:
                    if observe_serial and not any(p.vid == 0x374e and p.pid == 0x0101 for p in list_ports.comports()):
                        disappeared = True
                    if time.monotonic() > deadline:
                        raise TimeoutError('Bounded JTAG operation timed out')
                    time.sleep(0.5)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
        if proc.returncode:
            raise RuntimeError('JTAG operation failed; private diagnostic retained')
        if observe_serial:
            save_json(folder / 'same-unit-usb-proof.json', {'serial_disappeared_during_fpga_read': disappeared})
            if not disappeared:
                raise RuntimeError('Did not observe matching serial interface stop with FPGA')
        return (folder / logfile).read_text(encoding='utf-8', errors='replace')

    detected = loader(['--detect'], 'fpga-detect.log')
    if '0x1281b' not in detected or 'GW5A-25' not in detected:
        raise RuntimeError('Unexpected FPGA identity')
    hashes = []
    try:
        for number in (1, 2):
            path = folder / f'fpga-read-{number}.bin'
            if path.exists():
                raise RuntimeError('Refusing to overwrite a backup')
            progress(f'FPGA: full flash read {number}/2; display may temporarily stop.')
            log = loader(['--dump-flash', '--file-size', str(SIZE), str(path)],
                         f'fpga-read-{number}.log', observe_serial=number == 1)
            if 'JEDEC ID: 0xef4016' not in log:
                raise RuntimeError('Unexpected FPGA flash identity/capacity')
            data = path.read_bytes()
            if len(data) != SIZE:
                raise RuntimeError('Wrong FPGA readback size')
            # JEDEC is recorded by the loader; parser validates the image separately.
            hashes.append(sha(data))
        if hashes[0] != hashes[1]:
            raise RuntimeError('Full FPGA readbacks differ')
        save_json(folder / 'fpga-verified.json', {'size': SIZE, 'sha256': hashes[0],
                  'reads': 2, 'saved_files_rehashed': True, 'idcode': '0001281b',
                  'flash_jedec': '0xef4016'})
        progress('FPGA: two complete saved readbacks match.')
    finally:
        loader(['--reset'], 'fpga-reconfigure.log')
        progress('FPGA: reconfiguration from existing stock flash requested.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['mcu', 'fpga'])
    parser.add_argument('--folder', type=Path)
    args = parser.parse_args()
    if args.stage == 'mcu':
        if args.folder:
            parser.error('MCU stage always creates a fresh private directory')
        folder = PRIVATE / ('chromatic-backup-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S'))
        folder.mkdir(parents=True, exist_ok=False)
        (PRIVATE / 'chromatic-tools').mkdir(parents=True, exist_ok=True)
        (PRIVATE / 'chromatic-tools/latest-backup-path.txt').write_text(str(folder), encoding='utf-8')
    else:
        if not args.folder:
            parser.error('FPGA stage requires the MCU backup folder')
        folder = args.folder.resolve(strict=True)
        if not folder.is_relative_to(PRIVATE.resolve()) or not (folder / 'mcu-verified.json').is_file():
            parser.error('Requires a verified MCU backup in the private directory')
    with (folder / f'{args.stage}-diagnostic.log').open('x', encoding='utf-8') as log:
        try:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                (mcu_backup if args.stage == 'mcu' else fpga_backup)(folder)
        except Exception:
            traceback.print_exc(file=log)
            progress('Backup stopped; private diagnostics retained. No flash programming was requested.')
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

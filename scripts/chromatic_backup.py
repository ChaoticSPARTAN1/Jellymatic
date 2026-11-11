# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Read-only, full-chip Chromatic backup by hand. No firmware/cart programming commands.

Chromatic Manager's Back up takes the same full backup by itself; this is the advanced, by-hand way:

  chromatic_backup.py mcu
      The ESP32 flash, read twice (every chunk checked on the chip), into a new folder
      <your Jellymatic folder>/chromatic-backup-<UTC time>. Nothing is recorded yet.
  chromatic_backup.py fpga --folder chromatic-backup-<UTC time> [--second-copy <another disk>]
      The console chip (FPGA) flash, read twice; then the three-file restore bundle, its second copy
      (default: user_paths.SECOND_COPY) compared byte for byte, the completion receipt and the recovery
      check. Only when all of that checks out is the folder recorded as the console's original
      (chromatic-tools/latest-backup-path.txt), exactly as the Manager's full backup does.

The Manager's rules apply: when a full backup is already recorded on this PC both stages refuse at once,
before the console is touched (an original is never replaced); the start-up menu release must be there
to recognise the official firmware; the console must run the official firmware exactly as it comes and
its console chip must not hold a Jellymatic image. Every check that reads nothing comes before the reads.

Run with the private esptool venv; all device identifiers and diagnostics stay inside the backup folder.
Console output is progress only.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from user_paths import PRIVATE, SECOND_COPY, TOOLS  # noqa: E402  (per-user folder, see user_paths.py)
import shutil
import subprocess
import sys
import time
import traceback

import chromatic_manager as cm

ROOT = Path(__file__).resolve().parents[1]
SIZE = 4 * 1024 * 1024
CHUNK = 256 * 1024
CONSOLE = sys.stdout
SERIAL_VID = 0x374e
# Files the fpga stage makes; a folder that already holds one of them is refused before the console is read.
FPGA_STAGE_FILES = ('fpga-read-1.bin', 'fpga-read-2.bin', 'fpga-verified.json', 'same-unit-usb-proof.json',
                    'restore-bundle', 'completion-receipt.json', 'recovery-validation.json', 'fpga-diagnostic.log',
                    'record-diagnostic.log')


class Refused(Exception):
    """Stopped before anything was read (or kept); the message says why."""


def progress(message):
    print(message, file=CONSOLE, flush=True)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save_json(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2)


def sentence(text):
    return text[:1].upper() + text[1:]


def gwu2x_devices():
    """Healthy GWU2X JTAG bridges Windows lists (enumeration only)."""
    command = "@(Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB\\VID_33AA&PID_0120*' -and $_.Status -eq 'OK' } | Select-Object InstanceId,Status) | ConvertTo-Json -Compress"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command],
                            check=True, capture_output=True, text=True, timeout=30)
    devices = json.loads(result.stdout or '[]')
    return [devices] if isinstance(devices, dict) else devices


def identity():
    from serial.tools import list_ports
    ports = [p for p in list_ports.comports() if p.vid == SERIAL_VID and p.pid == 0x0101]
    if len(ports) != 1:
        raise RuntimeError('Expected exactly one stock Chromatic serial interface')
    devices = gwu2x_devices()
    if len(devices) != 1:
        raise RuntimeError('Expected exactly one healthy GWU2X interface')
    return ports[0], devices


def console_back(seconds=90, poll=1.0):
    """(serial interface back, exactly one JTAG bridge) after the FPGA was reconfigured from its own
    flash, watched for up to `seconds`. Enumeration only. Never raises."""
    end = time.monotonic() + seconds
    while True:
        try:
            from serial.tools import list_ports
            serial = any(p.vid == SERIAL_VID and p.pid is not None and (p.pid >> 8) == 0x01
                         for p in list_ports.comports())
        except Exception:
            serial = False
        try:
            bridge = len(gwu2x_devices()) == 1
        except Exception:
            bridge = False
        if (serial and bridge) or time.monotonic() >= end:
            return serial, bridge
        time.sleep(poll)


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


def loader_path():
    return Path(os.environ.get('JELLYMATIC_OPENFPGALOADER') or (TOOLS / 'openFPGALoader/openFPGALoader.exe'))


def fpga_backup(folder):
    """Both console chip reads; returns console_back() after the FPGA was reconfigured from its flash."""
    identity()  # Refuse ambiguous/unavailable physical interface.
    exe = loader_path()
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
                    if observe_serial and not any(p.vid == SERIAL_VID and p.pid == 0x0101 for p in list_ports.comports()):
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
    return console_back()


# ---- the checks that read nothing, and the record -----------------------------------------------------
def load_release():
    """The start-up menu release (the build in release/launcher, else a pinned copy), read only."""
    return cm.Release.load(repin=False)


def check_not_recorded():
    """The Manager's rule (chromatic_manager.full_backup_recorded): an original is never replaced."""
    if cm.full_backup_recorded():
        raise Refused(cm.ALREADY_RECORDED)


def release_or_refuse():
    try:
        release = load_release()
    except cm.Refused:
        release = None
    if release is None:
        raise Refused(cm.NO_RELEASE_FOR_BACKUP)
    return release


def check_stock_esp32(folder, release):
    """The ESP32 read must be the official firmware exactly as it comes (describe() == 'stock')."""
    state = cm.describe((folder / 'mcu-read-1.bin').read_bytes(), release).name
    if state != 'stock':
        raise Refused(cm.NOT_ORIGINAL.format(what=f'its ESP32 reads as "{state}"'))


def fpga_folder(name):
    """The mcu stage's folder: a path, or just its name inside the per-user folder."""
    folder = Path(name)
    if not folder.is_absolute() and not folder.exists():
        folder = PRIVATE / folder
    try:
        folder = folder.resolve(strict=True)
    except OSError:
        raise Refused('that backup folder does not exist. Give the folder the mcu stage made.')
    if (not folder.is_relative_to(PRIVATE.resolve()) or not folder.name.startswith(cm.FULL_BACKUP_PREFIX)
            or not (folder / 'mcu-verified.json').is_file()):
        raise Refused('the fpga stage needs the folder of a finished mcu stage, in your Jellymatic folder.')
    if any((folder / name).exists() for name in FPGA_STAGE_FILES):
        raise Refused('that folder already holds a console chip read or a restore bundle; it is never overwritten. '
                      'Start again with the mcu stage.')
    return folder


def record(folder, second, back):
    """Bundle, second copy (compared byte for byte), completion receipt and recovery check, every copy
    checked again; only then is `folder` recorded as the console's original and loaded back as the jobs
    load it. On any failure everything this step made is removed and nothing is recorded."""
    import chromatic_fpga as cf
    import chromatic_trial_runner as tr
    import verify_chromatic_backup as vb
    from hardware_preflight import check_backup_copies
    latest = PRIVATE / cm.LATEST_FULL_BACKUP
    serial_back, bridge_back = back
    made, registered = [], False
    try:
        made.append(folder / 'restore-bundle')
        bundle = vb.make_bundle(folder)  # validates the saved reads again, writes and re-reads the bundle
        if second.exists():
            raise ValueError('second copy folder already exists')
        made.append(second)
        vb.copy_bundle(bundle, second)
        made.append(folder / 'completion-receipt.json')
        vb.write_completion_receipt(folder, second)
        made.append(folder / 'recovery-validation.json')
        vb.write_json_durable(folder / 'recovery-validation.json', {
            'format': 1, 'checked_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
            'serial_returned': bool(serial_back), 'jtag_remains_available': bool(bridge_back),
            'no_flash_program_requested': True, 'esp32_reads_identical': True, 'fpga_reads_identical': True,
            'how': 'chromatic_backup.py by hand: read commands only; the console chip was reconfigured from its '
                   'untouched flash and the console watched coming back on USB.'})
        check_backup_copies(folder, second)
        tr.load_stock(bundle, second)
        latest.parent.mkdir(parents=True, exist_ok=True)
        if latest.exists():
            raise Refused(cm.ALREADY_RECORDED)
        registered = True
        tr.write_durable(latest, str(folder).encode('utf-8'))
        cf.OriginalBackup.load(latest)
        if tr.load_stock().image != (folder / 'mcu-read-1.bin').read_bytes():
            raise ValueError('registered backup does not load back')
    except BaseException:
        if registered:  # only the record this run wrote is taken back
            with contextlib.suppress(OSError):
                latest.unlink(missing_ok=True)
        for path in reversed(made):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
        raise


def run_stage(folder, stage, work):
    """(ok, result): runs one device stage with its output in the folder's private diagnostic log."""
    with (folder / f'{stage}-diagnostic.log').open('x', encoding='utf-8') as log:
        try:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                return True, work(folder)
        except Exception:
            traceback.print_exc(file=log)
            progress('Backup stopped; private diagnostics retained. No flash programming was requested. '
                     'Nothing was recorded.')
            return False, None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('stage', choices=['mcu', 'fpga'])
    parser.add_argument('--folder', type=Path, help='fpga stage: the folder (or its name) the mcu stage made')
    parser.add_argument('--second-copy', type=Path,
                        help='fpga stage: where the second copy goes (default: JELLYMATIC_SECOND_COPY); ideally '
                             'another disk or a USB stick')
    args = parser.parse_args(argv)
    if args.stage == 'mcu' and (args.folder or args.second_copy):
        parser.error('the mcu stage always creates a fresh folder; --folder and --second-copy are for the fpga stage')
    if args.stage == 'fpga' and not args.folder:
        parser.error('the fpga stage needs --folder (the folder the mcu stage made)')
    try:
        # Everything that reads nothing first: the record, the release, the folders.
        check_not_recorded()
        release = release_or_refuse()
        if args.stage == 'mcu':
            folder = PRIVATE / (cm.FULL_BACKUP_PREFIX + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S'))
            folder.mkdir(parents=True, exist_ok=False)
            ok, _ = run_stage(folder, 'mcu', mcu_backup)
            if not ok:
                return 1
            try:
                check_stock_esp32(folder, release)
            except Refused:
                shutil.rmtree(folder, ignore_errors=True)  # not an original: nothing of it is kept
                raise
            progress(f'ESP32 stage done (nothing recorded yet). Next: scripts\\chromatic_backup.py fpga --folder '
                     f'{folder.name}')
            return 0

        folder = fpga_folder(args.folder)
        second = (args.second_copy or SECOND_COPY) / folder.name
        if second.exists():
            raise Refused('the second copy folder for this backup already exists; it is never overwritten.')
        check_stock_esp32(folder, release)
        ok, back = run_stage(folder, 'fpga', fpga_backup)
        if not ok:
            return 1
        import chromatic_fpga as cf
        ours = cf.jellymatic_image((folder / 'fpga-read-1.bin').read_bytes())
        if ours:
            shutil.rmtree(folder, ignore_errors=True)  # not an original: nothing of it is kept
            raise Refused(cm.NOT_ORIGINAL.format(what=f"its console chip holds Jellymatic's {ours} image"))
        check_not_recorded()  # again, right before the record: one may have been made meanwhile
        with (folder / 'record-diagnostic.log').open('x', encoding='utf-8') as log:
            try:
                record(folder, second, back)
            except Refused:
                raise
            except Exception:
                traceback.print_exc(file=log)
                progress('The saved backup did not check out in both copies, so it was not recorded (the restore '
                         'bundle and second copy were removed). Nothing was written to the console. Check the '
                         'second-copy disk, then start again with the mcu stage.')
                return 1
    except Refused as refusal:
        progress(f'Refused: {sentence(str(refusal))}')
        return 1
    progress('Full backup recorded: both chips read twice and identical, the restore bundle and its second copy '
             'saved and checked byte for byte. Nothing was written to the console.')
    if not all(back):
        progress('The console was not seen coming back on USB after the console chip read. Switch it off and on; '
                 'it starts from its untouched flash as before.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

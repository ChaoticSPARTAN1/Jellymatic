# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Bounded Chromatic MCU trial that always ends back on stock, unattended.

A person starts this locally; no assistant turn is needed to finish it. Plain
runs are a dry run that opens no device. A real trial needs --execute and the
exact candidate SHA-256, and it always finishes by restoring the console.

Phase A scope is the ESP32 only. The stock FPGA is what provides the USB serial
link, and that link is how a bad MCU image gets fixed, so this tool has no FPGA
write path at all and refuses to be pointed at one.

Every step is journalled and flushed first. If the PC, the tool or the power dies
partway, run this again with no arguments: an unfinished journal goes straight to
restoring stock. If restoring cannot finish it stops and prints what to do.

No output names a device, MAC, setting or firmware content. This runner's own erase
and program path has not been exercised on a console; docs/Recovery.md covers recovery.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from user_paths import PRIVATE  # noqa: E402  (per-user folder, see user_paths.py)
import shutil
import struct
import sys
import threading
import time
import zlib

from verify_chromatic_backup import SIZE, sha, validate_fpga, validate_mcu

ROOT = Path(__file__).resolve().parents[1]
WORK = PRIVATE / 'chromatic-trial'
SECTOR = 4096
SETTINGS = (0x9000, 0x10000)  # NVS and PHY data: the console's own settings, never a trial's to change
STOCK_APP = (0x10000, 0x100000)
EXIT_OK, EXIT_REFUSED, EXIT_RECOVERED, EXIT_MANUAL = 0, 1, 2, 3
MANUAL_STEPS = """
STOP: stock could not be restored automatically. Nothing else will be attempted.
  1. Leave the console plugged in and switched on. Do not start any other flashing tool.
  2. Run this tool once more with no arguments; it resumes from where it stopped.
  3. If it stops here again, follow docs/Recovery.md.
Your backups are unchanged. Do not take a new backup to restore from.
"""


class Refused(Exception):
    """Stopped before any flash write; the console is as it was."""


class StepFailed(Exception):
    pass


class StepTimeout(StepFailed):
    pass


class WrongUnit(StepFailed):
    """Never retried: a different console must not receive this unit's image."""


@dataclass(frozen=True)
class Deadlines:
    preflight: float = 300
    connect: float = 60
    read: float = 900  # a full 4 MiB read took 384 s on this console
    write: float = 300
    boot: float = 90
    device_return: float = 120
    poll: float = 1.0
    restore_attempts: int = 3


@dataclass(frozen=True)
class StockBackup:
    image: bytes
    identity: dict
    sha256: str


def partitions(data):
    """Partition entries from a checksummed table: (type, subtype, offset, size)."""
    table = data[0x8000:0x8c00]
    found = []
    for at in range(0, len(table), 32):
        entry = table[at:at + 32]
        if entry[:2] == b'\xaa\x50':
            found.append((entry[2], entry[3], *struct.unpack_from('<II', entry, 4)))
        elif entry[:2] == b'\xeb\xeb':
            if hashlib.md5(table[:at]).digest() != entry[16:32]:
                raise Refused('Candidate partition table checksum is wrong.')
            return found
        else:
            break
    raise Refused('Candidate has no checksummed partition table.')


def check_candidate(data, expected_sha, stock):
    """The user's rule: a candidate keeps stock firmware alongside whatever it adds."""
    if not expected_sha or sha(data) != expected_sha.lower():
        raise Refused('Candidate SHA-256 does not match the value you passed.')
    if len(data) != SIZE or data[0x1000] != 0xe9:
        raise Refused('Candidate is not a complete 4 MiB ESP32 flash image.')
    try:
        validate_fpga(data)
    except ValueError:
        pass
    else:
        raise Refused('That is an FPGA image. Phase A never writes the FPGA.')
    entries = partitions(data)
    if (1, 2, 0x9000, 0x6000) not in entries or (1, 1, 0xf000, 0x1000) not in entries:
        raise Refused('Candidate moves the settings partitions.')
    for _, _, offset, size in entries:
        if offset + size > SIZE:
            raise Refused('Candidate has a partition outside the flash.')
    app = stock.image[STOCK_APP[0]:STOCK_APP[0] + STOCK_APP[1]].rstrip(b'\xff')
    for kind, _, offset, size in entries:
        if kind == 0 and not offset % 0x10000 and len(app) <= size and data[offset:offset + len(app)] == app:
            return
    raise Refused('Candidate does not contain the stock firmware byte for byte.')


def same_outside_settings(a, b):
    return a[:SETTINGS[0]] == b[:SETTINGS[0]] and a[SETTINGS[1]:] == b[SETTINGS[1]:]


def with_settings_from(image, settings_source):
    return image[:SETTINGS[0]] + settings_source[SETTINGS[0]:SETTINGS[1]] + image[SETTINGS[1]:]


def changed_regions(current, target):
    """Sector-aligned runs that differ, so only what changed is erased."""
    regions, start = [], None
    for offset in range(0, SIZE, SECTOR):
        differs = current[offset:offset + SECTOR] != target[offset:offset + SECTOR]
        if differs and start is None:
            start = offset
        elif not differs and start is not None:
            regions.append((start, target[start:offset]))
            start = None
    if start is not None:
        regions.append((start, target[start:SIZE]))
    return regions


def fsync_path(path):
    if os.name == 'nt':
        return  # directories cannot be opened for fsync on Windows; files still are
    handle = os.open(path, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def write_durable(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('wb') as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)
    fsync_path(path.parent)


class Journal:
    """Append-only, one CRC-checked line per event, flushed to disk before returning."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.path = self.folder / 'journal.jsonl'
        self.records, self.damaged, self.good_end = [], False, None

    def load(self):
        self.records, self.damaged, self.good_end = [], False, None
        if not self.path.exists():
            return self
        raw = self.path.read_bytes()
        # Everything up to the last newline was appended whole; a tail after it is torn.
        self.good_end = raw.rfind(b'\n') + 1 if raw and not raw.endswith(b'\n') else None
        lines = raw.split(b'\n')
        if lines and not lines[-1]:
            lines.pop()
        for index, line in enumerate(lines):
            body, _, check = line.rpartition(b'\t')
            try:
                valid = bool(body) and int(check, 16) == zlib.crc32(body)
                record = json.loads(body) if valid else None
            except ValueError:
                record = None
            if record is None:
                # A torn final line is just a crash mid-append. Anything earlier
                # means records are missing, so assume the worst about writes.
                self.damaged = index != len(lines) - 1
                break
            self.records.append(record)
        return self

    def append(self, phase, event, **data):
        record = {'phase': phase, 'event': event, 'at': time.time(), **data}
        body = json.dumps(record, separators=(',', ':')).encode('ascii')
        self.folder.mkdir(parents=True, exist_ok=True)
        if self.good_end is not None and self.path.exists():
            # Drop a torn final line before appending, or every later load would
            # read it as damage in the middle and never see the journal closed.
            with self.path.open('r+b') as file:
                file.truncate(self.good_end)
                file.flush()
                os.fsync(file.fileno())
            self.good_end = None
        with self.path.open('ab') as file:
            file.write(body + b'\t' + f'{zlib.crc32(body):08x}'.encode('ascii') + b'\n')
            file.flush()
            os.fsync(file.fileno())
        self.records.append(record)

    @property
    def exists(self):
        return self.path.exists()

    @property
    def closed(self):
        return bool(self.records) and not self.damaged and self.records[-1]['phase'] == 'close'

    @property
    def writes_started(self):
        return self.damaged or any(r['phase'] == 'write' for r in self.records)

    def value(self, key):
        for record in reversed(self.records):
            if key in record:
                return record[key]
        return None

    def archive(self):
        closed = self.folder / 'closed'
        closed.mkdir(exist_ok=True)
        stamp = time.strftime('%Y%m%dT%H%M%S')
        for number in range(1000):
            target = closed / f'{stamp}-{number}'
            if not target.exists():
                break
        target.mkdir()
        for path in [self.path, *self.folder.glob('baseline-*.bin')]:
            if path.exists():
                shutil.move(str(path), str(target / path.name))


class Runner:
    def __init__(self, folder, load_stock, programmer_factory, gate=lambda: None, deadlines=Deadlines(),
                 journal_class=Journal, clock=time.monotonic, sleep=time.sleep, say=print):
        self.folder = Path(folder)
        self.load_stock = load_stock
        self.factory = programmer_factory
        self.gate = gate
        self.deadlines = deadlines
        self.journal = journal_class(self.folder)
        self.clock, self.sleep, self.say = clock, sleep, say
        self.device = None

    def bounded(self, seconds, function):
        """Run one device operation with a hard deadline, in case the port hangs."""
        outcome = {}

        def work():
            try:
                outcome['value'] = function()
            except BaseException as error:
                outcome['error'] = error

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        thread.join(seconds)
        if thread.is_alive():
            if self.device is not None:
                self.device.abort()  # closing the port is what frees a stuck call
            raise StepTimeout('A device operation passed its deadline.')
        if 'error' in outcome:
            raise outcome['error']
        return outcome.get('value')

    def step(self, phase, seconds, function):
        self.journal.append(phase, 'begin')
        result = self.bounded(seconds, function)
        self.journal.append(phase, 'end')
        return result

    def wait_present(self, seconds):
        limit = self.clock() + seconds
        while not self.device.device_present():
            if self.clock() >= limit:
                raise StepFailed('The console did not appear in time.')
            self.sleep(self.deadlines.poll)

    def check_unit(self, stock):
        found = self.device.identity()
        for key in ('chip', 'revision', 'mac', 'flash_id'):
            if found.get(key) != stock.identity.get(key):
                raise WrongUnit('This is not the console the backup came from.')

    def status(self):
        self.journal.load()
        if not self.journal.exists:
            return 'No trial has been started.'
        return 'The last trial closed cleanly.' if self.journal.closed else 'A trial is unfinished; run with no arguments.'

    def dry_run(self, candidate=None, expected_sha=None):
        """Offline only: no programmer is created, so nothing can touch a device."""
        try:
            self.gate()
            stock = self.load_stock()
            if candidate is None:
                self.say('Dry run: backups and gates pass. Nothing was touched.')
                return EXIT_OK
            check_candidate(candidate, expected_sha, stock)
            target = with_settings_from(candidate, stock.image)
            count = sum(len(data) for _, data in changed_regions(stock.image, target)) // SECTOR
            self.say(f'Dry run: candidate accepted, about {count} sectors would change. Nothing was touched.')
            return EXIT_OK
        except (Refused, ValueError, KeyError, OSError) as error:
            self.say(f'Dry run refused: {error if isinstance(error, Refused) else "backup or gate check failed"}')
            return EXIT_REFUSED

    def trial(self, candidate, expected_sha, marker):
        self.journal.load()
        if self.journal.exists and self.journal.closed:
            self.journal.archive()
            self.journal.load()
        if self.journal.exists:
            raise Refused('An earlier trial is unfinished. Run with no arguments first.')
        if not marker:
            raise Refused('A boot marker is required. USB stays up even if the MCU is dead, '
                          'so a serial port alone proves nothing about the candidate.')
        observed = False
        try:
            self.step('preflight', self.deadlines.preflight, self.gate)
            stock = self.bounded(self.deadlines.preflight, self.load_stock)
            check_candidate(candidate, expected_sha, stock)
            self.device = self.factory()
            baseline = self.step('identity', self.deadlines.connect + 2 * self.deadlines.read,
                                 lambda: self.take_baseline(stock))
            target = with_settings_from(candidate, baseline)
            regions = changed_regions(baseline, target)
            self.journal.append('plan', 'ready', sectors=sum(len(d) for _, d in regions) // SECTOR,
                                candidate=sha(candidate))
            self.step('write', self.deadlines.write, lambda: self.write_regions(regions))
            self.step('verify', self.deadlines.read, lambda: self.expect(target))
            observed = self.step('boot', self.deadlines.boot + self.deadlines.device_return,
                                 lambda: self.device.boot(marker, self.deadlines.boot))
            if not observed:
                raise StepFailed('The candidate did not report that it booted.')
        except (Exception, KeyboardInterrupt) as error:
            self.journal.append('trial', 'failed')
            if not self.journal.writes_started:
                self.close_device()
                self.journal.append('close', 'nothing-changed')
                self.journal.archive()
                self.say(f'Stopped before any flash write: {error if isinstance(error, Refused) else "see the journal"}')
                return EXIT_REFUSED
            self.say('Trial failed; restoring stock now.')
        else:
            self.journal.append('trial', 'candidate-booted')
            self.say('Candidate booted. Restoring stock now.')
        result = self.restore()
        if result != EXIT_OK:
            return result
        return EXIT_OK if observed else EXIT_RECOVERED

    def take_baseline(self, stock):
        self.device.connect()
        self.check_unit(stock)
        first, second = self.device.read_all(), self.device.read_all()
        if len(first) != SIZE or first != second:
            raise StepFailed('Two full reads of the console differ.')
        if not same_outside_settings(first, stock.image):
            raise Refused('The console is not on the backed-up stock firmware.')
        # Settings may have changed since the backup; put back what is there now.
        digest = sha(first)
        write_durable(self.folder / f'baseline-{digest}.bin', first)
        self.journal.append('identity', 'baseline', baseline=digest)
        return first

    def write_regions(self, regions):
        for offset, data in regions:
            self.device.write(offset, data)

    def expect(self, image):
        found = self.device.read_all()
        if len(found) != SIZE or sha(found) != sha(image):
            raise StepFailed('Full readback does not match.')

    def close_device(self):
        if self.device is not None:
            try:
                self.device.abort()
            except Exception:
                pass

    def restore_target(self, stock):
        wanted = self.journal.value('baseline')
        candidates = [self.folder / f'baseline-{wanted}.bin'] if wanted else []
        candidates += sorted(self.folder.glob('baseline-*.bin'))
        for path in candidates:
            if path.exists():
                data = path.read_bytes()
                if path.stem == f'baseline-{sha(data)}' and same_outside_settings(data, stock.image):
                    return data
        return stock.image  # the original backup, with its original settings

    def restore_once(self, stock, target):
        self.device.connect()
        self.check_unit(stock)
        current = self.device.read_all()
        target = with_settings_from(target, current)  # never put old settings back
        self.restored_image = target
        regions = changed_regions(current, target)
        self.journal.append('restore', 'plan', sectors=sum(len(d) for _, d in regions) // SECTOR)
        self.write_regions(regions)

    def restore(self):
        """Bounded attempts, then stop and tell the person; never loop forever."""
        try:
            stock = self.load_stock()
            target = self.restore_target(stock)
        except (Exception, KeyboardInterrupt):
            return self.manual()
        if self.device is None:
            self.device = self.factory()
        seconds = self.deadlines.connect + 2 * self.deadlines.read + self.deadlines.write
        for attempt in range(self.deadlines.restore_attempts):
            try:
                self.journal.append('restore', 'attempt', number=attempt + 1)
                self.bounded(self.deadlines.device_return + 1, lambda: self.wait_present(self.deadlines.device_return))
                self.step('restore', seconds, lambda: self.restore_once(stock, target))
                self.step('verify_stock', self.deadlines.read, lambda: self.expect(self.restored_image))
                self.step('return', self.deadlines.device_return, self.return_to_normal)
                self.journal.append('close', 'stock-verified', image=sha(self.restored_image))
                self.journal.archive()
                self.say('Stock restored and verified by full readback.')
                return EXIT_OK
            except WrongUnit:
                self.journal.append('restore', 'wrong-unit')
                return self.manual()
            except (Exception, KeyboardInterrupt):
                self.journal.append('restore', 'attempt-failed')
                self.close_device()
                self.sleep(self.deadlines.poll)
        return self.manual()

    def return_to_normal(self):
        self.device.boot(None, 0)
        self.wait_present(self.deadlines.device_return)

    def manual(self):
        self.journal.append('restore', 'manual-required')
        self.close_device()
        self.say(MANUAL_STEPS)
        return EXIT_MANUAL

    def resume(self):
        """Called whenever an unfinished journal exists, whatever the arguments were."""
        self.journal.load()
        if self.journal.closed:
            self.journal.archive()
            self.say('The last trial had already finished.')
            return EXIT_OK
        if not self.journal.writes_started:
            self.journal.append('close', 'nothing-changed')
            self.journal.archive()
            self.say('The unfinished trial never wrote to the console. Nothing to restore.')
            return EXIT_REFUSED
        self.say('Unfinished trial found; restoring stock.')
        result = self.restore()
        return EXIT_RECOVERED if result == EXIT_OK else result


class RealProgrammer:
    """The only hardware code here, and it can only reach the ESP32.

    Calls follow esptool 4.12's own write_flash and chromatic_backup.py's reads.
    It has never been run against a console. Nothing in the tests creates one.
    """
    CHUNK = 256 * 1024

    def __init__(self):
        self.esp = None

    def device_present(self):
        from serial.tools import list_ports
        return sum(p.vid == 0x374e and p.pid == 0x0101 for p in list_ports.comports()) == 1

    def connect(self):
        import esptool
        from chromatic_backup import identity
        self.abort()
        port, _ = identity()
        self.esp = esptool.CHIP_DEFS['esp32'](port.device, 115200, False)
        self.esp.connect('default_reset', 3)
        description, revision = self.esp.get_chip_description(), self.esp.get_chip_revision()
        self.found = {'chip': description.split(' ')[0], 'revision': revision // 100,
                      'mac': ':'.join(f'{b:02x}' for b in self.esp.read_mac())}
        self.esp = self.esp.run_stub()
        if self.esp.flash_id() != 0x164020:
            raise StepFailed('Unexpected flash chip.')
        self.found['flash_id'] = '204016'
        self.esp.flash_set_parameters(SIZE)

    def identity(self):
        return self.found

    def read_all(self):
        data = bytearray()
        for offset in range(0, SIZE, self.CHUNK):
            chunk = self.esp.read_flash(offset, self.CHUNK)
            if len(chunk) != self.CHUNK or hashlib.md5(chunk).hexdigest() != self.esp.flash_md5sum(offset, self.CHUNK).lower():
                raise StepFailed('A flash read failed its on-chip checksum.')
            data += chunk
        return bytes(data)

    def write(self, offset, data):
        from esptool.loader import ESPLoader
        size = self.esp.FLASH_WRITE_SIZE
        blocks = self.esp.flash_begin(len(data), offset)
        for number in range(blocks):
            block = data[number * size:(number + 1) * size]
            self.esp.flash_block(block + b'\xff' * (size - len(block)), number, timeout=30)
        # The stub acks a block before writing it; this read waits for the last one.
        self.esp.read_reg(ESPLoader.CHIP_DETECT_MAGIC_REG_ADDR, timeout=30)
        if hashlib.md5(data).hexdigest() != self.esp.flash_md5sum(offset, len(data)).lower():
            raise StepFailed('A written region failed its on-chip checksum.')

    def boot(self, marker, seconds):
        """Normal boot. With a marker, True only if the app prints it on the serial console."""
        port = self.esp._port
        self.esp.hard_reset()
        seen = b''
        limit = time.monotonic() + seconds
        try:
            while marker and time.monotonic() < limit:
                seen = (seen + port.read(port.in_waiting or 1))[-4096:]
                if marker.encode('ascii') in seen:
                    return True
            return False
        finally:
            port.close()
            self.esp = None

    def abort(self):
        if self.esp is not None:
            try:
                self.esp._port.close()
            finally:
                self.esp = None


def backup_folders():
    folder = Path((PRIVATE / 'chromatic-tools/latest-backup-path.txt').read_text(encoding='utf-8').strip())
    receipt = json.loads((folder / 'completion-receipt.json').read_text(encoding='utf-8'))
    return folder / 'restore-bundle', Path(receipt['secondary_bundle'])


def load_stock(primary=None, secondary=None):
    """Both copies must agree and match their recorded hash before either is used."""
    if primary is None:
        primary, secondary = backup_folders()
    primary, secondary = Path(primary), Path(secondary)
    record_path = next(primary.glob('*-backup.json'))
    record = json.loads(record_path.read_text(encoding='utf-8'))
    esp = record['esp']
    image_path = next(primary.glob('*-esp32_flash_4MB.bin'))
    image = image_path.read_bytes()
    if sha(image) != esp['sha256'] or len(image) != SIZE:
        raise Refused('The stock backup does not match its recorded hash.')
    if (secondary / image_path.name).read_bytes() != image:
        raise Refused('The two stock backup copies differ.')
    validate_mcu(image)
    identity = {'chip': esp['chip'], 'revision': esp['revision'], 'mac': esp['mac'], 'flash_id': esp['flash_id']}
    return StockBackup(image, identity, esp['sha256'])


def real_gate():
    from hardware_preflight import check_backup_copies, check_checkpoint, check_usage
    bundle, secondary = backup_folders()
    folder = bundle.parent
    check_usage(json.loads((PRIVATE / 'chromatic-tools/usage-snapshot.json').read_text(encoding='utf-8')))
    check_checkpoint(json.loads((folder / 'recovery-validation.json').read_text(encoding='utf-8')))
    check_backup_copies(folder, secondary)


def main(argv=None, runner=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--execute', action='store_true', help='Really write the candidate, then restore stock.')
    parser.add_argument('--candidate', type=Path, help='Complete 4 MiB ESP32 image.')
    parser.add_argument('--candidate-sha256', help='Exact SHA-256 you reviewed.')
    parser.add_argument('--boot-marker', help='Text the candidate prints on serial once it is running.')
    parser.add_argument('--target', choices=('mcu', 'fpga'), default='mcu')
    parser.add_argument('--status', action='store_true', help='Say whether a trial is unfinished, then stop.')
    args = parser.parse_args(argv)
    real = runner is None
    runner = runner or Runner(WORK, load_stock, RealProgrammer, real_gate)
    if real:
        # Chromatic Manager supersedes this tool; never run over its job or its lock.
        manager_journal = PRIVATE / 'chromatic-manager'
        if (manager_journal / 'journal.jsonl').exists() and not Journal(manager_journal).load().closed:
            runner.say('Refused: Chromatic Manager has an unfinished job. Finish it there first.')
            return EXIT_REFUSED
    if args.target == 'fpga':
        runner.say('Refused: Phase A never writes the FPGA. It carries the USB link that recovery depends on.')
        return EXIT_REFUSED
    if args.status:
        runner.say(runner.status())
        return EXIT_OK
    runner.journal.load()
    if runner.journal.exists:
        return runner.resume()  # restoring comes first, whatever else was asked for
    candidate = args.candidate.read_bytes() if args.candidate else None
    if not args.execute:
        return runner.dry_run(candidate, args.candidate_sha256)
    if candidate is None or not args.candidate_sha256:
        runner.say('Refused: --execute needs --candidate and its exact --candidate-sha256.')
        return EXIT_REFUSED
    try:
        return runner.trial(candidate, args.candidate_sha256, args.boot_marker)
    except Refused as error:
        runner.say(f'Refused: {error}')
        return EXIT_REFUSED


if __name__ == '__main__':
    # Same operating-system lock as Chromatic Manager: one Chromatic tool at a time.
    from chromatic_lock import Busy, DeviceLock
    try:
        with DeviceLock():
            raise SystemExit(main(sys.argv[1:]))
    except Busy as error:
        print(f'Refused: {error}')
        raise SystemExit(EXIT_REFUSED)

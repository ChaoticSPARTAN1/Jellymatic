# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""The first full backup, starting from an EMPTY per-user folder (JELLYMATIC_HOME) and an empty second
copy folder, against the simulated console of test_chromatic_manager (the synthetic stock image, no
ModRetro code) and a simulated console chip (FPGA) and JTAG bridge.

Checks: status works with no backup and writes nothing; an unknown model and every read mismatch
refuse with nothing saved and nothing written; the first backup reads both chips twice, saves the
bundle and its second copy, writes the completion receipt and the recovery check, and only then
registers it; every loader reads it back from disk (load_stock, OriginalBackup.load, the hardware
preflight, the Manager core's AI rule); a broken second copy is refused; Install, Back up and a
console chip job from an empty folder take the full backup first and then go on. No hardware: the
console never sees a write during a backup, and nothing leaves the temporary folders.
"""
import contextlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_TEMP = tempfile.TemporaryDirectory()
HOME = Path(_TEMP.name) / 'home'
SECOND = Path(_TEMP.name) / 'second'
# Before any project module is imported: user_paths reads these once.
os.environ['JELLYMATIC_HOME'] = str(HOME)
os.environ['JELLYMATIC_SECOND_COPY'] = str(SECOND)
os.environ.pop('JELLYMATIC_STOCK_DUMP', None)

try:
    import esptool.bin_image  # noqa: F401  (verify_chromatic_backup checks the saved images with it)
    ESPTOOL = 'esptool'
except ImportError:
    # No esptool in this Python: a small stand-in for esptool.bin_image that parses the two executable
    # images of the synthetic stock flash itself (header, segments, XOR checksum, appended SHA-256), so the
    # backup flow is still tested in full rather than skipped. The Manager itself always needs esptool.
    import hashlib as _hashlib
    import io as _io
    import struct as _struct
    import types as _types

    class _Image:
        def __init__(self, stream):
            data = stream.read() if isinstance(stream, _io.IOBase) else bytes(stream)
            if len(data) < 24 or data[0] != 0xe9:
                raise ValueError('not an ESP32 image')
            self.chip_id = _struct.unpack_from('<H', data, 12)[0]
            self.append_digest = data[23] == 1
            at, self.segments, self._checksum = 24, [], 0xef
            for _ in range(data[1]):
                _, length = _struct.unpack_from('<II', data, at)
                body = data[at + 8:at + 8 + length]
                self.segments.append(body)
                for byte in body:
                    self._checksum ^= byte
                at += 8 + length
            end = (at + 16) & ~15
            self.checksum = data[end - 1]
            self.stored_digest = data[end:end + 32] if self.append_digest else None
            self.calc_digest = _hashlib.sha256(data[:end]).digest() if self.append_digest else None

        def calculate_checksum(self):
            return self._checksum

    _package = _types.ModuleType('esptool')
    _package.__path__ = []
    _package.bin_image = _types.ModuleType('esptool.bin_image')
    _package.bin_image.ESP32FirmwareImage = _Image
    sys.modules['esptool'], sys.modules['esptool.bin_image'] = _package, _package.bin_image
    ESPTOOL = 'a stand-in for esptool.bin_image (esptool is not installed in this Python)'

import chromatic_fpga as cf  # noqa: E402
import chromatic_manager as cm  # noqa: E402
import chromatic_manager_core as core  # noqa: E402
import chromatic_trial_runner as tr  # noqa: E402
import hardware_preflight as preflight  # noqa: E402
import user_paths  # noqa: E402
from compose_chromatic_image import STOCK_ROWS, _app_image, synthetic_stock  # noqa: E402
from test_chromatic_manager import FAST, IDENTITY, Console, Device, check  # noqa: E402

assert user_paths.PRIVATE == HOME and user_paths.SECOND_COPY == SECOND, 'the test must own the per-user folders'
SIZE = cm.SIZE


# ---- a synthetic launcher release (no ESP-IDF build needed) ---------------------------------------
def synthetic_release():
    import struct
    bootloader = _app_image({16: b'jm-test-bootloader'}, b'\x33' * 5000, load=0x3FFF0000)
    raw = [b'\xaa\x50' + bytes([kind, sub]) + struct.pack('<II', off, size) + name.encode().ljust(16, b'\0')
           + bytes(4) for kind, sub, off, size, name in STOCK_ROWS + (cm.LAUNCHER_ROW,)]
    launcher = _app_image({16: b'9.9.9', 48: cm.LAUNCHER_PROJECT.encode(), 112: b'v5.3'}, b'\x11' * 8000)
    return cm.Release.check(bootloader, cm.table_bytes(raw), launcher)


RELEASE = synthetic_release()
STOCK = synthetic_stock()


def bitstream(seed, length=900_000):
    header = b'\xff' * 8 + b'\xa5\xc3' + bytes([0x06, 0, 0, 0]) + cf.IDCODE.to_bytes(4, 'big')
    body, state = bytearray(), seed
    while len(body) < length - len(header):
        state = (state * 1103515245 + 12345) & 0x7fffffff
        body += state.to_bytes(4, 'little')
    data = header + bytes(body[:length - len(header)])
    return data + b'\xff' * (SIZE - len(data))


FPGA_ORIGINAL = bitstream(11)


# ---- the simulated console chip and JTAG bridge ------------------------------------------------------
class Chip:
    def __init__(self, flash=FPGA_ORIGINAL):
        self.flash = bytes(flash)
        self.serial_on = True        # the console's USB serial interface, made by the FPGA image
        self.gwu2x = 1
        self.idcode = cf.IDCODE
        self.reads = self.resets = 0
        self.flip_read = None        # the n-th read comes back with one byte flipped
        self.serial_stays = False    # the serial interface does not go away during a read (another console's bridge)
        self.blip = False            # the port list misses the serial port once, then sees it again (no real stop)
        self.unreadable = False      # Windows' port list cannot be read during the read (counts as nothing)
        self.on_read = None          # callable() run during a read (for example another console plugged in)


class Jtag:
    """The read commands chromatic_fpga.JtagLoader gives the full backup; a write is a test failure."""

    def __init__(self, chip):
        self.c = chip
        self.looks = []              # what each port listing during a read shows (scripted per test)

    def check_tool(self):
        pass

    def present(self):
        return {'serial': self.c.serial_on, 'gwu2x': self.c.gwu2x}

    def serial_present(self):
        return self.c.serial_on

    def serial_pids(self):
        if self.looks:
            return self.looks.pop(0)
        return [0x0101] if self.c.serial_on else []

    def detect(self):
        return {'idcode': self.c.idcode, 'family': 'GW5A-25'}

    def dump(self, path, during=None):
        if not self.c.serial_stays:
            self.c.serial_on = False  # the loader stops the FPGA to reach its flash
        if self.c.blip:
            self.looks = [[], [0x0101], [], [0x0101]]  # gone in single looks only
        elif self.c.unreadable:
            self.looks = [None, None, None, None]
        if during is not None:
            for _ in range(4):        # the real loader looks about five times a second while it reads
                during()
        self.looks = []
        if self.c.on_read:
            self.c.on_read()
        self.c.reads += 1
        data = bytearray(self.c.flash)
        if self.c.flip_read == self.c.reads:
            data[1234] ^= 1
        Path(path).write_bytes(bytes(data))
        return bytes(data)

    def reset(self):
        self.c.resets += 1
        self.c.serial_on = True

    def write(self, source):
        raise AssertionError('a backup or check wrote the console chip')

    def load_sram(self, source):
        raise AssertionError('a backup or check loaded the console chip RAM')


class FlakyDevice(Device):
    """The second full ESP32 read comes back with one byte changed (a bad cable)."""

    def read_all(self, say=None):
        data = bytearray(super().read_all(say))
        if self.c.full_reads == 2:
            data[0x20000] ^= 0x01
        return bytes(data)


FPGA_TIMING = cf.FpgaTiming()
FPGA_TIMING.poll, FPGA_TIMING.boot = 0, 2


class Rig:
    def __init__(self, image=STOCK, device=Device, chip=None):
        self.console = Console(image, RELEASE)
        self.chip = chip or Chip()
        self.device = device
        self.log = []
        self.work = Path(_TEMP.name) / 'work'

    def manager(self):
        return cm.Manager(folder=self.work, programmer_factory=lambda: self.device(self.console),
                          release_loader=lambda: RELEASE, timing=FAST, say=self.log.append, sleep=lambda s: None,
                          fpga_loader_factory=lambda logs: Jtag(self.chip), fpga_timing=FPGA_TIMING)

    def said(self, needle):
        return any(needle in line for line in self.log)


def empty_folders():
    for folder in (HOME, SECOND, Path(_TEMP.name) / 'work'):
        shutil.rmtree(folder, ignore_errors=True)
    HOME.mkdir(parents=True)
    SECOND.mkdir(parents=True)


def kept_files():
    """Everything a run left in the per-user and second-copy folders (journals aside)."""
    return sorted(p.relative_to(Path(_TEMP.name)).as_posix() for root in (HOME, SECOND) for p in root.rglob('*')
                  if p.is_file())


def nothing_kept(rig, label):
    check(not list(HOME.glob(cm.FULL_BACKUP_PREFIX + '*')) and not any(SECOND.iterdir()),
          f'{label}: no backup folder kept, no second copy')
    check(not (HOME / 'chromatic-tools/latest-backup-path.txt').exists(), f'{label}: nothing registered')
    check(rig.console.writes == 0 and bytes(rig.console.flash) == rig.console_before, f'{label}: console untouched')
    check(not (rig.work / 'full-backup-reads').exists(), f'{label}: no staged console chip reads left')


def fresh(device=Device, chip=None, image=STOCK):
    empty_folders()
    rig = Rig(image, device, chip)
    rig.console_before = bytes(rig.console.flash)
    return rig


def main():
    counts = {}

    # 1. Status from an empty folder: works (model checked on the chip), says there is no backup, keeps nothing.
    rig = fresh()
    check(rig.manager().status() == cm.EXIT_OK, 'status works with no backup')
    check(rig.said('No backup of this console yet') and rig.said('Console: stock'), 'status says: stock, no backup yet')
    check(not kept_files() and rig.console.writes == 0, 'status kept nothing and wrote nothing')
    check(rig.chip.reads == 0, 'status does not read the console chip')

    # 2. Unknown model: refused before any read, nothing kept.
    rig = fresh()
    rig.console.identity_value = dict(IDENTITY, revision=1)
    m = rig.manager()
    check(m.guarded(m.backup) == cm.EXIT_REFUSED, 'unknown ESP32 revision refused')
    check(rig.console.full_reads == 0 and rig.chip.reads == 0, 'unknown model: nothing read')
    nothing_kept(rig, 'unknown model')
    rig = fresh(chip=Chip())
    rig.chip.idcode = 0x1234
    m = rig.manager()
    check(m.guarded(m.backup) == cm.EXIT_REFUSED and rig.chip.reads == 0,
          'unknown console chip refused before it is read')
    nothing_kept(rig, 'unknown console chip')

    # 3. A read mismatch refuses with nothing written or kept: ESP32, then console chip.
    rig = fresh(device=FlakyDevice)
    m = rig.manager()
    check(m.guarded(m.backup) == cm.EXIT_REFUSED, 'ESP32 read mismatch refused')
    check(rig.said('two full reads of the ESP32 flash differ') and rig.chip.reads == 0, 'ESP32 mismatch: said, FPGA not read')
    nothing_kept(rig, 'ESP32 mismatch')
    chip = Chip()
    chip.flip_read = 2
    rig = fresh(chip=chip)
    m = rig.manager()
    check(m.guarded(m.backup) == cm.EXIT_REFUSED, 'console chip read mismatch refused')
    check(rig.said('two full reads of the console chip flash differ') and chip.resets == 1 and chip.serial_on,
          'console chip mismatch: said, chip reconfigured from its flash and back on USB')
    nothing_kept(rig, 'console chip mismatch')
    chip = Chip()
    chip.serial_stays = True
    rig = fresh(chip=chip)
    m = rig.manager()
    check(m.guarded(m.backup) == cm.EXIT_REFUSED and rig.said('cannot be tied to this console'),
          'a JTAG bridge not tied to the console just read is refused')
    nothing_kept(rig, 'same-unit proof missing')
    chip = Chip()
    chip.gwu2x = 2
    rig = fresh(chip=chip)
    m = rig.manager()
    check(m.guarded(m.backup) == cm.EXIT_REFUSED and chip.reads == 0, 'two JTAG bridges refused')
    nothing_kept(rig, 'two bridges')
    counts['refusals'] = 6

    # 4. The first backup from an empty folder: both copies, receipts, registration.
    image = bytearray(STOCK)
    image[cm.PROVISION[0]:cm.PROVISION[0] + 16] = b'SECRET-SETUP-REC'  # a console setup that must not be saved
    rig = fresh(image=bytes(image))
    m = rig.manager()
    check(m.guarded(m.backup) == cm.EXIT_OK, 'first backup succeeds from an empty folder')
    check(rig.console.writes == 0 and bytes(rig.console.flash) == rig.console_before, 'the backup wrote nothing')
    check(rig.console.full_reads == 2 and rig.chip.reads == 2 and rig.chip.resets == 1, 'both chips read twice, FPGA reconfigured')
    folders = list(HOME.glob(cm.FULL_BACKUP_PREFIX + '*'))
    check(len(folders) == 1, 'one full backup folder in the per-user folder')
    folder = folders[0]
    bundle = sorted((folder / 'restore-bundle').iterdir())
    second = SECOND / folder.name
    check(len(bundle) == 3 and sorted(p.name for p in second.iterdir()) == [p.name for p in bundle]
          and all(p.read_bytes() == (second / p.name).read_bytes() for p in bundle),
          'three-file bundle saved and its second copy matches byte for byte')
    receipt = json.loads((folder / 'completion-receipt.json').read_text(encoding='utf-8'))
    check(Path(receipt['secondary_bundle']) == second and set(receipt['sha256']) == {p.name for p in bundle},
          'completion receipt names the second copy and the hash of every file')
    validation = json.loads((folder / 'recovery-validation.json').read_text(encoding='utf-8'))
    preflight.check_checkpoint(validation)
    check(validation['no_flash_program_requested'] is True and validation['serial_returned'] is True,
          'recovery check written and accepted by the preflight')
    latest = HOME / 'chromatic-tools/latest-backup-path.txt'
    check(Path(latest.read_text(encoding='utf-8').strip()) == folder, 'registered last, pointing at the new backup')
    check(not (rig.work / 'full-backup-reads').exists(), 'staged console chip reads removed')

    # Load-from-disk: every reader of the full backup takes it from the files just written.
    stock = tr.load_stock()
    check(stock.image == cm.without_setup(rig.console_before) and b'SECRET-SETUP-REC' not in stock.image,
          'load_stock: the ESP32 image from disk, console setup left out')
    check(stock.identity == {k: IDENTITY[k] for k in ('chip', 'revision', 'mac', 'flash_id')},
          'load_stock: the identity recorded from the chip')
    primary, secondary = tr.backup_folders()
    check(primary == folder / 'restore-bundle' and secondary == second, 'trial runner finds both copies')
    original = cf.OriginalBackup.load()
    check(original.image == FPGA_ORIGINAL and original.taken.count('-') == 2, 'OriginalBackup.load: the FPGA image from disk')
    preflight.check_backup_copies(folder, secondary)
    names = [name for name, _, _ in cm.backup_images()]
    check(names == ['original'], 'the Manager lists the full backup as the original')
    usage = HOME / core.USAGE_SNAPSHOT
    usage.parent.mkdir(parents=True, exist_ok=True)
    usage.write_text(json.dumps({'observed_at': datetime.now(timezone.utc).isoformat(), 'used_percent_by_window': [10],
                                 'reporter': 'example', 'ordinary_usage_allowed': True}), encoding='utf-8')
    check(core.hardware_rule(HOME) is None, 'the AI rule accepts the backup records once a usage reading is there')
    usage.unlink()

    # Status, then Back up again: now an ordinary ESP32 backup, not a second full one.
    rig.log.clear()
    check(rig.manager().status() == cm.EXIT_OK and not rig.said('No backup of this console yet'),
          'status works after the backup and no longer asks for one')
    before = rig.chip.reads
    m = rig.manager()
    check(m.guarded(m.backup) == cm.EXIT_OK and rig.chip.reads == before, 'the next Back up is an ESP32 backup')
    check(len(list(cm.BACKUPS.glob('*/esp32-flash.bin'))) == 1 and len(list(HOME.glob(cm.FULL_BACKUP_PREFIX + '*'))) == 1,
          'one ESP32 backup added, the full backup unchanged')

    # 5. Install after the first backup proceeds.
    check(rig.manager().install() == cm.EXIT_OK and cm.describe(bytes(rig.console.flash), RELEASE).name == 'installed',
          'install after the first backup')

    # A broken second copy: every loader refuses it.
    damaged = next(p for p in second.iterdir() if p.name.endswith('esp32_flash_4MB.bin'))
    data = bytearray(damaged.read_bytes())
    data[100] ^= 1
    damaged.write_bytes(bytes(data))
    try:
        tr.load_stock()
        check(False, 'load_stock accepted a damaged second copy')
    except tr.Refused:
        pass
    try:
        preflight.check_backup_copies(folder, second)
        check(False, 'the preflight accepted a damaged second copy')
    except ValueError:
        pass
    fpga_second = next(p for p in second.iterdir() if p.name.endswith('fpga_flash_4MB.bin'))
    fpga_second.write_bytes(b'\xff' * SIZE)
    try:
        cf.OriginalBackup.load()
        check(False, 'OriginalBackup.load accepted a damaged second copy')
    except cf.Refused:
        pass
    # ... and Back up never replaces that original: it refuses, the record and the original stay as they are.
    pointer, primary_files = latest.read_bytes(), {p.name: p.read_bytes() for p in bundle}
    reads_before, flash_before = (rig.console.full_reads, rig.chip.reads), bytes(rig.console.flash)
    rig.log.clear()
    m = rig.manager()
    check(m.guarded(m.backup) == cm.EXIT_REFUSED and rig.said('could not be checked in both copies'),
          'a damaged second copy: Back up refuses and says why')
    check(latest.read_bytes() == pointer and {p.name: p.read_bytes() for p in bundle} == primary_files
          and len(list(HOME.glob(cm.FULL_BACKUP_PREFIX + '*'))) == 1, 'the record and the original are unchanged')
    check((rig.console.full_reads, rig.chip.reads) == reads_before and bytes(rig.console.flash) == flash_before,
          'nothing was read or written')
    try:
        rig.manager().full_backup()
        check(False, 'a second full backup was started while one is recorded')
    except cm.Refused:
        pass
    counts['refusals'] += 1

    # The audit's case (a): the original exists, its second copy's disk is unplugged, the console chip has
    # changed since. Back up refuses (no new "original", the record untouched); with the disk back, the old
    # original loads again.
    rig = fresh()
    m = rig.manager()
    check(m.guarded(m.backup) == cm.EXIT_OK, '(a) first backup')
    first = latest.read_bytes()
    m = rig.manager()
    check(m.guarded(m.backup) == cm.EXIT_OK, '(a) an ordinary ESP32 backup next')
    away = Path(_TEMP.name) / 'stick-away'
    shutil.move(str(SECOND), str(away))
    SECOND.mkdir()
    rig.chip.flash = bitstream(99)
    reads_before = (rig.console.full_reads, rig.chip.reads)
    m = rig.manager()
    check(m.guarded(m.backup) == cm.EXIT_REFUSED and rig.said('Is the second-copy disk'),
          '(a) second copy unplugged: Back up refuses')
    check(latest.read_bytes() == first and len(list(HOME.glob(cm.FULL_BACKUP_PREFIX + '*'))) == 1
          and (rig.console.full_reads, rig.chip.reads) == reads_before, '(a) record unchanged, nothing read')
    shutil.rmtree(SECOND)
    shutil.move(str(away), str(SECOND))
    check(cf.OriginalBackup.load().image == FPGA_ORIGINAL, '(a) with the disk back, the old original loads')
    # The same with no ESP32 backups at all: Install refuses instead of taking a new "original".
    rig = fresh()
    m = rig.manager()
    check(m.guarded(m.backup) == cm.EXIT_OK, 'first backup (for Install with the second copy unplugged)')
    first = latest.read_bytes()
    shutil.move(str(SECOND), str(away))
    SECOND.mkdir()
    before = bytes(rig.console.flash)
    rig.log.clear()
    check(rig.manager().install() == cm.EXIT_REFUSED and rig.said('Is the second-copy disk'),
          'Install with the second copy unplugged and no ESP32 backup: refused')
    check(latest.read_bytes() == first and bytes(rig.console.flash) == before and rig.console.writes == 0
          and rig.chip.reads == 2, 'record unchanged, nothing read again, nothing written')
    shutil.rmtree(SECOND)
    shutil.move(str(away), str(SECOND))
    counts['refusals'] += 2

    # The audit's case (b): a console that already has the start-up menu is never taken as the original.
    rig = fresh()
    check(rig.manager().install() == cm.EXIT_OK, '(b) install from an empty folder')
    empty_folders()
    rig.console_before, rig.console.writes = bytes(rig.console.flash), 0
    rig.log.clear()
    m = rig.manager()
    check(m.guarded(m.backup) == cm.EXIT_REFUSED and rig.said('does not run the official ModRetro firmware')
          and rig.said('installed'), '(b) an installed console is refused as an original')
    nothing_kept(rig, '(b) installed console')
    # ... nor a console chip that holds one of Jellymatic's own images (here a synthetic image pinned the
    # way the real ones are: the .bin SHA-256 and size in the image's build receipt).
    ours = bitstream(55, length=700_000)
    receipt = Path(_TEMP.name) / 'sound-build-receipt.json'
    receipt.write_text(json.dumps({'outputs': {'build/evt1_x2.bin': {
        'sha256': __import__('hashlib').sha256(ours[:700_000]).hexdigest(), 'size': 700_000}}}), encoding='utf-8')
    saved_receipt, cf.MEDIA_BUILD_RECEIPT = cf.MEDIA_BUILD_RECEIPT, receipt
    try:
        check(cf.jellymatic_image(ours) == 'sound 18.40' and cf.jellymatic_image(FPGA_ORIGINAL) is None,
              'Jellymatic images are recognised by their pins, the original is not')
        chip = Chip(flash=ours)
        rig = fresh(chip=chip)
        m = rig.manager()
        check(m.guarded(m.backup) == cm.EXIT_REFUSED and rig.said("Jellymatic's sound 18.40 image"),
              'a console chip with the sound image is refused as an original')
        nothing_kept(rig, 'Jellymatic FPGA image')
    finally:
        cf.MEDIA_BUILD_RECEIPT = saved_receipt
    counts['refusals'] += 2

    # Same unit: the serial port must be gone in two looks in a row, and the ESP32 on USB afterwards must
    # be the one read before the console chip.
    for flag, label in (('blip', 'the port missing in single looks only'), ('unreadable', 'an unreadable port list')):
        chip = Chip()
        chip.serial_stays = True
        setattr(chip, flag, True)
        rig = fresh(chip=chip)
        m = rig.manager()
        check(m.guarded(m.backup) == cm.EXIT_REFUSED and rig.said('cannot be tied to this console'),
              f'{label} is no proof that the port went away: refused')
        nothing_kept(rig, label)
    chip = Chip()
    rig = fresh(chip=chip)
    chip.on_read = lambda: rig.console.identity_value.update(mac='bb')  # another console plugged in meanwhile
    m = rig.manager()
    check(m.guarded(m.backup) == cm.EXIT_REFUSED and rig.said('is not the one whose ESP32 was read'),
          'another ESP32 on USB after the console chip reads: refused')
    nothing_kept(rig, 'ESP32 changed during the console chip reads')
    counts['refusals'] += 3

    # 6. Install from an empty folder: the full backup first (one step, no question), then the install.
    rig = fresh()
    check(rig.manager().install() == cm.EXIT_OK, 'install from an empty folder')
    check(rig.said('the full backup comes first') and rig.chip.reads == 2, 'install took the full backup first')
    check(cm.describe(bytes(rig.console.flash), RELEASE).name == 'installed' and tr.load_stock().image == STOCK,
          'installed, and the backup holds the console as it was before')
    # ... but a refused backup stops the install with nothing written.
    rig = fresh(device=FlakyDevice)
    check(rig.manager().install() == cm.EXIT_REFUSED, 'install refused when the first backup fails')
    nothing_kept(rig, 'install after a failed first backup')

    # 7. A console chip job from an empty folder (the one-step updates go through run_action): backup first.
    rig = fresh()
    manager = rig.manager()
    jobs = cf.FpgaJobs(folder=rig.work / 'fpga', loader_factory=lambda: Jtag(rig.chip), timing=FPGA_TIMING,
                       say=rig.log.append, sleep=lambda s: None, media_loader=lambda: (_ for _ in ()).throw(
                           cf.Refused('no sound image here')),
                       link_loader=lambda allow_unreviewed=False: (_ for _ in ()).throw(cf.Refused('no link image')))
    code = core.run_action(cm, manager, 'fpga_check', lambda prompt: 'no', lambda prompt: '', contextlib.nullcontext(),
                           fpga=jobs)
    check(code == cm.EXIT_OK and rig.said('no full backup of the console yet') and rig.said('your original image'),
          'Check FPGA from an empty folder: full backup first, then the check finds the original')
    check(rig.chip.reads == 3 and rig.console.writes == 0, 'two backup reads and one check read; nothing written')

    # 8. Everything that reads nothing is checked before the ESP32 reads: a missing start-up menu release,
    # a missing programmer (openFPGALoader) or not exactly one JTAG bridge refuse at once, with no chip read.
    rig = fresh()
    m = cm.Manager(folder=rig.work, programmer_factory=lambda: Device(rig.console),
                   release_loader=lambda: (_ for _ in ()).throw(cm.Refused('No launcher release found.')),
                   timing=FAST, say=rig.log.append, sleep=lambda s: None,
                   fpga_loader_factory=lambda logs: Jtag(rig.chip), fpga_timing=FPGA_TIMING)
    check(m.guarded(m.backup) == cm.EXIT_REFUSED and rig.said('needed to recognise the official firmware'),
          'no start-up menu release: the full backup refuses and says why')
    check(rig.console.full_reads == 0 and rig.console.part_reads == [] and rig.chip.reads == 0,
          'no release: refused before any ESP32 or console chip read')
    nothing_kept(rig, 'no release')

    class NoTool(Jtag):
        def check_tool(self):
            raise cf.Refused('the programmer (openFPGALoader) is missing: see docs/Install-with-Chromatic-Manager.md.')
    rig = fresh()
    m = rig.manager()
    m.fpga_loader_factory = lambda logs: NoTool(rig.chip)
    check(m.guarded(m.backup) == cm.EXIT_REFUSED and rig.said('openFPGALoader') and rig.console.full_reads == 0
          and rig.chip.reads == 0, 'no programmer: refused before the ESP32 reads')
    nothing_kept(rig, 'no programmer')
    for bridges in (0, 2):
        chip = Chip()
        chip.gwu2x = bridges
        rig = fresh(chip=chip)
        m = rig.manager()
        check(m.guarded(m.backup) == cm.EXIT_REFUSED and rig.said('exactly one Chromatic JTAG bridge')
              and rig.console.full_reads == 0 and chip.reads == 0, f'{bridges} JTAG bridges: refused before the ESP32 reads')
        nothing_kept(rig, f'{bridges} bridges')
    counts['refusals'] += 4

    # 9. The same full backup by hand (scripts/chromatic_backup.py, the Install guide's advanced way). Its two
    # device stages are simulated (they write what the real reads write); everything else runs for real.
    import chromatic_backup as cb
    import verify_chromatic_backup as vb
    latest = HOME / 'chromatic-tools/latest-backup-path.txt'
    device_calls = []
    seen = {'esp32': STOCK, 'fpga': FPGA_ORIGINAL}

    def fake_mcu(folder):
        device_calls.append('mcu')
        for number in (1, 2):
            (folder / f'mcu-read-{number}.bin').write_bytes(seen['esp32'])
        (folder / 'mcu-identity.json').write_text(json.dumps({
            'chip': IDENTITY['chip'], 'revision': IDENTITY['revision'] * 100, 'mac': IDENTITY['mac'],
            'flash_id': IDENTITY['flash_id'], 'size': SIZE}), encoding='utf-8')
        (folder / 'mcu-verified.json').write_text(json.dumps({'size': SIZE, 'sha256': cm.sha(seen['esp32']),
                                                              'reads': 2}), encoding='utf-8')

    def fake_fpga(folder):
        device_calls.append('fpga')
        check(not latest.exists(), 'by hand: nothing recorded while the console chip is read')
        for number in (1, 2):
            (folder / f'fpga-read-{number}.bin').write_bytes(seen['fpga'])
        (folder / 'same-unit-usb-proof.json').write_text(json.dumps({'serial_disappeared_during_fpga_read': True}),
                                                         encoding='utf-8')
        (folder / 'fpga-verified.json').write_text(json.dumps({'size': SIZE, 'sha256': cm.sha(seen['fpga']),
                                                               'reads': 2}), encoding='utf-8')
        return True, True

    cb.mcu_backup, cb.fpga_backup, cb.load_release = fake_mcu, fake_fpga, lambda: RELEASE
    cb.CONSOLE = __import__('io').StringIO()

    def by_hand(*argv):
        cb.CONSOLE.seek(0)
        cb.CONSOLE.truncate()
        code = cb.main(list(argv))
        return code, cb.CONSOLE.getvalue()

    def made_folder(said):
        return HOME / said.split('--folder ')[1].split()[0]

    empty_folders()
    code, said = by_hand('mcu')
    check(code == 0 and device_calls == ['mcu'], 'by hand: the ESP32 stage runs')
    check(not latest.exists(), 'by hand: the ESP32 stage records nothing (L-a: the record used to be written here)')
    folder = made_folder(said)
    check(folder.is_dir() and folder.name.startswith(cm.FULL_BACKUP_PREFIX), 'by hand: the ESP32 stage names its folder')
    # The record is written only after the bundle exists in both copies and checks out.
    copy_bundle, order = vb.copy_bundle, []

    def watched_copy(bundle, target):
        order.append(('copy', latest.exists()))
        return copy_bundle(bundle, target)
    vb.copy_bundle = watched_copy
    try:
        code, said = by_hand('fpga', '--folder', folder.name)
    finally:
        vb.copy_bundle = copy_bundle
    check(code == 0 and device_calls == ['mcu', 'fpga'] and 'Full backup recorded' in said, 'by hand: the console chip stage runs')
    check(order == [('copy', False)], 'by hand: nothing was recorded before the second copy was made')
    check(Path(latest.read_text(encoding='utf-8').strip()).resolve() == folder.resolve(), 'by hand: recorded last, pointing at the backup')
    second = SECOND / folder.name
    receipt = json.loads((folder / 'completion-receipt.json').read_text(encoding='utf-8'))
    check(Path(receipt['secondary_bundle']).resolve() == second.resolve() and len(list(second.iterdir())) == 3, 'by hand: second copy and receipt')
    preflight.check_checkpoint(json.loads((folder / 'recovery-validation.json').read_text(encoding='utf-8')))
    check(tr.load_stock().image == STOCK and cf.OriginalBackup.load().image == FPGA_ORIGINAL,
          'by hand: both loaders read the recorded backup from disk')
    # With a full backup recorded, both stages refuse at once: the console is not touched, nothing changes.
    pointer, calls = latest.read_bytes(), len(device_calls)
    for stage in (('mcu',), ('fpga', '--folder', folder.name)):
        code, said = by_hand(*stage)
        check(code == 1 and cm.ALREADY_RECORDED[:40] in said.replace('A full', 'a full'),
              f'by hand: {stage[0]} stage refused while a full backup is recorded (same wording as the Manager)')
    check(len(device_calls) == calls and latest.read_bytes() == pointer
          and len(list(HOME.glob(cm.FULL_BACKUP_PREFIX + '*'))) == 1, 'by hand: nothing read, the record unchanged')
    # ... also when that record cannot be checked (second copy unplugged): never replaced.
    shutil.rmtree(second)
    code, said = by_hand('mcu')
    check(code == 1 and len(device_calls) == calls and latest.read_bytes() == pointer,
          'by hand: an unchecked recorded backup is never replaced either')

    # A second copy that does not check out: nothing is recorded and what the step made is removed.
    empty_folders()
    device_calls.clear()
    code, said = by_hand('mcu')
    folder = made_folder(said)

    def damaged_copy(bundle, target):
        copy_bundle(bundle, target)
        victim = next(p for p in Path(target).iterdir() if p.name.endswith('fpga_flash_4MB.bin'))
        data = bytearray(victim.read_bytes())
        data[10] ^= 1
        victim.write_bytes(bytes(data))
        return target
    vb.copy_bundle = damaged_copy
    try:
        code, said = by_hand('fpga', '--folder', str(folder))
    finally:
        vb.copy_bundle = copy_bundle
    check(code == 1 and 'not recorded' in said and not latest.exists(), 'by hand: a bad second copy is never recorded')
    check(not (folder / 'restore-bundle').exists() and not (SECOND / folder.name).exists()
          and not (folder / 'completion-receipt.json').exists(), 'by hand: the bundle and second copy were removed')
    code, said = by_hand('fpga', '--folder', folder.name)
    check(code == 1 and 'never overwritten' in said and device_calls == ['mcu', 'fpga'],
          'by hand: a folder with console chip reads is not read over again')

    # Checks that read nothing come first: no release refuses before the ESP32 is read; a console that is not
    # the official firmware as it comes, or a console chip with a Jellymatic image, is not kept or recorded.
    empty_folders()
    device_calls.clear()
    cb.load_release = lambda: (_ for _ in ()).throw(cm.Refused('No launcher release found.'))
    code, said = by_hand('mcu')
    check(code == 1 and 'needed to recognise' in said and device_calls == [] and not kept_files(),
          'by hand: no start-up menu release refuses before the ESP32 is read')
    cb.load_release = lambda: RELEASE
    installed = cm.put(cm.put(cm.put(STOCK, cm.BOOT, RELEASE.bootloader), cm.TABLE, RELEASE.table), cm.SLOT_APP,
                       RELEASE.launcher)
    seen['esp32'] = installed
    code, said = by_hand('mcu')
    check(code == 1 and 'does not run the official ModRetro firmware' in said and not kept_files(),
          'by hand: a console with the start-up menu is refused and its reads are not kept')
    seen['esp32'] = STOCK
    ours = bitstream(55, length=700_000)
    saved_pin, cf.LINK_BIN_SHA256 = cf.LINK_BIN_SHA256, cf.sha(ours.rstrip(b'\xff'))
    try:
        seen['fpga'] = ours
        code, said = by_hand('mcu')
        folder = made_folder(said)
        code, said = by_hand('fpga', '--folder', folder.name)
        check(code == 1 and 'Jellymatic' in said and not latest.exists() and not folder.exists(),
              'by hand: a console chip with a Jellymatic image is refused, nothing kept or recorded')
    finally:
        cf.LINK_BIN_SHA256 = saved_pin
        seen['fpga'] = FPGA_ORIGINAL
    counts['by_hand'] = 9
    counts['scenarios'] = 9

    print(f'PASS: first full backup from an empty per-user folder: status with no backup, {counts["refusals"]} '
          'refusals with nothing kept or written (unknown ESP32 model, unknown console chip, ESP32 and console chip '
          'read mismatches, a bridge not tied to the console, two bridges, a port missing only in single looks or '
          'an unreadable port list, another ESP32 after the console chip reads, a console with the start-up menu, '
          'a console chip with a Jellymatic image); an existing original is never replaced (damaged or unplugged '
          'second copy: Back up and Install refuse, the record and original unchanged, the old original loads once '
          'the disk is back); the backup reads both chips twice, saves the bundle and its second copy, writes the '
          'completion receipt and recovery check, registers last; load_stock, OriginalBackup.load, the preflight and '
          'the AI rule read it from disk; Install and Check FPGA from an empty folder take the backup first, a '
          'failed backup stops them; no release, no programmer or not one JTAG bridge refuse before any chip read; '
          f'the by-hand chromatic_backup.py ({counts["by_hand"]} groups of checks) records only after the bundle and '
          'its second copy check out, refuses at once while a full backup is recorded, and keeps no non-original. '
          f'Synthetic stock image, simulated chips, {ESPTOOL}, no hardware.')


if __name__ == '__main__':
    try:
        main()
    finally:
        _TEMP.cleanup()

# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Chromatic Manager FPGA jobs (chromatic_fpga.py) against a simulated console chip and JTAG
bridge: wrong chip id, two or no bridges, backup that fails its checks, the sound image before
and after the rehearsal, readback mismatch, power cut during the write and during the readback,
the tool dying mid-job, boot not seen, recovery attempts exhausted, owner Stop, AI-started
(approved image, unapproved image, usage gate) against owner-started, and the Manager core's
dispatch and pending gating. Synthetic images only: no device, no private file, no real backup.
Prints only pass/fail; every message the jobs produce is checked for leaked hashes and paths.
"""
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import chromatic_fpga as cf
from chromatic_trial_runner import EXIT_MANUAL, EXIT_OK, EXIT_RECOVERED, EXIT_REFUSED, Refused, StepFailed

SIZE = cf.SIZE


def check(condition, message):
    if not condition:
        raise SystemExit(f'FAIL: {message}')


# ---- synthetic images -------------------------------------------------------------------------
def synthetic_bitstream(seed, length=900_000):
    """A fake GW5A-25 bitstream: Gowin sync, type 6, idcode, then deterministic bytes."""
    header = b'\xff' * 8 + b'\xa5\xc3' + bytes([0x06, 0, 0, 0]) + (0x0001281b).to_bytes(4, 'big')
    body = bytearray()
    state = seed
    while len(body) < length - len(header):
        state = (state * 1103515245 + 12345) & 0x7fffffff
        body += (state & 0xffffffff).to_bytes(4, 'little')
    return header + bytes(body[:length - len(header)])


def write_fs(path, bitstream):
    """The .fs text form the loader (and fs_to_bin) packs back into `bitstream`."""
    lines = ['//Synthetic test bitstream', '//Device: GW5A-25']
    for i in range(0, len(bitstream), 128):
        chunk = bitstream[i:i + 128]
        lines.append(''.join(f'{b:08b}' for b in chunk))
    Path(path).write_text('\n'.join(lines) + '\n', encoding='ascii')


ORIGINAL_BITSTREAM = synthetic_bitstream(1)
ORIGINAL = ORIGINAL_BITSTREAM + b'\xff' * (SIZE - len(ORIGINAL_BITSTREAM))
MEDIA_BITSTREAM = synthetic_bitstream(2, length=900_100)
LINK_BITSTREAM = synthetic_bitstream(3, length=900_300)  # the sound + Pocket link image (18.41), synthetic
LEAK = re.compile(r'[0-9a-fA-F]{16,}|[A-Za-z]:\\|/private/|\\private\\')


# ---- the simulated chip and bridge -------------------------------------------------------------
class PowerCut(cf.LoaderFailed):
    pass


class FakeChip:
    def __init__(self, flash=ORIGINAL):
        self.flash = bytearray(flash)
        self.idcode, self.family, self.gwu2x = cf.IDCODE, 'GW5A-25', 1
        self.configured = True           # running from flash: the serial interface exists if the image is sane
        self.cut_write_after = None      # bytes programmed before the power fails (once)
        self.cut_at_read = None          # the n-th read (1-based, counting from now) fails: power cut while reading
        self.corrupt_after_write = None  # byte index flipped after a write (readback mismatch), once
        self.boot_fails = False          # the serial interface never returns
        self.writes, self.reads, self.resets, self.detects = 0, 0, 0, 0
        self.sources = []
        self.sram = None                 # a bitstream loaded into the configuration RAM (Try FPGA image), else the flash runs
        self.sram_sources, self.sram_loads = [], 0
        self.sram_fails = False          # the JTAG SRAM load fails (chip left unconfigured)
        self.sram_falls_back = False     # the SRAM load "succeeds" but the chip comes back on its flash image
        self.reported = ...              # what the start-up menu reports instead of the running image (... = the truth)
        self.version_reads = 0
        self.off_for = 0                 # polls during which the console reads as switched off (its serial port gone)
        self.phase = 'menu'              # the phase the start-up menu's own status line names (None: no status line)
        self.status_reads = 0
        self.sram_dead = False           # an image loaded into the RAM never brings the USB serial port back
        self.player = 0                  # the USB product ID's low byte: 0 after every configuration, 1 once stock firmware runs
        self.usb_off = False             # switched off (or unplugged): no USB serial port at all, for as long as this is set
        self.usb_unreadable = False      # Windows' list of COM ports cannot be read
        self.usb_looks = 0               # how often that list was looked at (usb_pids)

    def power_cycle(self):
        """The owner switches the console off and on: the flash image runs again, and the next poll of
        the USB port finds it gone before it comes back (what FpgaJobs._watch_power_cycle looks for)."""
        self.configured, self.sram, self.off_for, self.player = True, None, 1, 0

    def running(self):
        return self.sram if self.sram is not None else bytes(self.flash)

    def version(self):
        """What the start-up menu's version line says for the image that runs: the synthetic
        images stand for the real ones (original 18.14, sound 18.40, sound + Pocket link 18.41)."""
        if self.reported is not ...:
            return self.reported
        running = self.running()
        if running.startswith(LINK_BITSTREAM):
            return (18, 41, 1)
        if running.startswith(MEDIA_BITSTREAM):
            return (18, 40, 1)
        if running.startswith(ORIGINAL_BITSTREAM):
            return (18, 14, 0)
        return None

    def serial_present(self):
        if self.usb_off:
            return False
        if self.off_for:
            self.off_for -= 1
            return False
        if self.sram is not None and self.sram_dead:
            return False
        return self.configured and not self.boot_fails and cf.bitstream_ok(self.running())

    def usb_pids(self):
        """Windows' list of COM ports as JtagLoader.serial_pids gives it: the product IDs of the
        console's serial interface (0x0100 after every configuration of the chip, 0x0101 once stock
        firmware runs). Looking changes nothing (serial_present counts off_for down; this does not)."""
        self.usb_looks += 1
        if self.usb_unreadable:
            return None
        if self.usb_off or self.off_for or (self.sram is not None and self.sram_dead):
            return []
        if not (self.configured and not self.boot_fails and cf.bitstream_ok(self.running())):
            return []
        return [(cf.SERIAL_PID_HIGH << 8) | self.player]


class FakeLoader:
    def __init__(self, chip):
        self.c = chip

    def check_tool(self):
        pass

    def present(self):
        return {'serial': self.c.serial_present(), 'gwu2x': self.c.gwu2x}

    def serial_pids(self):
        """Enumeration only, as the real loader's: what the one Approve's watch looks at."""
        return self.c.usb_pids()

    def detect(self):
        self.c.detects += 1
        return {'idcode': self.c.idcode, 'family': self.c.family}

    def dump(self, path):
        self.c.configured = False  # the loader erases SRAM to reach the flash
        self.c.reads += 1
        if self.c.cut_at_read is not None and self.c.reads == self.c.cut_at_read:
            self.c.cut_at_read = None
            raise PowerCut('power cut while reading')
        Path(path).write_bytes(bytes(self.c.flash))
        return bytes(self.c.flash)

    def write(self, source):
        self.c.configured = False
        self.c.writes += 1
        self.c.sources.append(str(source))
        source = Path(source)
        data = cf.fs_to_bin(source) if source.suffix == '.fs' else source.read_bytes()
        end = (len(data) + cf.SECTOR - 1) // cf.SECTOR * cf.SECTOR
        self.c.flash[:end] = b'\xff' * end  # erase
        cut = self.c.cut_write_after
        if cut is not None:
            self.c.cut_write_after = None
            self.c.flash[:cut] = data[:cut]
            raise PowerCut('power cut while writing')
        self.c.flash[:len(data)] = data
        if self.c.corrupt_after_write is not None:
            self.c.flash[self.c.corrupt_after_write] ^= 0x01
            self.c.corrupt_after_write = None

    def reset(self):
        self.c.resets += 1
        self.c.configured = True
        self.c.sram = None  # a reconfiguration loads the flash again
        self.c.player = 0   # and stock firmware has not started yet: product ID 0x0100

    def load_sram(self, source):
        """The SRAM load: the chip runs `source` until reset or power-off; the flash is never read or
        written. The real JtagLoader checks its own argument list with sram_only() before this."""
        cf.sram_only([cf.SRAM_SWITCH, str(source)])
        self.c.sram_loads += 1
        self.c.sram_sources.append(str(source))
        self.c.configured = False
        if self.c.sram_fails:
            self.c.sram_fails = False
            raise cf.LoaderFailed('The JTAG sram failed (private log kept).')
        self.c.sram = None if self.c.sram_falls_back else cf.fs_to_bin(Path(source))
        self.c.configured = True
        self.c.player = 0

    def launcher_fpga_version(self):
        """The start-up menu's FPGA line, read passively (launcher 1.3.5 says it every 5 s): the FPGA is
        not reconfigured and nothing is restarted."""
        self.c.version_reads += 1
        self.last_version_read = 'passive'
        return self.c.version() if self.c.serial_present() else None

    def launcher_status(self):
        """The start-up menu's own status line (every second), listened to passively."""
        self.c.status_reads += 1
        if not self.c.serial_present() or self.c.phase is None:
            return None
        return {'phase': self.c.phase, 'uart': 412, 'msgs': 9}


class Rig:
    """One temp folder: the synthetic backup bundle (as OriginalBackup gives it), the synthetic
    sound .fs, a chip, and FpgaJobs wired to them."""

    def __init__(self, flash=ORIGINAL, session='session-a', media_bitstream=MEDIA_BITSTREAM):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.backup_path = root / 'bundle' / 'chromatic-test-fpga_flash_4MB.bin'
        self.backup_path.parent.mkdir()
        self.backup_path.write_bytes(ORIGINAL)
        self.media_fs = root / 'build' / 'evt1_x2.fs'
        self.media_fs.parent.mkdir()
        write_fs(self.media_fs, media_bitstream)
        self.receipt = root / 'stage-receipt.json'
        self.receipt.write_text(json.dumps({'version_register': {'major': 18, 'minor': 40, 'debug': 1}}), encoding='utf-8')
        self.build_receipt = root / 'build' / 'build-receipt.json'
        self.write_build_receipt()
        self.chip = FakeChip(flash)
        self.lines = []
        self.work = root / 'work'
        self.session = session
        self.backup_fails = None
        self.media_fails = None
        self.make_link_tree(root / 'link', LINK_BITSTREAM)
        self.stop = lambda: None
        self.timing = cf.FpgaTiming()
        self.timing.poll, self.timing.boot = 0, 3

    def write_build_receipt(self, sha_override=None, setup=0, hold=0):
        self.build_receipt.write_text(json.dumps({
            'outputs': {'build/evt1_x2.fs': {'sha256': sha_override or hashlib.sha256(self.media_fs.read_bytes()).hexdigest()}},
            'timing': {'setup_violated_endpoints': setup, 'hold_violated_endpoints': hold}}), encoding='utf-8')

    def backup(self):
        if self.backup_fails:
            raise Refused(self.backup_fails)
        return cf.OriginalBackup(self.backup_path, ORIGINAL, '2026-10-28')

    def media(self):
        if self.media_fails:
            raise Refused(self.media_fails)
        return cf.MediaImage(self.media_fs, self.receipt, self.build_receipt)

    def jobs(self, session=None, loader=None):
        clock = [0.0]

        def tick():
            clock[0] += 1
            return clock[0]
        return cf.FpgaJobs(folder=self.work, loader_factory=lambda: (loader or FakeLoader)(self.chip),
                           backup_loader=self.backup,
                           media_loader=self.media, timing=self.timing, say=self.lines.append, sleep=lambda s: None,
                           clock=tick, session=session or self.session, stop_point=lambda: self.stop(),
                           link_loader=self.link)

    # The sound + Pocket link image (18.41) as a fixture copy of the staged tree's shape: the .fs and
    # Gowin's .bin, both receipts (18.41 debug, 19 hooks, 0/0 timing, staged sources pinned), and
    # NEVER-INSTALL.txt only when a test puts it there (the real tree keeps it until the trial).
    def make_link_tree(self, tree, bitstream):
        self.link_tree = Path(tree)
        build = self.link_tree / 'build'
        build.mkdir(parents=True)
        self.link_fs = build / 'evt1_x2.fs'
        write_fs(self.link_fs, bitstream)
        (build / 'evt1_x2.bin').write_bytes(bitstream)
        source = self.link_tree / 'src/rtl/BSP/pocket_link/chromatic_pocket_link.v'
        source.parent.mkdir(parents=True)
        source.write_text('// synthetic staged source\n', encoding='ascii')
        self.link_receipt = self.link_tree / 'stage-receipt.json'
        self.link_receipt.write_text(json.dumps({'version_register': {'major': 18, 'minor': 41, 'debug': 1},
                                                 'link_hooks': [f'hook {n}' for n in range(1, 20)]}), encoding='utf-8')
        self.link_build_receipt = build / 'build-receipt.json'
        digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        self.link_build_receipt.write_text(json.dumps({
            'outputs': {'build/evt1_x2.fs': {'sha256': digest(self.link_fs)},
                        'build/evt1_x2.bin': {'sha256': digest(build / 'evt1_x2.bin')}},
            'timing': {'setup_violated_endpoints': 0, 'hold_violated_endpoints': 0},
            'stage_receipt_sha256': digest(self.link_receipt),
            'sources_sha256': {'src/rtl/BSP/pocket_link/chromatic_pocket_link.v': digest(source)}}), encoding='utf-8')
        # The fixture's own pins (LinkImage pins the real image by hash: LINK_PINS); the tests hand these in.
        self.link_pins = {'fs': digest(self.link_fs), 'bin': digest(build / 'evt1_x2.bin'), 'stage': digest(self.link_receipt)}

    def link_marker(self, present, where='build'):
        mark = (self.link_tree / 'build' if where == 'build' else self.link_tree) / cf.LINK_MARK
        if present:
            mark.write_text('Unreviewed: never install.\n', encoding='ascii')
        elif mark.exists():
            mark.unlink()

    def link(self, allow_unreviewed=False, pins=...):
        return cf.LinkImage(self.link_fs, self.link_receipt, self.link_build_receipt, allow_unreviewed=allow_unreviewed,
                            pins=self.link_pins if pins is ... else pins)

    def expected_link(self):
        return cf.full_flash_image(LINK_BITSTREAM)

    def expected_media(self):
        return cf.full_flash_image(cf.fs_to_bin(self.media_fs))

    def flash(self):
        return bytes(self.chip.flash)

    def said(self, needle):
        return any(needle in line for line in self.lines)

    def no_leaks(self):
        for line in self.lines:
            if LEAK.search(line) and 'image ' not in line:  # 'image 1a2b3c4d' is the 8-char fingerprint by design
                raise SystemExit(f'FAIL: a message leaked a hash or path: {line!r}')
            if re.search(r'image [0-9a-f]{9,}', line):
                raise SystemExit(f'FAIL: a message shows more than the 8-character fingerprint: {line!r}')


# ---- checks --------------------------------------------------------------------------------------
def image_checks():
    bits = synthetic_bitstream(7, 5000)
    with tempfile.TemporaryDirectory() as temp:
        fs = Path(temp) / 'x.fs'
        write_fs(fs, bits)
        check(cf.fs_to_bin(fs) == bits, '.fs packs back to the same bytes')
        Path(temp, 'bad.fs').write_text('//c\n0101\n', encoding='ascii')
        try:
            cf.fs_to_bin(Path(temp) / 'bad.fs')
            check(False, 'a non-byte line is refused')
        except Refused:
            pass
    check(cf.bitstream_ok(ORIGINAL) and cf.bitstream_ok(MEDIA_BITSTREAM), 'synthetic headers pass the chip check')
    other = bytearray(ORIGINAL)
    other[16] ^= 0x10  # a different idcode
    check(not cf.bitstream_ok(bytes(other)), 'a bitstream for another chip is rejected')
    after = cf.flash_after_write(MEDIA_BITSTREAM, ORIGINAL)
    end = (len(MEDIA_BITSTREAM) + 4095) // 4096 * 4096
    check(len(after) == SIZE and after[:len(MEDIA_BITSTREAM)] == MEDIA_BITSTREAM
          and after[len(MEDIA_BITSTREAM):end] == b'\xff' * (end - len(MEDIA_BITSTREAM)) and after[end:] == ORIGINAL[end:],
          'a bare .fs write: bitstream, 0xFF to the sector end, the old tail (why the sound write uses a 4 MiB image)')
    # The sound write's image: the whole 4 MiB, bitstream then 0xFF, for a shorter, a longer and an equal bitstream.
    for bits in (synthetic_bitstream(3, length=len(ORIGINAL_BITSTREAM) - 6595), synthetic_bitstream(4, length=len(ORIGINAL_BITSTREAM) + 4096),
                 synthetic_bitstream(5, length=len(ORIGINAL_BITSTREAM))):
        full = cf.full_flash_image(bits)
        check(len(full) == SIZE and full[:len(bits)] == bits and full[len(bits):] == b'\xff' * (SIZE - len(bits)),
              f'full flash image for a {len(bits)}-byte bitstream: exactly 4 MiB, bitstream then all 0xFF')
    try:
        cf.full_flash_image(b'\x00' * (SIZE + 1))
        check(False, 'a bitstream larger than the flash is refused')
    except Refused:
        pass
    check(cf.media_fingerprint(Path('nowhere') / 'x.fs') is None and cf.media_text(Path('nowhere') / 'x.fs') is None,
          'no built image: no fingerprint, no text')
    # B1: the console's serial interface is matched on the PID's high byte only (player number varies).
    check(cf.serial_port_matches(0x374e, 0x0100) and cf.serial_port_matches(0x374e, 0x0101) and cf.serial_port_matches(0x374e, 0x0104)
          and not cf.serial_port_matches(0x374e, 0x0201) and not cf.serial_port_matches(0x33aa, 0x0120)
          and not cf.serial_port_matches(0x374e, None), 'serial port: 0x0100 (reset, start-up menu) and any player number match')
    # (2) the built image is pinned to its build receipt; (1) the original's tail must be erased flash.
    rig = Rig()
    rig.write_build_receipt(sha_override='0' * 64)
    try:
        rig.media()
        check(False, 'a sound image that does not match its build receipt is refused')
    except Refused as error:
        check('build receipt describes' in str(error), 'receipt mismatch wording')
    rig.write_build_receipt(hold=1)
    try:
        rig.media()
        check(False, 'a receipt with hold violations is refused')
    except Refused:
        pass
    rig.build_receipt.unlink()
    try:
        rig.media()
        check(False, 'no receipt: refused')
    except Refused as error:
        check('build receipt' in str(error), 'missing receipt wording')
    rig.write_build_receipt()
    # (3) the receipt may pin the staged source too: a stage receipt or source file changed after the build refuses.
    pinned = json.loads(rig.build_receipt.read_text())
    pinned['stage_receipt_sha256'] = hashlib.sha256(rig.receipt.read_bytes()).hexdigest()
    src = rig.receipt.parent / 'src' / 'cart.v'
    src.parent.mkdir()
    src.write_text('module cart; endmodule\n')
    pinned['sources_sha256'] = {'src/cart.v': hashlib.sha256(src.read_bytes()).hexdigest()}
    rig.build_receipt.write_text(json.dumps(pinned))
    check(rig.media().fingerprint == hashlib.sha256(rig.media_fs.read_bytes()).hexdigest(), 'receipt with matching source pins accepted')
    src.write_text('module cart; /* edited after the build */ endmodule\n')
    try:
        rig.media()
        check(False, 'a staged source edited after the build is refused')
    except Refused as error:
        check('changed after the sound image was built' in str(error), 'source change wording')
    src.unlink()
    try:
        rig.media()
        check(False, 'a missing staged source is refused')
    except Refused:
        pass
    pinned['sources_sha256'] = {}
    rig.receipt.write_text(json.dumps({'version_register': {'major': 18, 'minor': 41, 'debug': 1}}), encoding='utf-8')
    rig.build_receipt.write_text(json.dumps(pinned))
    try:
        rig.media()
        check(False, 'a tree re-staged after the build (stage receipt differs) is refused')
    except Refused as error:
        check('re-staged after' in str(error), 're-stage wording')
    rig.receipt.write_text(json.dumps({'version_register': {'major': 18, 'minor': 40, 'debug': 1}}), encoding='utf-8')
    rig.write_build_receipt()
    media = rig.media()
    dirty = bytearray(ORIGINAL)
    dirty[SIZE - 100] = 0x00  # data in the tail: covered, because the whole 4 MiB is written
    check(media.expected(bytes(dirty)) == media.expected(ORIGINAL) == cf.full_flash_image(cf.fs_to_bin(rig.media_fs)),
          'expected flash is the whole written image, whatever the original held')
    try:
        media.expected(ORIGINAL[:-1])
        check(False, 'an original that is not 4 MiB is refused')
    except Refused:
        pass
    # The .fs must pack to the build's own .bin when the receipt names one (the loader packs the same way).
    pinned = json.loads(rig.build_receipt.read_text())
    gowin_bin = rig.media_fs.with_suffix('.bin')
    gowin_bin.write_bytes(cf.fs_to_bin(rig.media_fs))
    pinned['outputs']['build/evt1_x2.bin'] = {'sha256': hashlib.sha256(gowin_bin.read_bytes()).hexdigest()}
    rig.build_receipt.write_text(json.dumps(pinned))
    check(rig.media().flash_image[:len(MEDIA_BITSTREAM)] == MEDIA_BITSTREAM, 'matching Gowin .bin accepted')
    gowin_bin.write_bytes(cf.fs_to_bin(rig.media_fs)[:-1] + b'\x00')
    try:
        rig.media()
        check(False, 'a .bin that differs from the .fs packing is refused')
    except Refused as error:
        check('does not pack' in str(error), 'packing wording')
    gowin_bin.unlink()
    try:
        rig.media()
        check(False, 'a receipt naming a .bin that is missing is refused')
    except Refused:
        pass
    rig.write_build_receipt()


def identity_checks():
    rig = Rig()
    rig.chip.idcode = 0x1081b
    check(rig.jobs().check() == EXIT_REFUSED and rig.said('not the expected console chip') and rig.chip.reads == 0,
          'wrong JTAG id: refused before any read')
    rig = Rig()
    rig.chip.gwu2x = 2
    check(rig.jobs().restore_original() == EXIT_REFUSED and rig.said('Expected exactly one Chromatic') and rig.chip.detects == 0,
          'two bridges: refused before detect')
    rig = Rig()
    rig.chip.gwu2x = 0
    check(rig.jobs().write_media() == EXIT_REFUSED and rig.chip.writes == 0, 'no bridge: refused')
    rig = Rig()
    rig.backup_fails = 'the second copy of the original FPGA image (other disk) differs. Resolve backup integrity first.'
    check(rig.jobs().restore_original() == EXIT_REFUSED and rig.said('second copy') and rig.chip.detects == 0
          and rig.chip.writes == 0, 'backup that fails its checks: refused before touching the chip')
    rig.no_leaks()


def check_job_checks():
    rig = Rig()
    code = rig.jobs().check()
    check(code == EXIT_OK and rig.said('holds your original image') and rig.chip.writes == 0 and rig.chip.resets == 1
          and rig.chip.configured, 'Check FPGA: original recognised, nothing written, chip reconfigured')
    rig = Rig(flash=cf.full_flash_image(MEDIA_BITSTREAM))
    check(rig.jobs().check() == EXIT_OK and rig.said('holds the sound (stock+media) image'), 'Check FPGA: sound image recognised')
    rig = Rig(flash=synthetic_bitstream(9) + b'\xff' * (SIZE - 900_000))
    check(rig.jobs().check() == EXIT_OK and rig.said('neither your original image nor the sound image'),
          'Check FPGA: unknown image reported, nothing written')
    rig = Rig()
    rig.chip.boot_fails = True
    check(rig.jobs().check() == cf.EXIT_NOT_SEEN and rig.said('not seen coming back'), 'Check FPGA: honest when the console does not return')
    rig = Rig()
    rig.chip.cut_at_read = 1
    check(rig.jobs().check() == EXIT_REFUSED and rig.chip.resets >= 1 and rig.chip.configured and rig.chip.writes == 0,
          'Check FPGA: a failed read stops and the chip is reconfigured (nothing written)')
    rig.no_leaks()


def rehearsal_and_media_checks():
    rig = Rig()
    jobs = rig.jobs()
    # The sound image before the rehearsal: refused, no write, chip untouched.
    check(jobs.write_media() == EXIT_REFUSED and rig.said('has not succeeded in this session yet') and rig.chip.writes == 0
          and rig.chip.reads == 0, 'FPGA sound before the rehearsal: refused before the chip is read')
    # The rehearsal: the original written over itself, verified, reconfigured.
    check(jobs.restore_original() == EXIT_OK and rig.flash() == ORIGINAL and rig.chip.writes == 1
          and rig.chip.sources[-1].endswith('fpga_flash_4MB.bin') and rig.chip.reads == 2 and rig.chip.resets == 1
          and rig.said('this write is the rehearsal') and rig.said('Rehearsal recorded'),
          'rehearsal: the full 4 MiB backup file written, read back, reconfigured')
    check(jobs.rehearsal() is not None and not jobs.pending(), 'rehearsal recorded for this session; no pending job')
    # Another process (session) may not use it.
    check(rig.jobs(session='session-b').write_media() == EXIT_REFUSED and rig.chip.writes == 1,
          'the rehearsal does not carry over to another session')
    # Now the sound image: the .fs is what the loader gets; the flash reads as the exact expected image.
    expected = rig.expected_media()
    check(jobs.write_media() == EXIT_OK and rig.flash() == expected and rig.chip.sources[-1].endswith('flash_4MB.bin')
          and rig.said('installed: all 4 MiB read back'), 'FPGA sound after the rehearsal: written and verified')
    written = Path(rig.chip.sources[-1])
    bits = cf.fs_to_bin(rig.media_fs)
    check(written.is_file() and written.stat().st_size == SIZE and written.read_bytes() == expected
          and expected[len(bits):] == b'\xff' * (SIZE - len(bits)) and len(bits) != len(ORIGINAL_BITSTREAM),
          'the file handed to the loader is exactly 4 MiB, equals the expected image, tail all 0xFF (bitstream length differs from the original)')
    check(rig.said('sound build 18.40 (image '), 'the job names the image by version and 8-character fingerprint')
    check(rig.jobs().check() == EXIT_OK and rig.said('holds the sound (stock+media) image'), 'Check FPGA sees the sound image')
    # Again: already there, nothing written.
    writes = rig.chip.writes
    check(jobs.write_media() == EXIT_OK and rig.said('already holds this sound image') and rig.chip.writes == writes,
          'FPGA sound twice: nothing written the second time')
    # The undo: Restore FPGA over the sound image.
    check(jobs.restore_original() == EXIT_OK and rig.flash() == ORIGINAL and rig.said('writing your original image back'),
          'Restore FPGA undoes the sound image')
    rig.no_leaks()
    # The rehearsal has expired.
    rig = Rig()
    jobs = rig.jobs()
    check(jobs.restore_original() == EXIT_OK, 'rehearsal')
    record = json.loads((rig.work / cf.REHEARSAL_FILE).read_text())
    record['at'] = time.time() - cf.REHEARSAL_VALID_S - 1
    (rig.work / cf.REHEARSAL_FILE).write_text(json.dumps(record))
    check(jobs.write_media() == EXIT_REFUSED and rig.said('has not succeeded in this session yet'), 'an expired rehearsal does not count')
    # The flash is not the original when the sound image is asked for: refused, chip reconfigured.
    rig = Rig()
    jobs = rig.jobs()
    check(jobs.restore_original() == EXIT_OK, 'rehearsal')
    rig.chip.flash[100] ^= 0xff
    check(jobs.write_media() == EXIT_REFUSED and rig.said('does not hold your original image') and rig.chip.writes == 1
          and rig.chip.configured, 'FPGA sound over a changed flash: refused, nothing written, chip reconfigured')
    # No built image.
    rig = Rig()
    rig.media_fails = 'no built sound image: fpga/images/sound-18.40/build/evt1_x2.fs is missing.'
    jobs = rig.jobs()
    check(jobs.restore_original() == EXIT_OK and rig.flash() == ORIGINAL, 'the rehearsal works without a built sound image')
    check(jobs.write_media() == EXIT_REFUSED and rig.said('no built sound image'), 'FPGA sound without a build: refused')
    # The media image identical to the original would be pointless.
    rig = Rig(media_bitstream=ORIGINAL_BITSTREAM)
    jobs = rig.jobs()
    check(jobs.restore_original() == EXIT_OK and jobs.write_media() == EXIT_REFUSED and rig.said('identical to your original'),
          'a sound image equal to the original is refused')
    # A restore over an unknown image needs the typed word.
    rig = Rig(flash=synthetic_bitstream(9) + b'\xff' * (SIZE - 900_000))
    check(rig.jobs().restore_original() == EXIT_REFUSED and rig.said('unknown image and that was not confirmed')
          and rig.chip.writes == 0 and rig.chip.configured, 'restore over an unknown image: refused without the word, reconfigured')
    check(rig.jobs().restore_original(confirm=lambda prompt: 'no') == EXIT_REFUSED and rig.chip.writes == 0, 'wrong word: refused')
    check(rig.jobs().restore_original(confirm=lambda prompt: 'change') == EXIT_OK and rig.flash() == ORIGINAL,
          'restore over an unknown image with the typed word')
    rig.no_leaks()


def approval_checks():
    """AI-started (approved fingerprint) against owner-started (None)."""
    rig = Rig()
    jobs = rig.jobs()
    check(jobs.restore_original() == EXIT_OK, 'rehearsal')
    fingerprint = hashlib.sha256(rig.media_fs.read_bytes()).hexdigest()
    check(jobs.write_media(approved_fingerprint='') == EXIT_REFUSED and rig.said('not the one approved in the window')
          and rig.chip.writes == 1, 'AI-started with no approved image: refused before the chip is read')
    check(jobs.write_media(approved_fingerprint='0' * 64) == EXIT_REFUSED and rig.chip.writes == 1,
          'AI-started with another approved image: refused')
    check(jobs.write_media(approved_fingerprint=fingerprint) == EXIT_OK and rig.flash() == rig.expected_media(),
          'AI-started with the approved image: written')
    check(jobs.restore_original() == EXIT_OK and jobs.write_media(approved_fingerprint=None) == EXIT_OK,
          'owner-started (None): the built image')
    rig.no_leaks()


def failure_checks():
    expected = None
    # Readback mismatch after the sound write: the original goes back (undone).
    rig = Rig()
    jobs = rig.jobs()
    check(jobs.restore_original() == EXIT_OK, 'rehearsal')
    rig.chip.corrupt_after_write = 5000
    check(jobs.write_media() == EXIT_RECOVERED and rig.flash() == ORIGINAL and rig.said('does not match the expected image')
          and rig.said('undone (your original image is back)') and not jobs.pending() and rig.chip.configured,
          'readback mismatch: original written back, verified, reconfigured; job closed')
    # Power cut in the middle of the sound write.
    rig = Rig()
    jobs = rig.jobs()
    check(jobs.restore_original() == EXIT_OK, 'rehearsal')
    rig.chip.cut_write_after = 300_000
    check(jobs.write_media() == EXIT_RECOVERED and rig.flash() == ORIGINAL and not jobs.pending(),
          'power cut mid-write: original back, verified')
    # Power cut during the readback after a good write: the flash already holds the target, so it is finished.
    rig = Rig()
    jobs = rig.jobs()
    check(jobs.restore_original() == EXIT_OK, 'rehearsal')
    rig.chip.cut_at_read = rig.chip.reads + 2  # the readback after the write, not the read before it
    expected = rig.expected_media()
    check(jobs.write_media() == EXIT_RECOVERED and rig.flash() == expected and rig.said('was finished')
          and not jobs.pending(), 'power cut during the readback: the verified target stays; job finished')
    # Power cut during the rehearsal write: recovery writes the original (same image), finished, and that
    # write counts as the rehearsal (the recovery wrote and verified).
    rig = Rig()
    rig.chip.cut_write_after = 100
    jobs = rig.jobs()
    check(jobs.restore_original() == EXIT_RECOVERED and rig.flash() == ORIGINAL and jobs.rehearsal() is not None,
          'power cut during the rehearsal: recovered, rehearsal credited for the recovery write')
    # B2: the loader exits before erasing anything (flash still the original, journal never reached
    # write/written): the job is finished, but it is NOT a rehearsal; FPGA sound stays refused.
    rig = Rig()
    jobs = rig.jobs()
    real_write = FakeLoader.write

    def exits_early(self, source):
        self.c.writes += 1
        raise cf.LoaderFailed('The JTAG write failed (private log kept).')
    FakeLoader.write = exits_early
    try:
        code = jobs.restore_original()
    finally:
        FakeLoader.write = real_write
    check(code == EXIT_RECOVERED and rig.flash() == ORIGINAL and not jobs.pending() and jobs.rehearsal() is None
          and rig.said('does not count as the rehearsal'), 'loader exit before erasing: finished, no rehearsal credit')
    check(jobs.write_media() == EXIT_REFUSED and rig.said('has not succeeded in this session yet') and rig.chip.writes == 1,
          'FPGA sound still refused after the unproven restore')
    # B2: the loader completed write+verify (journal write/written) and the power failed in our readback:
    # the flash already reads as the original, the job is finished and the rehearsal IS credited.
    rig = Rig()
    jobs = rig.jobs()
    rig.chip.cut_at_read = rig.chip.reads + 2
    check(jobs.restore_original() == EXIT_RECOVERED and rig.flash() == ORIGINAL and jobs.rehearsal() is not None,
          'completed write, power cut in the readback: finished with rehearsal credit')
    check(jobs.write_media() == EXIT_OK and rig.flash() == rig.expected_media(), 'FPGA sound allowed after it')
    # (4) the unknown-image question is the FPGA's own prompt (not the ESP32 version-change wording).
    rig = Rig(flash=synthetic_bitstream(9) + b'\xff' * (SIZE - 900_000))
    prompts = []
    check(rig.jobs().restore_original(confirm=lambda prompt: prompts.append(prompt) or 'change') == EXIT_OK
          and prompts == [cf.FPGA_UNKNOWN_PROMPT] and not prompts[0].startswith('Type change to continue'),
          'unknown image: the FPGA prompt, distinct from the ESP32 one')
    # The tool dies mid-job (process killed after the write began): the next run must finish it.
    rig = Rig()
    jobs = rig.jobs()
    check(jobs.restore_original() == EXIT_OK, 'rehearsal')

    class Died(BaseException):
        pass

    real_write = FakeLoader.write

    def dying_write(self, source):
        real_write(self, source)
        self.c.flash[4000] ^= 0xff  # the write did not complete as the chip sees it
        raise Died()
    FakeLoader.write = dying_write
    try:
        jobs.write_media()
        check(False, 'the process died')
    except Died:
        pass
    finally:
        FakeLoader.write = real_write
    later = rig.jobs()
    check(later.pending(), 'a torn job is pending on the next run')
    check(later.restore_original() == EXIT_REFUSED and later.write_media() == EXIT_REFUSED and later.check() == EXIT_REFUSED
          and rig.said('unfinished console chip job'), 'every other FPGA job refuses while one is pending')
    check(later.resume() == EXIT_RECOVERED and rig.flash() == ORIGINAL and not later.pending(),
          'Finish it: original back and verified')
    # Boot not seen after a verified write.
    rig = Rig()
    jobs = rig.jobs()
    rig.chip.boot_fails = True
    check(jobs.restore_original() == cf.EXIT_NOT_SEEN and rig.flash() == ORIGINAL and not jobs.pending()
          and jobs.rehearsal() is None, 'console not seen returning: code 5, job closed, no rehearsal credit')
    # Recovery attempts exhausted: manual, job stays pending, abandon closes it without writing.
    rig = Rig()
    jobs = rig.jobs()
    check(jobs.restore_original() == EXIT_OK, 'rehearsal')
    rig.chip.corrupt_after_write = 5000
    real_write = FakeLoader.write

    def always_bad(self, source):
        real_write(self, source)
        self.c.flash[7] ^= 0xff
    FakeLoader.write = always_bad
    try:
        code = jobs.write_media()
    finally:
        FakeLoader.write = real_write
    check(code == EXIT_MANUAL and jobs.pending() and rig.said('This needs you') and rig.said('Rescue with a backup')
          and rig.chip.writes == 1 + 1 + rig.timing.attempts, 'attempts exhausted: manual ladder, job kept pending')
    writes = rig.chip.writes
    check(jobs.abandon() == EXIT_OK and not jobs.pending() and rig.chip.writes == writes, 'abandon closes without writing')
    # Owner Stop before the write: nothing written, no job.
    rig = Rig()

    class Stopped(KeyboardInterrupt):
        owner_stop = True
    rig.stop = lambda: (_ for _ in ()).throw(Stopped())
    check(rig.jobs().restore_original() == EXIT_REFUSED and rig.chip.writes == 0 and rig.said('Stopped before any write')
          and not rig.jobs().pending(), 'owner Stop at the last moment: nothing written')
    rig.no_leaks()


def trial_checks():
    """Try FPGA image (temporary): the SRAM-only load. Refused without a recent Check FPGA or a
    rehearsal; the flash is never read or written by it; the chip runs the pinned image; power
    cycle / reset returns to the flash; the guard refuses any loader line that names the flash."""
    # The guard itself.
    for bad in (['--write-flash', 'x.fs'], ['--write-sram', '--write-flash', 'x.fs'], ['--write-sram', '-f', 'x.fs'],
                ['--write-sram', '--dump-flash', '--file-size', '4194304', 'x.fs'], ['--write-sram', '-o', '0', 'x.fs'],
                ['--write-sram', '--offset=0', 'x.fs'], ['--write-sram', '--bulk-erase', 'x.fs'],
                ['--write-sram', '--verify', 'x.fs'], ['--write-sram', '--external-flash', 'x.fs'],
                ['--write-sram', 'flash.bin'], ['--write-sram', 'a.fs', 'b.fs'], ['--write-sram'], ['x.fs'],
                ['--write-sram', '--write-sram', 'x.fs'], ['--write-sram', '--unprotect-flash', 'x.fs']):
        try:
            cf.sram_only(bad)
            check(False, f'flash-touching or malformed trial line accepted: {bad}')
        except Refused:
            pass
    check(cf.sram_only(['--write-sram', 'build/evt1_x2.fs']) == ['--write-sram', 'build/evt1_x2.fs'], 'the one allowed shape passes')
    check(cf.sram_only(['--write-sram', 'H:/x/media-flash-fix/evt1_x2.fs']) is not None, 'a folder name containing "flash" before a .fs is fine')
    # Without a recent check or rehearsal: refused before the chip is touched.
    rig = Rig()
    jobs = rig.jobs()
    check(jobs.try_media() == EXIT_REFUSED and rig.said('not recently seen the console chip') and rig.chip.detects == 0
          and rig.chip.sram_loads == 0, 'trial without a known flash: refused before the chip is touched')
    # After Check FPGA found the original: the load runs, flash untouched, serial back, journal closed, no pending job.
    check(jobs.check() == EXIT_OK and rig.said('holds your original image'), 'check')
    reads, resets = rig.chip.reads, rig.chip.resets
    check(jobs.try_media() == EXIT_OK and rig.chip.sram_loads == 1 and rig.chip.sram_sources[-1].endswith('.fs')
          and rig.chip.sram == cf.fs_to_bin(rig.media_fs) and rig.flash() == ORIGINAL and rig.chip.writes == 0
          and rig.chip.reads == reads and rig.chip.resets == resets and rig.chip.configured
          and rig.said('running from the console chip\'s RAM') and rig.said('came back') and not jobs.pending(),
          'trial after a check: SRAM loaded with the built image, flash neither read nor written, console back, no pending job')
    check(rig.said('sound build 18.40 (image '), 'the trial names the image by version and fingerprint')
    # Power cycle / reset returns to the flash.
    FakeLoader(rig.chip).reset()
    check(rig.chip.running() == ORIGINAL, 'a reconfiguration returns to the flash image')
    # Check FPGA after a trial still reads the flash (original) and the trial image is gone.
    check(jobs.check() == EXIT_OK and rig.said('holds your original image') and rig.chip.sram is None, 'check after the trial: flash still original')
    # A stale check does not count; a rehearsal in this session does.
    record = json.loads((rig.work / 'last-check.json').read_text())
    record['at'] = time.time() - cf.TRIAL_CHECK_VALID_S - 1
    (rig.work / 'last-check.json').write_text(json.dumps(record))
    check(jobs.try_media() == EXIT_REFUSED and rig.chip.sram_loads == 1, 'a day-old check does not count')
    check(jobs.restore_original() == EXIT_OK and jobs.try_media() == EXIT_OK and rig.chip.sram_loads == 2, 'a rehearsal in this session counts')
    # A check that found another image does not count.
    rig = Rig(flash=cf.full_flash_image(MEDIA_BITSTREAM))
    jobs = rig.jobs()
    check(jobs.check() == EXIT_OK and jobs.try_media() == EXIT_REFUSED and rig.chip.sram_loads == 0, 'flash holding the sound image: trial refused')
    # The image is pinned to its receipt, exactly as for the flash write; the AI needs the approved fingerprint.
    rig = Rig()
    jobs = rig.jobs()
    check(jobs.check() == EXIT_OK, 'check')
    rig.write_build_receipt(hold=1)
    check(jobs.try_media() == EXIT_REFUSED and rig.said('0 setup / 0 hold') and rig.chip.sram_loads == 0, 'a receipt with violations: trial refused')
    rig.write_build_receipt()
    fingerprint = hashlib.sha256(rig.media_fs.read_bytes()).hexdigest()
    check(jobs.try_media(approved_fingerprint='') == EXIT_REFUSED and rig.chip.sram_loads == 0, 'AI-started without an approved image: refused')
    check(jobs.try_media(approved_fingerprint=fingerprint) == EXIT_OK and rig.chip.sram_loads == 1, 'AI-started with the approved image: loaded')
    # Identity still gates it.
    rig.chip.gwu2x = 2
    check(jobs.try_media() == EXIT_REFUSED and rig.chip.sram_loads == 1, 'two bridges: refused')
    rig.chip.gwu2x = 1
    rig.chip.idcode = 0x1081b
    check(jobs.try_media() == EXIT_REFUSED and rig.chip.sram_loads == 1, 'wrong chip: refused')
    rig.chip.idcode = cf.IDCODE
    # The load fails: the chip is reconfigured from its (untouched) flash and nothing is pending.
    rig.chip.sram_fails = True
    resets = rig.chip.resets
    check(jobs.try_media() == EXIT_REFUSED and rig.said('did not complete') and rig.chip.resets == resets + 1
          and rig.chip.running() == ORIGINAL and rig.chip.configured and not jobs.pending(), 'failed load: back on the flash, job closed')
    # Not seen coming back on the trial image: honest exit code, flash untouched.
    rig.chip.boot_fails = True
    check(jobs.try_media() == cf.EXIT_NOT_SEEN and rig.said('not seen coming back on USB on the trial image') and rig.flash() == ORIGINAL,
          'trial image not seen: code 5, flash untouched')
    rig.chip.boot_fails = False
    # An unfinished flash job blocks it.
    rig = Rig()
    jobs = rig.jobs()
    check(jobs.check() == EXIT_OK and jobs.restore_original() == EXIT_OK, 'rehearsal')
    rig.chip.corrupt_after_write = 5000
    real_write = FakeLoader.write

    def always_bad(self, source):
        real_write(self, source)
        self.c.flash[7] ^= 0xff
    FakeLoader.write = always_bad
    try:
        check(jobs.write_media() == EXIT_MANUAL and jobs.pending(), 'a stuck flash job')
    finally:
        FakeLoader.write = real_write
    check(jobs.try_media() == EXIT_REFUSED and rig.chip.sram_loads == 0, 'a pending flash job blocks the trial')
    # Owner Stop just before the load: nothing loaded.
    rig = Rig()
    jobs = rig.jobs()
    check(jobs.check() == EXIT_OK, 'check')

    class Stopped(KeyboardInterrupt):
        owner_stop = True
    rig.stop = lambda: (_ for _ in ()).throw(Stopped())
    check(jobs.try_media() == EXIT_REFUSED and rig.chip.sram_loads == 0 and not jobs.pending(), 'owner Stop before the load: nothing loaded')
    rig.no_leaks()

    # What the flash holds is the newest thing this tool saw: a sound write after the rehearsal wins
    #, so the trial never says "the flash still holds your original image" then.
    rig = Rig()
    jobs = rig.jobs()
    check(jobs.restore_original() == EXIT_OK and jobs.known_flash() == 'original' and jobs.known_original(),
          'after the rehearsal: the original')
    check(jobs.write_media() == EXIT_OK and jobs.rehearsal() is not None and jobs.known_flash() == 'media'
          and not jobs.known_original(), 'a later sound write wins over the earlier rehearsal')
    loads = rig.chip.sram_loads
    check(jobs.try_media() == EXIT_REFUSED and rig.chip.sram_loads == loads, 'the sound trial: refused over the sound image')
    rig.chip.flash[100] ^= 0xff  # something changed the flash; Check FPGA finds an unknown image
    check(jobs.check() == EXIT_OK and jobs.last_known() == 'unknown' and jobs.known_flash() is None
          and not jobs.known_original(), 'a later check of an unknown image: not known, never "original"')
    rig.chip.flash[100] ^= 0xff
    check(jobs.restore_original() == EXIT_OK and jobs.known_flash() == 'original' and jobs.known_original(),
          'a later rehearsal wins again')
    check(jobs.check() == EXIT_OK and jobs.known_flash() == 'original', 'a later check of the original: still the original')
    rig.no_leaks()


def real_link_pin_checks():
    """The approved image (LINK_PINS) is the only 18.41 LinkImage ever loads. Its tree and its three
    hashes are written in ONE place, chromatic_fpga's LINK_TREE / LINK_*_SHA256 block: this test
    reads them from there and names no hash and no folder itself, so a re-pinned image needs no
    change here. The pinned tree (receipts, outputs, restored sources) loads with every pin; every
    OTHER build beside it under fpga/images is refused by hash alone, with
    and without its NEVER-INSTALL.txt, before any receipt is read."""
    pins = (cf.LINK_FS_SHA256, cf.LINK_BIN_SHA256, cf.LINK_STAGE_SHA256)
    check(cf.LINK_PINS == {'fs': cf.LINK_FS_SHA256, 'bin': cf.LINK_BIN_SHA256, 'stage': cf.LINK_STAGE_SHA256}
          and all(re.fullmatch('[0-9a-f]{64}', pin) for pin in pins) and len(set(pins)) == 3,
          'the approved image is pinned by three hashes (its .fs, Gowin\'s .bin, its stage receipt)')
    check(cf.LINK_FS == cf.LINK_TREE / 'build/evt1_x2.fs' and cf.LINK_RECEIPT == cf.LINK_TREE / 'stage-receipt.json'
          and cf.LINK_BUILD_RECEIPT == cf.LINK_TREE / 'build/build-receipt.json'
          and cf.LINK_TREE.parent == cf.ROOT / 'fpga/images',
          'the image, its receipts and its pins all hang from the one LINK_TREE')
    scripts = Path(cf.__file__).resolve().parent
    own = Path(cf.__file__).read_text(encoding='utf-8')
    block = own[own.index('LINK_TREE = ROOT'):own.index('LINK_MARK = ')]
    check(all(own.count(pin) == 1 and pin in block for pin in pins) and own.count(cf.LINK_TREE.name) == 1
          and cf.LINK_TREE.name in block, 'chromatic_fpga writes the tree and each hash once, in one block')
    for other in sorted(scripts.glob('chromatic_manager*.py')):
        text = other.read_text(encoding='utf-8')
        check(not any(pin in text for pin in pins) and cf.LINK_TREE.name not in text,
              f'{other.name} names no pinned hash and no image folder of its own')
    if cf.LINK_FS.is_file():
        image = cf.LinkImage(allow_unreviewed=True)
        pinned = json.loads(cf.LINK_BUILD_RECEIPT.read_text(encoding='utf-8'))
        check(pinned['timing'] == {'setup_violated_endpoints': 0, 'hold_violated_endpoints': 0}
              and pinned['slack_floor_ns'] == 1.0 and pinned['slack_floor_ok'] is True,
              'the pinned image: 0 setup / 0 hold violated endpoints, the 1.0 ns floor kept')
        check(image.fingerprint == cf.LINK_FS_SHA256 and hashlib.sha256(image.bitstream).hexdigest() == cf.LINK_BIN_SHA256
              and hashlib.sha256(cf.LINK_RECEIPT.read_bytes()).hexdigest() == cf.LINK_STAGE_SHA256
              and len(pinned['sources_sha256']) >= 16
              and all(hashlib.sha256((cf.LINK_TREE / rel).read_bytes()).hexdigest() == digest
                      for rel, digest in pinned['sources_sha256'].items())
              and pinned['worst_reported_setup']['slack_ns'] >= pinned['slack_floor_ns'] >= 1.0
              and pinned['qspi_paths_all_positive'] is True,
              'the pinned tree loads: .fs/.bin/stage receipt pins, its sources as its receipt lists them, timing over the floor')
    files = ('build/evt1_x2.fs', 'build/evt1_x2.bin', 'build/build-receipt.json', 'stage-receipt.json')
    others = [tree for tree in sorted(cf.LINK_TREE.parent.iterdir()) if tree.is_dir() and tree != cf.LINK_TREE
              and all((tree / rel).is_file() for rel in files)] if cf.LINK_TREE.parent.is_dir() else []
    seen_other = []
    for tree in others:  # another build (the earlier trial image, a later fix, a build in progress): never the approved image
        try:
            sha = hashlib.sha256((tree / files[0]).read_bytes()).hexdigest()
            if sha == cf.LINK_FS_SHA256:
                continue  # a byte copy of the approved image is the approved image
            seen_other.append(tree.name)
            for allow in (False, True):
                try:
                    cf.LinkImage(tree / files[0], tree / files[3], tree / files[2], allow_unreviewed=allow)
                    check(False, f'{tree.name}: an image that is not the pinned one loaded (allow_unreviewed={allow})')
                except Refused as error:
                    check('not the approved 18.41 stream image' in str(error),
                          f'{tree.name}: refused by its hash (allow_unreviewed={allow})')
            # Without any marker, in a copy with its own receipts: the pin alone refuses it.
            with tempfile.TemporaryDirectory() as work:
                copy = Path(work) / tree.name
                (copy / 'build').mkdir(parents=True)
                for rel in files:
                    shutil.copyfile(tree / rel, copy / rel)
                check(not (copy / cf.LINK_MARK).exists() and not (copy / 'build' / cf.LINK_MARK).exists(), 'copy has no marker')
                if hashlib.sha256((copy / files[0]).read_bytes()).hexdigest() == cf.LINK_FS_SHA256:
                    continue
                try:
                    cf.LinkImage(copy / files[0], copy / files[3], copy / files[2])
                    check(False, f'{tree.name}: loaded from a copy without NEVER-INSTALL.txt')
                except Refused as error:
                    check('not the approved 18.41 stream image' in str(error),
                          f'{tree.name} without any marker: refused by the pin')
                check(cf.link_ready_text(copy / files[0], copy / files[3], copy / files[2]) is None
                      and cf.link_text(copy / files[0], copy / files[3], copy / files[2]) is None,
                      f'nothing offers or names the image in {tree.name}')
        except OSError:
            continue  # a build another job is writing right now: not readable this moment, nothing to check
    return seen_other


def link_checks():
    """The sound + Pocket link image (18.41), owner-present: refused while NEVER-INSTALL.txt is there;
    from a fixture copy without it, the SRAM try (as try_media), the owner's answers recorded with
    that try, the flash write only after a confirmed try of exactly this image, tamper refusals on
    the try record and the image, one-click returns to the sound image and to the original, and a
    failed write put right."""
    sound = cf.full_flash_image(MEDIA_BITSTREAM)
    good = {'games': 'good', 'link_game': 'good', 'sound': 'good', 'pocket_link': 'good'}
    check(cf.LINK_TRY_VALID_S == cf.TRIAL_CHECK_VALID_S == 24 * 3600, 'a confirmed try counts 24 hours, like the check')

    # The real staged tree: while its marker is there, nothing gets the image (inspection only).
    real_marked = (cf.LINK_FS.parent / cf.LINK_MARK).exists() or (cf.LINK_RECEIPT.parent / cf.LINK_MARK).exists()
    if real_marked:
        try:
            cf.LinkImage()
            check(False, 'the real 18.41 image loaded while NEVER-INSTALL.txt is there')
        except Refused as error:
            check('unreviewed' in str(error), 'the real 18.41 image: refused as unreviewed')
        check(cf.link_ready_text() is None, 'nothing offers the real 18.41 image while it is unreviewed')
    elif cf.LINK_FS.is_file():
        check(cf.LinkImage().reviewed and cf.link_ready_text() is not None, 'the real 18.41 image, reviewed: ready')
    real_link_pin_checks()

    # Marker present on the fixture: try, write and the answers are all refused; the chip is untouched.
    rig = Rig(flash=sound)
    jobs = rig.jobs()
    check(jobs.check() == EXIT_OK and rig.said('holds the sound (stock+media) image'), 'check finds the sound image')
    for where in ('build', 'top'):
        rig.link_marker(True, where)
        reads = rig.chip.reads
        check(jobs.try_link() == EXIT_REFUSED and rig.said('unreviewed') and rig.chip.sram_loads == 0
              and rig.chip.reads == reads and rig.chip.detects == 1, f'marker ({where}): the try is refused, chip untouched')
        check(jobs.write_link() == EXIT_REFUSED and rig.chip.writes == 0 and rig.chip.reads == reads,
              f'marker ({where}): the write is refused')
        try:
            jobs.confirm_link_try(good)
            check(False, 'answers accepted for an unreviewed image')
        except Refused:
            pass
        image = rig.link(allow_unreviewed=True)
        try:
            image.write_file(rig.work / 'never')
            check(False, 'an unreviewed image wrote a flash file')
        except Refused:
            pass
        check(not (rig.work / 'never').exists(), 'no flash file for an unreviewed image')
        rig.link_marker(False, where)
    # A marker that comes back after the image was loaded still stops write_file.
    image = rig.link()
    rig.link_marker(True)
    try:
        image.write_file(rig.work / 'late')
        check(False, 'write_file ignored a marker that came back')
    except Refused:
        pass
    rig.link_marker(False)
    check(image.short().startswith('sound + Pocket link build 18.41 (image ') and image.reviewed,
          'the fixture copy without the marker loads, 18.41')

    # Check FPGA and Restore FPGA recognise the image on the flash even while it is unreviewed.
    probe = Rig(flash=cf.full_flash_image(LINK_BITSTREAM))
    probe.link_marker(True)
    pj = probe.jobs()
    check(pj.check() == EXIT_OK and probe.said('Its flash holds the Pocket link image') and pj.last_known() == 'link',
          'Check FPGA recognises the Pocket link image (unreviewed or not)')
    check(pj.restore_original(confirm=None) == EXIT_OK and probe.flash() == ORIGINAL
          and probe.said('The flash holds the Pocket link image') and pj.last_known() == 'original',
          'Restore FPGA over the Pocket link image: one click, no typed change')

    # The flow: check (sound image), try, answers, write.
    check(jobs.write_link() == EXIT_REFUSED and rig.said('has not been confirmed by you') and rig.chip.writes == 0
          and rig.chip.reads == 1, 'write before any try: refused before the chip is read')
    try:
        jobs.confirm_link_try(good)
        check(False, 'answers accepted with no try')
    except Refused:
        pass
    reads, writes = rig.chip.reads, rig.chip.writes
    check(jobs.try_link() == EXIT_OK and rig.chip.sram_loads == 1 and rig.chip.sram == LINK_BITSTREAM
          and rig.chip.sram_sources[-1].endswith('.fs') and rig.flash() == sound and rig.chip.reads == reads
          and rig.chip.writes == writes and rig.said('running from the console chip\'s RAM')
          and rig.said('the flash still holds the sound image (18.40)') and not jobs.pending(),
          'try: SRAM only, the flash neither read nor written, the sound image stays in the flash')
    record = jobs.link_try()
    check(record['image'] == rig.link().fingerprint and record['seen'] is True and 'confirmed' not in record,
          'the try is recorded, not yet confirmed')
    check(record['running'] == [18, 41, 1] and record['read'] == 'passive' and rig.chip.version_reads == 1
          and rig.said(cf.SAID_LINK_RUNNING) and rig.said('nothing was restarted') and not rig.said(cf.SAID_LINK_NOT_RUNNING),
          'the try proves 18.41 runs: the start-up menu\'s own line was read once, passively, and recorded with the try')
    said = '\n'.join(rig.lines)
    check(said.index('Pocket link row') < said.index('Jellyfin sound') < said.index('then games'),
          'the try names the looks Jellyfin first, games last')
    check(jobs.write_link() == EXIT_REFUSED and rig.chip.writes == 0, 'a try without answers does not open the write')
    # a second RAM load in one session without a power cycle between them left the console in game mode
    # with a white screen. A second try in the same session waits for the console to go off and on; it refuses when
    # that never happens, the chip untouched and the first try's record kept.
    loads, before = rig.chip.sram_loads, len(rig.lines)
    check(jobs.try_link() == EXIT_REFUSED and rig.said('not switched off and on') and rig.chip.sram_loads == loads
          and jobs.link_try() == record and 'Switch the console OFF and ON now' in '\n'.join(rig.lines[before:]),
          'a second try in the same session without a power cycle: refused, nothing loaded, the first record kept')
    rig.chip.power_cycle()  # the owner switches off and on: back to the sound image
    check(rig.chip.running() == sound, 'a power cycle returns to the sound image')
    # The gate is keyed on the recorded try, not on the Manager process. A new Manager run
    # (another session) within LINK_TRY_POWER_CYCLE_S is gated too; a record older than that is not; the record's
    # time is what counts, and a record with no readable time counts as recent.
    check(cf.LINK_TRY_POWER_CYCLE_S == 180 * 60 and jobs.link_try_needs_power_cycle(), 'a fresh try: power cycle needed')
    rig.chip.off_for = 0  # the console is on its flash image again, but no tool watched it go off and on
    restarted = rig.jobs(session='session-restarted')
    check(restarted.link_try_needs_power_cycle(), 'a restarted Manager (another session) sees the same need')
    check(restarted.try_link() == EXIT_REFUSED and rig.said('not switched off and on') and rig.chip.sram_loads == loads
          and jobs.link_try() == record, 'a restarted Manager, no watched power cycle: refused, nothing loaded')
    rig.chip.power_cycle()
    at = record['at']
    check(not jobs.link_try_needs_power_cycle(now=at + cf.LINK_TRY_POWER_CYCLE_S + 1)
          and jobs.link_try_needs_power_cycle(now=at + cf.LINK_TRY_POWER_CYCLE_S)
          and jobs.link_try_needs_power_cycle(now=at - 60), 'the gate ends after 180 minutes; a future time still counts')
    path0 = rig.work / cf.LINK_TRY_FILE
    saved0 = path0.read_text(encoding='utf-8')
    data = json.loads(saved0)
    data['at'] = at - cf.LINK_TRY_POWER_CYCLE_S - 5
    path0.write_text(json.dumps(data), encoding='utf-8')
    check(not restarted.link_try_needs_power_cycle(), 'an old record does not gate')
    data['at'] = 'soon'
    path0.write_text(json.dumps(data), encoding='utf-8')
    check(restarted.link_try_needs_power_cycle(), 'a record without a readable time gates')
    path0.write_text(saved0, encoding='utf-8')
    check(rig.said('while this tool watches') and rig.said('not seen and does not count'),
          'the gate says the power cycle must happen while the tool watches')
    # The pin on fixture images: another image with the first one's pins is refused before its markers are looked at.
    other = Rig(flash=sound)
    other.make_link_tree(Path(other.temp.name) / 'link-other', synthetic_bitstream(5, length=900_400))
    for where in ('build', 'top'):
        other.link_marker(True, where)
        try:
            other.link(pins=rig.link_pins)
            check(False, 'another image loaded with the approved pins')
        except Refused as error:
            check('not the approved 18.41 stream image' in str(error) and 'unreviewed' not in str(error),
                  f'another image, marker ({where}): refused by the pin first')
        other.link_marker(False, where)
    try:
        other.link(pins=rig.link_pins)
        check(False, 'another image loaded with the approved pins, no marker')
    except Refused as error:
        check('not the approved 18.41 stream image' in str(error), 'another image without a marker: refused by the pin')
    check(other.link().fingerprint != rig.link().fingerprint, 'with its own pins the other fixture loads')
    for bad in ({'fs': rig.link_pins['fs']}, {**rig.link_pins, 'bin': 'x'}):
        try:
            rig.link(pins=bad)
            check(False, 'malformed pins accepted')
        except Refused:
            pass
    try:
        rig.link(pins={**rig.link_pins, 'stage': '0' * 64})
        check(False, 'a stage receipt pin that does not match accepted')
    except Refused as error:
        check('stage receipt' in str(error), 'the stage receipt pin is checked')
    try:
        rig.link(pins={**rig.link_pins, 'bin': '0' * 64})
        check(False, 'a .bin pin that does not match accepted')
    except Refused as error:
        check('.bin' in str(error), 'the .bin pin is checked')
    other.no_leaks()

    # Answers: something wrong is recorded and keeps the write shut.
    check(jobs.confirm_link_try(dict(good, sound='wrong')) is False and jobs.link_try()['confirmed'] is False,
          'a wrong answer is recorded, not a confirmation')
    check(jobs.write_link() == EXIT_REFUSED and rig.chip.writes == 0, 'a try answered wrong: write refused')
    check(jobs.confirm_link_try(dict(good, link_game='skipped')) is True, 'all good, link-cable game skipped: confirmed')
    check(jobs.link_try_confirmed(rig.link().fingerprint) is not None, 'the confirmed try opens the write')

    # Tampered try records are refused (the record is restored after each).
    path = rig.work / cf.LINK_TRY_FILE
    saved = path.read_text(encoding='utf-8')

    def tampered(what, change):
        data = json.loads(saved)
        change(data)
        path.write_text(json.dumps(data), encoding='utf-8')
        fingerprint = rig.link().fingerprint
        check(jobs.link_try_confirmed(fingerprint) is None, f'try record tampered ({what}): not a confirmation')
        path.write_text(saved, encoding='utf-8')
    tampered('another image', lambda d: d.__setitem__('image', 'ab' * 32))
    tampered('not seen', lambda d: d.__setitem__('seen', False))
    tampered('not confirmed', lambda d: d.__setitem__('confirmed', False))
    tampered('a check missing', lambda d: d['answers'].pop('pocket_link'))
    tampered('an extra check', lambda d: d['answers'].__setitem__('extra', 'good'))
    tampered('sound skipped', lambda d: d['answers'].__setitem__('sound', 'skipped'))
    tampered('a wrong answer', lambda d: d['answers'].__setitem__('games', 'wrong'))
    tampered('answers not a dict', lambda d: d.__setitem__('answers', ['good'] * 4))
    tampered('confirmed before the try', lambda d: d.__setitem__('confirmed_at', d['at'] - 1))
    tampered('try too old', lambda d: (d.__setitem__('at', d['at'] - cf.LINK_TRY_VALID_S - 10),
                                       d.__setitem__('confirmed_at', d['at'] + 1)))
    tampered('try in the future', lambda d: (d.__setitem__('at', time.time() + 3600),
                                             d.__setitem__('confirmed_at', time.time() + 3601)))
    tampered('times not numbers', lambda d: d.__setitem__('at', 'now'))
    tampered('running 18.40', lambda d: d.__setitem__('running', [18, 40, 1]))
    tampered('running not recorded', lambda d: d.pop('running'))
    tampered('running unreadable', lambda d: d.__setitem__('running', None))
    path.write_text('{not json', encoding='utf-8')
    check(jobs.link_try_confirmed(rig.link().fingerprint) is None and jobs.write_link() == EXIT_REFUSED
          and rig.chip.writes == 0, 'an unreadable try record: write refused')
    path.write_text(saved, encoding='utf-8')
    # A changed image (rebuilt, re-pinned) is a different fingerprint: the old try does not count.
    other = Rig(flash=sound)
    other.make_link_tree(Path(other.temp.name) / 'link2', synthetic_bitstream(4, length=900_400))
    (other.work).mkdir(parents=True, exist_ok=True)
    (other.work / cf.LINK_TRY_FILE).write_text(saved, encoding='utf-8')
    check(other.jobs().link_try_confirmed(other.link().fingerprint) is None, 'a try of another image does not count')
    # The AI grant needs the approved fingerprint.
    check(jobs.write_link(approved_fingerprint='') == EXIT_REFUSED and rig.said('not the one approved in the window')
          and rig.chip.writes == 0, 'AI-started write without the approved image: refused')
    check(jobs.try_link(approved_fingerprint='') == EXIT_REFUSED and rig.chip.sram_loads == 1,
          'AI-started try without the approved image: refused')

    # The write over the sound image: full 4 MiB, verified.
    check(jobs.write_link() == EXIT_OK and rig.flash() == rig.expected_link() and rig.chip.writes == 1
          and rig.said('installed: all 4 MiB read back and identical') and rig.said('over the sound image (18.40)')
          and jobs.last_known() == 'link' and not jobs.pending(), 'write: the Pocket link image over the sound image, verified')
    check(rig.chip.sources[-1].endswith('-flash_4MB.bin') and Path(rig.chip.sources[-1]).stat().st_size == SIZE,
          'the loader got a full 4 MiB image')
    check(jobs.write_link() == EXIT_OK and rig.chip.writes == 1 and rig.said('already holds this Pocket link image'),
          'written again: already there, nothing written')
    check(jobs.check() == EXIT_OK and rig.said('Its flash holds the Pocket link image'), 'check after the write')

    # One click back to the sound image, in a new Manager run (no rehearsal there).
    later = rig.jobs(session='session-b')
    check(later.rehearsal() is None and later.write_media() == EXIT_OK and rig.flash() == sound
          and rig.said('over the sound + Pocket link image'), 'FPGA sound writes 18.40 back over 18.41 in one click')
    # And the recorded try still opens the write in that later run.
    check(later.write_link() == EXIT_OK and rig.flash() == rig.expected_link(), 'a recorded confirmed try counts in a later run')
    # One click back to the original.
    check(later.restore_original(confirm=None) == EXIT_OK and rig.flash() == ORIGINAL, 'Restore FPGA from 18.41: one click')
    # Over the original, leaving it needs the rehearsal of this run (here: that restore was it).
    third = rig.jobs(session='session-c')
    writes = rig.chip.writes
    check(third.write_link() == EXIT_REFUSED and rig.said('Restore FPGA has not run in this session')
          and rig.chip.writes == writes and rig.flash() == ORIGINAL, 'over the original without a rehearsal: refused')
    check(later.write_link() == EXIT_OK and rig.flash() == rig.expected_link(), 'over the original after the rehearsal: written')
    # An unknown image on the flash: refused after the read, nothing written.
    rig.chip.flash[100] ^= 0xff
    writes = rig.chip.writes
    check(later.write_link() == EXIT_REFUSED and rig.said('holds neither the sound image nor your original image')
          and rig.chip.writes == writes, 'an unknown flash: the Pocket link image is not written')
    check(later.write_media() == EXIT_REFUSED and rig.chip.writes == writes, 'an unknown flash: FPGA sound refuses too')
    rig.chip.flash[100] ^= 0xff

    # A failed write: the original goes back, the job is recovered, FPGA sound is one click again.
    rig2 = Rig(flash=sound)
    j2 = rig2.jobs()
    check(j2.check() == EXIT_OK and j2.try_link() == EXIT_OK and j2.confirm_link_try(good), 'tried and confirmed')
    rig2.chip.power_cycle()
    rig2.chip.corrupt_after_write = 5000
    check(j2.write_link() == EXIT_RECOVERED and rig2.flash() == ORIGINAL and not j2.pending()
          and rig2.said('undone (your original image is back)') and j2.last_known() == 'original',
          'readback mismatch in the link write: the original is back')
    check(j2.write_media() == EXIT_OK and rig2.flash() == sound, 'then FPGA sound (the recovery was a rehearsal)')
    # Power cut in the link write.
    check(j2.write_link() == EXIT_OK, 'write again')
    check(j2.write_media() == EXIT_OK, 'back to sound')
    rig2.chip.cut_write_after = 1000
    check(j2.write_link() == EXIT_RECOVERED and rig2.flash() == ORIGINAL and not j2.pending(),
          'power cut in the link write: original back')
    # Cut during the readback after a good link write: the flash already reads as 18.41, so recovery
    # finishes it (nothing written back). Said as "finished", never "your original image is back".
    check(j2.write_media() == EXIT_OK, 'the sound image again')
    rig2.chip.power_cycle()  # a try already ran in this session: the owner switches off and on before the next
    check(j2.try_link() == EXIT_OK and rig2.said('switched off and on (its USB serial port went away and came back)')
          and j2.confirm_link_try(good), 'tried again after the watched power cycle')
    rig2.chip.power_cycle()
    rig2.chip.cut_at_read = rig2.chip.reads + 2  # the readback after the write, not the read before it
    said_before = len(rig2.lines)
    check(j2.write_link() == EXIT_RECOVERED and rig2.flash() == rig2.expected_link() and not j2.pending()
          and rig2.said('console chip job was finished') and j2.last_known() == 'link'
          and j2.known_flash() == 'link', 'readback cut in the link write: finished, 18.41 on and checked')
    finished_lines = [line for line in rig2.lines[said_before:] if 'console chip job was' in line]
    check(any('job was finished' in line for line in finished_lines)
          and not any('original image is back' in line for line in finished_lines),
          'the finished case does not claim the original is back')
    check(j2.write_media() == EXIT_OK and rig2.flash() == sound, 'back to sound')
    # A new try drops the old confirmation.
    check(j2.restore_original() == EXIT_OK, 'restore')
    rig2.chip.power_cycle()  # a try already ran in this session
    check(j2.try_link() == EXIT_OK and j2.link_try().get('confirmed') is None
          and j2.link_try_confirmed(rig2.link().fingerprint) is None, 'a new try needs new answers')
    # The PC clock moved back since the try: the answers are kept, never a confirmation, and it says why.
    path2 = rig2.work / cf.LINK_TRY_FILE
    record = json.loads(path2.read_text(encoding='utf-8'))
    record['at'] = time.time() + 3600
    path2.write_text(json.dumps(record), encoding='utf-8')
    check(j2.confirm_link_try(good) is False and j2.link_try()['confirmed'] is False
          and j2.link_try()['answers'] == good and rig2.said('clock was moved back')
          and j2.link_try_confirmed(rig2.link().fingerprint) is None, 'clock moved back: False, said, the write shut')
    record['at'] = time.time() - cf.LINK_TRY_VALID_S - 60
    path2.write_text(json.dumps(record), encoding='utf-8')
    try:
        j2.confirm_link_try(good)
        check(False, 'answers accepted for a try older than 24 hours')
    except Refused as error:
        check('too old' in str(error), 'a try older than 24 hours: too old')
    FakeLoader(rig2.chip).reset()

    # The try's own preconditions.
    rig3 = Rig(flash=sound)
    j3 = rig3.jobs()
    check(j3.try_link() == EXIT_REFUSED and rig3.said('not recently seen what the console chip') and rig3.chip.detects == 0,
          'try without a recent check: refused before the chip is touched')
    check(j3.check() == EXIT_OK, 'check')
    rig3.backup_fails = 'the second copy of the original FPGA image (other disk) differs.'
    check(j3.try_link() == EXIT_REFUSED and rig3.chip.sram_loads == 0, 'try with a failing backup: refused')
    rig3.backup_fails = None
    record = json.loads((rig3.work / cf.LAST_CHECK_FILE).read_text())
    record['at'] = time.time() - cf.TRIAL_CHECK_VALID_S - 1
    (rig3.work / cf.LAST_CHECK_FILE).write_text(json.dumps(record))
    check(j3.try_link() == EXIT_REFUSED and rig3.chip.sram_loads == 0, 'a day-old check does not count for the try')
    check(j3.check() == EXIT_OK, 'check again')
    rig3.chip.sram_fails = True
    check(j3.try_link() == EXIT_REFUSED and rig3.said('did not complete') and j3.link_try() is None
          and rig3.chip.running() == sound, 'a failed load: back on the flash, no try recorded')
    rig3.chip.boot_fails = True
    check(j3.try_link() == cf.EXIT_NOT_SEEN and j3.link_try()['seen'] is False, 'not seen on the trial image: code 5')
    try:
        j3.confirm_link_try(good)
        check(False, 'answers accepted for a try not seen running')
    except Refused:
        pass
    rig3.chip.boot_fails = False
    running_checks(rig3, j3, good, sound)
    for r in (rig, rig2, rig3, probe):
        r.no_leaks()


def running_checks(rig, jobs, good, sound):
    """the Jellyfin menu showed no Pocket link row during the owner's try, because by
    then the console ran the sound image (18.40) from its flash. The USB port returning proves only
    that some image runs; the try must prove 18.41 from the start-up menu's own version line, or
    stop before any look, record what it saw, and never let answers about another image open the
    write."""
    cases = (('the load fell back to the flash image (18.40)', 'sram_falls_back', ..., 'version 18.40 with the debug bit'),
             ('the version line did not arrive (port busy, no launcher)', None, None, 'nothing readable'),
             ('the FPGA gave no version reply', None, 'none', 'no version reply'),
             ('18.41 without the debug bit', None, (18, 41, 0), 'version 18.41'),
             ('the sound image reported', None, (18, 40, 1), 'version 18.40'),
             ('the USB port held by another program', None, 'busy', 'in use by another program'),
             ('the ESP32 in its ROM download mode', None, 'download', 'download mode'))
    for what, flag, reported, words in cases:
        check(jobs.check() == EXIT_OK, 'check')
        rig.chip.sram_falls_back = flag == 'sram_falls_back'
        rig.chip.reported = reported
        rig.chip.power_cycle()  # a try already ran in this session: the owner switches off and on before the next
        reads, writes, before = rig.chip.reads, rig.chip.writes, len(rig.lines)
        code = jobs.try_link()
        said = '\n'.join(rig.lines[before:])
        record = jobs.link_try()
        check(code == EXIT_REFUSED and cf.SAID_LINK_NOT_RUNNING in said and words in said
              and "is running from the console chip's RAM" not in said and cf.SAID_LINK_RUNNING not in said
              and rig.chip.reads == reads and rig.chip.writes == writes and rig.flash() == sound,
              f'{what}: the try stops before any look, says what the console reported, nothing written')
        check(record is not None and record['seen'] is True and record['running'] != [18, 41, 1] and not jobs.pending(),
              f'{what}: what was seen is recorded with the try')
        try:
            jobs.confirm_link_try(good)
            check(False, f'{what}: answers accepted for a try not proven running 18.41')
        except Refused as error:
            check('not confirmed running 18.41' in str(error), f'{what}: answers refused, said why')
        check(jobs.link_try_confirmed(rig.link().fingerprint) is None and jobs.write_link() == EXIT_REFUSED
              and rig.chip.writes == writes, f'{what}: the write stays shut')
        if reported == 'busy':
            check('Close the program that holds the console' in said, 'busy port: told to close the program')
        if reported == 'download':
            check('ROM download mode' in said and 'screen shows the game layer' in said,
                  'download mode: the white game screen explained, power cycle told')
        FakeLoader(rig.chip).reset()
    rig.chip.sram_falls_back, rig.chip.reported = False, ...
    check(jobs.check() == EXIT_OK, 'check')
    rig.chip.power_cycle()
    check(jobs.try_link() == EXIT_OK and jobs.link_try()['running'] == [18, 41, 1]
          and jobs.confirm_link_try(good) is True, 'then a real 18.41 try confirms')
    FakeLoader(rig.chip).reset()
    # A loader without the version read (an older fake or tool) never confirms.
    class Blind(FakeLoader):
        launcher_fpga_version = None
    blind = cf.FpgaJobs(folder=rig.work, loader_factory=lambda: Blind(rig.chip), backup_loader=rig.backup,
                        media_loader=rig.media, timing=rig.timing, say=rig.lines.append, sleep=lambda s: None,
                        clock=iter(range(1, 10 ** 6)).__next__, session=rig.session, link_loader=rig.link)
    check(blind.check() == EXIT_OK, 'check')
    rig.chip.power_cycle()
    check(blind.try_link() == EXIT_REFUSED and rig.said('nothing readable'), 'no way to read the version: not confirmed')
    FakeLoader(rig.chip).reset()


def update_checks():
    """Update the Pocket link in one step (FpgaJobs.update_link), with no question rounds and one
    Approve: the flash is read, the pinned image is tried from RAM (always a fresh load), this
    tool's own checks replace the owner's four answers (recorded as confirmed_by "auto"), then,
    with the image still running from RAM and nothing touched since the last check, the owner is
    asked once to keep it (an owner action plus the Approve question). Only that Approve, recorded
    with the try, opens write_link, which runs unchanged; the console must then report 18.41 from
    its flash. A failed check, Not right, a console that reports another image at the Approve, a
    Stop, or nothing that can ask: nothing is written. A watched power cycle is an owner action,
    asked for at most once in a run."""
    sound = cf.full_flash_image(MEDIA_BITSTREAM)
    good = {'games': 'good', 'link_game': 'good', 'sound': 'good', 'pocket_link': 'good'}
    title, keep_title = cf.POWER_CYCLE_TITLE, cf.KEEP_TITLE
    kept_pair = [('waiting', keep_title), ('done', keep_title)]
    cycle_pair = [('waiting', title), ('done', title)]
    rigs = []

    def fresh(flash=sound, loader=None):
        rig = Rig(flash=flash)
        rigs.append(rig)
        return rig, rig.jobs(loader=loader)

    def states(jobs):
        return [(event['state'], event['title']) for event in jobs.owner.history]

    def responds(rig, jobs, also=lambda: None):
        """The owner does what the power-cycle sign asks: switches the console off and on while the
        job watches. (The keep sign is answered through the question, not here.)"""
        def listener(event):
            if event['state'] == 'waiting' and event['title'] == title:
                also()
                rig.chip.power_cycle()
        jobs.owner.listen(listener)

    def owner(rig, jobs, answer=cf.KEEP_ANSWER, during=lambda: None):
        """The window's side of the one Approve: remembers what the console and the job looked like
        while the question waited, does `during` (what the owner does meanwhile), then answers."""
        def confirm(prompt):
            confirm.asked.append({
                'prompt': prompt, 'running_from_ram': rig.chip.sram is not None, 'writes': rig.chip.writes,
                'reads': rig.chip.reads, 'resets': rig.chip.resets, 'status_reads': rig.chip.status_reads,
                'version_reads': rig.chip.version_reads, 'waiting': jobs.owner.waiting,
                'sign': dict(jobs.owner.current or {}), 'flash': rig.flash(),
                'record': dict(jobs.link_try() or {})})
            during()
            if isinstance(answer, BaseException):
                raise answer
            return answer
        confirm.asked = []
        return confirm

    def plan_notes(rig):
        """The journal plan lines of the closed jobs, as text."""
        return [line for path in sorted(rig.work.rglob('journal.jsonl'))
                for line in path.read_text(encoding='utf-8', errors='replace').splitlines() if 'try_confirmed_by' in line]

    # The launcher's own status line: parsed only whole, the newest one counts.
    line = b'PJ-DIAG t=5012 stats phase=menu uart=412 msgs=9 polls=3 heap=1\r\n'
    check(cf.launcher_status(b'x\r\n' + line) == {'phase': 'menu', 'uart': 412, 'msgs': 9}
          and cf.launcher_status(line + b'PJ-DIAG t=6012 stats phase=media uart=500 msgs=11 polls=4\r\n')
          == {'phase': 'media', 'uart': 500, 'msgs': 11}
          and cf.launcher_status(b'PJ-DIAG t=5012 stats phase=menu uart=412 msgs=9') is None
          and cf.launcher_status(b'PJ-DIAG t=5012 stats phase=MENU uart=1 msgs=1 ') is None
          and cf.launcher_status(None) is None and cf.launcher_status(b'') is None
          and cf.MENU_PHASES == ('menu', 'media', 'other'), 'the status line parses only when whole; the newest counts')
    check(cf.AUTO_CHECKS == ('loaded', 'usb_back', 'not_download', 'version', 'menu')
          and set(cf.AUTO_CHECK_WORDS) == set(cf.AUTO_CHECKS) and len(cf.AUTO_NOT_CHECKED) == 4
          and (cf.CONFIRMED_BY_AUTO, cf.CONFIRMED_BY_OWNER) == ('auto', 'owner'),
          'five automatic checks, four things named as not checked by a machine')
    check(cf.KEEP_ANSWER == 'keep' and cf.LINK_KEEP_MUST_HEAR is False and 0 < cf.KEEP_VALID_S <= 600
          and cf.keep_text('media').startswith('Start a game for a moment and check the picture and the sound.')
          and 'Press Approve to keep the new image.' in cf.keep_text('media')
          and 'If you switched your Chromatic off and on, press Not right.' in cf.keep_text('media')
          and 'Not right changes nothing and leaves ' + cf.FLASH_WORDS_FOR['media'] in cf.keep_text('media')
          and 'off and on, changes nothing' not in cf.keep_text('media')
          and cf.FLASH_WORDS_FOR['original'] in cf.keep_text('original'),
          'the Approve words: start a game and look, Approve keeps it, after an off-and-on press Not right, Not right '
          'leaves what the flash holds')

    # A saved try of ANOTHER image, confirmed by the owner and still fresh (what the earlier trial
    # image's record is to a newly approved image): it never counts. Only the image it names stands
    # between it and a write; the one-step update makes its own run from RAM of the approved image
    # and asks its own Approve, and without that Approve nothing is written.
    rig, jobs = fresh()
    fingerprint = rig.link().fingerprint
    at = time.time() - cf.LINK_TRY_POWER_CYCLE_S - 600
    saved = {'image': 'ab' * 32, 'session': 'session-earlier', 'at': at, 'seen': True, 'running': list(cf.LINK_RUNNING),
             'answers': dict(good), 'confirmed': True, 'confirmed_at': at + 60, 'confirmed_by': cf.CONFIRMED_BY_OWNER}
    try_path = jobs._link_try_path()
    try_path.parent.mkdir(parents=True, exist_ok=True)
    try_path.write_text(json.dumps({**saved, 'image': fingerprint}), encoding='ascii')
    check(jobs.link_try_confirmed(fingerprint) is not None, 'such a record of THIS image would count (so the next check means the image)')
    try_path.write_text(json.dumps(saved), encoding='ascii')
    check(jobs.link_try_confirmed(fingerprint) is None and jobs.link_try_confirmed(fingerprint, accept_auto=True) is None
          and not jobs.link_try_needs_power_cycle(),
          'a saved, owner-confirmed try of another image opens no write of this one')
    check(jobs.check() == EXIT_OK and jobs.write_link() == EXIT_REFUSED and rig.chip.writes == 0 and rig.chip.sram_loads == 0
          and rig.flash() == sound, 'step by step: the write is refused on another image\'s try, nothing loaded or written')
    no = owner(rig, jobs, answer='')
    check(jobs.update_link(confirm=no) == EXIT_REFUSED and rig.chip.sram_loads == 1 and rig.chip.writes == 0
          and rig.flash() == sound and len(no.asked) == 1 and no.asked[0]['running_from_ram']
          and no.asked[0]['record'].get('image') == fingerprint and no.asked[0]['record'].get('session') == jobs.session
          and no.asked[0]['record'].get('confirmed_by') == cf.CONFIRMED_BY_AUTO,
          'one step with another image\'s try on record: its own run from RAM of this image, its own Approve asked; '
          'not approved, so nothing written')
    rig, jobs = fresh()
    try_path = jobs._link_try_path()
    try_path.parent.mkdir(parents=True, exist_ok=True)
    try_path.write_text(json.dumps(saved), encoding='ascii')
    yes = owner(rig, jobs)
    check(jobs.update_link(confirm=yes) == EXIT_OK and rig.chip.sram_loads == 1 and rig.chip.writes == 1
          and rig.flash() == rig.expected_link() and len(yes.asked) == 1 and yes.asked[0]['running_from_ram']
          and yes.asked[0]['writes'] == 0 and jobs.link_try()['image'] == rig.link().fingerprint
          and jobs.link_try()['session'] == jobs.session,
          'and approved: written only after this image ran from RAM in this run; the record now names this image')

    # From the sound image, first time: the try, the checks, ONE question with the image running, the write.
    rig, jobs = fresh()
    fingerprint = rig.link().fingerprint
    yes = owner(rig, jobs)
    check(jobs.update_link(confirm=yes) == EXIT_OK and rig.flash() == rig.expected_link() and rig.chip.sram_loads == 1
          and rig.chip.writes == 1 and not jobs.pending() and jobs.last_known() == 'link',
          'one step from the sound image: tried from RAM once, approved once, written once, read back identical')
    said = '\n'.join(rig.lines)
    order = [said.index(words) for words in (
        'Step 1 of 5', 'Its flash holds the sound (stock+media) image', 'Step 2 of 5', cf.SAID_LINK_RUNNING,
        'This app checks it now', cf.SAID_AUTO_PASSED, 'Step 3 of 5', f"{keep_title}, from the console chip's RAM.",
        cf.SAID_KEPT, 'Step 4 of 5', "Writing the console chip's flash",
        'installed: all 4 MiB read back and identical', 'Step 5 of 5', cf.SAID_LINK_FROM_FLASH)]
    check(order == sorted(order), 'the five steps are said in order, each with what it found; the Approve before the write')
    check(all(f'  passed: {cf.AUTO_CHECK_WORDS[key]}' in rig.lines for key in cf.AUTO_CHECKS)
          and all(item in said for item in cf.AUTO_NOT_CHECKED) and 'FAILED' not in said,
          'every automatic check is said as passed, and what no machine checked is named')
    asked = yes.asked
    check(len(asked) == 1 and asked[0]['prompt'] == cf.LINK_KEEP_PROMPT + cf.keep_text('media')
          and asked[0]['running_from_ram'] and asked[0]['writes'] == 0 and asked[0]['flash'] == sound
          and asked[0]['reads'] == 1 and asked[0]['status_reads'] == 1,
          'asked once, with the new image running from RAM, the flash unwritten and not read since Check FPGA')
    check(asked[0]['waiting'] and asked[0]['sign']['title'] == keep_title and asked[0]['sign']['kind'] == 'action'
          and asked[0]['sign']['instruction'] == cf.keep_text('media') and asked[0]['sign']['seconds'] is None
          and states(jobs) == kept_pair and jobs.owner.history[1]['note'] == 'You approved the new image.',
          'while it waits the owner sign says so, big, with no countdown; after Approve it is thanked')
    check(asked[0]['record'].get('confirmed_by') == 'auto' and asked[0]['record'].get('confirmed') is True
          and 'owner_approved' not in asked[0]['record']
          and rig.chip.version_reads >= asked[0]['version_reads'] + 1,
          'the machine part is on record before the question; the console is asked what it runs only after the answer')
    record = jobs.link_try()
    check(record['image'] == fingerprint and record['confirmed'] is True and record['confirmed_by'] == 'auto'
          and 'answers' not in record and record['auto_checks'] == dict.fromkeys(cf.AUTO_CHECKS, True)
          and record['status'] == {'phase': 'menu', 'uart': 412, 'msgs': 9} and record['running'] == [18, 41, 1]
          and rig.chip.status_reads == 1, 'the try is recorded as confirmed by the machine, with its checks, no answers')
    check(record['owner_approved'] is True and record['owner_approved_session'] == rig.session
          and record['owner_approved_running'] == 'heard' and record['owner_approved_at'] >= record['confirmed_at'],
          'and the owner\'s Approve is recorded with it: given with the image running, heard reporting 18.41')
    notes = plan_notes(rig)
    check(len(notes) == 1 and re.search(r'"try_confirmed_by":\s*"auto"', notes[0])
          and re.search(r'"owner_approved_running":\s*"heard"', notes[0]),
          'the write\'s journal plan names who confirmed the try and that the owner approved it running')
    before = len(rig.lines)
    code = jobs.update_link()
    said = '\n'.join(rig.lines[before:])
    check(code == EXIT_OK and rig.chip.writes == 1 and rig.chip.sram_loads == 1 and 'Steps 2 to 4 are not needed' in said
          and cf.SAID_LINK_FROM_FLASH in said and 'Step 3 of 5' not in said and states(jobs) == kept_pair
          and len(yes.asked) == 1,
          'pressed again: the flash already holds it, nothing loaded, asked or written, only the last check')

    # Nothing that can ask for the Approve: refused before the image is loaded.
    rig, jobs = fresh()
    check(jobs.update_link() == EXIT_REFUSED and rig.said(cf.SAID_NO_ASKER) and rig.said('Nothing was loaded')
          and rig.chip.sram_loads == 0 and rig.chip.writes == 0 and rig.chip.reads == 1 and rig.flash() == sound
          and jobs.link_try() is None and not rig.said('Step 2 of 5') and jobs.owner.history == [],
          'no way to ask for the Approve: refused after the read, nothing loaded, nothing written')

    # confirmed_by "auto" is its own kind: alone it opens nothing; with the owner's Approve given while the
    # image ran it opens the one-step write only, never the question-based one.
    rig, jobs = fresh()
    check(jobs.check() == EXIT_OK and jobs.try_link(auto=True) == EXIT_OK, 'tried')
    try:
        jobs.approve_link_try(True, heard='heard')
        check(False, 'an Approve was recorded for a try the machine has not checked')
    except Refused as error:
        check('nothing to approve' in str(error) and 'owner_approved' not in jobs.link_try(),
              'no Approve can be recorded before the automatic checks passed')
    check(jobs.auto_confirm_link_try() is True
          and rig.said('This app checks it now') and not rig.said('Look at the console now')
          and rig.said('You look at a game now, before anything is written'), 'tried and machine-confirmed')
    check(jobs.link_try_confirmed(fingerprint) is None and jobs.link_try_confirmed(fingerprint, accept_auto=True) is None
          and jobs.write_link(accept_auto=True) == EXIT_REFUSED and rig.said('has not been confirmed by you')
          and rig.chip.writes == 0 and rig.chip.reads == 1,
          'the machine\'s word alone opens no write, one-step or not (refused before a read)')
    other = rig.jobs(session='session-b')
    try:
        other.approve_link_try(True, heard='heard')
        check(False, 'an Approve was recorded from another run')
    except Refused as error:
        check('nothing to approve' in str(error), 'an Approve from another run of the app is not recorded')
    check(jobs.approve_link_try(False) is False and jobs.link_try()['owner_approved'] is False
          and jobs.link_try_confirmed(fingerprint, accept_auto=True) is None
          and jobs.write_link(accept_auto=True) == EXIT_REFUSED and rig.chip.writes == 0,
          'Not right is recorded, and the write stays shut')
    check(jobs.approve_link_try(True, heard='heard') is True
          and jobs.link_try_confirmed(fingerprint, accept_auto=True) is not None
          and jobs.link_try_confirmed(fingerprint) is None and other.link_try_confirmed(fingerprint, accept_auto=True) is None
          and jobs.write_link() == EXIT_REFUSED and rig.chip.writes == 0 and rig.chip.reads == 1,
          'checks plus Approve open the one-step write for this run only; the question-based write stays shut')
    path = rig.work / cf.LINK_TRY_FILE
    saved = path.read_text(encoding='utf-8')

    def tampered(what, change):
        data = json.loads(saved)
        change(data)
        path.write_text(json.dumps(data), encoding='utf-8')
        check(jobs.link_try_confirmed(fingerprint, accept_auto=True) is None
              and jobs.write_link(accept_auto=True) == EXIT_REFUSED and rig.chip.writes == 0,
              f'machine-confirmed record tampered ({what}): not a confirmation, write refused')
        path.write_text(saved, encoding='utf-8')

    def shifted(data, by):
        for key in ('at', 'confirmed_at', 'owner_approved_at'):
            data[key] -= by
    tampered('a check failed', lambda d: d['auto_checks'].__setitem__('menu', False))
    tampered('a check missing', lambda d: d['auto_checks'].pop('version'))
    tampered('an extra check', lambda d: d['auto_checks'].__setitem__('extra', True))
    tampered('a check that is not true', lambda d: d['auto_checks'].__setitem__('menu', 1))
    tampered('the checks gone', lambda d: d.pop('auto_checks'))
    tampered('who confirmed it gone', lambda d: d.pop('confirmed_by'))
    tampered('not confirmed', lambda d: d.__setitem__('confirmed', False))
    tampered('not seen', lambda d: d.__setitem__('seen', False))
    tampered('running 18.40', lambda d: d.__setitem__('running', [18, 40, 1]))
    tampered('another image', lambda d: d.__setitem__('image', 'ab' * 32))
    tampered('confirmed before the try', lambda d: d.__setitem__('confirmed_at', d['at'] - 1))
    tampered('try too old', lambda d: (d.__setitem__('at', d['at'] - cf.LINK_TRY_VALID_S - 10),
                                       d.__setitem__('confirmed_at', d['at'] + 1)))
    tampered('try in the future', lambda d: (d.__setitem__('at', time.time() + 3600),
                                             d.__setitem__('confirmed_at', time.time() + 3601)))
    tampered('the Approve gone', lambda d: d.pop('owner_approved'))
    tampered('the Approve not given', lambda d: d.__setitem__('owner_approved', False))
    tampered('the Approve not a plain yes', lambda d: d.__setitem__('owner_approved', 1))
    tampered('the Approve from another run', lambda d: d.__setitem__('owner_approved_session', 'session-b'))
    tampered('the Approve\'s run gone', lambda d: d.pop('owner_approved_session'))
    tampered('the try from another run', lambda d: d.__setitem__('session', 'session-b'))
    tampered('the Approve before the checks', lambda d: d.__setitem__('owner_approved_at', d['confirmed_at'] - 1))
    tampered('the Approve without a time', lambda d: d.pop('owner_approved_at'))
    tampered('the Approve\'s time not a number', lambda d: d.__setitem__('owner_approved_at', 'now'))
    tampered('the Approve in the future', lambda d: d.__setitem__('owner_approved_at', time.time() + 3600))
    tampered('the Approve older than its few minutes', lambda d: shifted(d, cf.KEEP_VALID_S + 10))
    check(jobs.link_try_confirmed(fingerprint, accept_auto=True) is not None, 'the untouched record still counts')
    # The owner's own answers decide once they are given, whatever the machine found before.
    check(jobs.confirm_link_try(dict(good, sound='wrong')) is False and jobs.link_try()['confirmed_by'] == 'owner'
          and jobs.link_try_confirmed(fingerprint, accept_auto=True) is None
          and jobs.write_link(accept_auto=True) == EXIT_REFUSED and rig.chip.writes == 0,
          'the owner answered Something is wrong after the machine passed it: the write is shut, one-step too')
    try:
        jobs.approve_link_try(True, heard='heard')
        check(False, 'an Approve was recorded over the owner\'s own answers')
    except Refused:
        check(True, 'no one-step Approve is recorded over a try the owner answered')
    check(jobs.confirm_link_try(good) is True and jobs.link_try()['confirmed_by'] == 'owner'
          and jobs.link_try_confirmed(fingerprint) is not None, 'the owner\'s All good confirms it as the owner\'s')
    # The one step never reuses an earlier try: the Approve must be given with the image running, so it loads
    # it again (after the one watched power cycle a repeated load needs) and asks.
    yes = owner(rig, jobs)
    responds(rig, jobs)
    before = len(rig.lines)
    code = jobs.update_link(confirm=yes)
    said = '\n'.join(rig.lines[before:])
    check(code == EXIT_OK and rig.flash() == rig.expected_link() and rig.chip.sram_loads == 2 and rig.chip.writes == 1
          and len(yes.asked) == 1 and yes.asked[0]['running_from_ram'] and states(jobs) == cycle_pair + kept_pair
          and jobs.link_try()['confirmed_by'] == 'auto' and jobs.link_try()['owner_approved'] is True,
          'a try answered step by step is not reused: loaded again, checked, approved while running, then written')

    # The machine's confirmation needs a readable clock too, and a try of this image.
    rig, jobs = fresh()
    check(jobs.check() == EXIT_OK and jobs.try_link(auto=True) == EXIT_OK, 'tried')
    path = rig.work / cf.LINK_TRY_FILE
    record = json.loads(path.read_text(encoding='utf-8'))
    record['at'] = time.time() + 3600
    path.write_text(json.dumps(record), encoding='utf-8')
    check(jobs.auto_confirm_link_try() is False and jobs.link_try()['confirmed'] is False
          and jobs.link_try()['auto_checks'] == dict.fromkeys(cf.AUTO_CHECKS, True) and rig.said('clock was moved back')
          and jobs.link_try_confirmed(fingerprint, accept_auto=True) is None,
          'clock moved back since the try: every check passed, still not a confirmation')
    record['at'] = time.time() - cf.LINK_TRY_VALID_S - 60
    path.write_text(json.dumps(record), encoding='utf-8')
    for what, words in (('older than 24 hours', 'too old'), ('gone', 'no temporary try')):
        try:
            jobs.auto_confirm_link_try()
            check(False, f'the machine confirmed a try that is {what}')
        except Refused as error:
            check(words in str(error), f'a try that is {what}: nothing to confirm')
        path.unlink(missing_ok=True)
    FakeLoader(rig.chip).reset()

    # Each automatic check failing: nothing written, nothing asked, the chip back on its untouched flash.
    class NoStatus(FakeLoader):
        launcher_status = None  # a tool that cannot read the status line never passes the menu check
    cases = (('version', 'the load fell back to the flash image (18.40)', lambda c: setattr(c, 'sram_falls_back', True), None),
             ('not_download', 'the ESP32 in its ROM download mode', lambda c: setattr(c, 'reported', 'download'), None),
             ('version', 'the version line did not arrive', lambda c: setattr(c, 'reported', None), None),
             ('version', 'the FPGA gave no version reply', lambda c: setattr(c, 'reported', 'none'), None),
             ('version', '18.41 without the debug bit', lambda c: setattr(c, 'reported', (18, 41, 0)), None),
             ('version', 'the USB port held by another program', lambda c: setattr(c, 'reported', 'busy'), None),
             ('menu', 'the start-up menu still starting', lambda c: setattr(c, 'phase', 'boot'), None),
             ('menu', 'the start-up menu in game mode', lambda c: setattr(c, 'phase', 'game'), None),
             ('menu', 'no status line', lambda c: setattr(c, 'phase', None), None),
             ('menu', 'a loader without the status read', lambda c: None, NoStatus),
             ('usb_back', 'the console not back on USB on the image', lambda c: setattr(c, 'sram_dead', True), None))
    for key, what, fault, loader in cases:
        rig, jobs = fresh(loader=loader)
        fault(rig.chip)
        yes = owner(rig, jobs)
        code = jobs.update_link(confirm=yes)
        said = '\n'.join(rig.lines)
        record = jobs.link_try()
        check(code == EXIT_REFUSED and cf.SAID_AUTO_FAILED in said and f'  FAILED: {cf.AUTO_CHECK_WORDS[key]}' in said
              and 'Next: press Update the Pocket link again' in said and 'the console is as it was' in said
              and 'Step 3 of 5' not in said and cf.SAID_AUTO_PASSED not in said and cf.SAID_LINK_FROM_FLASH not in said,
              f'{what}: the update stops at the automatic checks and says which one failed and what to do next')
        check(rig.flash() == sound and rig.chip.writes == 0 and rig.chip.sram_loads == 1 and rig.chip.sram is None
              and rig.chip.reads == 1 and not jobs.pending() and jobs.owner.history == [] and yes.asked == [],
              f'{what}: nothing written, no Approve asked, the flash not read again, the chip restarted from its flash')
        check(record['confirmed'] is False and record['confirmed_by'] == 'auto' and record['auto_checks'][key] is False
              and record['auto_checks']['loaded'] is True and 'answers' not in record and 'owner_approved' not in record
              and jobs.link_try_confirmed(fingerprint, accept_auto=True) is None
              and jobs.write_link(accept_auto=True) == EXIT_REFUSED and rig.chip.writes == 0,
              f'{what}: recorded as not confirmed, and the write stays shut')
        try:
            jobs.approve_link_try(True, heard='heard')
            check(False, f'{what}: an Approve was recorded for a try that failed its checks')
        except Refused:
            pass
        if key != 'menu':
            check(rig.chip.status_reads == 0 and record['status'] is None and record['auto_checks']['menu'] is False,
                  f'{what}: the menu is not asked about an image that is not proven to be 18.41')
        if key == 'usb_back':
            check(record['auto_checks'] == dict(dict.fromkeys(cf.AUTO_CHECKS, False), loaded=True),
                  'not back on USB: nothing else counts as passed')

    # Not right (or closing the dialog, or any answer that is not the Approve): nothing written, the chip back
    # on its untouched flash, said plainly, recorded.
    for answer in ('', None, 'no', 'yes', 'approve'):
        rig, jobs = fresh()
        no = owner(rig, jobs, answer=answer)
        resets = rig.chip.resets
        code = jobs.update_link(confirm=no)
        said = '\n'.join(rig.lines)
        record = jobs.link_try()
        check(code == EXIT_REFUSED and f'{cf.SAID_NOT_KEPT}: you did not approve the new image, so nothing was written' in said
              and 'the console is as it was' in said and 'Next: press Update the Pocket link again' in said
              and 'Step 4 of 5' not in said and cf.SAID_KEPT not in said and cf.SAID_LINK_FROM_FLASH not in said,
              f'answered {answer!r}: not kept, said plainly with the next step')
        check(len(no.asked) == 1 and no.asked[0]['running_from_ram'] and rig.flash() == sound and rig.chip.writes == 0
              and rig.chip.reads == 1 and rig.chip.sram is None and rig.chip.resets > resets and not jobs.pending()
              and states(jobs) == [('waiting', keep_title), ('closed', keep_title)],
              f'answered {answer!r}: nothing written, the flash not read again, the chip restarted from its flash')
        check(record['confirmed_by'] == 'auto' and record['owner_approved'] is False
              and jobs.link_try_confirmed(fingerprint, accept_auto=True) is None
              and jobs.write_link(accept_auto=True) == EXIT_REFUSED and rig.chip.writes == 0,
              f'answered {answer!r}: recorded as not approved, and the write stays shut')
    rig, jobs = fresh()
    check(jobs.update_link(confirm=owner(rig, jobs, answer='  KEEP ')) == EXIT_OK and rig.flash() == rig.expected_link(),
          'the Approve answer is read without regard to case or spaces around it')
    # The next press after Not right: the image was loaded minutes ago, so one watched power cycle, then the same.
    rig, jobs = fresh()
    check(jobs.update_link(confirm=owner(rig, jobs, answer='')) == EXIT_REFUSED and rig.chip.sram_loads == 1, 'not kept')
    yes = owner(rig, jobs)
    responds(rig, jobs)
    check(jobs.update_link(confirm=yes) == EXIT_OK and rig.flash() == rig.expected_link() and rig.chip.sram_loads == 2
          and rig.chip.writes == 1 and states(jobs)[2:] == cycle_pair + kept_pair and len(yes.asked) == 1,
          'pressed again after Not right: one watched power cycle, tried again, approved, written')

    # Approve pressed after the console was switched off and on, unseen by the watch on its USB port (between
    # two looks, back at its start-up menu): it runs its flash image again and says so, and nothing is written.
    rig, jobs = fresh()

    def off_and_on():
        rig.chip.configured, rig.chip.sram = True, None  # in one step: the list of COM ports never shows it gone
    late = owner(rig, jobs, during=off_and_on)
    code = jobs.update_link(confirm=late)
    said = '\n'.join(rig.lines)
    record = jobs.link_try()
    check(code == EXIT_REFUSED and f'{cf.SAID_TRY_ENDED} when you pressed Approve' in said and '18.40' in said
          and 'Leave the console on between the try and Approve' in said and 'Step 4 of 5' not in said
          and cf.SAID_KEPT not in said and rig.chip.writes == 0 and rig.chip.reads == 1 and rig.flash() == sound
          and states(jobs) == [('waiting', keep_title), ('closed', keep_title)],
          'Approve after an off-and-on: the console reports 18.40, the update stops, nothing written')
    check(record['owner_approved'] is False and record['owner_approved_running'] == [18, 40, 1]
          and jobs.link_try_confirmed(fingerprint, accept_auto=True) is None
          and jobs.write_link(accept_auto=True) == EXIT_REFUSED and rig.chip.writes == 0,
          'recorded as not approved, with what the console reported, and the write stays shut')

    # After Approve the console is not heard naming its image (a game runs, the port is busy): by the rule in
    # LINK_KEEP_MUST_HEAR this does not stop the update, and the record says "not heard", never "heard".
    class DeafOnce(FakeLoader):
        def launcher_fpga_version(self):
            if getattr(self.c, 'deaf', None):
                self.c.version_reads += 1
                found, self.c.deaf = self.c.deaf, None
                return None if found == 'silent' else found
            return super().launcher_fpga_version()
    for what in ('silent', 'busy', 'none'):
        rig, jobs = fresh(loader=DeafOnce)
        quiet = owner(rig, jobs, during=lambda: setattr(rig.chip, 'deaf', what))
        code = jobs.update_link(confirm=quiet)
        record = jobs.link_try()
        check(code == EXIT_OK and rig.flash() == rig.expected_link() and rig.chip.writes == 1
              and record['owner_approved'] is True and record['owner_approved_running'] == 'not heard'
              and rig.said('was not heard naming its image') and not rig.said('reported 18.41 with the debug bit). Recorded')
              and re.search(r'"owner_approved_running":\s*"not heard"', plan_notes(rig)[0]) and states(jobs) == kept_pair,
              f'after Approve the console answered {what}: written, and recorded as not heard')
    rig, jobs = fresh(loader=DeafOnce)
    cf.LINK_KEEP_MUST_HEAR = True
    try:
        quiet = owner(rig, jobs, during=lambda: setattr(rig.chip, 'deaf', 'silent'))
        code = jobs.update_link(confirm=quiet)
    finally:
        cf.LINK_KEEP_MUST_HEAR = False
    check(code == EXIT_REFUSED and rig.said(cf.SAID_TRY_ENDED) and rig.said('nothing readable') and rig.chip.writes == 0
          and rig.flash() == sound and jobs.link_try()['owner_approved'] is False
          and jobs.link_try()['owner_approved_running'] == 'not heard'
          and states(jobs) == [('waiting', keep_title), ('closed', keep_title)],
          'with LINK_KEEP_MUST_HEAR set, a console not heard after Approve stops the update: nothing written')

    # The console switched off and on while the Approve waits. A switch-off takes the USB
    # serial port away and the console then starts from its flash; with a game started it is silent, so the
    # version read after Approve cannot tell that from the new image. The wait is therefore watched through
    # Windows' list of COM ports alone (KeepWatch): one sequence is allowed, 0x0100 ... away ... 0x0101 (stock
    # firmware starting, which the prompt asks for), and everything else ends the update with nothing written.
    def watch_of(seen_looks, reenum=8):
        now = [0.0]
        seen = cf.KeepWatch(reenum, clock=lambda: now[0])
        for look in seen_looks:
            seen.see(look)
            now[0] += 1.0
        return seen
    for seen_looks, why, settled in (
            (['fresh'] * 5, None, True),
            (['fresh', 'absent', 'absent', 'stock', 'stock'], None, True),  # stock firmware starts: the one allowed absence
            (['fresh', 'stock'], None, True),                               # the same, its absence between two looks
            (['stock'] * 3, None, True),
            (['there'] * 3, None, True),
            (['fresh', 'absent'], None, False),                             # away right now: not settled
            (['fresh'] + ['absent'] * 9, None, False),
            (['fresh'] + ['absent'] * 10, 'stayed away', True),
            (['absent'], 'left', True),
            (['fresh', 'absent', 'fresh'], 'configured again', True),       # off and on, back at the start-up menu
            (['fresh', 'stock', 'fresh'], 'configured again', True),
            (['fresh', 'absent', 'stock', 'absent'], 'left', True),         # off after a game had started
            (['stock', 'absent'], 'left', True),
            (['there', 'absent'], 'left', True),                            # no product ID to go by: no absence allowed
            (['fresh', 'there'], 'odd', True),
            (['there', 'stock'], 'odd', True),
            (['fresh', 'odd'], 'odd', True),
            (['fresh', 'nonsense'], 'odd', True),
            (['absent', 'fresh', 'stock'], 'left', True)):                  # latched: nothing seen later undoes it
        seen = watch_of(seen_looks)
        check(seen.why == why and seen.settled() is settled, f'the USB watch on {seen_looks}: {why}, settled {settled}')
    check(cf.FpgaTiming().keep_poll <= 0.5 and 0 < cf.FpgaTiming().keep_reenum <= 10 and cf.FpgaTiming().keep_join > 0,
          'the watch looks at least twice a second and allows stock firmware a few seconds to come back')

    class Listed:
        def __init__(self, found):
            self.found = found

        def serial_pids(self):
            if isinstance(self.found, Exception):
                raise self.found
            return self.found

    class NoIds:
        def __init__(self, there):
            self.there = there

        def present(self):
            return {'serial': self.there, 'gwu2x': 1}
    rig, jobs = fresh()
    for loader, look in ((Listed(None), 'absent'), (Listed([]), 'absent'), (Listed(OSError('no list')), 'absent'),
                         (Listed([0x0100]), 'fresh'), (Listed([0x0101]), 'stock'), (Listed([0x0102]), 'odd'),
                         (Listed([0x0100, 0x0101]), 'odd'), (Listed([0x0200]), 'odd'), (NoIds(True), 'there'),
                         (NoIds(False), 'absent'), (None, 'absent')):
        jobs.loader = loader
        check(jobs._console_usb_look() == look,
              f'one look at the USB list ({getattr(loader, "found", getattr(loader, "there", None))!r}): {look}')
    jobs.loader = None

    def watched(loader=None, reenum=5):
        rig, jobs = fresh(loader=loader)
        rig.timing.keep_poll, rig.timing.keep_reenum = 0.01, reenum
        return rig, jobs

    def looks(rig, jobs, count=3):
        """The owner's hands are slower than the watch: wait until it looked `count` more times at
        the list of COM ports (or has latched, after which it looks no more)."""
        target, deadline = rig.chip.usb_looks + count, time.monotonic() + 20
        while rig.chip.usb_looks < target:
            if jobs._keep_seen.why is not None:
                return
            check(time.monotonic() < deadline, 'the USB watch keeps looking while the Approve waits')
            time.sleep(0.002)

    def switch(rig, jobs, **state):
        """One thing done to the console while the Approve waits, and seen by the watch."""
        for name, value in state.items():
            setattr(rig.chip, name, value)
        looks(rig, jobs)

    def off_and_on_seen(rig, jobs):
        switch(rig, jobs, usb_off=True)                                         # switched off ...
        switch(rig, jobs, sram=None, configured=True, player=0, usb_off=False)  # ... and on: the flash image, at its menu

    def game_starts(rig, jobs):
        switch(rig, jobs, usb_off=True)                                         # the product ID changes: away ...
        switch(rig, jobs, player=1, usb_off=False)                              # ... and back as 0x0101

    def nothing_written(rig, jobs, asked, what):
        said = '\n'.join(rig.lines)
        record = jobs.link_try()
        check(f'{cf.SAID_SWITCHED_OFF} while the new image waited for your Approve' in said
              and 'nothing was written' in said and 'Next: press Update the Pocket link again' in said
              and 'Leave the console switched on and plugged in until this app says Done' in said
              and 'Step 4 of 5' not in said and cf.SAID_KEPT not in said and cf.SAID_LINK_FROM_FLASH not in said,
              f'{what}: said as switched off, nothing written, with the next step')
        check(len(asked.asked) == 1 and asked.asked[0]['running_from_ram'] and rig.chip.writes == 0 and rig.chip.reads == 1
              and rig.flash() == sound and not jobs.pending() and rig.chip.resets > asked.asked[0]['resets']
              and rig.chip.version_reads == asked.asked[0]['version_reads']
              and states(jobs) == [('waiting', keep_title), ('closed', keep_title)] and not jobs._keep_watch.is_alive(),
              f'{what}: nothing written, the flash not read again, the console not even asked for its version, the '
              'chip restarted from its flash, the sign taken down, the watch ended')
        check(record['confirmed_by'] == 'auto' and record['owner_approved'] is False
              and record['owner_approved_running'] == cf.HEARD_SWITCHED_OFF
              and jobs.link_try_confirmed(fingerprint, accept_auto=True) is None
              and jobs.write_link(accept_auto=True) == EXIT_REFUSED and rig.chip.writes == 0,
              f'{what}: recorded as not approved (switched off), and the write stays shut')

    # Off and on during the prompt, then a game (stock firmware, silent), then Approve, and
    # the version read hears nothing. Before the watch this wrote 18.41 over an image nobody had looked at.
    rig, jobs = watched()

    def cycle_then_silent_game():
        looks(rig, jobs)
        off_and_on_seen(rig, jobs)
        game_starts(rig, jobs)
        rig.chip.reported = None
    cycled = owner(rig, jobs, during=cycle_then_silent_game)
    check(jobs.update_link(confirm=cycled) == EXIT_REFUSED and jobs._keep_seen.why == 'configured again'
          and rig.said('USB port was seen: configured again') and rig.said('the console is as it was'),
          'off and on during the prompt, then a silent game, then Approve: stopped (the port came back as after a '
          'switch-on)')
    nothing_written(rig, jobs, cycled, 'off and on, then a silent game')
    # The same from inside a game: the port leaves a second time.
    rig, jobs = watched()

    def game_then_cycle():
        looks(rig, jobs)
        game_starts(rig, jobs)
        switch(rig, jobs, usb_off=True)
        switch(rig, jobs, sram=None, configured=True, player=1, usb_off=False)  # ... and straight back into a game
        rig.chip.reported = None
    cycled = owner(rig, jobs, during=game_then_cycle)
    check(jobs.update_link(confirm=cycled) == EXIT_REFUSED and jobs._keep_seen.why == 'left',
          'a game started, then off and on, then Approve: stopped (the port left after stock firmware had started)')
    nothing_written(rig, jobs, cycled, 'a game, then off and on')
    # Switched off and left off (or unplugged) when Approve is pressed: waited for keep_reenum, then stopped; the
    # restart cannot be seen, so the next step is to switch it on.
    rig, jobs = watched(reenum=0.2)

    def left_off():
        looks(rig, jobs)
        rig.chip.usb_off = True
    gone = owner(rig, jobs, during=left_off)
    check(jobs.update_link(confirm=gone) == EXIT_REFUSED and jobs._keep_seen.why in ('stayed away', 'left')
          and rig.said(cf.SAID_SWITCHED_OFF) and rig.said('Switch the console off and on: it starts from that image, as before')
          and rig.chip.writes == 0 and rig.flash() == sound and not jobs.pending()
          and jobs.link_try()['owner_approved'] is False
          and jobs.link_try()['owner_approved_running'] == cf.HEARD_SWITCHED_OFF
          and states(jobs) == [('waiting', keep_title), ('closed', keep_title)],
          'switched off and left off at the Approve: nothing written, told to switch it on')
    rig.chip.usb_off = False
    # A list of COM ports that could not be read for a moment counts as the port being away, and the port then
    # showing 0x0100 again as a switch-on: the try is simply run again.
    rig, jobs = watched()

    def hiccup():
        looks(rig, jobs)
        switch(rig, jobs, usb_unreadable=True)
        switch(rig, jobs, usb_unreadable=False)
    blind = owner(rig, jobs, during=hiccup)
    check(jobs.update_link(confirm=blind) == EXIT_REFUSED and rig.said('check the USB cable'),
          'the USB list unreadable for a moment while the Approve waits: stopped')
    nothing_written(rig, jobs, blind, 'an unreadable USB list')
    # A list that does not answer at all: the watch is not waited for beyond keep_join, and that stops the update.
    hang = threading.Event()

    class Hanging(FakeLoader):
        def serial_pids(self):
            hang.wait(20)
            return super().serial_pids()
    rig, jobs = watched(loader=Hanging)
    rig.timing.keep_join = 0.2
    stuck = owner(rig, jobs)
    code = jobs.update_link(confirm=stuck)
    hang.set()
    jobs._keep_watch.join(20)
    check(code == EXIT_REFUSED and jobs._keep_seen.why == 'no answer from the USB list' and rig.said(cf.SAID_SWITCHED_OFF)
          and rig.chip.writes == 0 and rig.flash() == sound and rig.chip.sram is None and not jobs.pending()
          and jobs.link_try()['owner_approved'] is False and not jobs._keep_watch.is_alive(),
          'a USB list that does not answer: nothing written')
    # Not right after an off-and-on (what the prompt now asks for): not kept, as ever.
    rig, jobs = watched()

    def only_cycle():
        looks(rig, jobs)
        off_and_on_seen(rig, jobs)
    check(jobs.update_link(confirm=owner(rig, jobs, answer='', during=only_cycle)) == EXIT_REFUSED
          and rig.said(f'{cf.SAID_NOT_KEPT}: you did not approve') and not rig.said(cf.SAID_SWITCHED_OFF)
          and rig.chip.writes == 0 and rig.flash() == sound and jobs.link_try()['owner_approved'] is False,
          'Not right after an off-and-on: not kept, nothing written')
    # What must still work: a game started as the prompt asks (the port leaves once and comes back as 0x0101),
    # never switched off, stock firmware silent or the port busy: written, recorded as not heard.
    for what in ('silent', 'busy', 'none'):
        rig, jobs = watched(loader=DeafOnce)

        def game_only():
            looks(rig, jobs)
            game_starts(rig, jobs)
            rig.chip.deaf = what
        playing = owner(rig, jobs, during=game_only)
        code = jobs.update_link(confirm=playing)
        record = jobs.link_try()
        check(code == EXIT_OK and rig.flash() == rig.expected_link() and rig.chip.writes == 1
              and jobs._keep_seen.why is None and jobs._keep_seen.phase == 'stock'
              and record['owner_approved'] is True and record['owner_approved_running'] == 'not heard'
              and not rig.said(cf.SAID_SWITCHED_OFF) and states(jobs) == kept_pair and not jobs._keep_watch.is_alive(),
              f'a game started while the Approve waited (port away once, back as 0x0101), console {what}: written, '
              'recorded as not heard')
    # Approve pressed in the very moment the port is away for that game start: waited for, then the same.
    rig, jobs = watched(loader=DeafOnce)
    timers = []

    def approve_mid_start():
        looks(rig, jobs)
        rig.chip.usb_off = True
        looks(rig, jobs)
        rig.chip.deaf = 'silent'
        timers.append(threading.Timer(0.3, lambda c=rig.chip: (setattr(c, 'player', 1), setattr(c, 'usb_off', False))))
        timers[-1].start()
    code = jobs.update_link(confirm=owner(rig, jobs, during=approve_mid_start))
    timers[-1].join()
    check(code == EXIT_OK and rig.flash() == rig.expected_link() and jobs._keep_seen.phase == 'stock'
          and jobs.link_try()['owner_approved_running'] == 'not heard',
          'Approve while the port is away for the game start: waited until it is back as 0x0101, then written')
    # The watch reads the list and nothing else: no version or status read, no reset, no flash read while it waits.
    rig, jobs = watched()
    counts = []

    def snapshot():
        return (rig.chip.version_reads, rig.chip.status_reads, rig.chip.resets, rig.chip.reads, rig.chip.detects,
                rig.chip.sram_loads, rig.chip.writes, rig.flash(), rig.chip.sram is not None)

    def long_wait():
        counts.append(snapshot())
        looks(rig, jobs, 20)
        counts.append(snapshot())
    waited = owner(rig, jobs, during=long_wait)
    check(jobs.update_link(confirm=waited) == EXIT_OK and len(counts) == 2 and counts[0] == counts[1] and counts[0][-1] is True
          and jobs._keep_seen.why is None and jobs._keep_seen.phase == 'fresh' and rig.flash() == rig.expected_link(),
          'twenty looks at the USB list while the Approve waits: no serial read, no JTAG, the image still running; '
          'then Approve, written')

    # An error nobody expected (ValueError, TypeError) never escapes with the RAM image left
    # running: the chip goes back to its flash, nothing is written, its text is not said (the type only).
    for error in (ValueError('secret words'), TypeError('secret words')):
        rig, jobs = fresh()
        broken = owner(rig, jobs, answer=error)
        code = jobs.update_link(confirm=broken)
        check(code == EXIT_REFUSED and rig.said(f'an unexpected error ({type(error).__name__})')
              and rig.said('The console chip was restarted from its flash')
              and rig.said('The update stopped before Approve: nothing was written.') and not rig.said('secret words')
              and rig.chip.sram_loads == 1 and rig.chip.sram is None and rig.chip.resets > broken.asked[0]['resets']
              and rig.chip.writes == 0 and rig.chip.reads == 1 and rig.flash() == sound and not jobs.pending()
              and 'owner_approved' not in jobs.link_try() and jobs.link_try_confirmed(fingerprint, accept_auto=True) is None
              and states(jobs) == [('waiting', keep_title), ('closed', keep_title)] and not jobs._keep_watch.is_alive(),
              f'{type(error).__name__} while the Approve waits: flash untouched, chip back on its flash, no pending job')

    class BreaksInChecks(FakeLoader):
        def launcher_status(self):
            raise TypeError('secret words')

    class BreaksInTheWrite(FakeLoader):
        def dump(self, path):
            if self.c.reads >= 1:
                raise ValueError('secret words')  # the read before the write
            return super().dump(path)
    for loader, what in ((BreaksInChecks, 'in the automatic checks'), (BreaksInTheWrite, 'in the read before the write')):
        rig, jobs = fresh(loader=loader)
        code = jobs.update_link(confirm=owner(rig, jobs))
        check(code == EXIT_REFUSED and not rig.said('secret words') and rig.chip.sram_loads == 1 and rig.chip.sram is None
              and rig.chip.writes == 0 and rig.flash() == sound and not jobs.pending()
              and not rig.said(cf.SAID_LINK_FROM_FLASH) and not rig.said('Step 5 of 5'),
              f'an unexpected error {what}: flash untouched, chip back on its flash, no pending job')
    check(rig.said('an unexpected error (ValueError)') and rig.said(cf.SAID_LATE_REFUSAL),
          'an unexpected error in the read before the write: said by its type, and that the write did not start')
    rig, jobs = fresh()
    for error in (ValueError('secret words'), TypeError('secret words'), KeyError('secret words')):
        before, resets = len(rig.lines), rig.chip.resets
        jobs.loader = FakeLoader(rig.chip)
        check(jobs.guarded(lambda: (_ for _ in ()).throw(error)) == EXIT_REFUSED and rig.chip.resets == resets + 1
              and f'an unexpected error ({type(error).__name__})' in rig.lines[-1] and 'secret words' not in rig.lines[-1]
              and len(rig.lines) == before + 1, f'guarded: {type(error).__name__} is caught, the chip restarted from flash')
    try:
        jobs.guarded(lambda: (_ for _ in ()).throw(SystemExit(3)))
        check(False, 'guarded lets SystemExit through')
    except SystemExit as leaving:
        check(leaving.code == 3, 'guarded lets SystemExit through')

    # A refusal after the Approve and before any write does not leave the new image running
    # from RAM: the chip is restarted from its untouched flash, and that is said.
    for held, flash in (('media', sound), ('original', ORIGINAL)):
        rig, jobs = fresh(flash=flash)
        if held == 'original':
            check(jobs.restore_original() == EXIT_OK, 'the rehearsal, for the update over the original')
        late = owner(rig, jobs, during=lambda: setattr(
            rig, 'backup_fails', 'the second copy of the original FPGA image (other disk) differs.'))
        writes = rig.chip.writes
        code = jobs.update_link(confirm=late)
        said = '\n'.join(rig.lines)
        check(code == EXIT_REFUSED and cf.SAID_KEPT in said and 'Step 4 of 5' in said
              and 'Refused, nothing written: the second copy' in said
              and f'{cf.SAID_LATE_REFUSAL} (the line above says why): nothing was written. The flash still holds '
                  f'{cf.FLASH_WORDS_FOR[held]}, and the console chip was restarted from it: the console is as it was.' in said
              and 'Next: press Update the Pocket link again' in said and 'Step 5 of 5' not in said
              and cf.SAID_LINK_FROM_FLASH not in said and cf.SAID_LINK_NOT_FROM_FLASH not in said,
              f'a refusal after Approve (flash: {held}): said, with where the console is and the next step')
        check(len(late.asked) == 1 and late.asked[0]['running_from_ram'] and rig.chip.sram is None
              and rig.chip.resets > late.asked[0]['resets'] and rig.chip.writes == writes and rig.flash() == flash
              and not jobs.pending() and states(jobs)[-2:] == kept_pair,
              f'a refusal after Approve (flash: {held}): nothing written, the RAM image no longer running')
    rig.backup_fails = None
    # The restart not seen: the owner is told to switch the console off and on.
    class DarkAfterRestart(FakeLoader):
        def reset(self):
            super().reset()
            if getattr(self.c, 'dark', False):
                self.c.usb_off = True
    rig, jobs = fresh(loader=DarkAfterRestart)

    def refuse_and_stay_dark():
        rig.backup_fails = 'the second copy of the original FPGA image (other disk) differs.'
        rig.chip.dark = True
    check(jobs.update_link(confirm=owner(rig, jobs, during=refuse_and_stay_dark)) == EXIT_REFUSED
          and rig.said(cf.SAID_LATE_REFUSAL) and rig.said('Switch the console off and on: it starts from that image, as before')
          and not rig.said('the console is as it was') and rig.chip.sram is None
          and rig.chip.writes == 0 and rig.flash() == sound and not jobs.pending(),
          'a refusal after Approve with the restart not seen: told to switch the console off and on')
    rig.chip.usb_off = False

    # After a failed run the image was loaded minutes ago: the next press needs the console switched off and
    # on first, asked as an owner action (once), watched. Not done: timed out, nothing loaded. Done: it goes on.
    rig, jobs = fresh()
    rig.chip.phase = 'boot'
    yes = owner(rig, jobs)
    check(jobs.update_link(confirm=yes) == EXIT_REFUSED and rig.chip.sram_loads == 1, 'a run that failed its menu check')
    failed = jobs.link_try()
    before = len(rig.lines)
    code = jobs.update_link(confirm=yes)
    said = '\n'.join(rig.lines[before:])
    check(code == EXIT_REFUSED and states(jobs) == [('waiting', title), ('timeout', title)]
          and jobs.owner.history[0]['seconds'] == rig.timing.boot and jobs.owner.history[0]['instruction'] == cf.POWER_CYCLE_TEXT
          and jobs.owner.history[0]['kind'] == 'action' and jobs.owner.history[1]['next'] == cf.POWER_CYCLE_MISSED,
          'pressed again without touching the console: one owner action with its time, then timed out with the next step')
    check('Switch the console OFF and ON now' in said and 'stopped before the image was tried' in said
          and rig.chip.sram_loads == 1 and rig.chip.writes == 0 and rig.flash() == sound and jobs.link_try() == failed
          and cf.SAID_AUTO_FAILED not in said and 'Step 3 of 5' not in said and yes.asked == [],
          'the missed power cycle: still said in the log, nothing loaded, nothing asked, nothing written, the old record kept')
    rig.chip.phase = 'menu'
    responds(rig, jobs)
    before = len(rig.lines)
    code = jobs.update_link(confirm=yes)
    said = '\n'.join(rig.lines[before:])
    check(code == EXIT_OK and rig.flash() == rig.expected_link() and rig.chip.sram_loads == 2 and rig.chip.writes == 1
          and states(jobs)[2:] == cycle_pair + kept_pair
          and jobs.owner.history[3]['note'] == 'Your Chromatic was switched off and on.'
          and 'switched off and on (its USB serial port went away and came back)' in said
          and cf.SAID_AUTO_PASSED in said and cf.SAID_LINK_FROM_FLASH in said and len(yes.asked) == 1,
          'the owner switches it off and on when asked: one sign, done, then tried, checked, approved and written')

    # What the flash holds decides before anything is loaded.
    rig, jobs = fresh(flash=ORIGINAL)
    yes = owner(rig, jobs)
    check(jobs.update_link(confirm=yes) == EXIT_REFUSED and rig.said(cf.SAID_UPDATE_NEEDS_SOUND) and rig.chip.sram_loads == 0
          and rig.chip.writes == 0 and rig.flash() == ORIGINAL and jobs.link_try() is None and yes.asked == [],
          'over the original without a rehearsal in this session: refused, told to set up sound first')
    check(jobs.restore_original() == EXIT_OK and jobs.update_link(confirm=yes) == EXIT_OK
          and rig.flash() == rig.expected_link() and rig.chip.sram_loads == 1 and rig.chip.writes == 2
          and yes.asked[0]['prompt'] == cf.LINK_KEEP_PROMPT + cf.keep_text('original'),
          'over the original after the rehearsal: updated, the Approve naming the original as what stays otherwise')
    rig, jobs = fresh()
    rig.chip.flash[100] ^= 0xff
    unknown = rig.flash()
    yes = owner(rig, jobs)
    check(jobs.update_link(confirm=yes) == EXIT_REFUSED and rig.said(cf.SAID_UPDATE_UNKNOWN) and rig.chip.sram_loads == 0
          and rig.chip.writes == 0 and rig.flash() == unknown and yes.asked == [],
          'an unknown flash: nothing loaded, nothing asked, nothing written')
    rig, jobs = fresh()
    rig.chip.boot_fails = True
    check(jobs.update_link(confirm=owner(rig, jobs)) == EXIT_REFUSED
          and rig.said('The update stopped here: nothing was loaded or written')
          and rig.chip.sram_loads == 0 and rig.chip.writes == 0, 'the console not back after the first read: stopped there')

    # The image itself: every refusal of the pinned image stands, before the console is touched.
    spoils = (('NEVER-INSTALL.txt in the build folder', lambda r: r.link_marker(True), 'unreviewed'),
              ('NEVER-INSTALL.txt at the top', lambda r: r.link_marker(True, 'top'), 'unreviewed'),
              ('an image that is not the pinned one', lambda r: r.link_pins.__setitem__('fs', '0' * 64),
               'not the approved 18.41 stream image'),
              ('a stage receipt that is not the pinned one', lambda r: r.link_pins.__setitem__('stage', '0' * 64),
               'stage receipt'),
              ('a backup that fails its check', lambda r: setattr(
                  r, 'backup_fails', 'the second copy of the original FPGA image (other disk) differs.'), 'second copy'))
    for what, spoil, words in spoils:
        rig, jobs = fresh()
        spoil(rig)
        yes = owner(rig, jobs)
        check(jobs.update_link(confirm=yes) == EXIT_REFUSED and rig.said(words) and not rig.said('Step 1 of 5')
              and rig.chip.detects == 0 and rig.chip.reads == 0 and rig.chip.sram_loads == 0 and rig.chip.writes == 0
              and jobs.owner.history == [] and yes.asked == [], f'{what}: refused before the console is touched')
    rig, jobs = fresh()
    yes = owner(rig, jobs)
    check(jobs.update_link(approved_fingerprint='', confirm=yes) == EXIT_REFUSED
          and rig.said('not the one approved in the window') and rig.chip.detects == 0 and yes.asked == [],
          'started with another image approved: refused before the console is touched')
    check(jobs.update_link(approved_fingerprint=fingerprint, confirm=yes) == EXIT_OK and rig.flash() == rig.expected_link(),
          'started with this image approved: updated')
    # An unfinished console chip job comes first.
    rig, jobs = fresh(flash=ORIGINAL)
    real_write = FakeLoader.write

    def always_bad(self, source):
        real_write(self, source)
        self.c.flash[7] ^= 0xff
    FakeLoader.write = always_bad
    try:
        check(jobs.restore_original() == EXIT_MANUAL and jobs.pending(), 'a stuck console chip job is pending')
    finally:
        FakeLoader.write = real_write
    reads, before = rig.chip.reads, len(rig.lines)
    check(jobs.update_link(confirm=owner(rig, jobs)) == EXIT_REFUSED
          and 'unfinished console chip job' in '\n'.join(rig.lines[before:])
          and rig.chip.reads == reads and rig.chip.sram_loads == 0, 'an unfinished job: the update is refused before a read')
    rigs.remove(rig)  # its lines name the manual ladder; checked for leaks by failure_checks

    # Owner Stop: at the try's last moment nothing is loaded; while the Approve waits nothing is written and the
    # chip goes back to its flash; at the write's last moment nothing is written either.
    class Stopped(KeyboardInterrupt):
        owner_stop = True
    rig, jobs = fresh()
    rig.stop = lambda: (_ for _ in ()).throw(Stopped())
    yes = owner(rig, jobs)
    check(jobs.update_link(confirm=yes) == EXIT_REFUSED and rig.said('Stopped before any write')
          and rig.said('stopped before the image was tried') and rig.chip.sram_loads == 0 and rig.chip.writes == 0
          and rig.flash() == sound and jobs.link_try() is None and yes.asked == [],
          'Stop before the load: nothing loaded, nothing asked, nothing written')
    rig, jobs = fresh()
    stopped = owner(rig, jobs, answer=Stopped())
    check(jobs.update_link(confirm=stopped) == EXIT_REFUSED and rig.said('Stopped before any write: cancelled.')
          and rig.said('The update stopped before Approve: nothing was written.') and rig.said('the console is as it was')
          and rig.chip.sram_loads == 1 and rig.chip.sram is None and rig.chip.writes == 0 and rig.chip.reads == 1
          and rig.flash() == sound and not jobs.pending() and 'owner_approved' not in jobs.link_try()
          and jobs.link_try_confirmed(fingerprint, accept_auto=True) is None
          and states(jobs) == [('waiting', keep_title), ('closed', keep_title)] and not rig.said('Step 4 of 5'),
          'Stop while the Approve waits: the sign taken down, the chip back on its flash, nothing written, no approval')
    rig, jobs = fresh()
    calls = [0]

    def stop_at_the_write():
        calls[0] += 1
        if calls[0] == 2:
            raise Stopped()
    rig.stop = stop_at_the_write
    yes = owner(rig, jobs)
    check(jobs.update_link(confirm=yes) == EXIT_REFUSED and rig.chip.sram_loads == 1 and rig.chip.writes == 0
          and rig.flash() == sound and not jobs.pending() and rig.said(cf.SAID_AUTO_PASSED) and rig.said(cf.SAID_KEPT)
          and not rig.said('Step 5 of 5') and len(yes.asked) == 1 and rig.chip.sram is None
          and rig.said(cf.SAID_LATE_REFUSAL) and rig.said('the console is as it was'),
          'Stop at the write\'s last moment: approved, but nothing written, no claim, the chip back on its flash')
    responds(rig, jobs)
    before = len(rig.lines)
    code = jobs.update_link(confirm=yes)
    said = '\n'.join(rig.lines[before:])
    check(code == EXIT_OK and rig.flash() == rig.expected_link() and rig.chip.sram_loads == 2 and rig.chip.writes == 1
          and len(yes.asked) == 2 and yes.asked[1]['running_from_ram'] and states(jobs)[2:] == cycle_pair + kept_pair,
          'pressed again: the earlier Approve is not reused: loaded again, asked again with the image running, written')

    # A write that fails is put right exactly as write_link does it, and is never called a success.
    for what, fault in (('read-back mismatch', lambda c: setattr(c, 'corrupt_after_write', 5000)),
                        ('power cut in the write', lambda c: setattr(c, 'cut_write_after', 1000))):
        rig, jobs = fresh()
        fault(rig.chip)
        yes = owner(rig, jobs)
        check(jobs.update_link(confirm=yes) == EXIT_RECOVERED and rig.flash() == ORIGINAL and not jobs.pending()
              and rig.said('undone (your original image is back)') and jobs.last_known() == 'original'
              and not rig.said(cf.SAID_LINK_FROM_FLASH) and not rig.said(cf.SAID_LINK_NOT_FROM_FLASH)
              and not rig.said('Step 5 of 5') and not rig.said('installed: all 4 MiB read back'),
              f'{what} in the one-step write: the original is back, no claim that the Pocket link runs')
    responds(rig, jobs)
    before = len(rig.lines)
    code = jobs.update_link(confirm=yes)  # the recovery was the rehearsal; the try is made and approved afresh
    said = '\n'.join(rig.lines[before:])
    check(code == EXIT_OK and rig.flash() == rig.expected_link() and rig.chip.sram_loads == 2 and len(yes.asked) == 2
          and yes.asked[1]['prompt'] == cf.LINK_KEEP_PROMPT + cf.keep_text('original'),
          'after the undone write: pressed again, tried and approved again, written over the original (the recovery '
          'was the rehearsal)')
    rig, jobs = fresh()
    rig.chip.cut_at_read = 3  # the check's read, the read before the write, then the read-back
    check(jobs.update_link(confirm=owner(rig, jobs)) == EXIT_RECOVERED and rig.flash() == rig.expected_link()
          and not jobs.pending() and rig.said('console chip job was finished') and jobs.last_known() == 'link'
          and not rig.said(cf.SAID_LINK_FROM_FLASH) and not rig.said(cf.SAID_LINK_NOT_FROM_FLASH)
          and not rig.said('Step 5 of 5'), 'power cut in the read-back: finished by the recovery, reported as recovered')
    before = len(rig.lines)
    code = jobs.update_link()
    check(code == EXIT_OK and rig.chip.writes == 1 and cf.SAID_LINK_FROM_FLASH in '\n'.join(rig.lines[before:]),
          'then pressed again: the flash holds it, only the last check, confirmed from flash')

    # After the write the console must report 18.41 from its flash. When it does not, one watched power cycle
    # is asked for (an owner action); never a second one in the same run.
    class QuietAfterWrite(FakeLoader):
        """The image is written and read back, but the start-up menu's version line does not come."""
        def write(self, source):
            super().write(source)
            self.c.reported = None

    class BusyAfterWrite(FakeLoader):
        def write(self, source):
            super().write(source)
            self.c.reported = 'busy'
    rig, jobs = fresh(loader=QuietAfterWrite)
    responds(rig, jobs, also=lambda: setattr(rig.chip, 'reported', ...))
    check(jobs.update_link(confirm=owner(rig, jobs)) == EXIT_OK and rig.flash() == rig.expected_link()
          and rig.chip.writes == 1 and states(jobs) == kept_pair + cycle_pair and rig.said(cf.SAID_LINK_FROM_FLASH)
          and rig.said(' so far.') and not rig.said(cf.SAID_LINK_NOT_FROM_FLASH),
          'no report after the write: one power cycle asked, done, then confirmed from flash')
    rig, jobs = fresh(loader=QuietAfterWrite)
    check(jobs.update_link(confirm=owner(rig, jobs)) == cf.EXIT_NOT_SEEN and rig.flash() == rig.expected_link()
          and rig.chip.writes == 1 and states(jobs) == kept_pair + [('waiting', title), ('timeout', title)]
          and 'it only checks' in jobs.owner.history[-1]['next']
          and rig.said(cf.SAID_LINK_NOT_FROM_FLASH) and not rig.said(cf.SAID_LINK_FROM_FLASH)
          and jobs.last_known() == 'link' and not jobs.pending(),
          'no report and the power cycle missed: written and read back, said as not heard from yet, never as confirmed')
    rig.chip.reported = ...
    before = len(rig.lines)
    code = jobs.update_link()
    said = '\n'.join(rig.lines[before:])
    check(code == EXIT_OK and rig.chip.writes == 1 and rig.chip.sram_loads == 1 and 'Steps 2 to 4 are not needed' in said
          and cf.SAID_LINK_FROM_FLASH in said and len(jobs.owner.history) == 4,
          'pressed again later: it only checks, and confirms it from flash')
    rig, jobs = fresh(loader=QuietAfterWrite)
    check(jobs.check() == EXIT_OK and jobs.try_link() == EXIT_OK, 'a step-by-step try, not answered')
    responds(rig, jobs)
    check(jobs.update_link(confirm=owner(rig, jobs)) == cf.EXIT_NOT_SEEN and rig.flash() == rig.expected_link()
          and rig.chip.sram_loads == 2 and states(jobs) == cycle_pair + kept_pair and rig.said(cf.SAID_LINK_NOT_FROM_FLASH)
          and rig.said('Switch the console off and on, then press Update the Pocket link again'),
          'the power cycle was already asked before the load: not asked a second time, the next step said instead')
    rig, jobs = fresh(loader=BusyAfterWrite)
    check(jobs.update_link(confirm=owner(rig, jobs)) == cf.EXIT_NOT_SEEN and rig.flash() == rig.expected_link()
          and states(jobs) == kept_pair and rig.said('Close that program, then press Update the Pocket link again'),
          'the port held by another program after the write: no power cycle asked, told to close it')
    for r in rigs:
        r.no_leaks()


def version_line_checks():
    """The start-up menu's version line: parsed only whole, same pattern as the Manager's status
    check; and JtagLoader's read (1.3.5 Manager): DTR/RTS low before the port opens (opening must
    not reset the ESP32 into its ROM loader); first PASSIVE (launcher 1.3.5 says the line every 5 s:
    no RTS pulse at all); only without a line one RTS pulse (EN low, then high) with esptool's
    Windows workaround (DTR written after every RTS change, or usbser.sys may not send it); the
    port closed; 'busy' when the port cannot be opened; 'download' when the ROM answers the restart.
    A fake serial module: no device."""
    import types
    import chromatic_manager as cm
    check(cf.FPGA_VERSION_LINE.pattern == cm.FPGA_LINE.pattern, 'same version line pattern as the Manager status')
    check(cf.boot_fpga_version(b'x\r\nPJ-DIAG t=22 fpga version=18.41 debug=1\r\nPJ-DIAG t=30 wifi') == (18, 41, 1)
          and cf.boot_fpga_version(b'PJ-DIAG t=22 fpga version=18.4') is None
          and cf.boot_fpga_version(b'PJ-DIAG t=9 fpga version=none\n') == 'none' and cf.boot_fpga_version(None) is None,
          'the version line parses only when whole')
    check(cf.LINK_RUNNING == (18, 41, 1), '18.41 with the debug bit')

    events = []

    console = {'passive': [b'garbage', b'PJ-DIAG t=8022 fpga ver', b'sion=18.41 debug=1\r\n'],  # a 1.3.5 launcher's 5 s line
               'boot': [], 'refuse_open': False}

    class Port:
        def __init__(self):
            self._dtr = self._rts = True  # pyserial's defaults: both asserted
            self.port, self.baudrate, self.timeout, self.is_open = None, None, None, False
            self.feed = list(console['passive'])
            self.pulsed = False  # EN was pulled low: the chip restarts and prints its boot lines

        dtr = property(lambda s: s._dtr, lambda s, v: (setattr(s, '_dtr', v), events.append(('dtr', v, s.is_open))))

        def _set_rts(self, v):
            self._rts = v
            events.append(('rts', v, self.is_open))
            if v and self.is_open:
                self.pulsed, self.feed = True, list(console['boot'])
        rts = property(lambda s: s._rts, _set_rts)

        def open(self):
            events.append(('open', self._dtr, self._rts))
            if console.get('busy_opens', 0) > 0:
                console['busy_opens'] -= 1
                raise OSError('PermissionError: the port is held by another program')
            if console['refuse_open']:
                raise OSError('PermissionError: the port is held by another program')
            self.is_open = True

        def reset_input_buffer(self):
            pass

        def read(self, n):
            return self.feed.pop(0) if self.feed and not self._rts else b''

        def close(self):
            events.append(('close',))
            self.is_open = False

    class Info:
        def __init__(self, vid, pid):
            self.device, self.vid, self.pid = 'COM9', vid, pid
    found = [Info(cf.SERIAL_VID, 0x0100)]
    serial_mod = types.ModuleType('serial')
    serial_mod.Serial = Port
    tools = types.ModuleType('serial.tools')
    lp = types.ModuleType('serial.tools.list_ports')
    lp.comports = lambda: list(found)
    serial_mod.tools, tools.list_ports = tools, lp
    saved = {k: sys.modules.get(k) for k in ('serial', 'serial.tools', 'serial.tools.list_ports')}
    sys.modules.update({'serial': serial_mod, 'serial.tools': tools, 'serial.tools.list_ports': lp})
    try:
        timing = cf.FpgaTiming()
        timing.version, timing.passive = 2, 0.5
        loader = cf.JtagLoader(Path(tempfile.gettempdir()), timing=timing)
        # A 1.3.5 launcher: its 5 s line is read as it comes; nothing is restarted.
        check(loader.launcher_fpga_version() == (18, 41, 1) and loader.last_version_read == 'passive',
              'the version read finds 18.41 in the console\'s own status line, passively')
        opened = [e for e in events if e[0] == 'open']
        rts_open = [e[1] for e in events if e[0] == 'rts' and e[2]]
        check(opened == [('open', False, False)] and rts_open == [] and events[-1] == ('close',),
              'DTR and RTS low before opening, NO RTS pulse (passive), the port closed')
        # The one-step update's menu check: the launcher's own status line (every second), only listened to.
        timing.menu = 0.3
        events.clear()
        console['passive'] = [b'PJ-DIAG t=4012 stats phase=boot uart=3 msgs=0 polls=1\r\nPJ-DIAG t=5012 stats pha',
                              b'se=menu uart=412 msgs=9 polls=3\r\n', b'PJ-DIAG t=6012 stats phase=boot uart=0 msgs=0 x\r\n']
        check(loader.launcher_status() == {'phase': 'menu', 'uart': 412, 'msgs': 9},
              'the status read stops at the first line that names a menu phase, split across reads or not')
        check([e for e in events if e[0] == 'open'] == [('open', False, False)] and events[-1] == ('close',)
              and not [e for e in events if e[0] in ('rts', 'dtr') and e[2]],
              'the status read: DTR and RTS low before opening, no line changed while open (nothing restarted), closed')
        events.clear()
        console['passive'] = [b'PJ-DIAG t=4012 stats phase=boot uart=3 msgs=0 polls=1\r\n']
        check(loader.launcher_status() == {'phase': 'boot', 'uart': 3, 'msgs': 0} and events[-1] == ('close',),
              'a menu that never leaves its start phase: that phase is returned when the time runs out')
        events.clear()
        console['passive'] = [b'PJ-DIAG t=8022 fpga version=18.41 debug=1\r\n']
        check(loader.launcher_status() is None and events[-1] == ('close',), 'no status line: None')
        events.clear()
        console['refuse_open'] = True
        check(loader.launcher_status() is None and not [e for e in events if e[0] in ('rts', 'dtr') and e[2]],
              'the status read on a port held by another program: None, nothing pulsed')
        console['refuse_open'] = False
        # An older launcher says nothing by itself: one RTS pulse, each RTS change followed by a DTR write (esptool's
        # usbser.sys workaround), then its boot line.
        events.clear()
        console['passive'] = [b'PJ-DIAG t=5000 stats phase=menu\r\n']
        console['boot'] = [b'rst:0x1 (POWERON_RESET)\r\nPJ-LAUNCHER-READY 1.3.4\r\nPJ-DIAG t=22 fpga ver', b'sion=18.41 debug=1\r\n']
        check(loader.launcher_fpga_version() == (18, 41, 1) and loader.last_version_read == 'restart',
              'without the passive line the start-up menu is restarted and its boot line read')
        line_events = [e[:2] for e in events if e[0] in ('rts', 'dtr') and e[2]]
        check(line_events == [('rts', True), ('dtr', False), ('rts', False), ('dtr', False)] and events[-1] == ('close',),
              'one RTS pulse, DTR (deasserted) written after each RTS change, the port closed')
        # The restart lands in the ROM download mode (IO0 low: a serial program's DTR): said, not "nothing readable".
        events.clear()
        console['boot'] = [b'rst:0x1 (POWERON_RESET),boot:0x3 (DOWNLOAD_BOOT)\r\nwaiting for download\r\n']
        check(loader.launcher_fpga_version() == 'download' and loader.last_version_read == 'restart',
              'the ROM\'s "waiting for download" is reported as download mode')
        # The port held by another program: 'busy', nothing pulsed.
        events.clear()
        console['refuse_open'] = True
        check(loader.launcher_fpga_version() == 'busy' and not [e for e in events if e[0] == 'rts' and e[2]],
              'a port that cannot be opened: busy, no pulse')
        console['refuse_open'] = False
        # Nothing at all after the restart: None.
        events.clear()
        console['boot'] = []
        check(loader.launcher_fpga_version() is None and loader.last_version_read == 'restart', 'no line either way: None')
        events.clear()
        found.append(Info(cf.SERIAL_VID, 0x0101))
        check(loader.launcher_fpga_version() is None and not events, 'two consoles: nothing opened, nothing read')
        found[:] = []
        check(loader.launcher_fpga_version() is None, 'no console port: None')
        # The status read gives a busy or late port the version read's window (timing.passive),
        # so the menu check cannot fail on a port that another reader had only just let go.
        status = {'phase': 'menu', 'uart': 412, 'msgs': 9}
        status_line = [b'PJ-DIAG t=5012 stats phase=menu uart=412 msgs=9 polls=3\r\n']
        found[:] = [Info(cf.SERIAL_VID, 0x0100)]
        timing.passive, timing.poll, timing.menu = 5, 0.02, 0.3
        events.clear()
        console['passive'], console['busy_opens'] = list(status_line), 2
        check(loader.launcher_status() == status and len([e for e in events if e[0] == 'open']) == 3
              and not [e for e in events if e[0] in ('rts', 'dtr') and e[2]] and events[-1] == ('close',),
              'the status read on a port busy at first: tried again, read at the third open, nothing pulsed, closed')
        found[:] = []
        events.clear()
        late = threading.Timer(0.3, lambda: found.append(Info(cf.SERIAL_VID, 0x0101)))
        late.start()
        check(loader.launcher_status() == status and [e for e in events if e[0] == 'open'] == [('open', False, False)],
              'the status read on a port that is not there yet: waited for, then read')
        late.join()
        found[:] = []
        events.clear()
        timing.passive = 0.3
        began = time.monotonic()
        check(loader.launcher_status() is None and time.monotonic() - began >= 0.3 and not events,
              'the status read with no console port for the whole window: None, nothing opened')
        found[:] = [Info(cf.SERIAL_VID, 0x0100)]
        console['refuse_open'] = True
        began = time.monotonic()
        check(loader.launcher_status() is None and time.monotonic() - began >= 0.3
              and len([e for e in events if e[0] == 'open']) >= 2 and not [e for e in events if e[0] in ('rts', 'dtr') and e[2]],
              'the status read on a port busy for the whole window: None after trying again, nothing pulsed')
        console['refuse_open'] = False
        # The one Approve's watch: the list of COM ports only. Nothing is opened, nothing is sent.
        found[:] = [Info(cf.SERIAL_VID, 0x0101), Info(0x1234, 0x0101), Info(cf.SERIAL_VID, None)]
        events.clear()
        check(loader.serial_pids() == [0x0101] and loader.serial_present() is True and not events,
              'the console\'s product IDs come from the list of COM ports alone: no port opened')
        found[:] = []
        check(loader.serial_pids() == [] and loader.serial_present() is False and not events, 'no console listed: none')

        def unreadable():
            raise OSError('the list cannot be read')
        lp.comports = unreadable
        check(loader.serial_pids() is None and loader.serial_present() is False and not events,
              'a list of COM ports that cannot be read: None (counted as not there), nothing opened')
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def core_checks():
    """The Manager core dispatches the FPGA keys, gates them on an unfinished job, and under the
    AI grant passes only the approved image; the usage gate blocks AI-started writes."""
    import chromatic_manager_core as core
    import chromatic_manager as cm
    from test_chromatic_manager import Harness, FAST  # noqa: F401  (sandboxed manager; no private files)
    from compose_chromatic_image import stock_dump
    check({'fpga_check', 'fpga_restore', 'fpga_media'} <= set(core.WRITE_KEYS)
          and all(core.BY_KEY[k].tab == 'recovery' and core.BY_KEY[k].confirm for k in core.FPGA_KEYS),
          'FPGA jobs are approved writes on the Fix a problem tab')
    rig = Rig()

    class Lock:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False
    stock = stock_dump(allow_synthetic=True)
    h = Harness(stock, cm.Release.load(repin=False))
    manager = h.manager()
    manager.say = rig.lines.append
    fpga = rig.jobs()
    asked, answer = [], ['']

    def ask(prompt):
        asked.append(prompt)
        return answer[0]
    run = lambda key, **kw: core.run_action(cm, manager, key, ask, lambda p: '', Lock(), fpga=fpga, **kw)
    check(run('fpga_media') == EXIT_REFUSED and rig.said('has not succeeded in this session yet'), 'dispatch: sound before rehearsal refused')
    check(run('fpga_restore') == EXIT_OK and rig.flash() == ORIGINAL, 'dispatch: restore')
    check(run('fpga_check') == EXIT_OK, 'dispatch: check')
    check(run('fpga_trial') == EXIT_OK and rig.chip.sram_loads == 1 and rig.flash() == ORIGINAL, 'dispatch: temporary trial (SRAM only)')
    FakeLoader(rig.chip).reset()
    fpga.approved_media = ''
    check(run('fpga_trial') == EXIT_REFUSED and rig.said('not the one approved in the window'), 'dispatch: AI grant without an approved image (trial)')
    check(run('fpga_media') == EXIT_REFUSED and rig.said('not the one approved in the window'), 'dispatch: AI grant without an approved image')
    fpga.approved_media = hashlib.sha256(rig.media_fs.read_bytes()).hexdigest()
    check(run('fpga_media') == EXIT_OK and rig.flash() == rig.expected_media(), 'dispatch: AI grant with the approved image')
    # Update the Pocket link in one step: a write the owner approves in the window, never a program's own start.
    check('fpga_link_update' in core.WRITE_KEYS and 'fpga_link_update' in core.FPGA_KEYS
          and core.OWNER_APPROVE_KEYS == ('fpga_link_update',) and 'fpga_link_update' not in core.PROGRAM_WRITE_KEYS
          and 'fpga_link_update' not in core.REQUESTABLE_KEYS and core.BY_KEY['fpga_link_update'].confirm,
          'the one-step update is an approved write that no program may start by itself')
    check((core.SAID_AUTO_FAILED, core.SAID_AUTO_PASSED, core.SAID_LINK_FROM_FLASH, core.SAID_LINK_NOT_FROM_FLASH,
           core.SAID_UPDATE_NEEDS_SOUND, core.SAID_UPDATE_UNKNOWN, core.SAID_NOT_KEPT, core.SAID_TRY_ENDED,
           core.SAID_KEPT, core.SAID_NO_ASKER, core.SAID_SWITCHED_OFF, core.SAID_LATE_REFUSAL)
          == (cf.SAID_AUTO_FAILED, cf.SAID_AUTO_PASSED, cf.SAID_LINK_FROM_FLASH, cf.SAID_LINK_NOT_FROM_FLASH,
              cf.SAID_UPDATE_NEEDS_SOUND, cf.SAID_UPDATE_UNKNOWN, cf.SAID_NOT_KEPT, cf.SAID_TRY_ENDED,
              cf.SAID_KEPT, cf.SAID_NO_ASKER, cf.SAID_SWITCHED_OFF, cf.SAID_LATE_REFUSAL),
          'the Manager core reads the very words the one-step update says')
    check(all(core.STEP_LINE.match(line) for line in ('Step 1 of 5: x', 'Step 5 of 5: y', 'Step 2 of 4: z'))
          and not core.STEP_LINE.match('Steps 2 to 4 are not needed'), 'the core reads a numbered step whatever the count')
    fpga.approved_media = ''
    loads, before = rig.chip.sram_loads, len(rig.lines)
    del asked[:]
    check(run('fpga_link_update') == EXIT_REFUSED and 'not the one approved in the window' in '\n'.join(rig.lines[before:])
          and rig.chip.sram_loads == loads and rig.flash() == rig.expected_media() and asked == [],
          'dispatch: the one-step update with another image approved is refused, nothing loaded, nothing asked')
    fpga.approved_media = None
    before = len(rig.lines)
    answer[0] = cf.KEEP_ANSWER  # the window's Approve, given through the job's own question
    check(run('fpga_link_update') == EXIT_OK and rig.flash() == rig.expected_link() and rig.chip.sram_loads == loads + 1
          and asked == [cf.LINK_KEEP_PROMPT + cf.keep_text('media')],
          'dispatch: the one-step update tries, checks, asks once through the job\'s question, and writes the image')
    answer[0] = ''
    said = rig.lines[before:]
    check(any(core.SAID_KEPT in line for line in said), 'and says that the owner approved it while it was running')
    check(any(core.SAID_FLASH_WRITING in line for line in said) and any(core.SAID_AUTO_PASSED in line for line in said)
          and any(core.SAID_LINK_FROM_FLASH in line for line in said),
          'the words the Manager core looks for (flash being written, checks passed, running from flash) are said')
    summary = core.fpga_summary('fpga_link_update', EXIT_OK, said, {'state': 'installed', 'fpga': 'sound'})
    check(summary == {'state': 'installed', 'fpga': 'link'}, 'the summary of that run: the flash holds the Pocket link image')
    before = len(rig.lines)
    check(run('fpga_link_update') == EXIT_OK and any(core.SAID_LINK_ALREADY in line for line in rig.lines[before:])
          and not any(core.SAID_FLASH_WRITING in line for line in rig.lines[before:]),
          'dispatch: pressed again, the words for "already there" are said and none for a write')
    fpga.approved_media = hashlib.sha256(rig.media_fs.read_bytes()).hexdigest()
    check(run('fpga_media') == EXIT_OK and rig.flash() == rig.expected_media(), 'dispatch: back to the sound image')
    # A pending FPGA job blocks the ESP32 writes and Finish/Close go to it.
    rig.chip.corrupt_after_write = 5000
    real_write = FakeLoader.write

    def always_bad(self, source):
        real_write(self, source)
        self.c.flash[7] ^= 0xff
    FakeLoader.write = always_bad
    try:
        check(run('fpga_restore') == EXIT_MANUAL and fpga.pending(), 'a stuck FPGA job is pending')
    finally:
        FakeLoader.write = real_write
    check(run('install') == EXIT_REFUSED and rig.said('earlier job did not finish') and h.console.writes == 0,
          'a pending FPGA job blocks an ESP32 write')
    check(run('resume') == EXIT_RECOVERED and rig.flash() == ORIGINAL and not fpga.pending(), 'Finish it finishes the FPGA job')
    # The service: pending() sees FPGA jobs; the AI grant carries the approved sound image; the usage gate applies.
    events = []
    service = core.ManagerService(cm, manager_factory=lambda say: manager, lock_factory=Lock, log_folder=rig.work / 'logs',
                                  root=rig.work, hardware_gate=lambda: 'The hardware usage check stopped it: test gate')
    service.fpga_factory = lambda m, approved=None: (setattr(fpga, 'approved_media', approved), fpga)[1]
    service.listeners.append(lambda event, data: events.append((event, data)))
    service.attach_confirmer()
    check(service.ai_gate('fpga_media') is not None and service.ai_gate('fpga_check') is not None,
          'usage gate: an AI-started FPGA job is blocked while the hardware rule fails')
    service.hardware_gate = lambda: None
    check(service.ai_gate('fpga_media') is None, 'usage gate passes')
    check(service.set_ai_control(True, release='r', fpga_image=fpga.approved_media), 'owner allows the AI with the sound image')
    check(service.ai_fpga_image == fpga.approved_media, 'the service remembers the approved sound image for this run')
    request = service.request_action('fpga_media', 'Claude Code via MCP', 'sound please')
    check(request['state'] == 'waiting', 'request recorded')
    check(service.resolve_request(request['id'], True, by_grant=True), 'started under the grant')
    service.wait(30)
    check(service.last_result is not None and service.last_result['exit_code'] == EXIT_OK and rig.flash() == rig.expected_media()
          and fpga.approved_media == service.ai_fpga_image, 'under the grant the job got the approved image and ran')
    check(service.stop_ai() is not None and service.ai_fpga_image is None, 'Stop takes the sound image approval back')
    check(service.set_ai_control(True, release='r'), 'allow without a sound image')
    request = service.request_action('fpga_restore', 'Claude Code via MCP', 'undo')
    check(service.resolve_request(request['id'], True, by_grant=True), 'restore under the grant')
    service.wait(30)
    check(service.last_result['exit_code'] == EXIT_OK and rig.flash() == ORIGINAL, 'restore needs no image approval')
    request = service.request_action('fpga_media', 'Claude Code via MCP', 'again')
    service.resolve_request(request['id'], True, by_grant=True)
    service.wait(30)
    check(service.last_result['exit_code'] == EXIT_REFUSED and rig.flash() == ORIGINAL,
          'under the grant with no approved sound image the write is refused')
    check(not service.pending(), 'service: no pending job')
    rig.no_leaks()


def main():
    image_checks()
    identity_checks()
    check_job_checks()
    rehearsal_and_media_checks()
    approval_checks()
    failure_checks()
    trial_checks()
    link_checks()
    update_checks()
    version_line_checks()
    core_checks()
    print('PASS: Chromatic FPGA jobs on a simulated console chip: wrong id, two/no bridges and a failing backup refused '
          'before any read; Try FPGA image (temporary): SRAM-only load, flash never read or written, refused without a '
          'recent original check or rehearsal, pinned image, flash-naming loader lines refused, power cycle returns to '
          'the flash; Check FPGA recognises original/sound/unknown and reconfigures; the rehearsal writes the '
          'full 4 MiB backup file and verifies; FPGA sound only after a rehearsal in this session, only over the '
          'original, verified against the exact expected flash; readback mismatch, power cuts in the write and the '
          'readback, a dying tool, boot not seen, exhausted attempts (manual ladder), abandon and owner Stop; AI '
          'approved/unapproved image and the usage gate; Manager core dispatch, pending gating and the grant. '
          'Pocket link 18.41: refused while NEVER-INSTALL.txt is there; from a fixture copy, the SRAM try, the '
          'owner\'s answers, the write only after a confirmed try of that image, try-record tampers refused, one-click '
          'returns to 18.40 and the original, failed writes put right; the try proves 18.41 from the start-up menu version '
          'line (fallback to the flash image, no line, no reply, no debug bit, 18.40: stopped before any look, answers '
          'refused, write shut), the serial read pulses RTS only after opening with DTR/RTS low. Update the Pocket '
          'link in one step: always a fresh try from RAM, five automatic checks (loaded, back on USB, not in download '
          'mode, 18.41 with the debug bit, menu phase from the status line) recorded as confirmed_by auto, then ONE '
          'Approve asked with the image still running from RAM and the flash untouched (an owner action with no '
          'countdown), recorded with the try and in the journal plan; the machine\'s word alone opens no write, and '
          'checks plus this run\'s Approve open the one-step write only; Not right, any other answer, Stop, Approve '
          'after an off-and-on (18.40 reported), nothing able to ask, each failing check, a tampered or stale record, '
          'a moved clock, an old or missing try: nothing written, chip restarted from its flash; while the Approve '
          'waits the console\'s USB port is watched by enumeration only (no port opened, no JTAG): off and on during '
          'the prompt, then a silent game, then Approve writes nothing (also from inside a game, left off, an '
          'unreadable or unanswering USB list), while a game started with its one re-enumeration (0x0100, away, '
          '0x0101) and a silent or busy console is still written and recorded as not heard (stops the update only '
          'with LINK_KEEP_MUST_HEAR); an unexpected error (ValueError, TypeError) while the Approve waits, in the '
          'checks or in the read before the write, and a refusal or Stop after the Approve, leave the flash '
          'untouched, the chip back on its flash and no pending job; the status read retries a busy or late port '
          'like the version read; flash already '
          'holding it: only the last check; original without rehearsal, unknown flash, unreviewed or re-hashed image, '
          'bad backup, pending job: refused; failed writes put right and never called a success; a watched power '
          'cycle is one owner action (waiting with its time, done, or timed out with the next step), never asked '
          'twice in a run; pins read from chromatic_fpga\'s one block. No hardware.')


if __name__ == '__main__':
    sys.exit(main())

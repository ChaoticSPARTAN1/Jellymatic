# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Chromatic Manager: status, backup, install or repair the start-up menu, switch it off,
restore stock, a guided official update, and finishing or abandoning an interrupted job.
ESP32 (MCU) flash only.

Only a person starts this; nothing runs in the background. Every write:
  * is planned from an exact image of the console, after its identity is matched to the
    backups. That image is either a full read (each 256 KiB chunk checked against the
    chip's own MD5), or, when every part of the flash matches an image this tool already
    holds, is put together from those images by the chip's MD5 of each part plus a read
    of the settings and setup sectors, and proven by the whole-flash MD5 on the chip
    (identify()). A stock firmware in no verified backup is always read in full, and
    backed up, with a second copy, before anything is written;
  * saves the before and after images and journals each step to disk first;
  * keeps the console bootable between steps where the layout allows it: install
    writes launcher, table, bootloader; removal and restore write the bootloader
    first; the guided update only erases the launcher area, which is always safe.
    Two short writes (the 4 KiB table and the bootloader) cannot be made power-safe;
    a power cut inside them is repaired by running the tool again, through the ROM
    download mode that flash contents cannot remove;
  * is checked on the chip per region and for the whole flash, then the console is
    reset and watched on the serial port until it is seen starting as intended;
  * is undone (install) or finished (everything else) automatically on failure, and
    on the next run if the PC or tool died. A console that changed since the job
    began (an official update, another tool) is never overwritten from the journal.
Never written: settings (NVS, PHY data); the FPGA; cartridges. The stock firmware slot
is written only by restore-stock, and only with explicit agreement when the version
would change.

Output never names a device, MAC, setting or firmware content.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from user_paths import PRIVATE, SECOND_COPY  # noqa: E402  (per-user folder, see user_paths.py)
import re
import shutil
import struct
import sys
import threading
import time

from chromatic_lock import Busy, DeviceLock
from chromatic_manager_owner import OwnerPrompts
from chromatic_trial_runner import (EXIT_MANUAL, EXIT_OK, EXIT_RECOVERED, EXIT_REFUSED, SECTOR, Journal, Refused,
                                    StepFailed, StepTimeout, WrongUnit, load_stock, write_durable)
from compose_chromatic_image import Refused as ImageRefused, check_image, parse_table, poison_flash_args
from verify_chromatic_backup import SIZE, sha

EXIT_ERROR = 4
EXIT_NOT_SEEN = 5   # flash is right, but the console was not seen starting
ROOT = Path(__file__).resolve().parents[1]
WORK = PRIVATE / 'chromatic-manager'
BACKUPS = PRIVATE / 'chromatic-manager-backups'
SECONDARY = SECOND_COPY / 'manager'  # the second copy of every backup (user_paths.SECOND_COPY; ideally another disk)
RELEASE = ROOT / 'release/launcher'  # the start-up menu this tool installs (scripts/Build-ChromaticLauncher.ps1 fills it)
PINNED = (PRIVATE / 'chromatic-manager-release', SECOND_COPY / 'manager-release')
# ModRetro's official updater is never shipped with Jellymatic: download it from ModRetro (modretro.com) yourself.
UPDATER = PRIVATE / 'official-updater/ChromaticFirmwareUpdater.exe'
LAUNCHER_PROJECT = 'chromatic_launcher'
STOCK_PROJECT = 'mcu_fw'    # ModRetro's stock MCU firmware project name
READY, TO_STOCK, STOCK_INVALID = b'PJ-LAUNCHER-READY', b'PJ-LAUNCHER-TO-STOCK', b'PJ-LAUNCHER-STOCK-INVALID'
MENU = b'PJ-LAUNCHER-MENU'  # 1.1.2+: printed once per boot when the menu first opens
# The console chip (FPGA) version the launcher learns at every start (its version request) and prints
# as one diagnostics line (launcher_main.c, after the version wait): "fpga version=18.40 debug=1",
# or "fpga version=none" when the FPGA did not answer. Older launchers do not print it.
FPGA_MARK = b' fpga version='
FPGA_LINE = re.compile(rb'PJ-DIAG t=\d+ fpga version=(?:(\d{1,2})\.(\d{1,2}) debug=([01])|(none))\r?\n')


def launcher_fpga(log):
    """(major, minor, debug) from the launcher's FPGA line, 'none' when the FPGA did not answer,
    or None when the line is not in `log` (an older launcher, or not seen)."""
    found = FPGA_LINE.search(log)
    if not found:
        return None
    if found.group(4):
        return 'none'
    return int(found.group(1)), int(found.group(2)), int(found.group(3))


def fpga_status_line(chip):
    """The status line for what launcher_fpga() found (chromatic_manager_core.summarize reads it)."""
    if chip is None:
        return 'Console chip (FPGA): not reported by this start-up menu.'
    if chip == 'none':
        return 'Console chip (FPGA): no version reply at start.'
    major, minor, debug = chip
    return f'Console chip (FPGA): version {major}.{minor}{" with the debug bit" if debug else ""}, as the start-up menu reports it.'

BOOT = (0x1000, 0x8000)
TABLE = (0x8000, 0x9000)
SETTINGS = (0x9000, 0x10000)
APP = (0x10000, 0x110000)
SLOT = (0x260000, 0x400000)
# Wi-Fi, pairing and Jellyfin for launcher 1.2: two sectors holding the same record
# (chromatic_provision, version 2; a version-1 record sits in B only). Kept by install and
# switch-off, erased by remove. Each sector is written on its own, so one always stays valid.
PROVISION = (0x3FE000, 0x400000)
PROVISION_A, PROVISION_B = (0x3FE000, 0x3FF000), (0x3FF000, 0x400000)
SLOT_APP = (SLOT[0], PROVISION[0])
STOCK_ROWS = ((1, 2, 0x9000, 0x6000, 'nvs'), (1, 1, 0xf000, 0x1000, 'phy_init'), (0, 0, 0x10000, 0x100000, 'factory'))
LAUNCHER_ROW = (0, 0x10, SLOT[0], SLOT[1] - SLOT[0], 'launcher')
# The launcher bootloader is ESP-IDF v5.3. Newer stock apps of the same major version
# boot from an older bootloader (IDF keeps that compatible); another major is refused.
BOOTLOADER_IDF_MAJOR = 'v5.'
# The pieces the console is identified by: together exactly the whole flash, in order.
SEGMENTS = ((0, BOOT[0]), BOOT, TABLE, SETTINGS, APP, (APP[1], SLOT[0]), SLOT_APP, PROVISION_A, PROVISION_B)
# Settings (NVS, PHY data) and the console setup are always read, never compared with other
# images or shown: they are the console's own, and only 36 KiB.
ALWAYS_READ = (SETTINGS, PROVISION_A, PROVISION_B)
assert SEGMENTS[0][0] == 0 and SEGMENTS[-1][1] == SIZE and all(
    a[1] == b[0] for a, b in zip(SEGMENTS, SEGMENTS[1:])), 'segments must cover the flash exactly'


@dataclass(frozen=True)
class Timing:
    """Measured on this console at 115200 baud: a full 4 MiB read 384 s
    (about 10.6 KB/s), writing 884 KiB 82 s, the stub's on-chip MD5 of all 4 MiB 5.5 s,
    connecting 1 to 3 s. Deadlines are generous multiples of those."""
    connect: float = 90
    read: float = 900
    md5: float = 180
    erase: float = 300
    write_base: float = 60
    write_bytes_per_s: float = 4000     # about a third of the measured link rate
    launcher_watch: float = 45          # launcher start, hand-over, then 15 s of quiet
    stock_watch: float = 20
    quiet_after_handover: float = 15    # longer than the 9 s RTC watchdog
    fpga_watch: float = 6               # status: the launcher's FPGA line comes about 1 s after the reset
    device_return: float = 120
    poll: float = 1.0
    attempts: int = 3

    def write(self, size):
        return self.write_base + size / self.write_bytes_per_s

    def read_part(self, size):
        """Deadline for reading `size` bytes: the full-read deadline pro rata, plus a margin."""
        return 60 + self.read * size / SIZE


MANUAL = """
STOP: the console could not be put back automatically. Nothing else will be tried now.
  1. Unplug the USB cable, switch the console off, wait 10 seconds, plug it back in
     and switch it on.
  2. Run Chromatic Manager again and choose to finish the unfinished job.
  3. If it stops here again, follow docs/Recovery.md, "If an install stops half-way".
The ROM download mode the tool uses is in the chip's mask ROM; flash contents cannot
remove it. Your backups are unchanged.
"""


def span(image, region):
    return image[region[0]:region[1]]


def put(image, region, data):
    """Copy of `image` with `region` replaced, padded with erased bytes."""
    start, end = region
    if len(data) > end - start:
        raise Refused('Data does not fit its region.')
    out = bytearray(image)
    out[start:end] = data + b'\xff' * (end - start - len(data))
    return bytes(out)


def app_description(data):
    """(project, version, idf) from an app image, or None if it is not a valid app."""
    try:
        check_image(data, 'app')
    except ImageRefused:
        return None
    desc = data[0x20:0x120]
    if len(desc) < 0x100 or struct.unpack_from('<I', desc)[0] != 0xabcd5432:
        return None
    text = lambda raw: raw.split(b'\0')[0].decode('ascii', 'replace')
    return text(desc[48:80]), text(desc[16:48]), text(desc[112:144])


def stock_description(image):
    """The app in the stock slot, only if it is ModRetro's stock firmware."""
    described = app_description(span(image, APP))
    return described if described and described[0] == STOCK_PROJECT else None


def table_bytes(rows_raw):
    """A partition table sector exactly as ESP-IDF writes it: rows, MD5 row, erased."""
    body = b''.join(rows_raw)
    md5_row = b'\xeb\xeb' + b'\xff' * 14 + hashlib.md5(body).digest()
    return (body + md5_row).ljust(TABLE[1] - TABLE[0], b'\xff')


@dataclass(frozen=True)
class Release:
    """The launcher files this tool installs, checked as ESP32 images on load."""
    bootloader: bytes
    table: bytes
    launcher: bytes
    version: str
    stock_table: bytes   # the exact stock sector: the launcher table's first three rows

    NAMES = ('bootloader.bin', 'partition-table.bin', 'chromatic_launcher.bin')

    @staticmethod
    def load(folder=None, pinned=None, repin=True):
        """The build if present (pinned to two disks for later), else a pinned copy, so
        Status, Switch off and Restore keep working if the build folder is gone. Loading
        the build always re-pins it: the pinned copies are the last build this tool saw, so
        the build folder must only ever hold a reviewed build. Defaults are looked up at
        call time (tests point PINNED at a sandbox). `repin=False` (jobs an AI starts, and
        the window's look at what an install would write) reads without re-pinning."""
        folder = Path(folder or RELEASE)
        pinned = PINNED if pinned is None else pinned
        built = (folder / 'bootloader/bootloader.bin', folder / 'partition_table/partition-table.bin',
                 folder / 'chromatic_launcher.bin')
        if all(p.exists() for p in built):
            poison_flash_args(folder)  # never leave a usable `esptool @flash_args` behind
            release = Release.check(*(p.read_bytes() for p in built))
            for target in (pinned if repin else ()):
                try:
                    Release.pin(release, Path(target))
                except OSError:
                    pass
            return release
        for source in pinned:
            try:
                return Release.from_pinned(Path(source))
            except (OSError, ValueError, KeyError, Refused):
                continue
        raise Refused('No launcher release found (build folder and both pinned copies are missing).')

    def fingerprint(self):
        """SHA-256 over the bootloader, table and launcher (each one's SHA-256, in that
        order): what the owner approves when an AI may install."""
        return hashlib.sha256(b''.join(hashlib.sha256(part).digest()
                                       for part in (self.bootloader, self.table, self.launcher))).hexdigest()

    @staticmethod
    def pin(release, target):
        target.mkdir(parents=True, exist_ok=True)
        manifest = {}
        for name, data in zip(Release.NAMES, (release.bootloader, release.table, release.launcher)):
            write_durable(target / name, data)
            manifest[name] = sha(data)
        write_durable(target / 'manifest.json', json.dumps(manifest, indent=1).encode('utf-8'))

    @staticmethod
    def from_pinned(source):
        manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
        parts = []
        for name in Release.NAMES:
            data = (source / name).read_bytes()
            if sha(data) != manifest[name]:
                raise ValueError('pinned release damaged')
            parts.append(data)
        return Release.check(*parts)

    @staticmethod
    def check(bootloader, table, launcher):
        try:
            check_image(bootloader, 'bootloader')
            rows, raw = parse_table(table)
        except ImageRefused as error:
            raise Refused(f'Launcher release is damaged: {error}')
        if len(bootloader) > BOOT[1] - BOOT[0] or len(table) > TABLE[1] - TABLE[0]:
            raise Refused('Launcher release does not fit the flash layout.')
        if tuple(rows) != STOCK_ROWS + (LAUNCHER_ROW,):
            raise Refused('Launcher partition table is not the reviewed layout.')
        described = app_description(launcher)
        if not described or described[0] != LAUNCHER_PROJECT or len(launcher) > SLOT_APP[1] - SLOT_APP[0]:
            raise Refused('Launcher image is damaged or too large.')
        if table_bytes(raw) != table.ljust(TABLE[1] - TABLE[0], b'\xff'):
            raise Refused('Launcher partition table is not in the standard format.')
        return Release(bootloader, table, launcher, described[1], table_bytes(raw[:3]))


@dataclass
class State:
    """What is on the console, worked out from a full read. No identifiers."""
    name: str
    boots: str
    launcher_version: str | None = None
    stock: tuple | None = None
    notes: list = field(default_factory=list)


def layout(image, release):
    """'stock', 'launcher' or None, by exact comparison of the whole table sector."""
    table = span(image, TABLE)
    if table == release.stock_table:
        return 'stock'
    if table == release.table.ljust(TABLE[1] - TABLE[0], b'\xff'):
        return 'launcher'
    return None


BOOT_DATE_TIME = (0x48, 0x60)  # esp_bootloader_desc_t.date_time in the image (build timestamp)


def same_bootloader(region, bootloader):
    """True when the BOOT region holds this bootloader build, ignoring only what a rebuild of
    the same source changes: the build date/time in its descriptor, and the one-byte image
    checksum and appended SHA-256 that cover it. Everything else must match exactly, and
    describe() separately requires the console's bootloader to pass its own checks. So a
    console installed from an earlier build of the same bootloader is never rewritten
    (bootloader writes are the ones that are not power-safe)."""
    padded = bootloader.ljust(BOOT[1] - BOOT[0], b'\xff')
    if len(region) != len(padded) or len(bootloader) < BOOT_DATE_TIME[1] + 33:
        return False
    covered = 33 if bootloader[23] == 1 else 1  # checksum byte, plus the SHA-256 when appended

    def masked(data):
        data = bytearray(data)
        data[BOOT_DATE_TIME[0]:BOOT_DATE_TIME[1]] = bytes(BOOT_DATE_TIME[1] - BOOT_DATE_TIME[0])
        data[len(bootloader) - covered:len(bootloader)] = bytes(covered)
        return bytes(data)
    return masked(region) == masked(padded)


def boot_valid(image):
    try:
        check_image(span(image, BOOT), 'bootloader')
        return True
    except ImageRefused:
        return False


def describe(image, release):
    table = layout(image, release)
    stock = stock_description(image)
    slot_app = app_description(span(image, SLOT_APP))
    slot_empty = span(image, SLOT_APP) == b'\xff' * (SLOT_APP[1] - SLOT_APP[0])
    ours_boot = same_bootloader(span(image, BOOT), release.bootloader)
    launcher_in_slot = bool(slot_app) and slot_app[0] == LAUNCHER_PROJECT
    version = slot_app[1] if launcher_in_slot else None
    if not boot_valid(image):
        return State('damaged', 'nothing: the bootloader is damaged', version, stock,
                     ['Choose Restore stock. ROM download mode still works.'])
    if table is None or not stock:
        return State('unknown', 'unknown: not a layout or firmware this tool knows; it will not write to it',
                     version, stock)
    if ours_boot and table == 'launcher':
        if launcher_in_slot:
            name = 'installed' if version == release.version else 'outdated'
            return State(name, 'start-up menu on power-on, stock otherwise', version, stock)
        return State('partial', 'stock (start-up menu switched off; its area is empty)', None, stock,
                     ['Choose Install to switch the start-up menu on, or Switch off for plain stock.'])
    if table == 'stock' and not ours_boot:
        if slot_empty:
            return State('stock', 'stock', None, stock)
        if launcher_in_slot:
            return State('dormant', 'stock (launcher present but switched off, e.g. after an official update)',
                         version, stock, ['Choose Install to switch the start-up menu back on.'])
        return State('stock-with-data', 'stock (unexpected data in the launcher area; left alone)', None, stock)
    return State('partial', 'stock (a half-finished install or removal)', version, stock,
                 ['Choose Install to finish, or Switch off.'])


STOCK_LAYOUT = ('stock', 'dormant')
BOOTS_STOCK = ('stock', 'dormant', 'partial', 'installed', 'outdated', 'stock-with-data')


def compatible_stock(image):
    """The launcher bootloader must be able to start the stock firmware on the console."""
    described = stock_description(image)
    if not described:
        raise Refused('The stock firmware slot does not hold ModRetro stock firmware.')
    if not described[2].startswith(BOOTLOADER_IDF_MAJOR):
        raise Refused('This stock firmware uses a different ESP-IDF major version; the start-up menu does not '
                      'support it yet. Stock is left as it is and works normally.')
    return described


def check_layout_change(before, after, allowed):
    for offset in range(0, SIZE, SECTOR):
        if before[offset:offset + SECTOR] != after[offset:offset + SECTOR]:
            if not any(start <= offset and offset + SECTOR <= end for start, end in allowed):
                raise Refused('A planned write reaches outside the bootloader, table and launcher slot.')
    if span(before, SETTINGS) != span(after, SETTINGS) or span(before, APP) != span(after, APP):
        raise Refused('A planned write would change settings or the stock firmware.')


def install_target(current, release):
    """Install, update or repair. Changes only bootloader, table and launcher slot."""
    state = describe(current, release)
    if state.name not in ('stock', 'dormant', 'installed', 'outdated', 'partial'):
        raise Refused(f'Install refused: console state is {state.name}.')
    compatible_stock(current)
    for name, data in (('bootloader', release.bootloader), ('launcher', release.launcher)):
        if data[2:4] != span(current, APP)[2:4]:
            raise Refused(f'The {name} flash settings differ from the stock firmware.')
    target = put(current, SLOT_APP, release.launcher)  # the provisioning sector stays
    target = put(target, TABLE, release.table)
    if not (boot_valid(current) and same_bootloader(span(current, BOOT), release.bootloader)):
        target = put(target, BOOT, release.bootloader)  # an earlier build of the same bootloader is kept
    check_layout_change(current, target, (BOOT, TABLE, SLOT))
    return target


def disable_target(current, release):
    """Power-safe switch-off: only the launcher app is erased (its provisioning sector
    stays). A half-erased launcher fails its check, so every in-between state boots stock."""
    target = put(current, SLOT_APP, b'')
    check_layout_change(current, target, (SLOT,))
    return target


def stock_boot_source(backups, current, release):
    """Stock bootloader to put back: from a stock-layout backup of this firmware if there
    is one, else the newest stock-layout backup (every stock bootloader starts the app)."""
    stock_layout = [b for _, b, _ in backups if describe(b, release).name in STOCK_LAYOUT]
    same = [b for b in stock_layout if span(b, APP) == span(current, APP)]
    return (same or stock_layout or [None])[0]


def remove_target(current, release, source):
    if source is None:
        raise Refused('No verified stock backup to take the stock bootloader from.')
    target = put(current, BOOT, span(source, BOOT))
    target = put(target, TABLE, release.stock_table)
    target = put(target, SLOT, b'')
    check_layout_change(current, target, (BOOT, TABLE, SLOT))
    return target


def restore_target(current, backup, release, source):
    """The backup's firmware in a plain stock layout, with today's settings."""
    image = backup
    if describe(backup, release).name not in STOCK_LAYOUT:
        if source is None:
            raise Refused('That backup has the launcher in it and no stock bootloader backup exists.')
        image = put(put(put(image, BOOT, span(source, BOOT)), TABLE, release.stock_table), SLOT, b'')
    image = image[:SETTINGS[0]] + span(current, SETTINGS) + image[SETTINGS[1]:]
    if describe(image, release).name not in STOCK_LAYOUT:
        raise Refused('That backup does not give a valid stock console.')
    return image


def ordered_regions(current, target, bootloader_last):
    """Sector runs to write, in the order that keeps the console bootable longest."""
    runs, start = [], None
    for offset in range(0, SIZE + SECTOR, SECTOR):
        differs = offset < SIZE and current[offset:offset + SECTOR] != target[offset:offset + SECTOR]
        if differs and start is None:
            start = offset
        elif not differs and start is not None:
            runs.append((start, offset))
            start = None
    pieces = []
    for start, end in runs:
        cut = sorted({start, end} | {edge for region in (BOOT, TABLE, SETTINGS, APP, SLOT, PROVISION_A, PROVISION_B)
                                     for edge in region
                                     if start < edge < end})
        pieces += list(zip(cut, cut[1:]))
    boot = [p for p in pieces if p[0] < BOOT[1]]
    rest = [p for p in pieces if p[0] >= BOOT[1]]
    if bootloader_last:
        order = sorted(rest, key=lambda p: -p[0]) + boot    # slot, then table, then bootloader
    else:
        order = boot + sorted(rest, key=lambda p: p[0])      # bootloader first, then the rest
    return [(start, target[start:end]) for start, end in order]


def consistent_with_job(current, before, target, bootloader_last):
    """True if every difference from both images lies inside one planned region: the
    only way the console can look after an interrupted run of this job."""
    regions = [(o, o + len(d)) for o, d in ordered_regions(before, target, bootloader_last)]
    torn = set()
    for offset in range(0, SIZE, SECTOR):
        if SETTINGS[0] <= offset < SETTINGS[1]:
            continue  # stock may have saved settings since
        piece = current[offset:offset + SECTOR]
        if piece in (before[offset:offset + SECTOR], target[offset:offset + SECTOR]):
            continue
        owner = next((r for r in regions if r[0] <= offset < r[1]), None)
        if owner is None:
            return False
        torn.add(owner)
    return len(torn) <= 1


PANIC = (b'Guru Meditation', b'abort() was called', b'Backtrace:', b'Rebooting...')


def launcher_boot_ok(log, quiet_seconds_seen):
    """The launcher announced itself and then ran for the whole quiet window (longer than
    the 9 s watchdog) without the chip resetting, crashing or reporting a bad stock image,
    and either:
      - its menu opened (`PJ-LAUNCHER-MENU`, launcher 1.1.2 and later, printed once per boot;
        docs/Design-notes.md) and it is waiting for the owner's choice; or
      - it handed over to stock normally (`PJ-LAUNCHER-TO-STOCK`: no cartridge, the owner
        chose Games, or an older launcher counted down): one restart into stock, and the
        launcher not coming back.
    `READY` with neither means the menu never opened: that fails."""
    if READY not in log or STOCK_INVALID in log or not quiet_seconds_seen or any(p in log for p in PANIC):
        return False
    after_ready = log[log.index(READY):]
    if TO_STOCK not in after_ready:
        return MENU in after_ready and b'rst:' not in after_ready
    cut = after_ready.index(TO_STOCK)
    before, after = after_ready[:cut], after_ready[cut:]
    return b'rst:' not in before and READY not in after and MENU not in after and after.count(b'rst:') <= 1


def stock_boot_ok(log):
    """The chip printed its reset banner once, and no launcher message appeared."""
    return b'PJ-LAUNCHER' not in log and log.count(b'rst:') == 1


# The first launcher that reads provisioning record v2 (docs/Design-notes.md).
# Older ones read only version 1 at 0x3FF000, so writing v2 would leave them without Wi-Fi:
# the Manager refuses until the console runs at least this version.
PROVISION_V2_LAUNCHER = (1, 1, 2)  # 1.1.2's C reader passes every v2 vector (test_chromatic_provision_c.py)


def reads_setup_v2(version):
    try:
        return tuple(int(part) for part in str(version).split('.')) >= PROVISION_V2_LAUNCHER
    except ValueError:
        return False


MENU_WAITS_FROM = (1, 1, 2)  # 1.1.0 and 1.1.1 count down to stock; from 1.1.2 the menu waits for a choice


def menu_waits(version):
    try:
        return tuple(int(part) for part in str(version).split('.')) >= MENU_WAITS_FROM
    except ValueError:
        return False


def without_setup(image):
    """The image with the console-setup sectors erased: what backups and closed journals keep."""
    return put(image, PROVISION, b'') if len(image) == SIZE else image


class ManagerJournal(Journal):
    KEEP_CLOSED_DAYS = 30

    def archive(self):
        """Closed jobs are kept for a while as a record, without the console setup (Wi-Fi
        passwords, keys, the Jellyfin token), and deleted after KEEP_CLOSED_DAYS. Nothing
        reads them back; an open job keeps its full images until it is finished."""
        # Scrub in place first, then move: the console setup never reaches closed/, even if
        # the PC dies half-way (a closed journal is archived again on the next run).
        for path in self.folder.glob('image-*.bin'):
            data = path.read_bytes()
            if len(data) == SIZE and data != without_setup(data):
                write_durable(path, without_setup(data))
        closed = self.folder / 'closed'
        closed.mkdir(exist_ok=True)
        # A fresh folder every time. Two jobs closed in the same second could draw the same
        # name (the clock's last digits repeat 1 time in 1000), and a failed mkdir here, after a
        # verified write, made that job report "did not finish" and run recovery (seen as an
        # intermittent failure in test_chromatic_manager_audit_fixes.py).
        stamp, number = time.strftime('%Y%m%dT%H%M%S'), time.monotonic_ns() % 100000
        while True:
            target = closed / f'{stamp}-{number}'
            try:
                target.mkdir()
                break
            except FileExistsError:
                number += 1
        for path in [self.path, *self.folder.glob('image-*.bin')]:
            if path.exists():
                shutil.move(str(path), str(target / path.name))
        expired = time.time() - self.KEEP_CLOSED_DAYS * 86400
        for old in closed.iterdir():
            try:
                if old.is_dir() and old != target and old.stat().st_mtime < expired:
                    shutil.rmtree(old)
            except OSError:
                pass  # a record that cannot be removed now is tried again next time


def keep_awake(on):
    """Stop Windows sleeping mid-job; a job can take half an hour."""
    if os.name == 'nt':
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))


def backup_images(say=None):
    """Verified images this tool may use, newest first, the original backup last.
    Each must match its recorded hash in both copies; a missing copy is named."""
    found = []
    for folder in sorted(BACKUPS.glob('*/'), reverse=True):
        try:
            record = json.loads((folder / 'record.json').read_text(encoding='utf-8'))
            image = (folder / 'esp32-flash.bin').read_bytes()
            second_path = SECONDARY / folder.name / 'esp32-flash.bin'
            if not second_path.exists():
                if say:
                    say(f'Backup {folder.name}: its second copy ({SECONDARY}) is missing, so it is not used.')
                continue
            if sha(image) == record['sha256'] == sha(second_path.read_bytes()) and len(image) == SIZE:
                found.append((folder.name, image, record['identity']))
        except (OSError, KeyError, ValueError):
            continue
    try:
        original = load_stock()
        found.append(('original', original.image, original.identity))
    except Exception:
        if say and (PRIVATE / 'chromatic-tools/latest-backup-path.txt').exists():  # else: none taken on this PC yet
            say('The original backup could not be verified in both copies; it is not used.')
    return found


def release_parts(release):
    """What this release puts in each part of the flash, exactly as it is written there."""
    return ((BOOT, release.bootloader.ljust(BOOT[1] - BOOT[0], b'\xff')),
            (TABLE, release.table.ljust(TABLE[1] - TABLE[0], b'\xff')), (TABLE, release.stock_table),
            (SLOT_APP, release.launcher.ljust(SLOT_APP[1] - SLOT_APP[0], b'\xff')))


def closed_job_images(folder, newest=10):
    """Full images of this tool's newest finished jobs (kept without the console setup), e.g.
    the launcher a later update replaces. Used only where their bytes match the chip's MD5."""
    try:
        records = sorted((p for p in (Path(folder) / 'closed').iterdir() if p.is_dir()), reverse=True)[:newest]
    except OSError:
        return
    for record in records:
        for path in sorted(record.glob('image-*.bin')):
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if len(data) == SIZE:
                yield data


# What the window shows, big, while a job waits for the owner (chromatic_manager_owner).
PLUG_IN_TITLE = 'Plug in your Chromatic'
PLUG_IN_TEXT = 'Plug it into this PC with its USB cable and switch it ON. Only one Chromatic connected.'
PLUG_IN_MISSED = ('Your Chromatic was not seen on USB. Plug it in, switch it on, close any other tool that uses it, '
                  'then press the button again.')
UPDATER_TITLE = 'Run ModRetro\'s updater now'
UPDATER_TEXT = ('Leave this app open. Use the updater\'s firmware update only (never Cart Clinic). Keep the console '
                'plugged in and ON. CLOSE the updater when it reports success, then answer the small window.')


# The console model this tool knows, checked on the chips themselves before the first full backup
# (when there is no backup yet to match the console against): the ESP32 chip and revision, its
# flash chip (JEDEC 0x204016 as esptool reports it), and, for the console chip, the Gowin GW5A-25
# (JTAG IDCODE 0x1281b) with its Winbond flash (JEDEC 0xef4016, chromatic_fpga.FLASH_JEDEC).
ESP32_MODEL = {'chip': 'ESP32-U4WDH', 'revision': 3, 'flash_id': '204016'}
UNKNOWN_MODEL = ('This console does not identify as the ModRetro Chromatic model this tool knows (its ESP32, '
                 'revision or flash chip differ). Nothing was read into a backup and nothing was written.')
FULL_BACKUP_PREFIX = 'chromatic-backup-'  # <your Jellymatic folder>/chromatic-backup-YYYYMMDD-HHMMSS (UTC)


LATEST_FULL_BACKUP = 'chromatic-tools/latest-backup-path.txt'  # in the per-user folder: the full backup's record
BACKUP_UNCHECKED = ('your full backup exists but could not be checked in both copies. Is the second-copy disk (or '
                    'USB stick) connected? Nothing was read or written.')
# A full backup is recorded: the Manager's full backup and the by-hand chromatic_backup.py refuse with this.
ALREADY_RECORDED = ('a full backup is already recorded on this PC, and it is never replaced. Nothing was read or '
                    'written.')
NO_RELEASE_FOR_BACKUP = ('the start-up menu release (release/launcher, see docs/Install-with-Chromatic-Manager.md) is '
                         'needed to recognise the official firmware, so no full backup is made. Nothing was read, saved '
                         'or written.')
NOT_ORIGINAL = ('the console does not run the official ModRetro firmware as it comes ({what}), so it is not taken as '
                'your original. Put the official firmware back with ModRetro\'s own updater first (docs/Recovery.md, '
                '"Official updates and restoring Chromatic OS"), then press Back up again. Nothing was saved or '
                'written.')


def full_backup_recorded():
    """Has a full backup ever been registered on this PC (its record file exists, whether or not its two
    copies verify right now)? The first full backup is taken only when it has not: an existing original
    is never replaced, for example while its second copy's disk is unplugged."""
    return (PRIVATE / LATEST_FULL_BACKUP).exists()


def check_esp32_model(found):
    """Refuses (nothing written) unless the ESP32 the programmer found is the known console model."""
    if any(found.get(key) != value for key, value in ESP32_MODEL.items()):
        raise Refused(UNKNOWN_MODEL)


class Manager:
    def __init__(self, folder=WORK, programmer_factory=None, release_loader=Release.load, backups=None,
                 save_backup=None, timing=Timing(), say=print, sleep=time.sleep, clock=time.monotonic,
                 fpga_loader_factory=None, fpga_timing=None):
        """`fpga_loader_factory(logs)`: the console chip's JTAG programmer for the full backup
        (chromatic_fpga.JtagLoader by default; it only ever reads there)."""
        self.folder = Path(folder)
        self.journal = ManagerJournal(self.folder)
        self.factory = programmer_factory or ManagerProgrammer
        self.fpga_loader_factory = fpga_loader_factory
        self.fpga_timing = fpga_timing
        self.release_loader = release_loader
        self.backups = backups or (lambda: backup_images(self.say))
        self.save_backup = save_backup or self._save_backup
        self.timing, self.say, self.sleep, self.clock = timing, say, sleep, clock
        self.device = None
        self.stop_point = lambda: None  # the window's Stop hook: may raise before a job writes anything
        # Everything the owner must do while a job runs is announced here (chromatic_manager_owner): the
        # window shows it big and bright with a countdown; the same line still goes to the log.
        self.owner = OwnerPrompts(lambda message: self.say(message))
        self.last_image = None
        self.last_read_full = False  # True when last_image came from a full read of the console
        self.last_written = None     # the image the last job verifiably wrote (whole-flash MD5), if any

    # ---- device plumbing -------------------------------------------------------
    def bounded(self, seconds, function):
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
            self.close_device(reset=False)  # a stuck port: just free it
            thread.join(10)
            raise StepTimeout('A device operation passed its deadline.')
        if 'error' in outcome:
            raise outcome['error']
        return outcome.get('value')

    def close_device(self, reset=True):
        """reset=True also restarts the chip so it runs its firmware, not the flasher."""
        if self.device is not None:
            try:
                self.device.close(reset)
            except Exception:
                pass

    def connect(self, identity):
        if self.device is None:
            self.device = self.factory()
        self.wait_present()
        self.bounded(self.timing.connect, self.device.connect)
        found = self.device.identity()
        if identity is None:  # no backup yet: only the model is known (the first full backup records the unit)
            check_esp32_model(found)
            return
        if any(found.get(k) != identity.get(k) for k in ('chip', 'revision', 'mac', 'flash_id')):
            raise WrongUnit('This is not the console the backups came from.')

    def read(self):
        """The whole flash, read over USB (about 6.5 minutes at the console's link speed)."""
        self.last_read_full = False
        self.say('Reading the console (all 4 MiB, about 6.5 minutes; each chunk is checked on the chip)...')
        image = self.bounded(self.timing.read, lambda: self.device.read_all(self.say))
        if len(image) != SIZE:
            raise StepFailed('A full read came back the wrong size.')
        self.last_image, self.last_read_full = image, True
        return image

    def identify(self, release, backups):
        """The whole flash, byte for byte, usually without reading all of it.

        The chip's own MD5 of each part (SEGMENTS) is compared with what this tool already
        holds: the release, the verified backups, erased flash, and its newest finished
        jobs. Settings and the console setup (36 KiB) are always read. A part that matches
        nothing is read (for example a bootloader from an earlier build: describe() then
        compares it with the date and checksum fields masked, as before). The result is
        accepted only if the whole-flash MD5 on the chip matches it; otherwise, and whenever
        the stock firmware is in no verified backup, the whole console is read instead.
        Takes about 20 seconds on a console this tool knows, against 6.5 minutes."""
        self.last_read_full = False
        self.say('Identifying the console from on-chip checksums (about 20 seconds; nothing is written)...')
        compared = [r for r in SEGMENTS if r not in ALWAYS_READ]
        digests = {r: self.bounded(self.timing.md5, lambda r=r: self.device.md5(r[0], r[1] - r[0])) for r in compared}
        parts = {}

        def offer(region, data):
            if region not in parts and hashlib.md5(data).hexdigest() == digests[region]:
                parts[region] = bytes(data)

        # The stock firmware is known only from a verified backup, so one never seen before is
        # always read in full from the chip (and install backs it up from that read).
        for _, image, _ in backups:
            offer(APP, memoryview(image)[APP[0]:APP[1]])
        if APP not in parts:
            self.say('This stock firmware is in no verified backup yet, so the whole console is read.')
            return self.read()

        def images():
            for _, image, _ in backups:
                yield image
            yield from closed_job_images(self.folder)
        for region, data in release_parts(release):
            offer(region, data)
        for region in compared:
            offer(region, b'\xff' * (region[1] - region[0]))
        for image in images():
            if all(r in parts for r in compared):
                break
            view = memoryview(image)
            for region in compared:
                if region != APP:
                    offer(region, view[region[0]:region[1]])
        missing = [r for r in SEGMENTS if r not in parts]
        unknown = sum(r[1] - r[0] for r in missing if r not in ALWAYS_READ)
        self.say(f'Reading {sum(r[1] - r[0] for r in missing) // 1024} KiB: the settings and console-setup areas '
                 '(never shown)' + (f', and {unknown // 1024} KiB that match no image this tool has' if unknown else '')
                 + '. The rest matches known images.')
        for start, end in missing:
            data = self.bounded(self.timing.read_part(end - start), lambda s=start, e=end: self.device.read(s, e - s))
            if len(data) != end - start:
                raise StepFailed('A flash read came back the wrong size.')
            parts[(start, end)] = data
        image = b''.join(parts[r] for r in SEGMENTS)
        whole = self.bounded(self.timing.md5, lambda: self.device.md5(0, SIZE))
        if len(image) != SIZE or whole != hashlib.md5(image).hexdigest():
            self.say('The parts do not add up to the whole-flash checksum on the chip; reading the whole console '
                     'instead.')
            return self.read()
        self.say('Console identified without a full read: the whole-flash checksum on the chip matches.')
        self.last_image = image
        return image

    def unit_identity(self, first_backup=True):
        """(identity, backups) of the console the verified backups came from. With no verified backup
        at all, the first full backup is taken now (full_backup: both chips, read twice, saved with a
        second copy and checked), which writes nothing to the console; nothing is written before it has
        succeeded. `first_backup=False` (status, read-only): refuses instead. A full backup that exists
        (its record, latest-backup-path.txt, is there) but does not verify is never replaced: refused."""
        found = self.backups()
        if not found:
            if not first_backup:
                raise Refused('No verified backup was found. Nothing will be written without one.')
            if full_backup_recorded():
                raise Refused(BACKUP_UNCHECKED)
            self.say('No backup of this console yet, so the full backup comes first (both chips, each read twice '
                     'and saved with a second copy; nothing is written to the console).')
            self.full_backup()
            found = self.backups()
            if not found:
                raise Refused('The new full backup could not be verified in both copies. Nothing was written.')
        return found[0][2], found

    def wait_present(self):
        if self.device.device_present():
            return
        self.owner.need(PLUG_IN_TITLE, PLUG_IN_TEXT, seconds=self.timing.device_return,
                        log='Waiting for the console: plug it in and switch it on (only one console connected).')
        limit = self.clock() + self.timing.device_return
        try:
            while not self.device.device_present():
                if self.clock() >= limit:
                    self.owner.missed(PLUG_IN_MISSED)
                    raise StepFailed('The console did not appear on USB. Is it plugged in and switched on, '
                                     'with no other Chromatic connected and no other tool using it?')
                self.sleep(self.timing.poll)
        except BaseException:
            self.owner.close()  # only if it still waits (the owner's Stop, an error): never left on screen
            raise
        self.owner.done('Your Chromatic is plugged in.')

    # ---- read-only -------------------------------------------------------------
    def status(self):
        self.journal.load()
        if self.journal.exists and not self.journal.closed:
            self.say('An unfinished job is waiting; the menu offers to finish it.')
        release = self.release_loader()
        try:
            identity, backups = self.unit_identity(first_backup=False)
        except Refused:  # no backup yet: status still reads the console (model checked, nothing written)
            identity, backups = None, []
        chip = None
        try:
            self.connect(identity)
            image = self.identify(release, backups)
            state = describe(image, release)
            if state.name == 'installed':  # this tool's launcher starts on the restart and names the FPGA
                chip = self.fpga_from_launcher()
        finally:
            self.close_device()
        self.say(f'Console: {state.name}. Boots: {state.boots}.')
        if state.stock:
            self.say(f'Stock firmware: version {state.stock[1]} (ESP-IDF {state.stock[2]}).')
        if state.launcher_version == release.version:
            self.say(f'Launcher in flash: version {state.launcher_version}, the one this tool installs (up to date).')
        elif state.launcher_version:
            self.say(f'Launcher in flash: version {state.launcher_version}; this tool installs {release.version} '
                     '(choose Install to update).')
        if not backups:
            self.say('No backup of this console yet. Press Back up, or Install: it takes the full backup first.')
        elif not any(span(b, APP) == span(image, APP) for _, b, _ in backups):
            self.say('No backup of this stock firmware yet; install and restore take one first.')
        for note in state.notes:
            self.say(note)
        if state.name in ('installed', 'outdated', 'partial', 'dormant'):
            self.say(self.console_setup(image))
        if state.name == 'installed':
            self.say(fpga_status_line(chip))
        return EXIT_OK

    def fpga_from_launcher(self):
        """The console restarts into its start-up menu (as closing the connection would do anyway)
        and the launcher's FPGA version line is read from its USB log: no JTAG, nothing written.
        None when it was not seen (an older launcher). Never raises on a device problem."""
        try:
            log, _ = self.bounded(self.timing.fpga_watch + 30, lambda: self.device.watch(
                self.timing.fpga_watch, FPGA_MARK, 0.3))
        except Exception:
            return None
        return launcher_fpga(log)

    # ---- backup ----------------------------------------------------------------
    def _save_backup(self, image, identity):
        """Both copies leave out the console setup (Wi-Fi passwords, the PC pairing key and
        the Jellyfin token): those two sectors are saved erased. Nothing a restore needs is
        in them, and Wi-Fi setup can always write them again from the PC's private files."""
        image = without_setup(image)
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        record = {'sha256': sha(image), 'identity': identity,
                  'stock': list(app_description(span(image, APP)) or ()), 'taken_utc': stamp,
                  'setup_sectors': 'left out (saved erased)'}
        for root in (BACKUPS, SECONDARY):
            folder = root / stamp
            folder.mkdir(parents=True, exist_ok=False)
            write_durable(folder / 'esp32-flash.bin', image)
            write_durable(folder / 'record.json', json.dumps(record, indent=1).encode('utf-8'))
            if sha((folder / 'esp32-flash.bin').read_bytes()) != record['sha256']:
                raise StepFailed('A saved backup copy does not read back.')
        return stamp

    def backup(self):
        """Back up. With no full backup of both chips on this PC yet (a new user, or only ESP32 backups
        from this tool), takes that full backup (full_backup); otherwise a new verified ESP32 backup."""
        found = self.backups()
        if not any(name == 'original' for name, _, _ in found):
            if full_backup_recorded():  # it exists but does not verify now: never replaced, never re-taken
                raise Refused(BACKUP_UNCHECKED)
            self.full_backup(found[0][2] if found else None)
            return EXIT_OK
        identity = found[0][2]
        try:
            self.connect(identity)
            image = self.read()
            if describe(image, self.release_loader()).name == 'damaged':
                raise Refused('The console bootloader is damaged; that is not a backup to keep.')
            self.save_backup(image, self.device.identity())
        finally:
            self.close_device()
        self.say('Backup taken: full read checked on the chip, saved and read back, with a second copy (the Wi-Fi and '
                 'Jellyfin setup is left out of backups).')
        return EXIT_OK

    # ---- the full backup of both chips --------------------------------------------
    def full_backup(self, identity=None):
        """The full backup of both chips, which every later job is checked against. Nothing is
        written to the console, and nothing is kept unless all of it checks out:

          1. ESP32: the console model on the chip (check_esp32_model; with `identity`, the exact
             unit of the existing ESP32 backups), then two full 4 MiB reads, each 256 KiB chunk
             checked against the chip's own MD5 (ManagerProgrammer.read_all, the reads of
             chromatic_backup.py). Both must be identical, hold ModRetro's stock layout
             (verify_chromatic_backup.validate_mcu) with a valid bootloader, and be the official
             firmware as it comes (describe() == 'stock': no start-up menu, nothing half-installed).
          2. Console chip (FPGA), over the GWU2X JTAG bridge (openFPGALoader, read commands only):
             exactly one bridge, the GW5A-25 IDCODE, two full 4 MiB reads with the flash's JEDEC
             id checked by the loader, identical, a bitstream for this chip, and none of Jellymatic's
             own images (chromatic_fpga.jellymatic_image). The console's USB serial interface (made by
             the FPGA) must be seen gone in two looks in a row during the first read; afterwards the
             FPGA is reconfigured from its untouched flash, and the ESP32 found on USB must be the one
             read in step 1 (chip, revision, MAC, flash), which ties both chips to one console.
          3. Saved in <your Jellymatic folder>/chromatic-backup-<UTC time>/: both reads of both
             chips and their records; the three-file restore bundle (make_bundle reads every saved
             file back and validates it); its second copy (user_paths.SECOND_COPY), compared byte
             for byte; completion-receipt.json and recovery-validation.json.
          4. Only then registered (chromatic-tools/latest-backup-path.txt) and loaded back exactly
             as the jobs load it (load_stock, chromatic_fpga.OriginalBackup.load). Any failure
             removes what this run saved, so a half-made backup is never used. Refused at once when a
             full backup is already recorded (full_backup_recorded): an original is never replaced.
        The console-setup sectors (Wi-Fi passwords, the pairing key, the Jellyfin token) are saved
        erased, as in every backup. Raises Refused or StepFailed (nothing written) on any problem."""
        import chromatic_fpga as cf
        import verify_chromatic_backup as vb
        # Everything that can be checked without reading a chip is checked first, so a missing piece
        # refuses at once instead of after the two long ESP32 reads: the record, the start-up menu release
        # (needed to recognise the official firmware), the console chip programmer and its JTAG bridge.
        if full_backup_recorded():
            raise Refused(ALREADY_RECORDED)
        release = self._release_for_backup()
        staging = self.folder / 'full-backup-reads'
        loader, timing = self._fpga_loader(cf, staging)
        self.stop_point()
        self.say('Full backup, part 1 of 3: the ESP32 flash, read twice (about 13 minutes; every chunk is checked on '
                 'the chip). Nothing is written to the console.')
        if self.device is None:
            self.device = self.factory()
        reads = []
        try:
            self.connect(identity)
            found = dict(self.device.identity())
            check_esp32_model(found)
            for number in (1, 2):
                self.say(f'ESP32 read {number} of 2.')
                reads.append(self.read())
        finally:
            self.close_device()
        if reads[0] != reads[1]:
            raise Refused('the two full reads of the ESP32 flash differ, so neither is kept. Nothing was saved and '
                          'nothing was written. Check the USB cable and try again.')
        mcu = reads[0]
        try:
            vb.validate_mcu(mcu)
        except ValueError:
            raise Refused('the ESP32 flash does not hold the ModRetro stock layout and firmware this tool knows, so no '
                          'full backup is made. Nothing was written.')
        if not boot_valid(mcu):
            raise Refused('the console bootloader is damaged; that is not a backup to keep. Nothing was written.')
        # The original must be the official firmware exactly as it comes: ModRetro's bootloader, partition
        # table and firmware, with the start-up menu's area empty (describe() == 'stock'). A console that
        # already has the start-up menu, a half install or anything else is refused.
        state = describe(mcu, release).name
        if state != 'stock':
            raise Refused(NOT_ORIGINAL.format(what=f'its ESP32 reads as "{state}"'))

        self.stop_point()
        self.say('Full backup, part 2 of 3: the console chip (FPGA) flash over its JTAG bridge, read twice (about a '
                 'minute; the screen goes dark, then the console restarts). Nothing is written.')
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            fpga_reads, serial_left, (serial_back, bridge_back) = self._read_fpga_twice(loader, timing, staging)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        if not serial_left:
            raise Refused('the console\'s USB serial interface did not go away while its console chip was read, so '
                          'that chip cannot be tied to this console. Nothing was saved or written. Connect only one '
                          'Chromatic and try again.')
        if fpga_reads[0] != fpga_reads[1] or len(fpga_reads[0]) != SIZE:
            raise Refused('the two full reads of the console chip flash differ, so neither is kept. Nothing was saved '
                          'and nothing was written. Check the USB cable and try again.')
        fpga = fpga_reads[0]
        try:
            vb.validate_fpga(fpga)
        except ValueError:
            raise Refused('the console chip flash does not hold a bitstream for this console chip, so no full backup '
                          'is made. Nothing was written.')
        ours = cf.jellymatic_image(fpga)
        if ours:
            raise Refused(NOT_ORIGINAL.format(what=f'its console chip holds Jellymatic\'s {ours} image'))
        # Same unit: the console on USB now must be the one whose ESP32 was read before the console chip
        # reads (the FPGA restarted the ESP32; another console plugged in meanwhile is refused).
        self.say('Checking that the console on USB is still the one whose ESP32 was read.')
        try:
            self.connect({key: found[key] for key in ('chip', 'revision', 'mac', 'flash_id')})
        except WrongUnit:
            raise Refused('the console on USB is not the one whose ESP32 was read before the console chip was read. '
                          'Connect only one Chromatic and try again. Nothing was saved or written.')
        finally:
            self.close_device()

        self.say('Full backup, part 3 of 3: saving both chips, reading the files back and making the second copy.')
        try:
            folder = self._save_full_backup(vb, cf, without_setup(mcu), fpga, found, serial_back, bridge_back)
        except (ValueError, KeyError, ImportError) as error:
            raise StepFailed(f'the saved full backup did not check out ({type(error).__name__}), so it was removed. '
                             'Nothing was written to the console.')
        self.say('Full backup taken: both chips read twice and identical, saved, read back and checked, with a second '
                 'copy. Nothing was written to the console. The second copy is usually on the same disk: for real '
                 'safety, set JELLYMATIC_SECOND_COPY to another disk or a USB stick.')
        if not (serial_back and bridge_back):
            self.say('The console was not seen coming back on USB after the console chip read. Switch it off and on; '
                     'it starts from its untouched flash as before.')
        return folder

    def _release_for_backup(self):
        """The start-up menu release, needed before the full backup to recognise the official firmware
        (describe() == 'stock'). Refused, before anything is read, when there is none."""
        try:
            release = self.release_loader()
        except Refused:
            release = None
        if release is None:
            raise Refused(NO_RELEASE_FOR_BACKUP)
        return release

    def _fpga_loader(self, cf, staging):
        """(loader, timing) for the console chip reads of the full backup, after the checks that read
        nothing: the programmer (openFPGALoader) is there, and Windows lists exactly one JTAG bridge and the
        console's USB serial interface (enumeration only). Refused before any chip is read otherwise."""
        timing = self.fpga_timing or cf.FpgaTiming()
        factory = self.fpga_loader_factory or (lambda logs: cf.JtagLoader(logs, timing=timing))
        loader = factory(staging / 'logs')
        loader.check_tool()
        self._one_bridge(loader)
        return loader, timing

    @staticmethod
    def _one_bridge(loader):
        found = loader.present()
        if found['gwu2x'] != 1:
            raise Refused(f'expected exactly one Chromatic JTAG bridge (GWU2X) on USB; found {found["gwu2x"]}. Plug in '
                          'only your Chromatic, switched on. Nothing was saved or written.')
        if not found['serial']:
            raise Refused('the console\'s USB serial interface is not there. Switch it on and try again. Nothing was '
                          'saved or written.')

    def _read_fpga_twice(self, loader, timing, staging):
        """Two full reads of the console chip flash ([image, image]), whether the console's USB serial
        interface went away during the first, and (serial back, one JTAG bridge) after the FPGA was
        reconfigured from its flash. Read commands only: --detect, --dump-flash, --reset. The loader
        comes from _fpga_loader (programmer checked); the bridge is looked for again here."""
        import chromatic_fpga as cf
        self._one_bridge(loader)
        chip = loader.detect()
        if chip.get('idcode') != cf.IDCODE or cf.FAMILY not in str(chip.get('family', '')):
            raise Refused('the console chip is not the GW5A-25 this tool knows. Nothing was saved or written.')
        left, absent = [], [0]

        def watch():
            """The port counts as gone only when two looks in a row find it missing; a listing that
            cannot be read counts as nothing (neither there nor gone)."""
            pids = getattr(loader, 'serial_pids', None)
            if pids is not None:
                found = pids()
                if found is None:
                    return
                gone = not found
            else:
                gone = not loader.serial_present()
            absent[0] = absent[0] + 1 if gone else 0
            if absent[0] >= 2:
                left.append(True)
        reads = []
        try:
            for number in (1, 2):
                self.say(f'Console chip read {number} of 2.')
                data = loader.dump(staging / f'fpga-read-{number}.bin', during=watch if number == 1 else None)
                reads.append(bytes(data))
        finally:
            back = self._fpga_back(loader, timing)
        return reads, bool(left), back

    def _fpga_back(self, loader, timing):
        """Reconfigure the FPGA from its own flash (--reset) and watch the console come back:
        (its USB serial interface returned, exactly one JTAG bridge). Never raises."""
        try:
            loader.reset()
        except Exception:
            return False, False
        deadline = self.clock() + timing.boot
        while True:
            try:
                found = loader.present()
            except Exception:
                found = {'serial': False, 'gwu2x': 0}
            if found['serial'] and found['gwu2x'] == 1:
                return True, True
            if self.clock() >= deadline:
                return bool(found['serial']), found['gwu2x'] == 1
            self.sleep(timing.poll)

    def _save_full_backup(self, vb, cf, mcu, fpga, found, serial_back, bridge_back):
        """Steps 3 and 4 of full_backup. Returns the backup folder; removes everything it made on failure."""
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')
        folder = PRIVATE / f'{FULL_BACKUP_PREFIX}{stamp}'
        second = SECOND_COPY / folder.name
        latest = PRIVATE / LATEST_FULL_BACKUP
        if latest.exists():  # an existing original is never replaced
            raise Refused('a full backup is already recorded on this PC, and it is never replaced. Nothing was saved.')
        registered = False
        made = []
        try:
            folder.mkdir(parents=True, exist_ok=False)
            made.append(folder)
            for number in (1, 2):
                write_durable(folder / f'mcu-read-{number}.bin', mcu)
                write_durable(folder / f'fpga-read-{number}.bin', fpga)
            records = {
                'mcu-identity.json': {'chip': found['chip'], 'revision': found['revision'] * 100, 'mac': found['mac'],
                                      'flash_id': found['flash_id'], 'size': SIZE},
                'mcu-verified.json': {'size': SIZE, 'sha256': sha(mcu), 'reads': 2, 'chunk_md5_verified': True,
                                      'saved_files_rehashed': True, 'setup_sectors': 'left out (saved erased)'},
                'fpga-verified.json': {'size': SIZE, 'sha256': sha(fpga), 'reads': 2, 'saved_files_rehashed': True,
                                       'idcode': f'{cf.IDCODE:08x}', 'flash_jedec': cf.FLASH_JEDEC},
                'same-unit-usb-proof.json': {'serial_disappeared_during_fpga_read': True},
            }
            for name, value in records.items():
                vb.write_json_durable(folder / name, value)
            bundle = vb.make_bundle(folder)  # validates the saved reads again, writes and re-reads the bundle
            if second.exists():
                raise ValueError('second copy folder already exists')
            made.append(second)
            vb.copy_bundle(bundle, second)
            vb.write_completion_receipt(folder, second)
            vb.write_json_durable(folder / 'recovery-validation.json', {
                'format': 1, 'checked_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
                'serial_returned': bool(serial_back), 'jtag_remains_available': bool(bridge_back),
                'no_flash_program_requested': True, 'esp32_reads_identical': True, 'fpga_reads_identical': True,
                'how': 'Chromatic Manager full backup: read commands only; the console chip was reconfigured from '
                       'its untouched flash and the console watched coming back on USB.'})
            from hardware_preflight import check_backup_copies
            check_backup_copies(folder, second)
            load_stock(bundle, second)
            latest.parent.mkdir(parents=True, exist_ok=True)
            if latest.exists():
                raise Refused('a full backup was recorded meanwhile; it is never replaced. Nothing was saved.')
            registered = True
            write_durable(latest, str(folder).encode('utf-8'))
            cf.OriginalBackup.load(latest)
            stock = load_stock()
            if stock.image != mcu:
                raise ValueError('registered backup does not load back')
            return folder
        except BaseException:
            if registered:  # only the record this run wrote is taken back
                try:
                    latest.unlink(missing_ok=True)
                except OSError:
                    pass
            for path in reversed(made):
                shutil.rmtree(path, ignore_errors=True)
            raise

    # ---- jobs ------------------------------------------------------------------
    def run_job(self, kind, before, target, bootloader_last, boot_check, identity):
        """Never raises: a failure is undone (install) or finished (everything else)."""
        writes = False
        self.last_written = None
        try:
            self.stop_point()  # last moment a job can still be stopped with nothing written
            self.folder.mkdir(parents=True, exist_ok=True)
            for data in (before, target):
                write_durable(self.folder / f'image-{sha(data)}.bin', data)
            self.journal.append('plan', 'ready', kind=kind, before=sha(before), target=sha(target),
                                bootloader_last=bootloader_last, identity=identity)
            writes = True
            self.write_and_verify(before, target, bootloader_last)
            self.last_written = target  # the chip's whole-flash MD5 matched it
            if not boot_check():
                raise StepFailed('The console did not start as expected after the write.')
            self.journal.append('close', 'done', image=sha(target))
            self.journal.archive()
            return EXIT_OK
        except (Exception, KeyboardInterrupt) as error:
            self.close_device(reset=False)
            if not writes or not self.journal.load().writes_started:
                try:
                    if self.journal.exists:
                        self.journal.append('close', 'nothing-written', reason=type(error).__name__)
                        self.journal.archive()
                except Exception:
                    pass
                self.close_device()
                self.say('Stopped before any write; nothing changed.')
                return EXIT_REFUSED
            try:
                self.journal.append('job', 'failed', reason=type(error).__name__)
            except Exception:
                pass
            self.say('The job did not finish; putting the console right now.')
            return self.recover()

    def write_and_verify(self, current, target, bootloader_last):
        regions = ordered_regions(current, target, bootloader_last)
        for number, (offset, data) in enumerate(regions, 1):
            self.journal.append('write', 'region', offset=offset, size=len(data))
            erased = data == b'\xff' * len(data)
            self.say(f'{"Erasing" if erased else "Writing"} step {number} of {len(regions)} ({len(data) // 1024} KiB)...')
            if erased:
                self.bounded(self.timing.erase, lambda o=offset, n=len(data): self.device.erase(o, n))
            else:
                self.bounded(self.timing.write(len(data)), lambda o=offset, d=data: self.device.write(o, d))
        found = self.bounded(self.timing.md5, lambda: self.device.md5(0, SIZE))
        if found != hashlib.md5(target).hexdigest():
            raise StepFailed('The whole-flash check on the chip does not match.')

    def recover(self):
        """Finish or undo the journalled job: install is undone, everything else finished.
        Refuses to write if the console no longer looks like this job left it; if it then
        boots something known, the job is closed as no longer applying."""
        self.journal.load()
        kind = self.journal.value('kind')
        before_sha, target_sha = self.journal.value('before'), self.journal.value('target')
        images = {}
        for name, digest in (('before', before_sha), ('target', target_sha)):
            path = self.folder / f'image-{digest}.bin' if digest else None
            if not path or not path.exists() or sha(path.read_bytes()) != digest:
                return self.manual('The saved images for this job are missing. Choose Status, then Abandon the '
                                   'unfinished job if the console works.')
            images[name] = path.read_bytes()
        bootloader_last = bool(self.journal.value('bootloader_last'))
        goal = images['before'] if kind == 'install' else images['target']
        identity = self.journal.value('identity')
        if not identity:
            try:
                identity, _ = self.unit_identity(first_backup=False)  # recovery never starts a backup
            except Refused:
                return self.manual('No record of which console this job was for.')
        try:
            release = self.release_loader()
        except Refused:
            release = None
        keep_awake(True)
        try:
            for attempt in range(self.timing.attempts):
                try:
                    self.journal.append('recover', 'attempt', number=attempt + 1)
                    if self.device is None:
                        self.device = self.factory()
                    self.connect(identity)
                    current = self.read()
                    if not consistent_with_job(current, images['before'], images['target'], bootloader_last):
                        state = describe(current, release).name if release else 'unknown'
                        if state in BOOTS_STOCK:
                            self.close_device()
                            self.journal.append('close', 'superseded', state=state)
                            self.journal.archive()
                            self.say(f'The console changed since that job started (for example an official update) '
                                     f'and now reads as {state}. Nothing was written; the old job is closed.')
                            return EXIT_OK
                        self.close_device()
                        return self.manual('The console changed since this job started and does not look like a '
                                           'known state. Nothing was written. Choose Restore stock, or see docs/Recovery.md.')
                    goal_now = goal[:SETTINGS[0]] + span(current, SETTINGS) + goal[SETTINGS[1]:]
                    self.stop_point()  # the owner's Stop: last moment before this attempt writes
                    # The stock bootloader goes back first whatever the job was.
                    self.write_and_verify(current, goal_now, bootloader_last=False)
                    self.last_written = goal_now
                    # Watch for what the result boots: the start-up menu (an undone update or a
                    # finished setup on an installed console) or stock.
                    menu = release is not None and describe(goal_now, release).name in ('installed', 'outdated')
                    seen = self.menu_or_stock_started(menu)
                    self.journal.append('close', 'recovered', image=sha(goal_now), seen=seen)
                    self.journal.archive()
                    what = 'undone' if kind == 'install' else 'finished'
                    if not seen:
                        self.say(f'The unfinished job was {what} and the flash checks out, but the console was not '
                                 'seen starting. Unplug USB, switch it off and on, and check a game plays.')
                        return EXIT_NOT_SEEN
                    self.say(f'The unfinished job was {what}; the flash checks out and '
                             f'{"the start-up menu" if menu else "stock"} was seen starting.')
                    return EXIT_RECOVERED
                except WrongUnit:
                    self.close_device()
                    return self.manual('This is not the console the job was for. Plug in the right one.')
                except (Exception, KeyboardInterrupt) as error:
                    self.close_device()
                    if getattr(error, 'owner_stop', False):  # Stop pressed before this attempt wrote
                        self.journal.append('recover', 'stopped-by-owner')
                        self.say('Stopped before anything was written. The unfinished job still waits; finish '
                                 'it when you are ready.')
                        return EXIT_REFUSED
                    self.journal.append('recover', 'attempt-failed')
                    self.sleep(self.timing.poll)
            return self.manual()
        finally:
            keep_awake(False)

    def manual(self, why=None):
        try:
            self.journal.append('recover', 'manual-required')
        except Exception:
            pass
        self.close_device()
        if why:
            self.say(why)
        self.say(MANUAL)
        return EXIT_MANUAL

    def abandon(self):
        """Close an unfinished job without writing, after the person has checked the console."""
        self.journal.load()
        if not self.journal.exists or self.journal.closed:
            self.say('No unfinished job.')
            return EXIT_OK
        self.journal.append('close', 'abandoned-by-user')
        self.journal.archive()
        self.say('The unfinished job is closed. Nothing was written.')
        return EXIT_OK

    def open_job_guard(self):
        self.journal.load()
        if self.journal.exists and not self.journal.closed:
            if not self.journal.writes_started:
                self.journal.append('close', 'nothing-written')  # opened, but never wrote
                self.journal.archive()
            else:
                raise Refused('An unfinished job exists. Finish or abandon it first (menu).')
        elif self.journal.exists:
            self.journal.archive()
        self.journal.load()

    def guarded(self, job):
        """Common frame: refusals and errors before any write leave the chip running stock."""
        try:
            return job()
        except Refused as error:
            self.close_device()
            self.say(f'Refused, nothing written: {error}')
            return EXIT_REFUSED
        except (StepFailed, OSError, RuntimeError, KeyboardInterrupt) as error:
            self.close_device()
            self.say(f'Stopped before any write: {error if not isinstance(error, KeyboardInterrupt) else "cancelled"}')
            return EXIT_REFUSED

    def install(self, current=None):
        """With `current`, uses that fresh read (same connection) instead of reading again."""
        def job():
            self.open_job_guard()
            release = self.release_loader()
            identity, backups = self.unit_identity()
            image = current
            if image is None:
                self.connect(identity)
                image = self.identify(release, backups)
            backed_up = any(span(b, APP) == span(image, APP) for _, b, _ in backups)
            if not backed_up and not (self.last_read_full and image is self.last_image):
                image = self.read()  # identify() already reads these in full; a backup only ever comes from a full read
            target = install_target(image, release)
            if target == image:
                self.close_device()
                self.say(f'Launcher {release.version} is already installed. Nothing written.')
                return EXIT_OK
            if not backed_up:
                self.say('Backing up the stock firmware now on the console first.')
                self.save_backup(image, self.device.identity())
            self.say('When the start-up menu appears on the console, leave it alone until this tool says it is '
                     'done (about 20 seconds after the menu appears).')
            result = self.run_job('install', image, target, True, self.launcher_started, identity)
            if result == EXIT_OK:
                after = ('It is waiting on the console now: choose Games to play.' if menu_waits(release.version)
                         else 'It counts down on the console, then starts the games by itself.')
                self.say(f'Start-up menu {release.version} installed. The whole-flash check matches, and the menu '
                         f'was seen running. {after}')
            return result
        return self.guarded(job)

    def _switch(self, kind, plan, done):
        def job():
            self.open_job_guard()
            release = self.release_loader()
            identity, backups = self.unit_identity()
            self.connect(identity)
            image = self.identify(release, backups)
            target = plan(image, release, backups)
            if target is None or target == image:
                self.close_device()
                self.say('Nothing to switch off. Nothing written.')
                return EXIT_OK
            old = self.setup_record(image)
            result = self.run_job(kind, image, target, False, self.stock_started, identity)
            if result == EXIT_OK:
                self.say(done)
            self.forget_erased_jellyfin(old.jellyfin if old else None)
            return result
        return self.guarded(job)

    def remove(self):
        def plan(image, release, backups):
            state = describe(image, release)
            if state.name == 'stock':
                return None
            if state.name not in ('installed', 'outdated', 'partial', 'dormant'):
                raise Refused(f'Switch-off refused: console state is {state.name}.')
            return remove_target(image, release, stock_boot_source(backups, image, release))
        return self._switch('remove', plan, 'Start-up menu removed: stock bootloader and table back, launcher area '
                            'erased (with its Wi-Fi, PC pairing and Jellyfin setup). Checked on the chip, and stock was '
                            'seen starting.')

    def disable(self):
        """Power-safe switch-off used before an official update: erase the launcher only."""
        def plan(image, release, backups):
            state = describe(image, release)
            if state.name in ('stock', 'partial') and span(image, SLOT_APP) == b'\xff' * (SLOT_APP[1] - SLOT_APP[0]):
                return None
            if state.name not in ('installed', 'outdated', 'partial', 'dormant'):
                raise Refused(f'Switch-off refused: console state is {state.name}.')
            return disable_target(image, release)
        return self._switch('disable', plan, 'Start-up menu switched off (its area erased; Wi-Fi and Jellyfin setup '
                                             'kept). Stock was seen starting.')

    def provision(self, record):
        """Write this chromatic_provision.Record (Wi-Fi, pairing, Jellyfin) to the console."""
        return self.provision_edit(lambda current: record)

    def provision_edit(self, edit, done='Console setup written to the launcher area and checked on the chip.'):
        """Read the console, let `edit(current)` turn the record in flash (a
        chromatic_provision.Record, or None) into the new one, and write that to both
        provisioning sectors, one sector at a time. Nothing else changes; the launcher only
        reads them. `edit` runs after the read, just before the write; it may raise Refused,
        or return None for "nothing to change". Records are never printed."""
        import chromatic_provision as cp

        def job():
            self.open_job_guard()
            release = self.release_loader()
            identity, backups = self.unit_identity()
            self.connect(identity)
            image = self.identify(release, backups)
            state = describe(image, release)
            if state.name == 'stock':
                raise Refused('the start-up menu is not on this console. Install it first.')
            if state.name not in ('installed', 'outdated', 'partial', 'dormant'):
                raise Refused(f'Console setup refused: console state is {state.name}.')
            if not reads_setup_v2(state.launcher_version):
                needed = '.'.join(map(str, PROVISION_V2_LAUNCHER))
                raise Refused(f'the start-up menu on the console ({state.launcher_version or "switched off"}) cannot '
                              f'read this setup; it needs launcher {needed} or newer. Install that first.')
            current, _ = cp.current(span(image, PROVISION_A), span(image, PROVISION_B))
            record = edit(current)
            if record is None:
                self.close_device()
                self.say('Nothing to change on the console. Nothing written.')
                return EXIT_OK
            sequence = ((current.sequence + 1) & 0xFFFFFFFF or 1) if current else 1
            sector = replace(record, sequence=sequence, version=cp.VERSION_2).sector()
            same = (current is not None and current.version == cp.VERSION_2
                    and replace(record, sequence=0, version=cp.VERSION_2) == replace(current, sequence=0)
                    and span(image, PROVISION_A) == span(image, PROVISION_B))
            if same:
                self.close_device()
                self.say('The console already has this setup. Nothing written.')
                return EXIT_OK
            target = put(put(image, PROVISION_A, sector), PROVISION_B, sector)
            check_layout_change(image, target, (PROVISION,))
            result = self.run_job('provision', image, target, False, lambda: True, identity)
            if result == EXIT_OK:
                self.close_device()
                self.say(done)
            return result
        return self.guarded(job)

    @staticmethod
    def setup_record(image):
        """The console-setup record in an image (chromatic_provision.Record), or None. Kept in
        memory only; never printed or logged."""
        import chromatic_provision as cp
        if image is None or len(image) != SIZE:
            return None
        return cp.current(span(image, PROVISION_A), span(image, PROVISION_B))[0]

    def written_setup(self):
        """(written, record): whether this run's last job verifiably wrote its image (the
        chip's whole-flash MD5 matched it), and the console-setup record in that image."""
        return self.last_written is not None, self.setup_record(self.last_written)

    def forget_erased_jellyfin(self, old):
        """After a job that erased the console setup (remove, restore): log that console's own
        Jellyfin sign-in out on the server, so no orphan device token is left. Only when the
        erase was verifiably written, only that one token (the link's revoke refuses anything
        but a `chromatic-` console device), never another session. Says yes/no only."""
        if old is None:
            return
        written, record = self.written_setup()
        still = record.jellyfin if record else None
        if not written or (still is not None and still.access_token == old.access_token):
            return
        try:
            gone = jellyfin_linker(old.url).revoke(old.access_token, old.device_id)
        except Exception:
            gone = False
        self.say('The console\'s Jellyfin sign-in was logged out on the server.' if gone else
                 'The console\'s Jellyfin sign-in could not be logged out; remove the "Chromatic" device in '
                 'Jellyfin\'s Dashboard > Devices.')

    def console_has_setup(self):
        """Does the console (as last read or identified) hold a console-setup record? None
        when nothing has been read. Says yes or no only; the record itself stays unread."""
        import chromatic_provision as cp
        if self.last_image is None or len(self.last_image) != SIZE:
            return None
        record, _ = cp.current(span(self.last_image, PROVISION_A), span(self.last_image, PROVISION_B))
        return record is not None

    def console_setup(self, image):
        """One line about the provisioning record: counts and yes/no only."""
        import chromatic_provision as cp
        record, _ = cp.current(span(image, PROVISION_A), span(image, PROVISION_B))
        if record is None:
            return 'Console setup: none (no Wi-Fi, PC pairing or Jellyfin on the console).'
        found = record.summary()
        jellyfin = JELLYFIN_WORDS[console_token_state(record.jellyfin)] if found['jellyfin'] else 'not set'
        return (f'Console setup: {found["networks"]} Wi-Fi network(s), PC pairing '
                f'{"yes" if found["pc_gateway"] else "no"}, Jellyfin {jellyfin}.')

    def restore_stock(self, which=None, allow_version_change=False, confirm=None):
        """Whole MCU image from a verified backup (newest by default), keeping today's stock settings (NVS,
        PHY). The start-up menu's setup sectors (Wi-Fi networks, PC pairing, Jellyfin sign-in) are erased."""
        def job():
            self.open_job_guard()
            release = self.release_loader()
            identity, backups = self.unit_identity()
            choices = [(name, image) for name, image, _ in backups if stock_description(image)]
            if which:
                choices = [c for c in choices if c[0] == which]
            if not choices:
                raise Refused('No matching verified backup of ModRetro stock firmware.')
            name, backup = choices[0]
            self.connect(identity)
            current = self.read()
            target = restore_target(current, backup, release, stock_boot_source(backups, current, release))
            if target == current:
                self.close_device()
                self.say('The console already matches that backup. Nothing written.')
                return EXIT_OK
            if span(current, APP) != span(target, APP):
                now_app, then_app = stock_description(current), stock_description(target)
                self.say(f'This changes the stock firmware from {now_app[1] if now_app else "damaged/other"} to '
                         f'{then_app[1]} (backup "{name}"). If an official update recently changed the console\'s '
                         'FPGA, an older MCU firmware may not match it; prefer the official updater then.')
                agreed = allow_version_change or (confirm is not None and confirm(
                    'Type change to continue with this version change: ').strip().lower() == 'change')
                if not agreed:
                    raise Refused('the stock firmware version would change and that was not confirmed.')
                self.say('Backing up the console as it is now, first.')
                self.save_backup(current, self.device.identity())
            old = self.setup_record(current)
            result = self.run_job('restore', current, target, False, self.stock_started, identity)
            if result == EXIT_OK:
                self.say(f'Stock restored from backup "{name}". Checked on the chip; ModRetro\'s settings kept; the '
                         'start-up menu\'s Wi-Fi, PC pairing and Jellyfin setup erased; stock seen starting.')
            self.forget_erased_jellyfin(old.jellyfin if old else None)
            return result
        return self.guarded(job)

    def resume(self):
        """Finish an unfinished job. Only on explicit request, never as a side effect."""
        self.journal.load()
        if not self.journal.exists:
            self.say('No unfinished job.')
            return EXIT_OK
        if self.journal.closed:
            self.journal.archive()
            self.say('The last job had already finished.')
            return EXIT_OK
        if not self.journal.writes_started:
            self.journal.append('close', 'nothing-written')
            self.journal.archive()
            self.say('The last job stopped before writing anything. Nothing to do.')
            return EXIT_OK
        self.say('Finishing the unfinished job.')
        return self.recover()

    def pending(self):
        self.journal.load()
        return self.journal.exists and not self.journal.closed and self.journal.writes_started

    # ---- guided official update -------------------------------------------------
    def guided_update(self, ask):
        """Official update with the start-up menu kept. Every step is a tested job, and
        stopping between any two of them leaves a console that boots stock."""
        if self.pending():
            self.say('An earlier job did not finish. Finish it first.')
            return EXIT_REFUSED
        keep_awake(True)
        try:
            return self._guided(ask)
        finally:
            keep_awake(False)

    def _ask(self, ask, prompt, answers):
        while True:
            try:
                answer = ask(prompt).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return 'cancel'
            if answer in answers:
                return answer
            self.say(f'Please type one of: {", ".join(answers)}.')

    def _guided(self, ask):
        self.say('Step 1 of 4: switching the start-up menu off (only its own area is erased, which is safe '
                 'even if power is lost). This checks the console first: about 20 seconds when its firmware is '
                 'already backed up, about 6.5 minutes when it must be read in full.')
        self.last_image = None
        result = self.disable()
        if result != EXIT_OK:
            self.say('Stopped at step 1. Nothing else was done.')
            return result
        before = self.last_image
        try:
            identity, backups = self.unit_identity()
            if before is not None and not any(span(b, APP) == span(before, APP) for _, b, _ in backups):
                self.save_backup(before, identity)
                self.say('Backed up the firmware as it was before the update.')
        except (Refused, StepFailed, OSError) as error:
            self.say(f'Could not take a backup ({error}). Stopping here; the console runs stock.')
            return EXIT_REFUSED
        self.owner.need(UPDATER_TITLE, UPDATER_TEXT, log=(
            'Step 2 of 4: run ModRetro\'s updater now. Leave this window open.\n'
            '  Download it from ModRetro (modretro.com) if you have not yet; you can keep it in the\n'
            '  official-updater folder of your Jellymatic folder.\n'
            '  - Use only its firmware update. Do not use Cart Clinic.\n'
            '  - Keep the console plugged in and switched ON, and keep this PC awake.\n'
            '  - If it cannot determine your version, choose Update anyway.\n'
            '  - Never close it while it is working. It erases console settings; that is how it works.\n'
            '  - When it reports success, CLOSE the updater, then come back here.\n'
            '  - If it reports an error: close it, unplug USB, switch the console off and on, plug in,\n'
            '    and run the updater again before coming back.'))
        try:
            answer = self._ask(ask, 'Type done once the updater reported success and is closed, failed if it could '
                                    'not finish, or cancel to stop here with the menu off: ',
                               ('done', 'failed', 'cancel'))
        except BaseException:
            self.owner.close()
            raise
        if answer == 'done':
            self.owner.done('The updater finished and is closed.')
        else:
            self.owner.close()
        if answer == 'cancel':
            self.say('Stopped. The menu stays off and the console runs stock. Choose Install any time to switch it '
                     'back on.')
            return EXIT_OK
        if answer == 'failed':
            self.say('The menu stays off. Try the updater again (unplug, switch off and on first). If it still fails, '
                     'see docs/Recovery.md, "Official updates and restoring Chromatic OS". Do not use Restore stock to undo a '
                     'half-finished update: the FPGA may already be new.')
            return EXIT_OK
        self.say('Step 3 of 4: checking the console and backing up the new firmware (new firmware is read in full, '
                 'about 6.5 minutes; unchanged firmware about 20 seconds).')
        release = self.release_loader()
        try:
            identity, backups_now = self.unit_identity()
            if self.device is None:
                self.device = self.factory()
            self.connect(identity)
            after = self.identify(release, backups_now)
        except (Refused, StepFailed, OSError, RuntimeError) as error:
            self.close_device()
            self.say(f'Could not reach the console ({error}). Make sure the updater is closed, then choose Install '
                     'from the menu; the console runs stock meanwhile.')
            return EXIT_REFUSED
        state = describe(after, release)
        old, new = stock_description(before) if before else None, stock_description(after)
        if state.name == 'damaged':
            self.close_device()
            self.say('The console\'s firmware is damaged: the updater did not finish writing it (it does not check '
                     'its own result). Run the official updater again. Do not use Restore stock: the FPGA may already '
                     'be updated.')
            return EXIT_REFUSED
        if state.name == 'unknown' and boot_valid(after) and app_description(span(after, APP)):
            self.close_device()
            self.say('The update worked, but this new firmware uses a layout the start-up menu does not support yet. '
                     'The console runs it normally. Keep it; do not restore.')
            return EXIT_OK
        if old and new and old == new:
            changed = self._ask(ask, 'The console\'s firmware did not change. Did the updater say it installed an '
                                     'update? Type yes or no: ', ('yes', 'no'))
            if changed == 'yes':
                self.close_device()
                self.say('Then its final step failed silently. The menu stays off. Run the updater again '
                         '(unplug, switch off and on first), then choose Install.')
                return EXIT_REFUSED
        elif old and new:
            self.say(f'Stock firmware updated from {old[1]} to {new[1]}.')
        self.say('Step 4 of 4: switching the start-up menu back on.')
        self.last_boot_log = None
        result = self.install(current=after)
        if result == EXIT_OK:
            self.say('Done: official update applied and the start-up menu is back on.')
            # ModRetro's updater writes its own console chip (FPGA) image, so a Jellyfin sound image is
            # gone afterwards. The start-up menu names the chip's version as it starts (no JTAG): say
            # it, so the window can offer the sound setup again (chromatic_manager_core.sound_after_update).
            log = getattr(self, 'last_boot_log', None)
            if isinstance(log, (bytes, bytearray)) and launcher_fpga(bytes(log)) is not None:
                self.say(fpga_status_line(launcher_fpga(bytes(log))))
        else:
            self.say('The start-up menu was not switched back on; the console runs the new stock firmware normally.')
        return result

    # ---- boot checks ---------------------------------------------------------
    def launcher_started(self):
        """Watch from the launcher's start: it must keep running (its menu waits for the
        owner) for the quiet window. Never needs a button press."""
        self.last_boot_log = None
        log, quiet = self.bounded(self.timing.launcher_watch + 30, lambda: self.device.watch(
            self.timing.launcher_watch, READY, self.timing.quiet_after_handover))
        self.last_boot_log = log  # the guided update reads the launcher's FPGA line from it
        self.close_device(reset=False)
        return launcher_boot_ok(log, quiet)

    def menu_or_stock_started(self, menu):
        """The boot check for an image: the launcher's (menu opens, or a normal hand-over) when
        it boots the start-up menu, stock's otherwise. Never raises."""
        if not menu:
            return self.stock_started()
        try:
            return self.launcher_started()
        except (Exception, KeyboardInterrupt):
            self.close_device(reset=False)
            return False

    def stock_started(self):
        try:
            log, _ = self.bounded(self.timing.stock_watch + 30,
                                  lambda: self.device.watch(self.timing.stock_watch, None, 0))
        except (Exception, KeyboardInterrupt):
            return False
        finally:
            self.close_device(reset=False)
        return stock_boot_ok(log)


class ManagerProgrammer:
    """esptool 4.12 over the console's USB serial port. Needs only that port: the FPGA's
    JTAG interface is not required. Each call uses its own handle, so a call that ran
    past its deadline cannot send commands on a later connection."""
    CHUNK = 256 * 1024
    progress = None  # optional callable(done, total) for byte counts; the window sets it, the CLI does not

    def __init__(self):
        self.esp = None
        self.found = {}

    def _report(self, done, total):
        if self.progress is not None:
            try:
                self.progress(done, total)
            except Exception:
                pass  # display only: it can never stop a read or a write

    @staticmethod
    def ports():
        """The console's USB serial port. ModRetro's FPGA puts the player number in the PID's
        low byte ("Chromatic - Player 01" is 0x0101); it is 0x0100 ("Player XX") until stock
        firmware sets it, e.g. while the start-up menu is showing. Any player number is
        accepted; the chip, MAC and flash ID are still matched to the backups."""
        from serial.tools import list_ports
        return [p.device for p in list_ports.comports() if p.vid == 0x374e and p.pid is not None
                and p.pid >> 8 == 0x01]

    def device_present(self):
        return len(self.ports()) == 1

    def connect(self):
        import esptool
        self.close(False)
        ports = self.ports()
        if len(ports) != 1:
            raise StepFailed('Expected exactly one Chromatic serial port.' if ports else
                             'No Chromatic serial port; is it plugged in and switched on?')
        esp = esptool.CHIP_DEFS['esp32'](ports[0], 115200, False)
        self.esp = esp
        esp.connect('default_reset', 3)
        description, revision = esp.get_chip_description(), esp.get_chip_revision()
        self.found = {'chip': description.split(' ')[0], 'revision': revision // 100,
                      'mac': ':'.join(f'{b:02x}' for b in esp.read_mac())}
        esp = esp.run_stub()
        self.esp = esp
        if esp.flash_id() != 0x164020:
            raise StepFailed('Unexpected flash chip.')
        self.found['flash_id'] = '204016'
        esp.flash_set_parameters(SIZE)

    def identity(self):
        return self.found

    def read_all(self, say=lambda message: None):
        esp, data = self.esp, bytearray()
        for offset in range(0, SIZE, self.CHUNK):
            chunk = esp.read_flash(offset, self.CHUNK, progress_fn=None if self.progress is None else
                                   lambda got, _size, base=offset: self._report(base + got, SIZE))
            if len(chunk) != self.CHUNK or hashlib.md5(chunk).hexdigest() != esp.flash_md5sum(offset, self.CHUNK).lower():
                raise StepFailed('A flash read failed its on-chip checksum.')
            data += chunk
            if (offset // self.CHUNK) % 4 == 3:
                say(f'  read {(offset + self.CHUNK) * 100 // SIZE}%')
        return bytes(data)

    def read(self, offset, size):
        """`size` bytes from `offset`, each piece of up to 256 KiB checked against the chip's MD5."""
        esp, data = self.esp, bytearray()
        for start in range(offset, offset + size, self.CHUNK):
            length = min(self.CHUNK, offset + size - start)
            chunk = esp.read_flash(start, length, progress_fn=None if self.progress is None else
                                   lambda got, _size, base=start - offset: self._report(base + got, size))
            if len(chunk) != length or hashlib.md5(chunk).hexdigest() != esp.flash_md5sum(start, length).lower():
                raise StepFailed('A flash read failed its on-chip checksum.')
            data += chunk
        return bytes(data)

    def md5(self, offset, size):
        return self.esp.flash_md5sum(offset, size).lower()

    def erase(self, offset, size):
        esp = self.esp
        if offset % SECTOR or size % SECTOR:
            raise StepFailed('Erases must be whole sectors.')
        esp.erase_region(offset, size)
        if esp.flash_md5sum(offset, size).lower() != hashlib.md5(b'\xff' * size).hexdigest():
            raise StepFailed('An erased region failed its on-chip check.')

    def write(self, offset, data):
        from esptool.loader import ESPLoader
        esp = self.esp
        if offset % SECTOR or len(data) % SECTOR:
            raise StepFailed('Writes must be whole sectors.')  # so nothing outside is erased
        size = esp.FLASH_WRITE_SIZE
        blocks = esp.flash_begin(len(data), offset)
        for number in range(blocks):
            block = data[number * size:(number + 1) * size]
            esp.flash_block(block + b'\xff' * (size - len(block)), number, timeout=30)
            self._report(min((number + 1) * size, len(data)), len(data))
        esp.read_reg(ESPLoader.CHIP_DETECT_MAGIC_REG_ADDR, timeout=30)  # waits for the last block
        if hashlib.md5(data).hexdigest() != esp.flash_md5sum(offset, len(data)).lower():
            raise StepFailed('A written region failed its on-chip checksum.')

    def watch(self, seconds, handover, quiet):
        """Reset into normal boot and record the serial output. With `handover`, keep
        listening until `quiet` seconds have passed after it. Returns (log, quiet_seen)."""
        esp = self.esp
        port = esp._port
        esp.hard_reset()
        port.timeout = 0.1  # esptool leaves it blocking; reads must not hang past the window
        log, start, since = b'', time.monotonic(), None
        try:
            while time.monotonic() - start < seconds:
                log = (log + port.read(256))[-65536:]
                if handover and since is None and handover in log:
                    since = time.monotonic()
                if since is not None and time.monotonic() - since >= quiet:
                    return log, True
            return log, False
        finally:
            port.close()
            self.esp = None

    def close(self, reset=True):
        esp, self.esp = self.esp, None
        if esp is None:
            return
        try:
            if reset:
                esp.hard_reset()  # leave the chip running its firmware, not the flasher stub
        finally:
            esp._port.close()


WIFI_FILE = PRIVATE / 'chromatic-wifi-networks.txt'  # owner-written; never read into AI context


def read_wifi_networks(path=None, limit=4):
    """(ssid, password) pairs from the owner's file, in order, up to `limit` (None: all; Wi-Fi
    setup reads all and lets the person choose when there are more than 4). Spaces around
    the '=' are ignored; a pair with an empty name is skipped. Never printed. The default
    path is looked up at call time, so a test that points WIFI_FILE elsewhere is obeyed."""
    networks, ssid = [], None
    for raw in Path(path or WIFI_FILE).read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        name, value = (part.strip() for part in line.split('=', 1))
        if name.lower() == 'ssid':
            ssid = value
        elif name.lower() == 'password' and ssid is not None:
            if ssid:
                networks.append((ssid, value))
            ssid = None
    return networks if limit is None else networks[:limit]


# ---- merging the console's Wi-Fi list --------------------------------
# Wi-Fi setup used to replace every saved network, so networks the owner added on the console
# were lost. It now reads the console's list first and merges: networks from this setup are
# added, or update the console's network of the same name (its new password); the console's
# other networks are kept with their passwords untouched. When the console already had
# networks (or the list is longer than the console keeps), the person sees the whole list,
# names only, and may reorder it or remove networks before anything is written.
WIFI_LIST_PROMPT = 'Wi-Fi networks the console will have'
NOTE_NEW, NOTE_PASSWORD, NOTE_KEPT = 'new', 'new password', 'already saved, password kept'
WIFI_LIST_TRIES = 5


def merge_networks(console, typed):
    """[(chromatic_provision.Network, note)]: this setup's networks first, in their order (a
    name that matches a console network updates it), then the console's other networks in
    their order, passwords exactly as they are. A name given twice counts once (the first)."""
    import chromatic_provision as cp
    on_console = {network.ssid: network for network in console}
    entries, names = [], set()
    for ssid, psk in typed:
        if ssid in names:
            continue
        names.add(ssid)
        old = on_console.get(ssid)
        note = NOTE_NEW if old is None else NOTE_KEPT if old.psk == psk else NOTE_PASSWORD
        entries.append((cp.Network(ssid, psk), note))
    for network in console:
        if network.ssid not in names:
            names.add(network.ssid)
            entries.append((network, NOTE_KEPT))
    return entries


def _printable(name):
    return ''.join(ch if ch.isprintable() else '?' for ch in name)


def network_list_prompt(entries, problem=''):
    """The question that shows the merged list (names only, never a password). The window
    reads it as the Wi-Fi list dialog; the text menu shows it with names masked."""
    import chromatic_provision as cp
    lines = [f'{WIFI_LIST_PROMPT} ({len(entries)}; it tries them from the top and keeps at most '
             f'{cp.MAX_NETWORKS}):']
    lines += [f'  {n}. network "{_printable(network.ssid)}" ({note})' for n, (network, note) in enumerate(entries, 1)]
    if problem:
        lines.append(problem)
    lines.append('Type ok to write this list, the numbers in a new order to change it (for example 2,1; a number '
                 'left out is removed), or cancel: ')
    return '\n'.join(lines)


def chosen_networks(entries, answer):
    """The list the person chose, or None for cancel. ValueError: one plain sentence."""
    import chromatic_provision as cp
    text = str(answer or '').strip().lower()
    if text in ('', 'cancel', 'no', 'n', 'q', 'quit'):
        return None
    if text in ('ok', 'yes', 'y', 'write'):
        chosen = list(entries)
    else:
        parts = [part for part in re.split(r'[\s,;]+', text) if part]
        if not parts or not all(part.isdigit() for part in parts):
            raise ValueError('Type ok, cancel, or the numbers in the order you want, such as 2,1.')
        order = [int(part) - 1 for part in parts]
        if any(not 0 <= n < len(entries) for n in order) or len(set(order)) != len(order):
            raise ValueError(f'Use each number from 1 to {len(entries)} at most once.')
        chosen = [entries[n] for n in order]
    if not chosen:
        raise ValueError('Keep at least one network.')
    if len(chosen) > cp.MAX_NETWORKS:
        extra = len(chosen) - cp.MAX_NETWORKS
        raise ValueError(f'The console keeps at most {cp.MAX_NETWORKS} networks. Remove {extra} to go on '
                         f'(leave {"its number" if extra == 1 else "their numbers"} out).')
    return chosen


def choose_networks(manager, ask, entries, console_count):
    """Ask about the merged list when there is something to decide; the chosen entries.
    Refused (nothing written) when the person cancels or no usable answer comes."""
    import chromatic_provision as cp
    if not console_count and len(entries) <= cp.MAX_NETWORKS:
        return entries  # nothing on the console to keep or remove, and it all fits
    problem = ''
    for _ in range(WIFI_LIST_TRIES):
        try:
            chosen = chosen_networks(entries, ask(network_list_prompt(entries, problem)))
        except ValueError as error:
            problem = str(error)
            continue
        if chosen is None:
            raise Refused('the Wi-Fi list was not confirmed.')
        return chosen
    raise Refused('the Wi-Fi list was not confirmed.')


def wifi_change_words(console, chosen):
    """One line in counts only: what this setup does to the console's Wi-Fi list."""
    names = {network.ssid for network in console}
    kept_names = {network.ssid for network, _ in chosen}
    added = sum(1 for network, note in chosen if note == NOTE_NEW)
    changed = sum(1 for network, note in chosen if note == NOTE_PASSWORD)
    removed = len(names - kept_names)
    kept = len(chosen) - added - changed
    parts = [f'{n} {word}' for n, word in ((added, 'added'), (changed, 'with a new password'), (kept, 'kept as they were'),
                                           (removed, 'removed')) if n]
    return f'Wi-Fi: {len(chosen)} network(s) on the console ({", ".join(parts)}).'


def private_prompts(ask):
    """The text menu's questions with private details masked, as the window shows them by
    default (owner rule: the privacy mask is on by default). Typing show at a masked question
    shows it once in full; the answer is then asked again."""
    import chromatic_manager_core as core

    def masked(prompt):
        hidden = core.mask_private(prompt)
        if hidden == prompt:
            return ask(prompt)
        answer = ask('(Private details are hidden; type show to see them.)\n' + hidden)
        if str(answer).strip().lower() == 'show':
            answer = ask(prompt)
        return answer
    return masked


# Remote Pilot's copy of the PC pairing key: in the bundled remote-pilot/local/ folder (Git-ignored),
# or REMOTE_PILOT_HOME/local/ when Remote Pilot is installed elsewhere.
REMOTE_PILOT_PAIRING = (Path(os.environ['REMOTE_PILOT_HOME']) if os.environ.get('REMOTE_PILOT_HOME')
                        else ROOT / 'remote-pilot') / 'local/chromatic/pairing.json'
PROVISION_COPY = PRIVATE / 'chromatic-provision.json'


def lan_ipv4():
    """This PC's private LAN address (the one its default route uses). Sends nothing."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(('192.168.0.1', 9))  # UDP connect only picks a route; no packet is sent
        address = probe.getsockname()[0]
    import ipaddress
    return address if ipaddress.IPv4Address(address).is_private else None


def jellyfin_settings_available():
    """Is the PC's private Jellyfin connection file there? (Only its existence is checked.)"""
    import chromatic_jellyfin_link as jl
    return jl.pc_connection_available()


def jellyfin_linker(server_url=None):
    """Quick Connect against the server in the PC's private connection file. With
    `server_url` only (for logging a console token out), no PC credentials are read."""
    import chromatic_jellyfin_link as jl
    if server_url is not None:
        from connection_config import Connection
        return jl.Linker(Connection(server_url))
    return jl.Linker(jl.load_pc_connection())


JELLYFIN_WORDS = {  # what status says about the console's own Jellyfin sign-in (summary 'setup'; core.setup_sign)
    'works': 'linked',
    'refused': 'sign-in no longer works',
    'unreachable': 'linked, not checked (server not reachable from this PC)',
    'unknown': 'linked, not checked',
}


def console_token_state(linked):
    """Status: does the console's own Jellyfin sign-in still work, for
    example after it was removed in the Dashboard? The existing link code (Linker.token_state:
    one GET /Users/Me with the console's token, as the console device) against the address in
    the console's record; no PC credentials are read and nothing is signed in or out. Runs after
    the console is released. 'works', 'refused', 'unreachable', or 'unknown' if it could not run."""
    try:
        return jellyfin_linker(linked.url).token_state(linked)
    except Exception:
        return 'unknown'


def _jellyfin_link(manager, linker):
    """A new console sign-in, or None with a generic reason said. Never prints values."""
    import chromatic_jellyfin_link as jl
    try:
        linked = linker.link()
    except jl.LinkError as error:
        manager.say(f'Jellyfin linked: no. {error}')
        return None
    return linked


def _settle_jellyfin(manager, result, new, old):
    """After a console-setup write: keep exactly one live console sign-in. What counts is what
    was verifiably written (the chip's whole-flash MD5 matched the image), not the exit code:
    a job that recovery closed as superseded (the console had changed) wrote nothing."""
    written, record = manager.written_setup()
    on_console = record.jellyfin if (written and record) else None
    if new is not None:
        linker = jellyfin_linker(new.url)
        if written and on_console is not None and on_console.access_token == new.access_token:
            if old is not None and old.access_token != new.access_token:
                gone = jellyfin_linker(old.url).revoke(old.access_token, old.device_id)
                manager.say('The console\'s previous Jellyfin sign-in was logged out.' if gone else
                            'The previous Jellyfin sign-in could not be logged out; remove the older "Chromatic" '
                            'device in Jellyfin\'s Dashboard > Devices.')
            works = linker.works(new)
            manager.say(f'Jellyfin linked: {"yes" if works else "no"}; server reachable: '
                        f'{"yes" if works or linker.reachable() else "no"}.')
        elif written or result in (EXIT_REFUSED, EXIT_OK):
            # The console verifiably does not hold the new sign-in (nothing written, a job closed
            # as superseded, or a verified image without it): withdraw that unused sign-in only.
            linker.revoke(new.access_token, new.device_id)
            manager.say('Jellyfin: the new sign-in was not written to the console, so it was withdrawn.')
        else:
            manager.say('Jellyfin: the console may hold the new sign-in or the old one. When Check status says the '
                        'console is fine, choose Re-link Jellyfin.')
    elif old is not None and written and (on_console is None or on_console.access_token != old.access_token):
        gone = jellyfin_linker(old.url).revoke(old.access_token, old.device_id)
        manager.say('Jellyfin sign-in removed from the console and logged out on the server.' if gone else
                    'Jellyfin sign-in removed from the console; remove the "Chromatic" device in Jellyfin\'s '
                    'Dashboard > Devices to finish.')


def wifi_setup(manager, ask=input, secret=None):
    """Wi-Fi (networks from the private file, or one typed, or none to only reorder or remove),
    merged with the networks already on the console (merge_networks: nothing the owner added on
    the console is lost, its passwords stay as they are), the list confirmed by the person when
    the console had networks (choose_networks: order, removals, at most 4), pairing with this
    PC, and, when the PC's private Jellyfin settings exist, the console's own Jellyfin sign-in
    (Quick Connect: a separate, revocable device token; the password never leaves the PC).
    Passwords, keys and tokens are never printed; copies stay in ignored private files."""
    import getpass
    import secrets
    import chromatic_provision as cp
    import wifi_gateway
    secret = secret or getpass.getpass
    if not (wifi_gateway.IDENTITY / 'pairing.bin').exists():
        manager.say('Creating this PC\'s private Jellyfin gateway identity (once).')
        wifi_gateway.initialize()
    der = (wifi_gateway.IDENTITY / 'server.der').read_bytes()
    token = (wifi_gateway.IDENTITY / 'pairing.bin').read_bytes()
    address = lan_ipv4()
    if not address or ask(f'Use this PC at {address} for Jellyfin? Type yes: ').strip().lower() != 'yes':
        address = ask('This PC\'s LAN address (for example 192.0.2.20): ').strip()
    networks = read_wifi_networks(WIFI_FILE, limit=None) if WIFI_FILE.exists() else []
    if networks:
        manager.say(f'Using {len(networks)} network(s) from your private Wi-Fi file; the console\'s own networks '
                    'are kept.')
    else:
        ssid = ask('Wi-Fi network name to add or change (2.4 GHz). Leave it empty to keep the console\'s networks '
                   'and only change their order or remove some: ').strip()
        if ssid:
            networks = [(ssid, secret('Wi-Fi password (hidden; leave empty for an open network): '))]
    key = secrets.token_bytes(32)
    try:
        gateway = cp.Gateway(address, 8443, hashlib.sha256(der).digest(), token, key).check()
        for ssid, psk in networks:
            cp.Network(ssid, psk).check()
    except ValueError as error:
        manager.say(f'Refused, nothing written: {error}')
        return EXIT_REFUSED
    linker = None
    if jellyfin_settings_available():
        try:
            linker = jellyfin_linker()
            manager.say('Jellyfin: the console will get its own sign-in from your PC settings (Quick Connect).')
        except (OSError, ValueError):
            manager.say('Jellyfin: the PC settings file could not be read, so Jellyfin is not linked this time.')
    seen = {}

    def edit(current):
        console = tuple(current.networks) if current else ()
        entries = merge_networks(console, networks)
        if not entries:
            raise Refused('no Wi-Fi network was typed and the console has none saved. Type your Wi-Fi name.')
        chosen = choose_networks(manager, ask, entries, len(console))
        seen['words'] = wifi_change_words(console, chosen)
        wifi = tuple(network for network, _ in chosen)
        seen['old'] = current.jellyfin if current else None
        jellyfin = seen['old']
        if linker is not None:
            seen['new'] = _jellyfin_link(manager, linker)
            jellyfin = seen['new'] or jellyfin
        return cp.Record(wifi, gateway, jellyfin)
    result = manager.provision_edit(edit, 'Wi-Fi, PC pairing and Jellyfin setup written to the launcher area and '
                                          'checked on the chip.')
    if result == EXIT_OK and seen.get('words'):
        manager.say(seen['words'] + ' The console checks each password when it joins; to fix one there, open '
                    'Setup > Wi-Fi networks.')
    new = seen.get('new')
    _settle_jellyfin(manager, result, new, seen.get('old') if new else None)
    if result == EXIT_OK:
        for path, body in ((PROVISION_COPY, {'chr1_key': key.hex(), 'gateway': address}),
                           (REMOTE_PILOT_PAIRING, {'key': key.hex()})):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                write_durable(path, json.dumps(body).encode('ascii'))
            except OSError:
                manager.say(f'Could not save the pairing copy at {path.parent}; set Remote Pilot up by hand.')
        manager.say('Remote Pilot is paired too. Start the Jellyfin gateway on this PC with '
                    f'--bind {address} before opening Jellyfin on the console.')
    return result


def jellyfin_relink(manager):
    """A new Jellyfin sign-in for the console (Quick Connect from the PC settings); the old
    one is logged out once the new one is written. Wi-Fi and pairing stay as they are."""
    if not jellyfin_settings_available():
        manager.say('Refused, nothing written: the PC has no private Jellyfin settings file '
                    '(jellyfin-connection.txt in your Jellymatic folder; or use the Settings tab).')
        return EXIT_REFUSED
    try:
        linker = jellyfin_linker()
    except (OSError, ValueError):
        manager.say('Refused, nothing written: the PC\'s Jellyfin settings file could not be read.')
        return EXIT_REFUSED
    if not linker.reachable():
        manager.say('Jellyfin server reachable: no. Nothing was read or written.')
        return EXIT_REFUSED
    seen = {}

    def edit(current):
        if current is None:
            raise Refused('the console has no Wi-Fi setup yet. Choose Wi-Fi setup first (it links Jellyfin too).')
        seen['old'] = current.jellyfin
        new = _jellyfin_link(manager, linker)
        if new is None:
            raise Refused('the console\'s Jellyfin sign-in was not changed.')
        seen['new'] = new
        return replace(current, jellyfin=new)
    result = manager.provision_edit(edit, 'New Jellyfin sign-in written to the launcher area and checked on the chip.')
    _settle_jellyfin(manager, result, seen.get('new'), seen.get('old'))
    return result


def jellyfin_forget(manager):
    """Remove the Jellyfin sign-in from the console and log it out on the server. Wi-Fi
    and pairing stay. Needs no PC credentials (the console's own token logs itself out)."""
    seen = {}

    def edit(current):
        if current is None or current.jellyfin is None:
            return None
        seen['old'] = current.jellyfin
        return replace(current, jellyfin=None)
    result = manager.provision_edit(edit, 'Jellyfin sign-in removed from the launcher area (both copies) and '
                                          'checked on the chip.')
    _settle_jellyfin(manager, result, None, seen.get('old'))
    return result


def menu(manager, ask=input):
    """The double-click front end: plain choices, a typed confirmation for every write."""
    if manager.pending():
        print('An earlier job did not finish.')
        print('  1. Finish it now (keep the console plugged in and on)')
        print('  2. Show what is on the console (writes nothing)')
        print('  3. Abandon it (only if Show says the console is fine)')
        print('  Q. Quit')
        pick = ask('Choose: ').strip().lower()
        if pick == '1':
            return manager.resume()
        if pick == '2':
            return manager.guarded(manager.status)
        if pick == '3' and person_confirms(ask, 'Close the unfinished job without writing', 'abandon'):
            return manager.abandon()
        print('Nothing done.')
        return EXIT_OK

    choices = {'1': ('Show what is on the console', lambda: manager.guarded(manager.status), False),
               '2': ('Take a backup', lambda: manager.guarded(manager.backup), False),
               '3': ('Install or repair the start-up menu (also after an official update)', manager.install, True),
               '4': ('Remove the start-up menu (back to plain stock; erases the console\'s Wi-Fi networks, '
                     'PC pairing and Jellyfin sign-in)', manager.remove, True),
               '5': ('Restore stock firmware from the newest backup (ModRetro\'s settings stay; erases the '
                     'console\'s Wi-Fi networks, PC pairing and Jellyfin sign-in)',
                     lambda: manager.restore_stock(confirm=ask), True),
               '6': ('Update Chromatic firmware, guided (menu off, official update, menu back on)',
                     lambda: manager.guided_update(ask), True),
               '7': ('Set up Wi-Fi (adds to the console\'s networks), PC pairing and the console\'s Jellyfin sign-in',
                     lambda: wifi_setup(manager, private_prompts(ask)), True),
               '8': ('Re-link Jellyfin (new console sign-in; the old one is logged out)',
                     lambda: jellyfin_relink(manager), True),
               '9': ('Forget Jellyfin on the console (and log its sign-in out)',
                     lambda: jellyfin_forget(manager), True),
               'c': ('Console chip (FPGA): check what its flash holds (reads, restarts the console)',
                     lambda: _fpga_menu_job(manager, lambda f: f.check()), True),
               'r': ('Console chip (FPGA): write the original image back (rehearsal / undo), verify, restart',
                     lambda: _fpga_menu_job(manager, lambda f: f.restore_original(confirm=ask)), True),
               's': ('Console chip (FPGA): install the sound image (after r in this session), verify, restart',
                     lambda: _fpga_menu_job(manager, lambda f: f.write_media()), True),
               't': ('Console chip (FPGA): try the sound image temporarily (chip RAM only, flash untouched; power off/on undoes)',
                     lambda: _fpga_menu_job(manager, lambda f: f.try_media()), True)}
    print('Chromatic Manager. ESP32 jobs 1-9; console chip (FPGA) jobs c/r/s/t over JTAG. Settings and cartridges are '
          'never written.')
    for key, (label, _, _) in choices.items():
        print(f'  {key}. {label}')
    print('  Q. Quit')
    pick = ask('Choose: ').strip().lower()
    if pick not in choices:
        return EXIT_OK
    label, action, writes = choices[pick]
    if writes and not person_confirms(ask, label):
        print('Nothing done.')
        return EXIT_OK
    return action()


def fpga_jobs(manager):
    """The console chip (FPGA) jobs with this manager's messages (chromatic_fpga.py)."""
    import chromatic_fpga
    return chromatic_fpga.FpgaJobs(say=manager.say, stop_point=manager.stop_point)


def _fpga_menu_job(manager, job):
    fpga = fpga_jobs(manager)
    return with_first_backup(manager, fpga, lambda: job(fpga))


def first_full_backup(manager, fpga):
    """Before a console chip (FPGA) job: when this PC has no full backup yet (its loader says
    chromatic_fpga.NoBackup), take it first with Manager.backup (full_backup, which writes nothing to
    the console). None to go on with the job, or the exit code that stops it. Any other backup
    problem is left to the job, which refuses with its own reason."""
    import chromatic_fpga
    loader = getattr(fpga, 'backup_loader', None)
    if loader is None:
        return None
    try:
        loader()
        return None
    except chromatic_fpga.NoBackup:
        pass
    except Exception:
        return None
    manager.say('This PC has no full backup of the console yet, so it is taken first (nothing is written to the '
                'console).')
    code = manager.guarded(manager.backup)
    return None if code == EXIT_OK else code


def with_first_backup(manager, fpga, job):
    """Run the console chip job `job()` after first_full_backup."""
    code = first_full_backup(manager, fpga)
    return job() if code is None else code


def confirmation_word(base='yes'):
    """The word a person types to allow one write: the base word plus three random digits,
    new every time, so neither a stray "yes" nor a script that knows the prompt can confirm."""
    import secrets
    return f'{base}{secrets.randbelow(900) + 100}'


def person_confirms(ask, what, base='yes'):
    word = confirmation_word(base)
    try:
        answer = ask(f'{what}. Keep the console plugged in and on. Type {word} to continue: ')
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() == word


def interactive():
    """A person at a console window (both ends a terminal), not a pipe or a script."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def main(argv=None, manager=None, lock=None, ask=None, at_console=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', nargs='?', choices=('status', 'backup', 'install', 'remove', 'restore-stock',
                                                       'resume', 'abandon', 'update', 'menu',
                                                       'jellyfin-relink', 'jellyfin-forget',
                                                       'fpga-check', 'fpga-restore', 'fpga-media', 'fpga-trial'))
    parser.add_argument('--backup-name', help='restore-stock: which backup (default: newest).')
    parser.add_argument('--allow-version-change', action='store_true',
                        help='restore-stock: allow the stock firmware version to change.')
    parser.add_argument('--yes', action='store_true',
                        help='Accepted for old scripts, but no longer enough: every write asks the person at the '
                             'console window to type a one-time word.')
    args = parser.parse_args(argv)
    manager = manager or Manager()
    ask = ask or input
    at_console = interactive() if at_console is None else at_console
    try:
        with (lock or DeviceLock()):
            writing = args.command in ('install', 'remove', 'restore-stock', 'resume', 'abandon', 'jellyfin-relink',
                                       'jellyfin-forget', 'update', 'fpga-check', 'fpga-restore', 'fpga-media', 'fpga-trial')
            if args.command in (None, 'menu') or writing:
                if not at_console:
                    manager.say('Refused: writes need a person at a console window to type the confirmation '
                                '(or use the Chromatic Manager window). Nothing written.')
                    return EXIT_REFUSED
            if args.command in (None, 'menu'):
                return menu(manager, ask)
            if writing:
                base = 'abandon' if args.command == 'abandon' else 'yes'
                if not person_confirms(ask, f'Chromatic Manager: {args.command}', base):
                    manager.say('Not confirmed. Nothing written.')
                    return EXIT_REFUSED
            if args.command == 'update':
                return manager.guided_update(ask)
            fpga = fpga_jobs(manager)
            if args.command not in ('status', 'resume', 'abandon') and (manager.pending() or fpga.pending()):
                manager.say('Refused: an earlier job did not finish. Run: resume (or abandon after status).')
                return EXIT_REFUSED
            if args.command == 'fpga-check':
                return with_first_backup(manager, fpga, fpga.check)
            if args.command == 'fpga-restore':
                return with_first_backup(manager, fpga, lambda: fpga.restore_original(confirm=ask))
            if args.command == 'fpga-media':
                return with_first_backup(manager, fpga, fpga.write_media)
            if args.command == 'fpga-trial':
                return with_first_backup(manager, fpga, fpga.try_media)
            if args.command == 'resume' and fpga.pending():
                return fpga.resume()
            if args.command == 'abandon' and fpga.pending():
                return fpga.abandon()
            if args.command == 'status':
                return manager.guarded(manager.status)
            if args.command == 'backup':
                return manager.guarded(manager.backup)
            if args.command == 'install':
                return manager.install()
            if args.command == 'remove':
                return manager.remove()
            if args.command == 'resume':
                return manager.resume()
            if args.command == 'abandon':
                return manager.abandon()
            if args.command == 'jellyfin-relink':
                return jellyfin_relink(manager)
            if args.command == 'jellyfin-forget':
                return jellyfin_forget(manager)
            return manager.restore_stock(args.backup_name, args.allow_version_change)
    except Busy as error:
        manager.say(str(error))
        return EXIT_REFUSED
    except Refused as error:
        manager.say(f'Refused: {error}')
        return EXIT_REFUSED
    except KeyboardInterrupt:
        manager.close_device()
        manager.say('Cancelled.')
        return EXIT_REFUSED
    except Exception as error:
        manager.close_device()
        manager.say(f'Unexpected error ({type(error).__name__}); nothing further was attempted.')
        return EXIT_ERROR


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))

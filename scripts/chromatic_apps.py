# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""The apps record of the console's start-up menu (launcher 1.6.4 and newer).

Which optional apps the start-up menu shows (TV, ChromaTube) and whether TV lists the Jellyfin server's Live TV
channels or opens straight to its built-in Local Weather channel. The console keeps it as one 32-byte record, the
same in two flash sectors (FLASH_A then FLASH_B), read by chromatic/launcher/shared/apps_record.h; pack() here and
pj_apps_parse() there agree on the vectors of scripts/test_chromatic_apps.py. The console itself may write the
other copy (its menu's Hide / Show hidden apps), so the newer valid copy wins (int32 distance, A when equal).

A record that is absent, blank, torn, corrupt or of another version means the DEFAULTS: TV with Local Weather only,
ChromaTube hidden. Launchers before 1.6.4 never read these sectors (1.6.0-1.6.3 always show both rows and TV's list).

Nothing in it is personal or secret. The PC keeps the last choice it wrote (SAVED, in the per-user private
folder), so an install can put it back when the console lost it (a removal, an official update).
"""
import json
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from user_paths import PRIVATE  # noqa: E402  (per-user folder, see user_paths.py)
SAVED = PRIVATE / 'chromatic-apps.json'
FLASH_A, FLASH_B = 0x3FA000, 0x3FB000
SECTOR = 0x1000
RECORD = 32
MAGIC = b'PJAP'
VERSION = 1
TV, TUBE, LIVETV = 1, 2, 4
KNOWN = TV | TUBE | LIVETV
DEFAULTS = TV                     # a new console: TV with Local Weather only, no ChromaTube
AS_1_6 = TV | TUBE | LIVETV       # what launchers 1.6.0-1.6.3 show: both rows, TV's server list
FIRST_LAUNCHER = (1, 6, 4)        # the first launcher that reads the record
SHOWED_BOTH = ((1, 6, 0), (1, 6, 3))  # launchers that showed both rows and the Live TV list, with no record


class AppsError(ValueError):
    """A choice that cannot be stored, in plain words."""


@dataclass(frozen=True)
class Apps:
    tv: bool = True
    tube: bool = False
    livetv: bool = False

    @property
    def flags(self):
        return (TV if self.tv else 0) | (TUBE if self.tube else 0) | (LIVETV if self.livetv else 0)

    @staticmethod
    def from_flags(flags):
        if flags & ~KNOWN:
            raise AppsError('Unknown app flags.')
        return Apps(bool(flags & TV), bool(flags & TUBE), bool(flags & LIVETV))


def pack(flags, sequence):
    """The 32-byte record (apps_record.h's layout)."""
    if flags & ~KNOWN or not 0 <= flags <= 0xFFFFFFFF:
        raise AppsError('Unknown app flags.')
    if not 0 < sequence <= 0xFFFFFFFF:
        raise AppsError('Bad record sequence.')
    body = struct.pack('<4sBBHII12s', MAGIC, VERSION, 0, RECORD, sequence, flags, bytes(12))
    return body + struct.pack('<I', zlib.crc32(body) & 0xFFFFFFFF)


def sector(flags, sequence):
    """One 4 KiB flash sector: the record, then erased bytes."""
    return pack(flags, sequence).ljust(SECTOR, b'\xff')


def parse(raw):
    """(flags, sequence) from one copy, or None: the same rules as pj_apps_parse()."""
    if raw is None or len(raw) < RECORD:
        return None
    raw = bytes(raw[:RECORD])
    magic, version, pad, length, sequence, flags, reserved, crc = struct.unpack('<4sBBHII12sI', raw)
    if magic != MAGIC or version != VERSION or pad or length != RECORD or not sequence or flags & ~KNOWN or any(reserved):
        return None
    if zlib.crc32(raw[:28]) & 0xFFFFFFFF != crc:
        return None
    return flags, sequence


def newer(a, b):
    """True when sequence b is newer than a, as (int32_t)(b - a) > 0."""
    return 0 < ((b - a) & 0xFFFFFFFF) < 0x80000000


def current(a, b):
    """(flags, sequence) of the copy in force (the newer valid one, A when equal), or (None, 0)."""
    pa, pb = parse(a), parse(b)
    if pb and (not pa or newer(pa[1], pb[1])):
        return pb
    return pa or (None, 0)


def shown(a, b):
    """The flags the console uses: the record's, or DEFAULTS."""
    flags, _ = current(a, b)
    return DEFAULTS if flags is None else flags


def next_sequence(sequence):
    return (sequence + 1) & 0xFFFFFFFF or 1


def sequence_for(a, b):
    """The sequence of the next write of both copies (A first, then B): newer than both copies, so a write cut after
    A leaves the new A in force, whichever copy the console's own write made newer."""
    pa, pb = parse(a), parse(b)
    if pa and pb:
        return next_sequence(pb[1] if newer(pa[1], pb[1]) else pa[1])
    return next_sequence((pa or pb or (0, 0))[1])


def with_apps(image, flags, a_range, b_range):
    """`image` with `flags` in both copies (None: both erased, the defaults); the ranges are the two sectors."""
    data = bytearray(image)
    if flags is None:
        for start, end in (a_range, b_range):
            data[start:end] = b'\xff' * (end - start)
        return bytes(data)
    block = sector(flags, sequence_for(image[a_range[0]:a_range[1]], image[b_range[0]:b_range[1]]))
    for start, end in (a_range, b_range):
        data[start:end] = block
    return bytes(data)


def launcher_tuple(version):
    try:
        return tuple(int(part) for part in str(version).split('.'))
    except ValueError:
        return None


def reads_record(version):
    found = launcher_tuple(version)
    return bool(found) and found >= FIRST_LAUNCHER


def showed_both(version):
    """Did this launcher show TV and ChromaTube (and TV's server list) without any record (1.6.0-1.6.3)?"""
    found = launcher_tuple(version)
    return bool(found) and SHOWED_BOTH[0] <= found <= SHOWED_BOTH[1]


def words(flags, recorded=True):
    """One line for status and the window: what the console's menu shows."""
    tv = ('TV shown (Local Weather and Live TV channels)' if flags & TV and flags & LIVETV else
          'TV shown (Local Weather only)' if flags & TV else
          'TV hidden (Live TV channels when shown)' if flags & LIVETV else 'TV hidden')
    tube = 'ChromaTube shown' if flags & TUBE else 'ChromaTube hidden'
    return f'{tv}, {tube}' + ('' if recorded else ' (the defaults: nothing set)')


def from_words(text):
    """The flags a words() line names, or None when it names none (read back from the last status)."""
    text = str(text or '')
    if 'TV shown' not in text and 'TV hidden' not in text:
        return None
    flags = 0
    if 'TV shown' in text:
        flags |= TV
    if 'Live TV channels' in text:
        flags |= LIVETV
    if 'ChromaTube shown' in text:
        flags |= TUBE
    return flags


# ---- the PC's saved copy ----------------------------------------------------------------------------
def load_saved(path=None):
    """The flags saved on this PC, or None."""
    try:
        data = json.loads(Path(path or SAVED).read_text(encoding='utf-8'))
        flags = int(data['flags'])
        Apps.from_flags(flags)
        return flags
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError, AppsError):
        return None


def save(flags, path=None):
    """Keep `flags` on this PC (None: forget it)."""
    from chromatic_trial_runner import write_durable
    path = Path(path or SAVED)
    if flags is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return None
    Apps.from_flags(flags)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_durable(path, json.dumps({'flags': flags, 'means': words(flags)}, indent=1).encode('utf-8'))
    return flags

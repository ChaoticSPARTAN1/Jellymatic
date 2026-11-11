# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Chromatic FPGA flash jobs for Chromatic Manager: check, restore the owner's original image
(the rehearsal, and the undo that is always available) and write the stock+media (sound) image.

The console chip (Gowin GW5A-25, JTAG id 0x1281b) loads its bitstream from a 4 MiB SPI flash
(Winbond, JEDEC 0xef4016) at power-on. It is reached through GWU2X, a separate USB JTAG bridge
chip (33aa:0120) that stays even when the FPGA image is bad; the same openFPGALoader v1.2.0
build (private/chromatic-tools, hash-audited) that took the owner's two matching full readbacks
is the only programmer used here, with the commands Shiny Relay uses to flash and to rescue:

  openFPGALoader --cable gwu2x --detect
  openFPGALoader --cable gwu2x --dump-flash --file-size 4194304 <file>
  openFPGALoader --cable gwu2x --write-flash --verify <file>     (.fs: bitstream; .bin: raw flash image)
  openFPGALoader --cable gwu2x --reset                           (reconfigure from flash)
  openFPGALoader --cable gwu2x --write-sram <file.fs>            (Try FPGA image: the chip's RAM only)

The temporary trial (try_media) loads the built .fs into the FPGA's configuration SRAM over JTAG,
the way Shiny Relay loads its open-source build ("Load its output to SRAM only, openFPGALoader
--cable gwu2x -m out/top.fs; never write it to flash", Shiny Relay's own docs/BUILDING.md), and nothing else: the
loader's SRAM write is a JTAG configuration, it opens no flash command, and the chip loads its
flash again at the next power-on (or --reset). The image to try is pinned to its build receipt,
the command line is checked by sram_only() for anything that names the flash, and the trial is
refused unless Check FPGA or Restore FPGA has recently shown the flash holds the original, so
whatever the trial shows, switching the console off and on returns to the known image. The FPGA
holds the ESP32's enable line (top.v ESP32_EN), so a reconfiguration also restarts the launcher,
which redraws the start-up menu: a quick visual check of the overlay path.

Facts checked with local code against the owner's backup (never printed): the saved 4 MiB image
is the stock v18.14 bitstream (928,574 bytes, byte for byte the official .fs) followed by 0xFF;
the loader packs a .fs exactly as Gowin's own .bin (verified on the media build); its flash
write erases 4 KiB sectors up to the end of what it writes, so writing the media .fs over the
original leaves a flash that is: media bitstream, 0xFF to the next 4 KiB boundary, the untouched
0xFF tail. That is the exact 4 MiB image every write is verified against, by a full readback.

Every job: exactly one GWU2X and the expected JTAG id, or nothing happens; the original backup
(both disk copies, hashes against its record) must verify, or nothing happens; the flash is read
and compared before a write; each step is journalled to disk first; after a write the whole flash
is read back and compared byte for byte with the expected image, then the FPGA is reconfigured
and the console's USB serial interface (made by the FPGA) is watched for its return. A failure
after the write began is put right by writing the original image back (up to three attempts),
also on the next run if the PC died. The ESP32 flash, cartridges and saves are never touched:
the loader never gets an ESP32 command, and the FPGA flash holds nothing else.

The media image may be written over the original only after a Restore FPGA (the rehearsal)
succeeded in the same owner session (this process) within REHEARSAL_VALID_S; over the Pocket link
image (which this tool wrote and verified) it may be written back at once. The combined sound +
Pocket link image (18.41, LinkImage) is the owner-present trial: try_link loads it into SRAM only
(as try_media), the owner's answers about what the console showed are recorded with that try
(confirm_link_try), and write_link writes it only when a try of exactly that image was confirmed
with every check answered All good, over the sound image (or over the original after a
rehearsal). Output never contains flash contents, hashes of the owner's backup, device identifiers
or paths of private files.

update_link is the one-step path (no question rounds): Check FPGA's
read, the same SRAM try, then checks a machine can make instead of the owner's answers
(auto_confirm_link_try: the load finished, the console came back on USB, its start-up menu reports
18.41 with the debug bit and is up, the ESP32 is not in its download mode), the try recorded as
confirmed_by "auto". Then, with the image still running from RAM, the one Approve of the whole
flow (the image must have run from RAM with a real game looked at
before it is written): the owner starts a game, looks, and presses Approve to keep the image, or
Not right. That Approve is recorded with the try (approve_link_try). Then the same write_link, and
a last read that the console runs 18.41 from its flash. A failed check, Not right, a console that
reports another image at the Approve, or no way to ask: nothing is written and the chip is
restarted from its untouched flash. A machine-confirmed try opens the write only inside update_link
and only together with that Approve, given in the same session a few minutes before; write_link on
its own (the step-by-step path) still needs the owner's answers. The image's pins, receipts,
NEVER-INSTALL refusal, backups, journal, read-back and recovery are the same code as before.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from user_paths import PRIVATE, TOOLS  # noqa: E402  (per-user folder, see user_paths.py)
import re
import secrets
import shutil
import subprocess
import threading
import time

from chromatic_manager_owner import OwnerPrompts
from chromatic_trial_runner import (EXIT_MANUAL, EXIT_OK, EXIT_RECOVERED, EXIT_REFUSED, Journal, Refused,
                                    StepFailed, StepTimeout, write_durable)

EXIT_NOT_SEEN = 5  # flash verified, but the console's serial interface was not seen returning
ROOT = Path(__file__).resolve().parents[1]
WORK = PRIVATE / 'chromatic-manager/fpga'
LATEST_BACKUP = PRIVATE / 'chromatic-tools/latest-backup-path.txt'
# openFPGALoader (Apache-2.0, built with GWU2X cable support; not shipped with Jellymatic): set
# JELLYMATIC_OPENFPGALOADER, or put it at <tools>/openFPGALoader/openFPGALoader.exe (user_paths.TOOLS).
LOADER = Path(os.environ.get('JELLYMATIC_OPENFPGALOADER') or (TOOLS / 'openFPGALoader/openFPGALoader.exe'))
# Optional pin: the SHA-256 of the openFPGALoader you checked (JELLYMATIC_OPENFPGALOADER_SHA256). Unset: any
# openFPGALoader at LOADER is used; every write is still read back and compared byte for byte.
LOADER_SHA256 = (os.environ.get('JELLYMATIC_OPENFPGALOADER_SHA256') or '').strip().lower() or None
# The two reviewed console chip images, each in a folder with the exact sources and receipts it was built
# from (fpga/images/). The bitstreams (.fs/.bin) are not in Git: build them (docs/Building-from-source.md)
# or copy them from a release. Each is refused unless it matches its receipt (and, for 18.41, the pins below).
MEDIA_TREE = ROOT / 'fpga/images/sound-18.40'
MEDIA_FS = MEDIA_TREE / 'build/evt1_x2.fs'
MEDIA_RECEIPT = MEDIA_TREE / 'stage-receipt.json'
SIZE = 4 * 1024 * 1024
IDCODE = 0x1281b
FAMILY = 'GW5A-25'
FLASH_JEDEC = '0xef4016'
SECTOR = 4096                    # the loader erases 4 KiB sectors up to the end of what it writes
SESSION = secrets.token_hex(8)   # one owner session = one process of the Manager
REHEARSAL_VALID_S = 6 * 3600
REHEARSAL_FILE = 'rehearsal.json'
SERIAL_VID = 0x374e
SERIAL_PID_HIGH = 0x01  # PID low byte = SysCtrl player number: 0x0100 after reset / at the start-up menu, 0x0101 in games
FPGA_UNKNOWN_PROMPT = 'Console chip: type change to write your original image over an unknown image: '
NO_REHEARSAL = ('Restore FPGA (the rehearsal that writes your original image back and checks it) has not succeeded in '
                'this session yet. Do that first, then FPGA sound.')
NO_LINK_TRY = ('the temporary try of this Pocket link image has not been confirmed by you (every check answered All '
               'good). Use Update the Pocket link: it tries the image first, nothing written.')
SAID_CLOCK_BACK = ('Your answers are kept, but they cannot confirm the try: this PC\'s clock is now earlier than the '
                   'time of the try (the clock was moved back). Nothing was written. Switch the console off and on, '
                   'then try the image again.')
MEDIA_BUILD_RECEIPT = MEDIA_TREE / 'build/build-receipt.json'
TRIAL_CHECK_VALID_S = 24 * 3600  # a Check FPGA that found the original counts this long for Try FPGA image
LAST_CHECK_FILE = 'last-check.json'  # what this tool last saw the flash hold (Check FPGA, or its own verified write)
# The guided Pocket link trial (owner-present): the SRAM try of the 18.41 image is recorded, and the
# owner's answers to what the console showed on it. The flash write of 18.41 needs a try of the same
# image (fingerprint) with every check answered "All good" (the link-cable game may be skipped).
LINK_TRY_FILE = 'link-try.json'
LINK_TRY_VALID_S = TRIAL_CHECK_VALID_S  # 24 hours, like the Check FPGA the try needs: an older confirmed try no
                                        # longer opens the write
# A second RAM load without a power cycle between them left the console in game mode with a white screen
# Any recorded try newer than this needs the console switched off and on while this tool
# watches, before the next load; keyed on the record on disk, not on the Manager process (a restarted
# Manager must not skip it).
LINK_TRY_POWER_CYCLE_S = 180 * 60
# In the order the guide asks them: the Jellyfin checks first, games last. Choosing
# Games hands over to stock firmware, and the start-up menu (so Jellyfin) only comes back on a power-on,
# which reloads the flash image: a Jellyfin check after the games check looked at 18.40, not the try.
LINK_CHECKS = ('pocket_link', 'sound', 'games', 'link_game')
LINK_SKIPPABLE = ('link_game',)  # the owner may have no link-cable game or second console
FLASH_WORDS_FOR = {'original': 'your original image', 'media': 'the sound image (18.40)',
                   'link': 'the sound + Pocket link image'}
# Anything on an openFPGALoader command line that could reach the flash. The trial refuses a line
# that carries one of these (or that lacks the SRAM switch), whatever built it.
FLASH_WORDS = ('-f', '--write-flash', '--dump-flash', '--bulk-erase', '--unprotect-flash', '--protect-flash',
               '--external-flash', '--flash-sector', '--file-size', '-o', '--offset', '--spi', '--mcufw',
               '--secondary-bitstream', '--read-register', '--write-register', '--verify')
SRAM_SWITCH = '--write-sram'


def sram_only(args):
    """The trial's guard: the loader arguments must select the SRAM write, name exactly one .fs
    file, and carry nothing that names or addresses the flash. Raises Refused otherwise."""
    words = [str(a) for a in args]
    if words.count(SRAM_SWITCH) != 1:
        raise Refused('the temporary trial command does not select the SRAM write; nothing is loaded.')
    for word in words:
        low = word.lower()
        if low == SRAM_SWITCH:
            continue
        if low in FLASH_WORDS or low.startswith(('--offset', '--file-size', '-o')) or ('flash' in low and not low.endswith('.fs')):
            raise Refused('the temporary trial command names the flash; nothing is loaded.')
    files = [w for w in words if not w.startswith('-')]
    if len(files) != 1 or not files[0].lower().endswith('.fs'):
        raise Refused('the temporary trial command must name exactly one .fs bitstream; nothing is loaded.')
    return list(words)


def serial_port_matches(vid, pid):
    """The console's own USB serial interface, whatever the player number (as chromatic_manager
    ports() does): the FPGA puts SysCtrl[7:4] in the PID's low byte, which is 0 until stock
    firmware runs, so right after a reconfiguration the port is 0x0100, not 0x0101."""
    return vid == SERIAL_VID and pid is not None and (pid >> 8) == SERIAL_PID_HIGH
MANUAL = """
This needs you. The console chip (FPGA) could not be put right automatically.
  1. Leave the console switched on and plugged in. A dark screen after a chip write is not a
     brick: the GWU2X bridge that this tool uses stays reachable even when the image is bad.
  2. Switch the console off and on, plug it back in, then press Finish it: the tool writes
     your original image back and checks it (docs/Design-notes.md, runbook).
  3. If that fails again: Shiny Relay's installer "Rescue with a backup" (FPGA only, over
     GWU2X) with your three-file backup bundle, or ModRetro's own updater once the serial
     port is back (it installs the latest official firmware, not your exact original).
  4. If GWU2X itself is missing from Windows: stop, try another cable and port, then ModRetro
     support. Never improvise fuses, protection bits or drivers.
"""


def sha(data):
    return hashlib.sha256(data).hexdigest()


class FpgaTiming:
    """Measured on this console through GWU2X at 6 MHz (backup logs): a full 4 MiB
    read took about 23 s, reconfiguration about 1 s. Writes were never run here; deadlines are
    generous multiples (a full 4 MiB erase, program and the loader's own verify)."""
    detect: float = 60
    read: float = 240
    write: float = 900
    reset: float = 60
    sram: float = 240           # --write-sram of a 929 KB bitstream over the 6 MHz GWU2X link
    boot: float = 90            # serial interface back after --reset
    version: float = 10         # the start-up menu's "fpga version=" line after a USB serial restart (~1 s)
    passive: float = 7          # launcher 1.3.5 says its "fpga version=" line every 5 s: read it without any restart
    menu: float = 20            # the start-up menu's "stats" line (every 1 s) naming the menu phase, after a (re)start
    poll: float = 1.0
    keep_poll: float = 0.25     # while the one Approve waits: how often the console's USB port is looked for (enumeration only)
    keep_join: float = 10       # that watcher must end this soon after the answer, or the update stops (nothing written)
    keep_reenum: float = 8      # the one absence allowed while stock firmware starts (its USB product ID changes) may last this long
    attempts: int = 3


# ---- images --------------------------------------------------------------------------------
def fs_to_bin(path):
    """A Gowin .fs (ASCII bitstream) packed to bytes exactly as openFPGALoader's FsParser does
    for a flash write (comment lines dropped, each data line's bits MSB first, no reversal).
    Checked equal to Gowin's own .bin of the same build."""
    out = bytearray()
    with Path(path).open('rb') as stream:
        for line in stream:
            line = line.strip()
            if not line or line.startswith(b'//'):
                continue
            if len(line) % 8 or any(c not in b'01' for c in line):
                raise Refused('the bitstream file is not a Gowin .fs text bitstream.')
            for i in range(0, len(line), 8):
                out.append(int(line[i:i + 8], 2))
    if not out:
        raise Refused('the bitstream file is empty.')
    return bytes(out)


def bitstream_ok(data):
    """The header every image for this chip must carry: Gowin sync 0xA5C3, type 6, idcode 0x1281b
    (the same check verify_chromatic_backup applies to the owner's backup)."""
    if len(data) < 256:
        return False
    at = data[:256].find(b'\xa5\xc3')
    if at < 0 or at + 10 > len(data) or data[at + 2] != 0x06:
        return False
    return (int.from_bytes(data[at + 6:at + 10], 'big') & 0x0fffffff) == IDCODE


def flash_after_write(bitstream, before):
    """The 4 MiB the flash holds after the loader wrote `bitstream` at offset 0 over `before`:
    the bitstream, 0xFF to the next 4 KiB sector boundary, then `before` unchanged. (What a bare
    .fs write does; the sound write no longer uses it, see full_flash_image.)"""
    end = (len(bitstream) + SECTOR - 1) // SECTOR * SECTOR
    return bitstream + b'\xff' * (end - len(bitstream)) + before[end:]


def full_flash_image(bitstream):
    """The whole 4 MiB flash for a bitstream: the bitstream at offset 0, then 0xFF (erased flash)
    to the end. Written as one 4 MiB .bin with the same --write-flash --verify command Restore FPGA
    uses for the owner's backup, so the loader erases and programs every sector and nothing of the
    previous image can survive, whatever the two bitstreams' lengths (a rebuilt bitstream can be
    shorter than the original: 921,979 against 928,574 bytes, so a bare .fs write
    would have left 2,878 bytes of the original behind). The readback is compared with exactly
    this image."""
    if not bitstream or len(bitstream) > SIZE:
        raise Refused('the bitstream does not fit the 4 MiB flash.')
    return bytes(bitstream) + b'\xff' * (SIZE - len(bitstream))


class MediaImage:
    """The stock+media (sound) bitstream from the Gowin build, with its fingerprint (SHA-256 of
    the .fs file, shown as its first 8 characters) and what the flash must read as afterwards."""

    def __init__(self, path=MEDIA_FS, receipt=MEDIA_RECEIPT, build_receipt=MEDIA_BUILD_RECEIPT):
        self.path = Path(path)
        if not self.path.is_file():
            raise Refused('no built sound image: fpga/images/sound-18.40/build/evt1_x2.fs is missing. '
                          'Build it with Gowin first (0 setup / 0 hold).')
        try:
            raw = self.path.read_bytes()
        except OSError:
            raise Refused('the built sound image could not be read.')
        self.fingerprint = sha(raw)
        # Pinned to the build receipt written next to it (hash of the .fs, 0 setup / 0 hold): a
        # rebuilt or swapped image without its receipt is refused, for the owner and the AI alike.
        try:
            pinned = json.loads(Path(build_receipt).read_text(encoding='utf-8'))
            pinned_sha = pinned['outputs']['build/evt1_x2.fs']['sha256']
            timing = pinned['timing']
        except (OSError, ValueError, KeyError, TypeError):
            raise Refused('the sound image has no readable build receipt (build/build-receipt.json); nothing is written.')
        if pinned_sha != self.fingerprint:
            raise Refused('the sound image is not the one its build receipt describes (rebuilt or changed); refresh the '
                          'receipt with the build, then try again.')
        if timing.get('setup_violated_endpoints') != 0 or timing.get('hold_violated_endpoints') != 0:
            raise Refused('the sound image\'s build receipt does not show 0 setup / 0 hold violations.')
        # The receipt also names the staged source the build came from: the stage receipt and every
        # patched source file. A tree re-staged or edited after the build (so the image no longer
        # matches the source next to it) is refused, for the owner and the AI alike.
        tree = Path(receipt).parent
        stage_sha = pinned.get('stage_receipt_sha256')
        if stage_sha is not None and (not Path(receipt).is_file() or sha(Path(receipt).read_bytes()) != stage_sha):
            raise Refused('the staged source was re-staged after the sound image was built (stage receipt differs); '
                          'rebuild with Gowin and refresh the receipt first.')
        for rel, digest in (pinned.get('sources_sha256') or {}).items():
            path = tree / rel
            try:
                if sha(path.read_bytes()) != digest:
                    raise Refused(f'the staged source {Path(rel).name} changed after the sound image was built; rebuild '
                                  'with Gowin and refresh the receipt first.')
            except OSError:
                raise Refused(f'the staged source {Path(rel).name} the sound image was built from is missing.')
        self.bitstream = fs_to_bin(self.path)
        if not bitstream_ok(self.bitstream):
            raise Refused('the sound image is not a bitstream for this console chip (GW5A-25).')
        # The bytes the loader would place from the .fs must equal Gowin's own .bin of the same
        # build when the receipt names it: the packing proof for the 4 MiB image written below.
        gowin_bin = (pinned.get('outputs') or {}).get('build/evt1_x2.bin')
        if gowin_bin:
            bin_path = self.path.with_suffix('.bin')
            try:
                packed = bin_path.read_bytes()
            except OSError:
                raise Refused("the build's own evt1_x2.bin next to the .fs is missing; nothing is written.")
            if sha(packed) != gowin_bin.get('sha256') or packed != self.bitstream:
                raise Refused("the .fs does not pack to the build's own evt1_x2.bin (or that file is not the one the "
                              'receipt names); nothing is written.')
        self.flash_image = full_flash_image(self.bitstream)
        self.version = None
        try:
            record = json.loads(Path(receipt).read_text(encoding='utf-8'))['version_register']
            self.version = f'{record["major"]}.{record["minor"]}'
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def short(self):
        return f'sound build {self.version or "(version unknown)"} (image {self.fingerprint[:8]})'

    def expected(self, original):
        """The exact 4 MiB the flash must read as after the write: the whole image written (the
        bitstream, then 0xFF to 4 MiB), independent of what the flash held before, because the
        write programs all 4 MiB. `original` is only checked for size."""
        if len(original) != SIZE:
            raise Refused('the original image is not a 4 MiB flash image; nothing is written.')
        return self.flash_image

    def write_file(self, folder):
        """The 4 MiB .bin handed to the loader (the same file shape as the restore bundle's
        fpga_flash_4MB.bin), written durably into the job folder and checked back."""
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f'sound-{self.fingerprint[:16]}-flash_4MB.bin'
        write_durable(path, self.flash_image)
        if path.read_bytes() != self.flash_image or path.stat().st_size != SIZE:
            raise Refused('the 4 MiB sound flash image could not be written to disk correctly; nothing is written.')
        return path


def media_fingerprint(path=MEDIA_FS):
    """SHA-256 of the built sound image file, or None when there is none (for prompts)."""
    try:
        return sha(Path(path).read_bytes())
    except OSError:
        return None


def media_text(path=MEDIA_FS, receipt=MEDIA_RECEIPT):
    """'sound build 18.40 (image 1a2b3c4d)' for the owner's prompts, or None."""
    try:
        return MediaImage(path, receipt).short()
    except Refused:
        return None


# ---- the combined sound + Pocket link image (18.41) ------------------------------------------------
# Staged and built by scripts/stage_chromatic_fpga_link.py. Only the guided owner-present jobs
# try_link / write_link use it (Manager: "Update the Pocket link"); every other job keeps MediaImage
# (18.40). While NEVER-INSTALL.txt sits beside the image (build/) or at the top of its staged
# tree, LinkImage refuses to load, so no job gets its bytes; the marker is removed at trial time,
# after the review. Recognising the image on the flash (check, restore) is not installing it
# and works either way.
#
# THE APPROVED IMAGE IS NAMED HERE AND NOWHERE ELSE: its tree and its three hashes, in this block.
# Approved now: the 18.41 STREAM image (built, exported to LINK_TREE with its
# receipts, reports and the 16 staged sources it was built from; filtered link inputs, the fast
# UART bridge; 0 setup / 0 hold violated endpoints, worst reported setup +1.447 ns over the 1.0 ns
# floor; its receipts are in LINK_TREE, see docs/Design-notes.md, FPGA-STREAM-IMAGE).
# It is approved for install on the condition that this exact .fs runs from RAM with a real game checked before it is written to
# flash: update_link does that (RAM load, machine checks, then the owner's Approve with the image
# running).
# Other 18.41 builds under staging/chromatic-fpga-media-link (the earlier trial image
# esp32t-18.41-trial-20261109, .fs 9e2d82af..., and anything the staging script builds later) are
# refused by hash, with or without a marker. LINK_FS_SHA256 pins the approved .fs, so no marker,
# folder swap or re-staging can put another image in its place. A try recorded for
# another image (link-try.json names the .fs it was a try of) never counts for this one.
LINK_TREE = ROOT / 'fpga/images/link-18.41'
LINK_FS = LINK_TREE / 'build/evt1_x2.fs'
LINK_RECEIPT = LINK_TREE / 'stage-receipt.json'
LINK_BUILD_RECEIPT = LINK_TREE / 'build/build-receipt.json'
LINK_FS_SHA256 = '8feb7929fb4cfdca6d84469df73b06649ca8ac6a973ed53ca087586cac32ab23'  # the approved stream .fs
LINK_BIN_SHA256 = '42dbec7a42ec763a45c1d95b8e56bc1b69fd93abafacbb7391048fc46f063504'  # Gowin's .bin of that build
LINK_STAGE_SHA256 = 'dbd3e9238477ef98262f3313f97d56f69498ded6088cd7fc39b9aaf8c1345b60'  # its stage receipt
LINK_PINS = {'fs': LINK_FS_SHA256, 'bin': LINK_BIN_SHA256, 'stage': LINK_STAGE_SHA256}
LINK_MARK = 'NEVER-INSTALL.txt'
LINK_VERSION = (18, 41)  # launcher_logic.h PJ_FPGA_MEDIA_MAJOR / PJ_FPGA_LINK_MINOR, with the debug bit
LINK_RUNNING = (*LINK_VERSION, 1)  # what the start-up menu must report while the try runs: 18.41, debug bit set
# The launcher's start-up line (launcher_main.c, after its version request; the same pattern as
# chromatic_manager.FPGA_LINE): "PJ-DIAG t=22 fpga version=18.41 debug=1", or "fpga version=none".
FPGA_VERSION_LINE = re.compile(rb'PJ-DIAG t=\d+ fpga version=(?:(\d{1,2})\.(\d{1,2}) debug=([01])|(none))\r?\n')
# What try_link says (chromatic_manager_core reads these words; its tests check they are still here).
SAID_LINK_RUNNING = 'Confirmed on the console: its start-up menu reports version 18.41 with the debug bit'
SAID_LINK_NOT_RUNNING = 'Not confirmed: the Pocket link image is not running'


def boot_fpga_version(log):
    """(major, minor, debug) from the launcher's start-up FPGA line in `log`, 'none' when the
    FPGA did not answer, None when the line is not there."""
    found = FPGA_VERSION_LINE.search(bytes(log or b''))
    if not found:
        return None
    if found.group(4):
        return 'none'
    return int(found.group(1)), int(found.group(2)), int(found.group(3))


def version_words(running):
    """A reported version in plain words, for the owner."""
    if running is None:
        return 'nothing readable (the start-up menu\'s line did not arrive over USB)'
    if running == 'none':
        return 'no version reply from the console chip'
    if running == 'busy':
        return ('nothing readable: the console\'s USB serial port stayed in use by another program (a serial '
                'terminal, or a Remote Pilot receiver that did not let go when this tool asked before the job)')
    if running == 'download':
        return ('the console\'s ESP32 waiting in its download mode (a serial program held its DTR line, IO0, while '
                'the chip restarted); no start-up menu runs')
    major, minor, debug = running
    return f'version {major}.{minor}{" with the debug bit" if debug else ""}'


ROM_DOWNLOAD = b'waiting for download'  # the ESP32 ROM's line when it boots with IO0 low: no launcher runs
# The launcher's status line, said every second (launcher_diag.h pj_diag_stats, PJ_DIAG_STATS_MS):
# "PJ-DIAG t=5012 stats phase=menu uart=412 msgs=9 polls=... heap=...". Only the phase and the two
# counters of bytes and messages heard from the console chip are taken; nothing else is kept.
STATS_LINE = re.compile(rb'PJ-DIAG t=\d+ stats phase=([a-z-]{1,16}) uart=(\d{1,10}) msgs=(\d{1,10}) ')
MENU_PHASES = ('menu', 'media', 'other')  # launcher_logic.h: the start-up menu is open (its list, Jellyfin, Other)


def launcher_status(log):
    """{'phase', 'uart', 'msgs'} from the newest launcher status line in `log`, or None."""
    found = None
    for found in STATS_LINE.finditer(bytes(log or b'')):
        pass
    if found is None:
        return None
    return {'phase': found.group(1).decode('ascii'), 'uart': int(found.group(2)), 'msgs': int(found.group(3))}


# The one-step update's checks, made by this tool instead of asked of the owner (auto_confirm_link_try).
AUTO_CHECKS = ('loaded', 'usb_back', 'not_download', 'version', 'menu')
AUTO_CHECK_WORDS = {
    'loaded': 'the image loaded into the console chip\'s RAM',
    'usb_back': 'the console came back on USB on that image',
    'not_download': 'the console\'s ESP32 is not in its download mode',
    'version': 'the start-up menu reports version 18.41 with the debug bit (the console chip answers it)',
    'menu': 'the start-up menu is up (its own status line says so)',
}
# What a machine cannot see here (the step-by-step path asks the owner these): said with every result.
AUTO_NOT_CHECKED = ('the Pocket link row and the link cable with a Pocket', 'Jellyfin sound',
                    'a game from a cartridge', 'a link-cable game')
CONFIRMED_BY_AUTO, CONFIRMED_BY_OWNER = 'auto', 'owner'
# What update_link says (chromatic_manager_core reads these words; its tests check they are still here).
SAID_AUTO_PASSED = 'Automatic checks passed'
SAID_AUTO_FAILED = 'Automatic checks failed'
SAID_LINK_FROM_FLASH = 'Confirmed: the console runs the Pocket link image (18.41) from its flash'
SAID_LINK_NOT_FROM_FLASH = 'Not confirmed yet: the console was not heard reporting 18.41 from its flash'
SAID_UPDATE_NEEDS_SOUND = 'Set up Jellyfin sound first'
SAID_UPDATE_UNKNOWN = 'The console chip holds an image this tool does not know'
# The one Approve of the one-step update, asked while the image runs from RAM (update_link). The window
# turns a question that starts with LINK_KEEP_PROMPT into its Approve / Not right dialog; the answer that
# keeps the image is KEEP_ANSWER, anything else (Not right, Cancel, closing the dialog) leaves the flash.
LINK_KEEP_PROMPT = 'Console chip: the new image is running now. '
KEEP_TITLE = 'The new image is running now'
KEEP_ANSWER = 'keep'
KEEP_VALID_S = 5 * 60   # the Approve opens the write that follows it at once, and nothing later
# After Approve the tool asks the console once more which image runs. A definite other version (the
# console was switched off and on, so it runs its flash image again) always stops the update. No answer
# at all does not, while this is False: with a game running the start-up menu is silent, and the read
# then depends on restarting the ESP32, which once gave a false "not running" on the console
# and has not been proven there since. True makes "not heard" stop the update too.
LINK_KEEP_MUST_HEAR = False
SAID_NOT_KEPT = 'Not kept'
SAID_TRY_ENDED = 'The new image was no longer running'
SAID_KEPT = 'You approved the new image while it was running'
SAID_NO_ASKER = 'This update needs the app window for its Approve'
# While that Approve waits, the console's USB serial port is watched by Windows enumeration alone (no port
# is opened, nothing is sent, no JTAG). A switch-off takes the port away, and the console then starts
# from its flash: with a game running its start-up menu is silent, so the version read after Approve
# could not tell that from the new image. KeepWatch below says which absences end
# the update with nothing written, whatever is heard afterwards.
SAID_SWITCHED_OFF = 'Your Chromatic was switched off'
# write_link refused (or was stopped) after the Approve and before any write: the RAM image is not left running.
SAID_LATE_REFUSAL = 'The write did not start after your Approve'
HEARD_SWITCHED_OFF = 'switched off'


class KeepWatch:
    """What Windows' list of COM ports shows of the console while the one Approve waits, look by
    look; `why` is set, and stays set, once the looks cannot be the new image still running.

    The console's port does not only leave on a switch-off. ModRetro's FPGA puts the player number
    in the USB product ID's low byte (SERIAL_PID_HIGH): 0x0100 after every configuration of the
    chip (power-on, a JTAG load) and 0x0101 once stock firmware has started, and that change makes
    the port leave and come back. The Approve prompt asks the owner to start a game, which is
    exactly that. So ONE sequence is allowed and nothing else:

        0x0100 ... [away, at most `reenum` seconds] ... 0x0101 ...

    Everything else latches (the try can simply be run again): the port away at the first look,
    away longer than `reenum`, away at any time once 0x0101 was seen, 0x0100 seen again after an
    absence or after 0x0101 (the chip was configured again: only a power cycle does that here,
    since this tool sends nothing), more than one console, or any other product ID. A loader that
    cannot give product IDs (looks of 'there') allows no absence at all.

    Looks: 'fresh' (0x0100), 'stock' (0x0101), 'there' (present, product ID unknown), 'absent'
    (not listed, or the list could not be read), 'odd'."""

    def __init__(self, reenum, clock=time.monotonic):
        self.reenum, self.clock = reenum, clock
        self.phase, self.away_since, self.why = None, None, None

    def see(self, look):
        if self.why:
            return
        if look == 'absent':
            if self.phase != 'fresh':
                self.why = 'left'           # at the first look, after stock had started, or with no product ID to go by
            elif self.away_since is None:
                self.away_since = self.clock()
            elif self.clock() - self.away_since > self.reenum:
                self.why = 'stayed away'
        elif look == 'fresh':
            if self.phase not in (None, 'fresh') or self.away_since is not None:
                self.why = 'configured again'
            else:
                self.phase = 'fresh'
        elif look == 'stock':
            if self.phase == 'there':
                self.why = 'odd'
            else:
                self.phase, self.away_since = 'stock', None
        elif look == 'there':
            if self.phase not in (None, 'there'):
                self.why = 'odd'
            else:
                self.phase = 'there'
        else:
            self.why = 'odd'

    def settled(self):
        """Latched, or not in the middle of the one allowed absence."""
        return self.why is not None or self.away_since is None


def keep_text(held):
    """What the window shows, big, under KEEP_TITLE while the one-step update waits for its Approve."""
    return ('Start a game for a moment and check the picture and the sound. Press Approve to keep the new image. '
            'If you switched your Chromatic off and on, press Not right. Not right changes nothing and leaves '
            f'{FLASH_WORDS_FOR[held]}.')


class LinkImage(MediaImage):
    """The combined stock + sound + Pocket link bitstream (version register 18.41, debug bit),
    pinned exactly as MediaImage pins the sound image (the .fs hash in its build receipt, 0 setup /
    0 hold, the stage receipt and every patched/added source, the .fs packing to Gowin's .bin, the
    chip header), plus: the .fs must be the approved stream image by hash (`pins`, LINK_PINS: the
    .fs, Gowin's .bin and the stage receipt; the .fs is checked before anything else, markers or
    not), and the stage receipt must name version 18.41 with the debug bit and carry the pocket-link
    hooks. Refused while NEVER-INSTALL.txt is present; `allow_unreviewed=True` is for inspection and
    for recognising the image on the flash only: write_file refuses an unreviewed image, and the
    trial and the write load it without that switch. Tests hand fixture images their own `pins`."""

    def __init__(self, path=LINK_FS, receipt=LINK_RECEIPT, build_receipt=LINK_BUILD_RECEIPT, allow_unreviewed=False,
                 pins=None):
        path, receipt = Path(path), Path(receipt)
        pins = dict(LINK_PINS if pins is None else pins)
        if set(pins) != set(LINK_PINS) or not all(isinstance(v, str) and len(v) == 64 for v in pins.values()):
            raise Refused('sound + Pocket link image: the pins must name the .fs, .bin and stage receipt hashes.')
        if not path.is_file():
            raise Refused('no built sound + Pocket link image: '
                          f'{LINK_FS.relative_to(ROOT).as_posix()} is missing (the approved stream build, kept with '
                          'its receipts and sources).')
        try:
            found = sha(path.read_bytes())
        except OSError:
            raise Refused('the sound + Pocket link image could not be read.')
        if found != pins['fs']:
            # Before the marker check, so a removed marker or another build in the folder changes nothing.
            raise Refused(f'the sound + Pocket link image is not the approved 18.41 stream image (its .fs is '
                          f'{found[:8]}, the approved one is {pins["fs"][:8]}); only that image is ever tried or '
                          'written. Every other build, the earlier trial image too, stays out.')
        marks = [m for m in (path.parent / LINK_MARK, receipt.parent / LINK_MARK) if m.exists()]
        if marks and not allow_unreviewed:
            raise Refused('the sound + Pocket link image 18.41 is unreviewed (NEVER-INSTALL.txt beside it); '
                          'it is never written or tried on a console. Nothing is loaded.')
        try:
            super().__init__(path, receipt, build_receipt)
        except Refused as error:
            raise Refused(f'sound + Pocket link image: {error}') from None
        try:
            staged = json.loads(receipt.read_text(encoding='utf-8'))
            pinned = json.loads(Path(build_receipt).read_text(encoding='utf-8'))
            record = staged['version_register']
            hooks = staged['link_hooks']
        except (OSError, ValueError, KeyError, TypeError):
            raise Refused('sound + Pocket link image: its stage receipt has no version register or link hooks.')
        if (record.get('major'), record.get('minor'), record.get('debug')) != (*LINK_VERSION, 1):
            raise Refused('sound + Pocket link image: the stage receipt does not name version 18.41 with the debug bit.')
        if not isinstance(hooks, list) or len(hooks) != 19:
            raise Refused('sound + Pocket link image: the stage receipt does not carry the 19 pocket-link hooks.')
        if pinned.get('stage_receipt_sha256') is None or not pinned.get('sources_sha256'):
            raise Refused('sound + Pocket link image: its build receipt does not pin the staged source.')
        # Last, after the receipts were read whole: the approved build's stage receipt and Gowin .bin by hash.
        if sha(receipt.read_bytes()) != pins['stage']:
            raise Refused('sound + Pocket link image: its stage receipt is not the approved stream build\'s '
                          f'({pins["stage"][:8]}).')
        if sha(self.bitstream) != pins['bin']:
            raise Refused('sound + Pocket link image: its bitstream is not the approved stream build\'s .bin '
                          f'({pins["bin"][:8]}).')
        self.reviewed = not marks
        self.mark_paths = (path.parent / LINK_MARK, receipt.parent / LINK_MARK)

    def short(self):
        return f'sound + Pocket link build {self.version or "(version unknown)"} (image {self.fingerprint[:8]})'

    def write_file(self, folder):
        """The 4 MiB .bin for the loader, as MediaImage writes it; never for an unreviewed image
        (and the marker is looked for again, in case it came back after loading)."""
        marks = [m for m in self.mark_paths if m.exists()]
        if not self.reviewed or marks:
            raise Refused('the sound + Pocket link image is unreviewed (NEVER-INSTALL.txt); nothing is written.')
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f'link-{self.fingerprint[:16]}-flash_4MB.bin'
        write_durable(path, self.flash_image)
        if path.read_bytes() != self.flash_image or path.stat().st_size != SIZE:
            raise Refused('the 4 MiB Pocket link flash image could not be written to disk correctly; nothing is written.')
        return path


def link_fingerprint(path=LINK_FS):
    """SHA-256 of the built combined image file, or None when there is none (inspection only)."""
    try:
        return sha(Path(path).read_bytes())
    except OSError:
        return None


def link_text(path=LINK_FS, receipt=LINK_RECEIPT, build_receipt=LINK_BUILD_RECEIPT):
    """'sound + Pocket link build 18.41 (image 1a2b3c4d)', with ', unreviewed' while NEVER-INSTALL
    is beside it, or None. Inspection text only: it is not a write or trial approval."""
    try:
        image = LinkImage(path, receipt, build_receipt, allow_unreviewed=True)
    except Refused:
        return None
    return image.short() + ('' if image.reviewed else ', unreviewed: never installed')


def link_ready_text(path=LINK_FS, receipt=LINK_RECEIPT, build_receipt=LINK_BUILD_RECEIPT):
    """'sound + Pocket link build 18.41 (image 1a2b3c4d)' when the reviewed image would be tried or
    written (no NEVER-INSTALL.txt, every pin holds), else None. For the Manager's offers and prompts."""
    try:
        return LinkImage(path, receipt, build_receipt).short()
    except Refused:
        return None


def jellymatic_image(flash):
    """'sound 18.40' or 'sound + Pocket link 18.41' when the console chip's flash starts with one of
    Jellymatic's own reviewed images, else None. Decided from the pins alone (no image file needed): the
    SHA-256 and size of each build's Gowin .bin in its build receipt, and LINK_BIN_SHA256. The first full
    backup refuses such a flash: an original must be ModRetro's own image."""
    known = []
    for label, receipt in (('sound 18.40', MEDIA_BUILD_RECEIPT), ('sound + Pocket link 18.41', LINK_BUILD_RECEIPT)):
        try:
            out = json.loads(Path(receipt).read_text(encoding='utf-8'))['outputs']['build/evt1_x2.bin']
            known.append((label, str(out['sha256']).lower(), int(out['size'])))
        except (OSError, ValueError, KeyError, TypeError):
            pass
    known.append(('sound + Pocket link 18.41', LINK_BIN_SHA256, None))
    flash = bytes(flash)
    trimmed = flash.rstrip(b'\xff')
    for label, digest, size in known:
        if (size and sha(flash[:size]) == digest) or sha(trimmed) == digest:
            return label
    return None


class NoBackup(Refused):
    """No full backup has been taken on this PC yet (no record at all). Chromatic Manager takes the
    first full backup by itself before a job that needs it (chromatic_manager.Manager.full_backup)."""


class OriginalBackup:
    """The original full FPGA flash image from the verified three-file restore bundle,
    checked against its record and against the second copy. Nothing of it is ever
    shown: callers get the bytes, the file to hand to the loader, and a date for messages."""

    def __init__(self, path, image, taken):
        self.path, self.image, self.taken = Path(path), image, taken
        self.sha = sha(image)

    @classmethod
    def load(cls, latest=None):
        latest = LATEST_BACKUP if latest is None else latest  # looked up at call time (tests point it elsewhere)
        try:
            folder = Path(Path(latest).read_text(encoding='utf-8').strip())
        except FileNotFoundError:
            raise NoBackup('no full backup yet. Press Back up (Fix a problem) first; Install and the updates take '
                           'it by themselves (docs/Install-with-Chromatic-Manager.md).')
        except OSError:
            raise Refused('the full backup record (chromatic-tools/latest-backup-path.txt in your Jellymatic folder) '
                          'cannot be read.')
        bundle = folder / 'restore-bundle'
        try:
            files = sorted(bundle.iterdir())
        except OSError:
            raise Refused('the original three-file restore bundle is missing. Take the full backup first.')
        images = [p for p in files if p.name.endswith('fpga_flash_4MB.bin')]
        records = [p for p in files if p.name.endswith('backup.json')]
        if len(files) != 3 or len(images) != 1 or len(records) != 1:
            raise Refused('the original restore bundle is incomplete (it must hold exactly three files).')
        try:
            record = json.loads(records[0].read_text(encoding='utf-8'))['fpga']
            receipt = json.loads((folder / 'completion-receipt.json').read_text(encoding='utf-8'))
            secondary = Path(receipt['secondary_bundle']) / images[0].name
        except (OSError, ValueError, KeyError, TypeError):
            raise Refused('the backup record or its completion receipt cannot be read.')
        try:
            image = images[0].read_bytes()
        except OSError:
            raise Refused('the saved original FPGA image could not be read.')
        if record.get('size') != SIZE or len(image) != SIZE or record.get('sha256') != sha(image):
            raise Refused('the saved original FPGA image does not match its own record.')
        if not str(record.get('idcode', '')).lower().endswith(f'{IDCODE:x}') or record.get('flash_jedec') != FLASH_JEDEC:
            raise Refused('the backup record is not for this console chip.')
        if not bitstream_ok(image):
            raise Refused('the saved original FPGA image is not a bitstream for this console chip.')
        try:
            if sha(secondary.read_bytes()) != record['sha256']:
                raise Refused('the second copy of the original FPGA image differs. Resolve backup integrity first.')
        except OSError:
            raise Refused('the second copy of the original FPGA image is missing. Resolve backup integrity first.')
        taken = folder.name.removeprefix('chromatic-backup-')[:8]
        taken = f'{taken[:4]}-{taken[4:6]}-{taken[6:8]}' if len(taken) == 8 and taken.isdigit() else 'the original backup'
        return cls(images[0], image, taken)


# ---- the programmer -------------------------------------------------------------------------
class LoaderFailed(StepFailed):
    pass


class JtagLoader:
    """openFPGALoader through GWU2X. Every call is bounded; the loader's own output goes to a
    private log in the job folder (it names the device) and is never returned or printed. The
    loader is only ever given --detect, --dump-flash, --write-flash --verify, --reset or (the
    temporary trial) --write-sram <file.fs>: no protection or fuse command, and nothing that
    could address the ESP32."""

    def __init__(self, logs, exe=LOADER, timing=None):
        self.exe, self.logs, self.timing = Path(exe), Path(logs), timing or FpgaTiming()
        self.env = dict(os.environ)
        self.env['PATH'] = str(self.exe.parent) + os.pathsep + 'C:\\msys64\\ucrt64\\bin' + os.pathsep + self.env['PATH']

    def check_tool(self):
        try:
            data = self.exe.read_bytes()
        except OSError:
            raise Refused('the programmer (openFPGALoader) is missing: see docs/Install-with-Chromatic-Manager.md.')
        if LOADER_SHA256 is not None and sha(data) != LOADER_SHA256:
            raise Refused('the programmer (openFPGALoader) is not the build you pinned (JELLYMATIC_OPENFPGALOADER_SHA256).')

    def serial_pids(self):
        """The USB product IDs of the console's own serial interface (one per console listed), from
        Windows' list of COM ports only: no port is opened, nothing is sent, no JTAG and no other
        program is started, so it can be asked while an image runs from RAM and while another
        program holds the port. None when the list cannot be read."""
        try:
            from serial.tools import list_ports
            return [p.pid for p in list_ports.comports() if serial_port_matches(p.vid, p.pid)]
        except Exception:
            return None

    def serial_present(self):
        """Is the console's own USB serial interface there? serial_pids: enumeration only. False
        when the list cannot be read."""
        return bool(self.serial_pids())

    def present(self):
        """{'serial': the console's own USB serial interface is there, 'gwu2x': how many GWU2X
        bridges Windows reports healthy}. Read-only Windows enumeration."""
        serial = self.serial_present()
        command = ("@(Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB\\VID_33AA&PID_0120*' "
                   "-and $_.Status -eq 'OK' } | Select-Object Status) | ConvertTo-Json -Compress")
        count = 0
        try:
            result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command], check=True,
                                    capture_output=True, text=True, timeout=30)
            devices = json.loads(result.stdout or '[]')
            count = 1 if isinstance(devices, dict) else len(devices)
        except Exception:
            count = 0
        return {'serial': serial, 'gwu2x': count}

    def _run(self, args, name, seconds, during=None):
        """`during`, if given, is called while the loader runs (about five times a second); it only
        looks (the full backup watches the console's serial port go away), and its errors are ignored."""
        self.logs.mkdir(parents=True, exist_ok=True)
        log_path = self.logs / f'{time.strftime("%Y%m%dT%H%M%S")}-{name}.log'
        with log_path.open('wb') as log:
            process = subprocess.Popen([str(self.exe), '--cable', 'gwu2x', *args], stdout=log,
                                       stderr=subprocess.STDOUT, env=self.env, stdin=subprocess.DEVNULL)
            deadline = time.monotonic() + seconds
            try:
                while process.poll() is None:
                    if time.monotonic() > deadline:
                        raise StepTimeout(f'The JTAG {name} passed its deadline.')
                    if during is not None:
                        try:
                            during()
                        except Exception:
                            pass
                    time.sleep(0.2)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        text = log_path.read_text(encoding='utf-8', errors='replace')
        if process.returncode:
            raise LoaderFailed(f'The JTAG {name} failed (private log kept).')
        return text

    def detect(self):
        """{'idcode': int, 'family': str} of the one device on the chain."""
        text = self._run(['--detect'], 'detect', self.timing.detect)
        idcode, family = None, ''
        for line in text.splitlines():
            key, _, value = line.strip().partition(' ')
            if key == 'idcode':
                try:
                    idcode = int(value.strip(), 16)
                except ValueError:
                    idcode = None
            elif key == 'model':
                family = value.strip()
        return {'idcode': idcode, 'family': family}

    def dump(self, path, during=None):
        text = self._run(['--dump-flash', '--file-size', str(SIZE), str(path)], 'read', self.timing.read, during)
        if f'JEDEC ID: {FLASH_JEDEC}' not in text:
            raise LoaderFailed('The flash chip did not identify as expected during the read.')
        data = Path(path).read_bytes()
        if len(data) != SIZE:
            raise LoaderFailed('The flash read came back the wrong size.')
        return data

    def write(self, source):
        text = self._run(['--write-flash', '--verify', str(source)], 'write', self.timing.write)
        if f'JEDEC ID: {FLASH_JEDEC}' not in text:
            raise LoaderFailed('The flash chip did not identify as expected during the write.')

    def reset(self):
        self._run(['--reset'], 'reset', self.timing.reset)

    def load_sram(self, source):
        """Configure the FPGA from `source` (.fs) over JTAG without touching the flash: the chip
        runs the image until it is reconfigured or powered off. The argument list is checked by
        sram_only() first; the loader is never handed anything else here."""
        args = sram_only([SRAM_SWITCH, str(source)])
        self._run(args, 'sram', self.timing.sram)

    def _open_console_port(self):
        """The console's USB serial port, opened with DTR and RTS deasserted: the FPGA maps RTS to the
        ESP32's EN and (DTR, RTS) == (0, 0) to IO0 high (top.v), so this open neither resets the chip
        nor puts it into its ROM download mode. None when there is not exactly one console port;
        'busy' when Windows refuses the open (another program holds the port)."""
        try:
            import serial
            from serial.tools import list_ports
            ports = [p.device for p in list_ports.comports() if serial_port_matches(p.vid, p.pid)]
        except Exception:
            return None
        if len(ports) != 1:
            return None
        port = serial.Serial()
        port.port, port.baudrate, port.timeout = ports[0], 115200, 0.1
        port.dtr, port.rts = False, False  # opening must not hold the ESP32 in reset or in its ROM loader
        try:
            port.open()
        except Exception:
            return 'busy'
        return port

    @staticmethod
    def restart_esp32(port):
        """esptool's hard reset through the port's RTS line: EN low for 0.1 s, then high with IO0 high (a
        normal boot into the start-up menu). With esptool's Windows workaround: the usbser.sys driver
        sends the set-control-line-state request only when DTR is written, so every RTS change is
        followed by a write of DTR's own (deasserted) state. Without that the pulse can stay in the
        driver and nothing restarts, which is the most likely reason read saw no line
        while the console plainly ran 18.41."""
        port.rts = True
        port.dtr = False
        time.sleep(0.1)
        port.rts = False
        port.dtr = False

    def launcher_fpga_version(self):
        """Which console chip image is running NOW, as the start-up menu reports it. First PASSIVELY:
        launcher 1.3.5 says its "fpga version=" line every 5 s, so the port is only listened to
        (timing.passive), nothing is restarted and a RAM trial image, the overlay and the launcher's
        state are left exactly as they are. Only when no line comes (an older launcher, or nothing
        running) is the ESP32 restarted through RTS (restart_esp32; the FPGA is NOT reconfigured, so a
        RAM trial image keeps running; the custom bootloader counts this as a power-on) and the boot
        line read (timing.version). Returns (major, minor, debug); 'none' (the launcher heard no FPGA);
        'busy' (the port is held by another program); 'download' (after the restart the ESP32's ROM
        said "waiting for download": IO0 was low, a serial program's DTR, so no launcher runs);
        None when nothing readable arrived. last_version_read says how: 'passive' or 'restart'.
        No JTAG, no flash, no esptool command; the serial text is never printed or kept."""
        self.last_version_read = None
        port = self._open_console_port()
        # 'busy' is retried for the passive window: the Manager asked Remote Pilot to let go of USB before the
        # job (remote_pilot_link.UsbHandoff), and right after the trial image brings the port back Windows may
        # still be finishing its enumeration; a holder that stays is reported as busy.
        deadline = time.monotonic() + self.timing.passive
        while port == 'busy' and time.monotonic() < deadline:
            time.sleep(max(0.05, min(self.timing.poll, deadline - time.monotonic())))
            port = self._open_console_port()
        if port is None or port == 'busy':
            return port
        log = b''
        try:
            port.reset_input_buffer()
            deadline = time.monotonic() + self.timing.passive
            while time.monotonic() < deadline:
                log = (log + port.read(256))[-16384:]
                found = boot_fpga_version(log)
                if found is not None:
                    self.last_version_read = 'passive'
                    return found
            log = b''
            port.reset_input_buffer()
            self.restart_esp32(port)
            self.last_version_read = 'restart'
            deadline = time.monotonic() + self.timing.version
            while time.monotonic() < deadline:
                log = (log + port.read(256))[-16384:]
                found = boot_fpga_version(log)
                if found is not None:
                    return found
                if ROM_DOWNLOAD in log:
                    return 'download'
            return None
        except Exception:
            return None
        finally:
            try:
                port.close()
            except Exception:
                pass


    def launcher_status(self):
        """What the start-up menu says about itself, PASSIVELY: its "stats" line (every second) is
        listened for until it names a menu phase or timing.menu runs out. Nothing is sent, nothing is
        restarted; the port is opened as launcher_fpga_version opens it (DTR and RTS deasserted).
        Returns {'phase', 'uart', 'msgs'} of the newest line, or None. The serial text is never
        printed or kept. A port that is busy or not there yet is tried again for timing.passive (the
        version read's window: another reader letting go, Windows still enumerating), so the menu
        check does not fail on a port that was only late."""
        port = self._open_console_port()
        deadline = time.monotonic() + self.timing.passive
        while (port is None or port == 'busy') and time.monotonic() < deadline:
            time.sleep(max(0.05, min(self.timing.poll, deadline - time.monotonic())))
            port = self._open_console_port()
        if port is None or port == 'busy':
            return None
        log, last = b'', None
        try:
            port.reset_input_buffer()
            deadline = time.monotonic() + self.timing.menu
            while time.monotonic() < deadline:
                log = (log + port.read(256))[-16384:]
                found = launcher_status(log)
                if found is not None:
                    last = found
                    if found['phase'] in MENU_PHASES:
                        break
            return last
        except Exception:
            return last
        finally:
            try:
                port.close()
            except Exception:
                pass


# ---- the jobs --------------------------------------------------------------------------------
class FpgaJournal(Journal):
    KEEP_CLOSED_DAYS = 30

    def archive(self):
        closed = self.folder / 'closed'
        closed.mkdir(exist_ok=True)
        # The next free name (as ManagerJournal.archive,: two jobs closed in the same
        # second can draw the same clock digits, and a failed mkdir here turned a finished job into
        # "a file or device operation failed" (the intermittent test_chromatic_fpga.py failures).
        stamp, number = time.strftime('%Y%m%dT%H%M%S'), time.monotonic_ns() % 100000
        while True:
            target = closed / f'{stamp}-{number}'
            try:
                target.mkdir()
                break
            except FileExistsError:
                number += 1
        for path in [self.path, *self.folder.glob('image-*.bin'), *self.folder.glob('read-*.bin')]:
            if path.exists():
                shutil.move(str(path), str(target / path.name))
        expired = time.time() - self.KEEP_CLOSED_DAYS * 86400
        for old in closed.iterdir():
            try:
                if old.is_dir() and old != target and old.stat().st_mtime < expired:
                    shutil.rmtree(old)
            except OSError:
                pass


# What the window shows, big, while Try Pocket link waits for the watched power cycle (chromatic_manager_owner).
POWER_CYCLE_TITLE = 'Switch your Chromatic OFF, then ON'
POWER_CYCLE_TEXT = ('Do it now, with the USB cable left plugged in. This app is watching: it goes on by itself when '
                    'the console is back.')
POWER_CYCLE_MISSED = ('Your Chromatic was not seen switching off and on. Nothing was loaded or written. Press the '
                      'button again, then switch it off and on when this sign shows.')


class FpgaJobs:
    """Check, restore-original and write-media on the console chip's flash. Never raises out of
    a job: refusals and errors before any write say so and leave the flash untouched; anything
    after a write began ends in the original image or in MANUAL."""

    def __init__(self, folder=WORK, loader_factory=None, backup_loader=OriginalBackup.load, media_loader=MediaImage,
                 timing=None, say=print, sleep=time.sleep, clock=time.monotonic, session=SESSION,
                 stop_point=lambda: None, link_loader=LinkImage, owner=None):
        """`link_loader(allow_unreviewed=False)`: the sound + Pocket link image (18.41). The trial
        and the write call it plainly (refused while NEVER-INSTALL.txt is there); recognising the
        image on the flash calls it with allow_unreviewed=True. `owner`: where a job announces what
        the owner must do now (chromatic_manager_owner.OwnerPrompts; the Manager's own when it runs
        the job, so the window shows it big with a countdown)."""
        self.folder = Path(folder)
        self.timing = timing or FpgaTiming()
        self.loader_factory = loader_factory or (lambda: JtagLoader(self.folder / 'logs', timing=self.timing))
        self.backup_loader, self.media_loader, self.link_loader = backup_loader, media_loader, link_loader
        self.say, self.sleep, self.clock, self.session = say, sleep, clock, session
        self.stop_point = stop_point
        self.owner = owner if owner is not None else OwnerPrompts(lambda message: self.say(message))
        self.journal = FpgaJournal(self.folder)
        self.loader = None
        self.checked = None  # what this object's own Check FPGA read found the flash to hold

    # ---- state ------------------------------------------------------------------------------
    def pending(self):
        self.journal.load()
        return self.journal.exists and not self.journal.closed and self.journal.writes_started

    def rehearsal(self):
        """The last successful Restore FPGA of this process, or None."""
        try:
            record = json.loads((self.folder / REHEARSAL_FILE).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        if record.get('session') != self.session or not isinstance(record.get('at'), (int, float)):
            return None
        if not 0 <= time.time() - record['at'] <= REHEARSAL_VALID_S:
            return None
        return record

    def _record_rehearsal(self, original_sha):
        self.folder.mkdir(parents=True, exist_ok=True)
        write_durable(self.folder / REHEARSAL_FILE,
                      json.dumps({'session': self.session, 'at': time.time(), 'original': original_sha}).encode('ascii'))

    # ---- device steps -----------------------------------------------------------------------
    def _identify(self):
        """Exactly one GWU2X, and the chain's one device is the GW5A-25. Refuses otherwise."""
        if self.loader is None:
            self.loader = self.loader_factory()
        self.loader.check_tool()
        found = self.loader.present()
        if found['gwu2x'] != 1:
            raise Refused('Expected exactly one Chromatic (its GWU2X bridge) on USB; found '
                          f'{found["gwu2x"]}. Plug in only your Chromatic, switched on.')
        chip = self.loader.detect()
        if chip.get('idcode') != IDCODE or FAMILY not in str(chip.get('family', '')):
            raise Refused('This is not the expected console chip (GW5A-25): nothing is read or written.')
        return found

    def _read(self, label):
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f'read-{label}.bin'
        if path.exists():
            path.unlink()
        self.say(f'Reading the console chip\'s flash (all 4 MiB, about half a minute; the screen goes dark)...')
        return self.loader.dump(path)

    @staticmethod
    def _which(image, original, media_expected=None, link_expected=None):
        if image == original:
            return 'original'
        if media_expected is not None and image == media_expected:
            return 'media'
        if link_expected is not None and image == link_expected:
            return 'link'
        return 'unknown'

    def _known_images(self, backup):
        """(sound flash image, sound + Pocket link flash image), each None when it cannot be built
        or pinned here: what the flash is compared with, besides the original."""
        images = []
        for load in (lambda: self.media_loader(), lambda: self.link_loader(allow_unreviewed=True)):
            try:
                images.append(load().expected(backup.image))
            except Refused:
                images.append(None)
        return tuple(images)

    def _reconfigure_and_watch(self):
        """--reset, then wait for the console's serial interface (made by the FPGA) to return.
        True when it came back; the owner also sees the screen come on."""
        self.say('Restarting the console chip from its flash and watching the console come back...')
        self.loader.reset()
        return self._watch()

    def _running_version(self):
        """What the start-up menu reports the console chip runs (JtagLoader.launcher_fpga_version),
        or None when it cannot be read. The USB port coming back only proves SOME image runs: a
        trial that fell back to the flash image looks the same from Windows."""
        reader = getattr(self.loader, 'launcher_fpga_version', None)
        if reader is None:
            return None
        try:
            found = reader()
        except Exception:
            return None
        if found is None or found in ('none', 'busy', 'download'):
            return found
        try:
            major, minor, debug = (int(v) for v in found)
        except (TypeError, ValueError):
            return None
        return major, minor, debug

    def _running_status(self):
        """The start-up menu's own status (JtagLoader.launcher_status): {'phase', 'uart', 'msgs'},
        or None when it cannot be read."""
        reader = getattr(self.loader, 'launcher_status', None)
        if reader is None:
            return None
        try:
            found = reader()
            return {'phase': str(found['phase'])[:16], 'uart': int(found['uart']), 'msgs': int(found['msgs'])}
        except Exception:
            return None

    def _watch_power_cycle(self):
        """The owner switches the console off and on: its USB serial interface goes away and comes back.
        True once both were seen within timing.boot; False when the console never went away."""
        if self.loader is None:  # before _identify (which makes it otherwise): a fresh per-job FpgaJobs
            self.loader = self.loader_factory()
        deadline = self.clock() + self.timing.boot
        gone = False
        while self.clock() < deadline:
            found = self.loader.present()
            if not found['serial']:
                gone = True
            elif gone and found['gwu2x'] == 1:
                return True
            self.sleep(self.timing.poll)
        return False

    def _watch(self):
        """Wait for the console's serial interface (made by the FPGA image now running) to appear."""
        deadline = self.clock() + self.timing.boot
        while self.clock() < deadline:
            found = self.loader.present()
            if found['serial'] and found['gwu2x'] == 1:
                return True
            self.sleep(self.timing.poll)
        return False

    # ---- jobs -------------------------------------------------------------------------------
    def _try_reset(self):
        """After a read that stopped the FPGA, bring the console back from its flash (best effort)."""
        if self.loader is not None:
            try:
                self.loader.reset()
            except Exception:
                pass

    def guarded(self, job):
        try:
            return job()
        except Refused as error:
            self.say(f'Refused, nothing written: {error}')
            return EXIT_REFUSED
        except StepFailed as error:
            self._try_reset()
            self.say(f'Stopped before any write: {error}')
            return EXIT_REFUSED
        except (OSError, RuntimeError) as error:  # may name a private file: the type only
            self._try_reset()
            self.say(f'Stopped before any write: a file or device operation failed ({type(error).__name__}).')
            return EXIT_REFUSED
        except KeyboardInterrupt:
            self._try_reset()
            self.say('Stopped before any write: cancelled.')
            return EXIT_REFUSED
        except Exception as error:  # anything else (ValueError, TypeError, ...): never out of the job with a
            # RAM image left running. SystemExit is not caught. Its text may name a private file: the type only.
            try:
                unfinished = bool(self.pending())
            except Exception:
                unfinished = False
            if unfinished:  # cannot happen before a write; said truthfully if it ever does (Home offers Finish it)
                self.say(f'Stopped: an unexpected error ({type(error).__name__}) with an unfinished console chip job. '
                         'Finish it first.')
                return EXIT_REFUSED
            self._try_reset()
            self.say(f'Stopped before any write: an unexpected error ({type(error).__name__}). The console chip was '
                     'restarted from its flash.')
            return EXIT_REFUSED

    def _console_on_usb(self):
        """Is the console's USB serial interface there right now? Enumeration only (the loader's
        serial_present; its present() for a loader without one): nothing is opened or sent.
        Anything that cannot be read counts as not there."""
        try:
            seen = getattr(self.loader, 'serial_present', None)
            return bool(seen() if seen is not None else self.loader.present()['serial'])
        except Exception:
            return False

    def _console_usb_look(self):
        """One look for KeepWatch: 'fresh', 'stock', 'there', 'absent' or 'odd'. Enumeration only
        (the loader's serial_pids; _console_on_usb for a loader without it)."""
        pids = getattr(self.loader, 'serial_pids', None)
        if pids is None:
            return 'there' if self._console_on_usb() else 'absent'
        try:
            found = pids()
        except Exception:
            found = None
        if not found:
            return 'absent'
        if len(found) != 1 or not serial_port_matches(SERIAL_VID, found[0]):
            return 'odd'
        low = found[0] & 0xFF
        return 'fresh' if low == 0x00 else 'stock' if low == 0x01 else 'odd'

    def open_job_guard(self):
        self.journal.load()
        if self.journal.exists and not self.journal.closed:
            if not self.journal.writes_started:
                self.journal.append('close', 'nothing-written')
                self.journal.archive()
            else:
                raise Refused('an unfinished console chip job exists. Finish or abandon it first.')
        elif self.journal.exists:
            self.journal.archive()
        self.journal.load()

    def check(self):
        """Check FPGA: identity, a full read, and what the flash holds (original, sound, other).
        Writes nothing; the chip is reconfigured from its flash afterwards (the screen returns)."""
        def job():
            if self.pending():
                raise Refused('an unfinished console chip job exists. Finish or abandon it first.')
            backup = self.backup_loader()
            media_expected, link_expected = self._known_images(backup)
            self._identify()
            try:
                image = self._read('check')
                which = self._which(image, backup.image, media_expected, link_expected)
            finally:
                seen = self._reconfigure_and_watch()
            text = {'original': f'your original image (the backup of {backup.taken}), byte for byte',
                    'media': 'the sound (stock+media) image this tool installs, byte for byte',
                    'link': 'the Pocket link image (stock + sound + Pocket link, 18.41) this tool installs, byte for '
                            'byte',
                    'unknown': 'neither your original image nor the sound image'}[which]
            self.say(f'Console chip: GW5A-25 confirmed. Its flash holds {text}. Nothing was written.')
            self._note_check(which)
            self.checked = which
            if not seen:
                self.say('The console was not seen coming back on USB. Switch it off and on; it then starts from '
                         'its flash as before.')
                return EXIT_NOT_SEEN
            self.say('The console came back (its USB serial interface returned).')
            return EXIT_OK
        return self.guarded(job)

    def _note_check(self, which):
        """What the flash was just seen to hold: by Check FPGA's read, or by this tool's own write
        and full readback ('original', 'media', 'link', 'unknown')."""
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            write_durable(self.folder / LAST_CHECK_FILE, json.dumps({'at': time.time(), 'holds': which}).encode('ascii'))
        except OSError:
            pass

    def _last_check(self):
        try:
            record = json.loads((self.folder / LAST_CHECK_FILE).read_text(encoding='utf-8'))
            return str(record['holds']), float(record['at'])
        except (OSError, ValueError, KeyError, TypeError):
            return None, None

    def last_known(self):
        """What this tool last saw the flash hold, whenever that was, or None. Only a hint for
        whether to read the flash at all: every write reads the flash itself before writing."""
        return self._last_check()[0]

    def known_flash(self):
        """What the flash holds, when this tool saw it recently: the newer of a Restore FPGA in this
        process ('original') and what a Check FPGA or this tool's own verified write found within
        TRIAL_CHECK_VALID_S ('original', 'media' or 'link'); None otherwise. So a sound or Pocket link
        write after the rehearsal is what counts, and a later check that found an unknown image
        makes it None, never 'original'. A temporary trial needs it, so a power cycle afterwards
        lands on a known image."""
        rehearsal = self.rehearsal()
        holds, at = self._last_check()
        if rehearsal is not None and (at is None or rehearsal['at'] > at):
            return 'original'
        if holds in FLASH_WORDS_FOR and at is not None and 0 <= time.time() - at <= TRIAL_CHECK_VALID_S:
            return holds
        return None

    def restore_original(self, confirm=None):
        """Restore FPGA: write the owner's original 4 MiB image (Shiny's "rescue with a backup",
        FPGA only), read back and compare all of it, reconfigure, watch the console return.
        Over an identical original this is the rehearsal; over the sound image it is the undo."""
        def job():
            self.open_job_guard()
            backup = self.backup_loader()
            media_expected, link_expected = self._known_images(backup)
            self._identify()
            current = self._read('before')
            which = self._which(current, backup.image, media_expected, link_expected)
            if which == 'original':
                self.say(f'The flash already holds your original image: this write is the rehearsal (same bytes, '
                         'same command, same checks as a real rescue).')
            elif which == 'media':
                self.say('The flash holds the sound image; writing your original image back.')
            elif which == 'link':
                self.say('The flash holds the Pocket link image (sound + Pocket link); writing your original image back.')
            else:
                self.say('The flash holds neither your original image nor the sound image (for example a newer official '
                         'firmware, or an interrupted write). Writing the original back changes it.')
                agreed = confirm is not None and confirm(FPGA_UNKNOWN_PROMPT).strip().lower() == 'change'
                if not agreed:
                    self.loader.reset()
                    raise Refused('the flash holds an unknown image and that was not confirmed. If an official update '
                                  'changed the console, use Check FPGA first and see the runbook.')
            code = self._run_job('restore', current, backup.image, backup.path, backup)
            if code in (EXIT_OK, EXIT_NOT_SEEN):
                self._note_check('original')
            if code == EXIT_OK:
                self._record_rehearsal(backup.sha)
                self.say(f'Original console chip image written back (backup of {backup.taken}), all 4 MiB read back '
                         'and identical, console restarted. Rehearsal recorded for this session.')
            return code
        return self.guarded(job)

    def write_media(self, approved_fingerprint=None):
        """FPGA sound: write the built stock+media bitstream as a full 4 MiB flash image (the
        bitstream, then 0xFF), with the same --write-flash --verify of a 4 MiB .bin that Restore
        FPGA uses, after a rehearsal in this session, only over the original image, then read
        back all 4 MiB and compare with exactly that image, reconfigure, watch."""
        def job():
            self.open_job_guard()
            backup = self.backup_loader()
            media = self.media_loader()
            if approved_fingerprint is not None and approved_fingerprint != media.fingerprint:
                raise Refused('the sound image is not the one approved in the window. Approve the new one first.')
            rehearsed = self.rehearsal() is not None and self.rehearsal().get('original') == backup.sha
            # Leaving the original needs the rehearsal in this session. Going back from the Pocket link
            # image (one this tool wrote and verified) to the sound image is one click: the flash is read
            # first, and only an exact Pocket link image (or the original after a rehearsal) is written over.
            if not rehearsed and self.last_known() != 'link':
                raise Refused(NO_REHEARSAL)
            expected = media.expected(backup.image)
            if expected == backup.image:
                raise Refused('the sound image is identical to your original image; nothing to install.')
            link_expected = self._known_images(backup)[1]
            self._identify()
            current = self._read('before')
            which = self._which(current, backup.image, expected, link_expected)
            if which == 'media':
                self.loader.reset()
                self._note_check('media')
                self.say('The flash already holds this sound image. Nothing written.')
                return EXIT_OK
            if which == 'original' and not rehearsed:
                self.loader.reset()
                raise Refused(NO_REHEARSAL)
            if which not in ('original', 'link'):
                self.loader.reset()
                raise Refused('the flash does not hold your original image, so the sound image is not written over it. '
                              'Run Restore FPGA first (it also tells you what the flash holds).')
            self.say(f'Writing {media.short()} over {FLASH_WORDS_FOR[which]} (all 4 MiB: the bitstream, then erased '
                     'flash)...')
            code = self._run_job('media', current, expected, media.write_file(self.folder), backup)
            if code in (EXIT_OK, EXIT_NOT_SEEN):
                self._note_check('media')
            if code == EXIT_OK:
                self.say(f'{media.short()} installed: all 4 MiB read back and identical to the expected flash, console '
                         'restarted. Games run on it with the sound additions switched off; Restore FPGA undoes it.')
            return code
        return self.guarded(job)

    def known_original(self):
        """True when this tool has recently seen the flash hold the owner's original image and has
        seen nothing else written since: known_flash() == 'original' (a Restore FPGA in this
        process, or a Check FPGA within TRIAL_CHECK_VALID_S, whichever is newer). A sound write
        after the rehearsal makes it False. The temporary trial needs that, so a power cycle lands
        on a known image."""
        return self.known_flash() == 'original'

    def try_media(self, approved_fingerprint=None):
        """Try FPGA image (temporary): load the built stock+media .fs into the console chip's
        configuration RAM over JTAG (--write-sram), never into the flash, and watch the console
        come back on it. The chip runs the image until the console is switched off and on (or
        Check FPGA / Restore FPGA reconfigure it from the flash). No journalled write, nothing to
        recover: the flash is not touched by any step here."""
        def job():
            self.open_job_guard()
            media = self.media_loader()
            if approved_fingerprint is not None and approved_fingerprint != media.fingerprint:
                raise Refused('the sound image is not the one approved in the window. Approve the new one first.')
            if not self.known_original():
                raise Refused('this tool has not recently seen the console chip\'s flash hold your original image '
                              '(Check FPGA within 24 hours, or Restore FPGA in this session). Do that first, so '
                              'switching the console off and on after the trial lands on a known image.')
            self._identify()
            self.stop_point()
            self.journal.append('trial', 'sram-load', image=media.fingerprint, session=self.session)
            self.say(f'Loading {media.short()} into the console chip\'s RAM only (the flash is not touched; the '
                     'screen goes dark for about half a minute, then the console restarts on the trial image)...')
            try:
                self.loader.load_sram(media.path)
            except (StepFailed, OSError) as error:
                self.journal.append('close', 'trial-failed', reason=type(error).__name__)
                self.journal.archive()
                self._try_reset()
                self.say('The temporary load did not complete; the console chip was restarted from its flash, which '
                         'was never touched. Switch the console off and on if it did not come back.')
                return EXIT_REFUSED
            seen = self._watch()
            self.journal.append('close', 'trial-running', image=media.fingerprint, seen=seen)
            self.journal.archive()
            self.say(f'{media.short()} is running from the console chip\'s RAM; the flash still holds your original '
                     'image. Look at the console now: the start-up menu must be drawn right, then Jellyfin picture '
                     'and sound. Switching the console off and on (or Check FPGA) returns to the flash image.')
            if not seen:
                self.say('The console was not seen coming back on USB on the trial image. If the screen is dark or '
                         'wrong, switch the console off and on: it starts from its flash as before.')
                return EXIT_NOT_SEEN
            self.say('The console came back (its USB serial interface returned) on the trial image.')
            return EXIT_OK
        return self.guarded(job)

    # ---- the sound + Pocket link image (18.41): owner-present, try first, then write ------------------
    def _link_try_path(self):
        return self.folder / LINK_TRY_FILE

    def link_try(self):
        """The recorded temporary try of the Pocket link image (link-try.json), or None. A dict
        with: image (the .fs fingerprint), session, at, seen, and once the owner answered,
        answers and confirmed."""
        try:
            record = json.loads(self._link_try_path().read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        return record if isinstance(record, dict) else None

    def link_try_needs_power_cycle(self, now=None):
        """True when a Pocket link try is recorded within LINK_TRY_POWER_CYCLE_S: the console may still
        run that RAM image and must be switched off and on, watched, before another load. Keyed on
        the record alone (its time), never on which Manager process made it; a record with a time in
        the future (the PC clock moved back) counts too, since it cannot be shown to be old."""
        record = self.link_try()
        if record is None:
            return False
        at = record.get('at')
        if not isinstance(at, (int, float)):
            return True  # a try with no readable time: treat it as recent
        now = time.time() if now is None else now
        return now - at <= LINK_TRY_POWER_CYCLE_S

    @staticmethod
    def answers_all_good(answers):
        """Every check answered, each 'good' (the link-cable game may be 'skipped'), nothing else."""
        if not isinstance(answers, dict) or set(answers) != set(LINK_CHECKS):
            return False
        return all(answers[k] == 'good' or (k in LINK_SKIPPABLE and answers[k] == 'skipped') for k in LINK_CHECKS)

    @staticmethod
    def auto_checks_all_passed(checks):
        """Every automatic check is there and passed, nothing else."""
        return isinstance(checks, dict) and set(checks) == set(AUTO_CHECKS) and all(v is True for v in checks.values())

    def link_try_confirmed(self, fingerprint, accept_auto=False):
        """The recorded try when it may open the flash write of the image `fingerprint`: the same
        image, loaded and seen running, within LINK_TRY_VALID_S, every check answered All good
        after the load (this run's try or a recorded one). None otherwise. With `accept_auto` (the
        one-step update only) a try the machine confirmed counts too, but never on the machine's
        word alone: confirmed_by "auto" with every automatic check passed AND the owner's Approve
        given with the image running (approve_link_try), in this session, after the checks, within
        KEEP_VALID_S. The owner's own answers always decide once they are given: a try the owner
        answered is confirmed_by "owner", whatever the machine found before."""
        record = self.link_try()
        if record is None or not fingerprint or record.get('image') != fingerprint:
            return None
        at, confirmed_at = record.get('at'), record.get('confirmed_at')
        if not isinstance(at, (int, float)) or not isinstance(confirmed_at, (int, float)):
            return None
        if record.get('seen') is not True or record.get('confirmed') is not True or confirmed_at < at:
            return None
        if record.get('running') != list(LINK_RUNNING):  # the answers were about 18.41, proven at the try
            return None
        if not 0 <= time.time() - at <= LINK_TRY_VALID_S:
            return None
        if record.get('confirmed_by') == CONFIRMED_BY_AUTO:
            if not accept_auto or not self.auto_checks_all_passed(record.get('auto_checks')):
                return None
            approved_at = record.get('owner_approved_at')
            if (record.get('owner_approved') is not True or record.get('session') != self.session
                    or record.get('owner_approved_session') != self.session
                    or not isinstance(approved_at, (int, float)) or approved_at < confirmed_at
                    or not 0 <= time.time() - approved_at <= KEEP_VALID_S):
                return None
        elif not self.answers_all_good(record.get('answers')):
            return None
        return record

    def confirm_link_try(self, answers):
        """The owner's answers after looking at the console on the trial image (the guide's look
        screens): recorded with the try. Returns True when they confirm it (every check All good,
        the link-cable game may be skipped); False when they do not (recorded too, so the write
        stays refused). Refused when there is no recorded try of the current image to answer for."""
        image = self.link_loader()
        record = self.link_try()
        if record is None or record.get('image') != image.fingerprint or record.get('seen') is not True:
            raise Refused('no temporary try of this Pocket link image is recorded (or it was not seen running); try it '
                          'first.')
        if record.get('running') != list(LINK_RUNNING):
            raise Refused('the recorded try was not confirmed running 18.41 on the console (its start-up menu reported '
                          'another image); try it again.')
        at = record.get('at')
        if not isinstance(at, (int, float)):
            raise Refused('the recorded temporary try has no time; try the image again.')
        now = time.time()  # read once: the moment checked is the moment recorded
        if now - at > LINK_TRY_VALID_S:
            raise Refused('the recorded temporary try is too old; try the image again.')
        answers = {k: str(v) for k, v in dict(answers or {}).items() if k in LINK_CHECKS}
        # The PC clock is now earlier than the try (it was moved back): link_try_confirmed would never
        # count these answers for the write, so they do not confirm the try here either (kept, not confirmed).
        clock_back = now < at
        good = self.answers_all_good(answers) and not clock_back
        record.update(answers=answers, confirmed=good, confirmed_at=now, confirmed_session=self.session,
                      confirmed_by=CONFIRMED_BY_OWNER)
        self.folder.mkdir(parents=True, exist_ok=True)
        write_durable(self._link_try_path(), json.dumps(record).encode('ascii'))
        if clock_back:
            self.say(SAID_CLOCK_BACK)
        return good

    def auto_confirm_link_try(self):
        """The one-step update's checks on the try that was just loaded, made by this tool instead
        of asked of the owner, and recorded with the try as confirmed_by "auto" (so the record says
        who confirmed it). Checked: the RAM load finished (the record exists for this image), the
        console came back on USB, the ESP32 is not in its download mode, the start-up menu reports
        18.41 with the debug bit, and the menu's own status line names a menu phase. NOT checked
        (AUTO_NOT_CHECKED): the Pocket link row and cable, sound, a game, a link-cable game. Returns
        True when every check passed. Refused when no try of the current image is recorded."""
        image = self.link_loader()
        record = self.link_try()
        if record is None or record.get('image') != image.fingerprint:
            raise Refused('no temporary try of this Pocket link image is recorded; nothing to check.')
        at = record.get('at')
        if not isinstance(at, (int, float)):
            raise Refused('the recorded temporary try has no time; try the image again.')
        now = time.time()
        if now - at > LINK_TRY_VALID_S:
            raise Refused('the recorded temporary try is too old; try the image again.')
        running = record.get('running')
        checks = dict.fromkeys(AUTO_CHECKS, False)
        checks['loaded'] = True  # try_link writes the record only after the loader finished the RAM load
        checks['usb_back'] = record.get('seen') is True
        checks['not_download'] = checks['usb_back'] and running != 'download'
        checks['version'] = checks['usb_back'] and running == list(LINK_RUNNING)
        status = self._running_status() if checks['version'] else None
        checks['menu'] = status is not None and status['phase'] in MENU_PHASES
        good = all(checks.values()) and now >= at  # a PC clock moved back confirms nothing (as for the owner's answers)
        record.pop('answers', None)
        record.update(confirmed=good, confirmed_at=now, confirmed_session=self.session,
                      confirmed_by=CONFIRMED_BY_AUTO, auto_checks=checks, status=status)
        self.folder.mkdir(parents=True, exist_ok=True)
        write_durable(self._link_try_path(), json.dumps(record).encode('ascii'))
        for key in AUTO_CHECKS:
            self.say(f'  {"passed" if checks[key] else "FAILED"}: {AUTO_CHECK_WORDS[key]}')
        if now < at:
            self.say(SAID_CLOCK_BACK)
        if good:
            self.say(f'{SAID_AUTO_PASSED}: all {len(AUTO_CHECKS)}, made by this app on the trial image. Not checked by a '
                     f'machine: {"; ".join(AUTO_NOT_CHECKED)}. You look at a game now, before anything is written; '
                     'the rest you test by plugging in.')
        return good

    def approve_link_try(self, approved, heard=None):
        """The owner's Approve (or Not right) given while the machine-confirmed try was running from
        RAM (update_link), recorded with that try: owner_approved, when, in which session, and what
        the console reported right after the click (`heard`: 'heard' when its start-up menu said
        18.41, 'not heard' when it said nothing, or the other version it named). confirmed_by stays
        "auto": the machine made the checks, the owner approved the image they saw running. Refused
        unless this session's automatic try of the current image passed every check."""
        image = self.link_loader()
        record = self.link_try()
        if (record is None or record.get('image') != image.fingerprint or record.get('session') != self.session
                or record.get('confirmed_by') != CONFIRMED_BY_AUTO or record.get('confirmed') is not True
                or not self.auto_checks_all_passed(record.get('auto_checks'))):
            raise Refused('no automatic try of this Pocket link image from this run passed its checks; there is '
                          'nothing to approve.')
        record.update(owner_approved=bool(approved), owner_approved_at=time.time(), owner_approved_session=self.session,
                      owner_approved_running=heard)
        self.folder.mkdir(parents=True, exist_ok=True)
        write_durable(self._link_try_path(), json.dumps(record).encode('ascii'))
        return bool(approved)

    def try_link(self, approved_fingerprint=None, auto=False):
        """Try the Pocket link image (temporary): exactly try_media's SRAM-only load (sram_only()
        guards the loader line; the flash is neither read nor written), for the reviewed 18.41
        image. Also needs the verified original backup (the undo exists) and a recent sight of what
        the flash holds (the image a power cycle returns to). Records the try for the owner's
        answers; any earlier try record is dropped first. `auto` (the one-step update) changes only
        the closing words: the checks that follow are this tool's, not the owner's."""
        def job():
            self.open_job_guard()
            self.backup_loader()  # both disk copies verified: the undo is there before anything runs
            link = self.link_loader()
            if approved_fingerprint is not None and approved_fingerprint != link.fingerprint:
                raise Refused('the Pocket link image is not the one approved in the window. Approve the new one first.')
            held = self.known_flash()
            if held is None:
                raise Refused('this tool has not recently seen what the console chip\'s flash holds (Check FPGA within '
                              '24 hours, or Restore FPGA in this session). Do that first, so switching the console '
                              'off and on after the trial lands on a known image.')
            # a second RAM load without a power cycle in between ended in game mode with a white
            # screen. A try recorded on disk within LINK_TRY_POWER_CYCLE_S (whichever Manager process made it) needs
            # the console switched off and on first: watched here, so the next load starts from a console that
            # booted from its flash with every chip (FPGA, ESP32, cartridge, display) freshly reset. A power cycle
            # done before this job started is not seen and does not count: it must happen while this tool watches.
            if self.link_try_needs_power_cycle():
                # Announced as an owner action (this request was in the log only, was missed, and
                # the job timed out): the window shows it big and bright with the countdown.
                self.owner.need(POWER_CYCLE_TITLE, POWER_CYCLE_TEXT, seconds=self.timing.boot, log=(
                    'A Pocket link try ran on this console in the last '
                    f'{LINK_TRY_POWER_CYCLE_S // 60} minutes. Switch the console OFF and ON now, while this tool '
                    'watches: it waits for the console\'s USB serial port to go away and come back (up to '
                    f'{int(self.timing.boot)} s) before loading the image again. Switching it off and on before '
                    'pressing the button is not seen and does not count.'))
                try:
                    cycled = self._watch_power_cycle()
                except BaseException:
                    self.owner.close()
                    raise
                if not cycled:
                    self.owner.missed(POWER_CYCLE_MISSED)
                    raise Refused('the console was not switched off and on while this tool watched, since the last '
                                  'Pocket link try. Start again and switch it off and on when asked.')
                self.owner.done('Your Chromatic was switched off and on.', log=(
                    'The console was switched off and on (its USB serial port went away and came back).'))
            self._identify()
            self.stop_point()
            try:
                self._link_try_path().unlink()
            except FileNotFoundError:
                pass
            self.journal.append('trial', 'sram-load', image=link.fingerprint, session=self.session, kind='link')
            self.say(f'Loading {link.short()} into the console chip\'s RAM only (the flash is not touched; the '
                     'screen goes dark for about half a minute, then the console restarts on the trial image)...')
            try:
                self.loader.load_sram(link.path)
            except (StepFailed, OSError) as error:
                self.journal.append('close', 'trial-failed', reason=type(error).__name__)
                self.journal.archive()
                self._try_reset()
                self.say('The temporary load did not complete; the console chip was restarted from its flash, which '
                         'was never touched. Switch the console off and on if it did not come back.')
                return EXIT_REFUSED
            seen = self._watch()
            # The USB port returning proves only that SOME image runs. The start-up menu names the one that
            # does (its version request, printed at every start): it must be 18.41 with the debug bit before
            # the owner is asked anything, or the answers would be about the flash image (the
            # Jellyfin menu had no Pocket link row because the console was on 18.40 by then).
            running = self._running_version() if seen else None
            how = getattr(self.loader, 'last_version_read', None)
            confirmed = running == LINK_RUNNING
            self.journal.append('close', 'trial-running', image=link.fingerprint, seen=seen, confirmed=confirmed, read=how)
            self.journal.archive()
            self.folder.mkdir(parents=True, exist_ok=True)
            write_durable(self._link_try_path(), json.dumps({'image': link.fingerprint, 'session': self.session,
                                                             'at': time.time(), 'seen': bool(seen),
                                                             'running': list(running) if isinstance(running, tuple)
                                                             else running, 'read': how, 'flash': held}).encode('ascii'))
            self._tried = link.fingerprint  # this call loaded the image and recorded the try (update_link asks)
            if not seen:
                self.say(f'{link.short()} was loaded into the console chip\'s RAM, but the console was not seen coming '
                         'back on USB on the trial image. If the screen is dark or wrong, switch the console off and '
                         f'on: it starts from {FLASH_WORDS_FOR[held]} in its flash, as before.')
                return EXIT_NOT_SEEN
            if not confirmed:
                self.say(f'{SAID_LINK_NOT_RUNNING}: the console\'s start-up menu reports {version_words(running)}, not '
                         f'18.41. Nothing was written; the flash still holds {FLASH_WORDS_FOR[held]}.'
                         + ('' if auto else ' Do not answer any checks on this try. Switch the console off and on, '
                                            'then start again.'))
                if running == 'busy':
                    self.say('Close the program that holds the console\'s serial port (a serial terminal, or a Remote '
                             'Pilot receiver this app did not start: it asks the receiver to let go before every job) '
                             'before the next try; the console itself may well be running the trial image.')
                elif running == 'download':
                    self.say('A serial program had the port open with DTR on while the console\'s ESP32 restarted, so it '
                             'booted into its ROM download mode instead of the start-up menu (the screen shows the '
                             'game layer, usually white, with no menu). Nothing was written anywhere. Close that program, '
                             'switch the console off and on, and it starts normally.')
                elif running is None and how == 'restart':
                    self.say('Neither the console\'s own status line (launcher 1.3.5 says it every 5 s) nor its boot line '
                             'after a restart arrived. Check the USB cable and that no other program reads the port.')
                return EXIT_REFUSED
            self.say(f'{SAID_LINK_RUNNING}, the Pocket link image '
                     + ('(read from the console\'s own status line; nothing was restarted).' if how == 'passive' else
                        '(the console restarted its start-up menu once more to read it).'))
            if auto:
                self.say(f'{link.short()} is running from the console chip\'s RAM; the flash still holds '
                         f'{FLASH_WORDS_FOR[held]}. This app checks it now.')
            else:
                self.say(f'{link.short()} is running from the console chip\'s RAM; the flash still holds '
                         f'{FLASH_WORDS_FOR[held]}. Look at the console now, in this order: the Pocket link row in the '
                         'Jellyfin menu, Jellyfin sound, then games and a link-cable game. Switching the console off '
                         'and on (or Check FPGA) returns to the flash image and ends the try.')
            self.say('The console came back (its USB serial interface returned) on the trial image.')
            return EXIT_OK
        return self.guarded(job)

    def write_link(self, approved_fingerprint=None, accept_auto=False):
        """FPGA Pocket link: write the reviewed 18.41 image as a full 4 MiB flash image with the
        same --write-flash --verify, full readback, reconfigure and watch as write_media. Refused
        unless a temporary try of exactly this image was confirmed by the owner (every check All
        good), or, with `accept_auto` (update_link only), by this tool's automatic checks together
        with the owner's Approve given while it ran (link_try_confirmed); written over the sound
        image (18.40), or over the original only after a rehearsal in this session. A failure after
        the write began puts the original back (recover). The journal's plan names who confirmed
        the try."""
        def job():
            self.open_job_guard()
            backup = self.backup_loader()
            link = self.link_loader()
            if approved_fingerprint is not None and approved_fingerprint != link.fingerprint:
                raise Refused('the Pocket link image is not the one approved in the window. Approve the new one first.')
            tried = self.link_try_confirmed(link.fingerprint, accept_auto=accept_auto)
            if tried is None:
                raise Refused(NO_LINK_TRY)
            expected = link.expected(backup.image)
            if expected == backup.image:
                raise Refused('the Pocket link image is identical to your original image; nothing to install.')
            rehearsed = self.rehearsal() is not None and self.rehearsal().get('original') == backup.sha
            if not rehearsed and self.last_known() not in ('media', 'link'):
                raise Refused('this tool has not seen the console chip hold the sound image (Check FPGA), and Restore '
                              'FPGA has not run in this session. Press Check FPGA first.')
            media_expected = self._known_images(backup)[0]
            self._identify()
            current = self._read('before')
            which = self._which(current, backup.image, media_expected, expected)
            if which == 'link':
                self.loader.reset()
                self._note_check('link')
                self.say('The flash already holds this Pocket link image. Nothing written.')
                return EXIT_OK
            if which == 'original' and not rehearsed:
                self.loader.reset()
                raise Refused('the flash holds your original image, and leaving it needs Restore FPGA (the rehearsal) '
                              'in this session first. Or set up Jellyfin sound first.')
            if which not in ('original', 'media'):
                self.loader.reset()
                raise Refused('the flash holds neither the sound image nor your original image, so the Pocket link '
                              'image is not written over it. Press Check FPGA, then see the runbook.')
            self.say(f'Writing {link.short()} over {FLASH_WORDS_FOR[which]} (all 4 MiB: the bitstream, then erased '
                     'flash)...')
            code = self._run_job('link', current, expected, link.write_file(self.folder), backup,
                                 try_confirmed_by=str(tried.get('confirmed_by') or CONFIRMED_BY_OWNER),
                                 owner_approved_running=tried.get('owner_approved_running'))
            if code in (EXIT_OK, EXIT_NOT_SEEN):
                self._note_check('link')
            if code == EXIT_OK:
                self.say(f'{link.short()} installed: all 4 MiB read back and identical to the expected flash, console '
                         'restarted. Games run on it as before; FPGA sound writes the sound image back, Restore FPGA '
                         'your original.')
            return code
        return self.guarded(job)

    def _ask_power_cycle(self, why):
        """The one watched power cycle a one-step update may ask for: announced as an owner action
        (big, with the countdown). True once the console was seen going away and coming back."""
        self._power_cycle_asked = True
        self.owner.need(POWER_CYCLE_TITLE, POWER_CYCLE_TEXT, seconds=self.timing.boot, log=(
            f'{why} Switch the console OFF and ON now, while this tool watches (up to {int(self.timing.boot)} s).'))
        try:
            cycled = self._watch_power_cycle()
        except BaseException:
            self.owner.close()
            raise
        if cycled:
            self.owner.done('Your Chromatic was switched off and on.', log=(
                'The console was switched off and on (its USB serial port went away and came back).'))
        return cycled

    def _link_from_flash(self):
        """After the flash was seen (read byte for byte) to hold the Pocket link image and the chip
        was restarted from it: does the console run it? The start-up menu's own version line decides.
        One watched power cycle is asked for when the line does not come and none was asked for yet
        in this job."""
        running = self._running_version()
        if running != LINK_RUNNING and running != 'busy' and not getattr(self, '_power_cycle_asked', False):
            self.say(f'The console\'s start-up menu reports {version_words(running)} so far.')
            if self._ask_power_cycle('The flash holds the Pocket link image (read back and identical), but the '
                                     'console was not heard reporting it.'):
                running = self._running_version()
            else:
                self.owner.missed('The Pocket link image is in the flash (read back and identical). Switch your '
                                  'Chromatic off and on, then press Update the Pocket link again: it only checks.')
        if running == LINK_RUNNING:
            self.say(f'{SAID_LINK_FROM_FLASH}: its start-up menu reports version 18.41 with the debug bit after the '
                     'console chip was restarted from the flash.')
            return EXIT_OK
        first = 'Close that program' if running == 'busy' else 'Switch the console off and on'
        self.say(f'{SAID_LINK_NOT_FROM_FLASH}: the flash holds the Pocket link image (read back, byte for byte), but '
                 f'the start-up menu reports {version_words(running)}. {first}, then press Update the Pocket link '
                 'again: it only checks. Put the previous image back (FPGA sound) is there if it stays wrong.')
        return EXIT_NOT_SEEN

    def _restart_from_flash(self):
        """Nothing was written: restart the console chip from its untouched flash and watch it come
        back, so the console is as it was. True when it was seen back."""
        if self.loader is None:
            return False
        try:
            self.loader.reset()
            return bool(self._watch())
        except Exception:
            return False

    def _ask_keep(self, held, confirm):
        """The one Approve of update_link, asked while the image that passed the automatic checks
        still runs from RAM: announced as an owner action (big, no countdown: the owner starts a
        game and looks), then asked through `confirm`. Between the last automatic check and the
        answer this tool touches neither JTAG nor the serial port, so the image keeps running:
        while it waits, only Windows' list of COM ports is read (_console_usb_look, every
        timing.keep_poll) and judged by KeepWatch, which allows the one absence of stock firmware
        starting and latches on anything else. Returns 'kept' (approved, recorded with the try),
        'not_kept', 'switched_off' (approved, but the watch latched: the console was switched off,
        so it runs its flash image again, whatever a silent game lets the version read hear) or
        'ended' (approved, but the console reports another image)."""
        text = keep_text(held)
        self.owner.need(KEEP_TITLE, text, seconds=None, log=f'{KEEP_TITLE}, from the console chip\'s RAM. {text}')
        seen, answered = KeepWatch(self.timing.keep_reenum), threading.Event()
        self._keep_seen = seen

        def watch():
            while not answered.is_set() and seen.why is None:
                seen.see(self._console_usb_look())
                answered.wait(self.timing.keep_poll)

        watcher = threading.Thread(target=watch, name='keep-usb-watch', daemon=True)
        self._keep_watch = watcher
        watcher.start()
        try:
            answer = str(confirm(LINK_KEEP_PROMPT + text) or '').strip().lower()
        except BaseException:
            answered.set()
            watcher.join(self.timing.keep_join)
            self.owner.close()
            raise
        # The watcher ends before this tool does anything else with the console; one that did not end (the
        # list could not be read in time) counts as a missing port: the try can simply be run again.
        answered.set()
        watcher.join(self.timing.keep_join)
        if watcher.is_alive():
            seen.why = seen.why or 'no answer from the USB list'
        if answer != KEEP_ANSWER:
            self.owner.close()
            self.approve_link_try(False)
            return 'not_kept'
        # One more look, for a port that left at the very click; and a port that is away right now (stock
        # firmware starting, or a switch-off) is looked for until it is back or keep_reenum has passed.
        if seen.why is None:
            seen.see(self._console_usb_look())
        while not seen.settled():
            time.sleep(max(0.01, min(self.timing.keep_poll, self.timing.keep_reenum)))
            seen.see(self._console_usb_look())
        if seen.why is not None:
            self.owner.close()
            self.approve_link_try(False, heard=HEARD_SWITCHED_OFF)
            self.say(f'(While the Approve waited, the console\'s USB port was seen: {seen.why}.)')
            return 'switched_off'
        # Approve was pressed. Which image does the console run now? Its own start-up menu says (heard
        # passively, or after one ESP32 restart when a game runs and the menu is silent; the console chip
        # is not reconfigured by either, and the write that follows stops it anyway).
        running = self._running_version()
        if isinstance(running, tuple) and running != LINK_RUNNING:
            self.owner.close()
            self.approve_link_try(False, heard=list(running))
            self._ended_on = running
            return 'ended'
        heard = running == LINK_RUNNING
        if not heard and LINK_KEEP_MUST_HEAR:
            self.owner.close()
            self.approve_link_try(False, heard='not heard')
            self._ended_on = running
            return 'ended'
        self.approve_link_try(True, heard='heard' if heard else 'not heard')
        self.owner.done('You approved the new image.', log=(
            f'{SAID_KEPT} from the console chip\'s RAM (after Approve the console '
            + ('reported 18.41 with the debug bit' if heard else 'was not heard naming its image: a game was running '
               'or the port was busy') + '). Recorded with the try.'))
        return 'kept'

    def update_link(self, approved_fingerprint=None, confirm=None):
        """Update the Pocket link, in one step, with no question rounds and ONE Approve: (1) Check
        FPGA's read of what the flash holds; already the Pocket link image: only the last check
        runs. (2) try_link loads the image into RAM (always a fresh load: the Approve must be given
        with the image running) and auto_confirm_link_try makes the checks a machine can make; one
        that fails ends here with nothing written and the chip restarted from its untouched flash.
        (3) With the image still running from RAM, `confirm` asks the owner to start a game, look,
        and Approve (KEEP_ANSWER) or not (_ask_keep); anything but Approve, a console whose USB port
        was seen leaving as for a switch-off during the wait (KeepWatch), or one that reports another
        image by then, ends here the same way. No `confirm` (nothing can ask): refused before the
        console is touched. (4) write_link, unchanged: the same backups, flash read before, journal,
        write, full read-back and recovery; if it refuses before any write, the chip is restarted
        from its untouched flash and that is said. (5) The start-up menu must report 18.41
        after the chip restarted from the flash. Exit codes: EXIT_OK; EXIT_REFUSED when nothing was
        written; EXIT_NOT_SEEN when the flash holds the image but the console was not heard
        reporting it; write_link's own codes after a write began (recovered, manual).

        A watched power cycle is asked for at most once in a run, as an owner action: before the
        RAM load when a try ran in the last LINK_TRY_POWER_CYCLE_S (try_link's rule, measured on the
        console), or else at the end when the console does not report after the write."""
        self._power_cycle_asked = False
        found = {}

        def before():
            if self.pending():
                raise Refused('an unfinished console chip job exists. Finish or abandon it first.')
            backup = self.backup_loader()
            link = self.link_loader()  # pins, receipts and the NEVER-INSTALL refusal, before the console is touched
            if approved_fingerprint is not None and approved_fingerprint != link.fingerprint:
                raise Refused('the Pocket link image is not the one approved in the window. Approve the new one first.')
            found.update(fingerprint=link.fingerprint, short=link.short(),
                         rehearsed=self.rehearsal() is not None and self.rehearsal().get('original') == backup.sha)
            return EXIT_OK
        code = self.guarded(before)
        if code != EXIT_OK:
            return code
        self.say(f'Step 1 of 5: checking what the console chip holds now (a read; nothing is written). The image to '
                 f'install: {found["short"]}.')
        self.checked = None
        code = self.check()
        if code == EXIT_NOT_SEEN:
            self.say('The update stopped here: nothing was loaded or written. Switch the console off and on, then '
                     'press Update the Pocket link again.')
            return EXIT_REFUSED
        if code != EXIT_OK:
            return code  # refused: nothing was written
        held = self.checked
        if held == 'link':
            self.say('Steps 2 to 4 are not needed: the flash already holds this Pocket link image. Nothing written.')
            self.say('Step 5 of 5: checking that the console runs it...')
            return self._link_from_flash()
        if held == 'original' and not found['rehearsed']:
            self.say(f'Refused, nothing written: the console chip holds your original image. {SAID_UPDATE_NEEDS_SOUND} '
                     '(Home, Set up Jellyfin sound): the Pocket link image goes over the sound image.')
            return EXIT_REFUSED
        if held not in ('original', 'media'):
            self.say(f'Refused, nothing written: {SAID_UPDATE_UNKNOWN} (neither your original, the sound image nor the '
                     'Pocket link image), so nothing is loaded or written over it. Open Fix a problem.')
            return EXIT_REFUSED
        if confirm is None:
            # Fail closed, before the image is loaded: the write needs the owner's Approve with the image running.
            self.say(f'Refused, nothing written: {SAID_NO_ASKER} (given while the new image runs), and nothing here '
                     f'can ask for it. Nothing was loaded; the flash still holds {FLASH_WORDS_FOR[held]}.')
            return EXIT_REFUSED
        self.say('Step 2 of 5: trying the image from the console chip\'s RAM first (the flash is not touched), '
                 'then checking it automatically...')
        if self.link_try_needs_power_cycle():
            self._power_cycle_asked = True  # try_link asks for it (watched) before it loads
        self._tried = None
        self.try_link(approved_fingerprint, auto=True)
        record = self.link_try() or {}
        # Loaded by THIS call (try_link marks it as it records the try), never an older record of the image.
        loaded = (self._tried == found['fingerprint'] and record.get('image') == found['fingerprint']
                  and record.get('session') == self.session and isinstance(record.get('at'), (int, float)))
        if not loaded:
            self.say('The update stopped before the image was tried: nothing was loaded and nothing was written. '
                     f'The flash still holds {FLASH_WORDS_FOR[held]}.')
            return EXIT_REFUSED
        still = f'The flash still holds {FLASH_WORDS_FOR[held]}'

        def back_words(back):
            return (f'{still}, and the console chip was restarted from it: the console is as it was.' if back else
                    f'{still}. Switch the console off and on: it starts from that image, as before.')
        again = ('Next: press Update the Pocket link again (it asks you to switch the console off and on first, and '
                 'watches).')
        passed = [False]

        def checks():
            passed[0] = self.auto_confirm_link_try()
            return EXIT_OK
        self.guarded(checks)
        if not passed[0]:
            # Nothing was written. The chip goes back to its flash image, so the console is as it was.
            self.say(f'{SAID_AUTO_FAILED}: nothing was written. {back_words(self._restart_from_flash())}')
            self.say(f'{again} If it fails again, use Step by step, or open Fix a problem.')
            return EXIT_REFUSED
        self.say('Step 3 of 5: the new image is running now, from the console chip\'s RAM. Nothing is written yet: '
                 'look at a game on it, then Approve keeps it.')
        outcome = [None]
        self._ended_on = None

        def keep():
            outcome[0] = self._ask_keep(held, confirm)
            return EXIT_OK
        self.guarded(keep)  # the owner's Stop, a refusal or an error: said there, and nothing is written
        if outcome[0] != 'kept':
            back = self._restart_from_flash()
            if outcome[0] == 'not_kept':
                self.say(f'{SAID_NOT_KEPT}: you did not approve the new image, so nothing was written. {back_words(back)}')
                self.say(f'{again} If the picture or the sound was wrong on the new image, tell whoever built it; '
                         'Step by step asks about each thing in turn.')
            elif outcome[0] == 'ended':
                self.say(f'{SAID_TRY_ENDED} when you pressed Approve: the console\'s start-up menu reports '
                         f'{version_words(self._ended_on)}, not 18.41 (switching the console off and on ends the '
                         f'try). Nothing was written. {back_words(back)}')
                self.say(f'{again} Leave the console on between the try and Approve.')
            elif outcome[0] == 'switched_off':
                self.say(f'{SAID_SWITCHED_OFF} while the new image waited for your Approve (its USB port went away, '
                         f'or came back as after a switch-on, which ends the try): nothing was written. '
                         f'{back_words(back)}')
                self.say(f'{again} Leave the console switched on and plugged in until this app says Done; if you '
                         'did not switch it off, check the USB cable.')
            else:
                self.say(f'The update stopped before Approve: nothing was written. {back_words(back)}')
            return EXIT_REFUSED
        self.say('Step 4 of 5: writing the image to the console chip\'s flash (read before, write, full read-back)...')
        self._recovered = None
        code = self.write_link(approved_fingerprint, accept_auto=True)
        if code == EXIT_REFUSED and self._recovered is None:
            try:
                unfinished = bool(self.pending())
            except Exception:
                unfinished = True
            if not unfinished:
                # Refused after the Approve and before any write (write_link said why): the new image must not
                # stay running from RAM as if it had been installed. Back to the untouched flash, said plainly.
                back = self._restart_from_flash()
                self.say(f'{SAID_LATE_REFUSAL} (the line above says why): nothing was written. {back_words(back)}')
                self.say(again)
            return code  # (an unfinished job, stopped by the owner while it was being put right: write_link said so)
        if code not in (EXIT_OK, EXIT_NOT_SEEN) or self._recovered is not None:
            return code  # refused before a write, or recovered after one (finished or undone): write_link said which
        self.say('Step 5 of 5: checking that the console runs the image from its flash...')
        return self._link_from_flash()

    def _run_job(self, kind, before, target, source, backup, **note):
        """Write `source` (a file the loader takes), verify the whole flash reads as `target`,
        reconfigure and watch. Never raises: a failure after the write began is recovered. `note`:
        extra facts kept in the journal's plan (who confirmed the try); they decide nothing."""
        writes = False
        try:
            self.stop_point()
            self.folder.mkdir(parents=True, exist_ok=True)
            for data in (before, target, backup.image):
                write_durable(self.folder / f'image-{sha(data)}.bin', data)
            self.journal.append('plan', 'ready', kind=kind, before=sha(before), target=sha(target),
                                original=backup.sha, source=str(source), source_sha=sha(Path(source).read_bytes()),
                                session=self.session, **note)
            writes = True
            self.journal.append('write', 'flash', kind=kind)
            kib = len(target.rstrip(b'\xff')) // 1024
            self.say(f'Writing the console chip\'s flash ({kib} KiB of bitstream; the loader erases, programs and '
                     'verifies; the screen stays dark; do not unplug)...')
            self.loader.write(source)
            self.journal.append('write', 'written')
            image = self._read('after')
            if image != target:
                raise StepFailed('The flash read back does not match the expected image byte for byte.')
            self.journal.append('verify', 'matched', image=sha(image))
            seen = self._reconfigure_and_watch()
            self.journal.append('close', 'done', image=sha(target), seen=seen)
            self.journal.archive()
            if not seen:
                self.say('The flash checks out, but the console was not seen coming back on USB. Switch it off and on, '
                         'and check that a game plays.')
                return EXIT_NOT_SEEN
            self.say('The console came back (its USB serial interface returned).')
            return EXIT_OK
        except (Exception, KeyboardInterrupt) as error:
            if not writes or not self.journal.load().writes_started:
                try:
                    if self.journal.exists:
                        self.journal.append('close', 'nothing-written', reason=type(error).__name__)
                        self.journal.archive()
                except Exception:
                    pass
                try:
                    self.loader.reset()
                except Exception:
                    pass
                self.say('Stopped before any write; nothing changed.')
                return EXIT_REFUSED
            try:
                self.journal.append('job', 'failed', reason=type(error).__name__)
            except Exception:
                pass
            # Own wording only (never loader output, never a path): StepFailed texts are this module's.
            why = str(error).strip() if isinstance(error, StepFailed) and str(error).strip() else type(error).__name__
            self.say(f'The job did not finish ({why}) Putting the console chip right now.')
            return self.recover()

    def recover(self):
        """Finish or undo the journalled job: a flash that already reads as the job's target is
        finished (restart, watch); anything else gets the original image written back, read
        back and compared, up to `attempts` times; then MANUAL."""
        self.journal.load()
        kind = self.journal.value('kind')
        target_sha, original_sha = self.journal.value('target'), self.journal.value('original')
        images = {}
        for name, digest in (('target', target_sha), ('original', original_sha)):
            path = self.folder / f'image-{digest}.bin' if digest else None
            if not path or not path.exists() or sha(path.read_bytes()) != digest:
                return self.manual('The saved images for this console chip job are missing.')
            images[name] = path.read_bytes()
        # A rehearsal is credited only when a write was proven: this job's loader reported a completed
        # write and verify (journal write/'written') or the recovery below writes and verifies itself.
        job_written = any(r.get('phase') == 'write' and r.get('event') == 'written' for r in self.journal.records)
        source = None
        try:
            backup = self.backup_loader()
            if backup.sha == original_sha:
                source = backup.path
        except Refused:
            pass
        if source is None:
            source = self.folder / f'image-{original_sha}.bin'  # the journal's own copy of the original, as .bin
        for attempt in range(self.timing.attempts):
            try:
                self.journal.append('recover', 'attempt', number=attempt + 1)
                self._identify()
                current = self._read('recover')
                wrote_now = False
                if current == images['target']:
                    outcome, goal = 'finished', images['target']
                else:
                    wrote_now = True
                    self.stop_point()
                    self.say('Writing your original console chip image back...')
                    self.journal.append('write', 'flash', kind='recover-original')
                    self.loader.write(source)
                    self.journal.append('write', 'written')
                    current = self._read('recover-after')
                    if current != images['original']:
                        raise StepFailed('The flash read back does not match the original image.')
                    outcome, goal = ('undone' if kind in ('media', 'link') else 'finished'), images['original']
                self.journal.append('verify', 'matched', image=sha(current))
                seen = self._reconfigure_and_watch()
                self.journal.append('close', 'recovered', image=sha(goal), seen=seen, outcome=outcome)
                self.journal.archive()
                self._recovered = outcome  # for update_link: a recovered write is never taken as a clean one
                self._note_check('original' if goal == images['original'] else
                                 {'media': 'media', 'link': 'link'}.get(kind, 'unknown'))
                if goal == images['original'] and (wrote_now or job_written):
                    self._record_rehearsal(sha(goal))  # the original was written and verified: a valid rehearsal
                elif goal == images['original']:
                    self.say('The loader stopped before writing, so this does not count as the rehearsal: run Restore '
                             'FPGA again before FPGA sound.')
                what = {'finished': 'finished', 'undone': 'undone (your original image is back)'}[outcome]
                if not seen:
                    self.say(f'The unfinished console chip job was {what} and the flash checks out, but the console was '
                             'not seen coming back. Switch it off and on, and check that a game plays.')
                    return EXIT_NOT_SEEN
                self.say(f'The unfinished console chip job was {what}; the flash checks out and the console came back.')
                return EXIT_RECOVERED
            except (Exception, KeyboardInterrupt) as error:
                if getattr(error, 'owner_stop', False):
                    self.journal.append('recover', 'stopped-by-owner')
                    self.say('Stopped before anything more was written. The unfinished job still waits; finish it when '
                             'you are ready.')
                    return EXIT_REFUSED
                try:
                    self.journal.append('recover', 'attempt-failed', reason=type(error).__name__)
                except Exception:
                    pass
                self.sleep(self.timing.poll)
        return self.manual()

    def manual(self, why=None):
        try:
            self.journal.append('recover', 'manual-required')
        except Exception:
            pass
        if why:
            self.say(why)
        self.say(MANUAL)
        return EXIT_MANUAL

    def resume(self):
        self.journal.load()
        if not self.journal.exists:
            self.say('No unfinished console chip job.')
            return EXIT_OK
        if self.journal.closed:
            self.journal.archive()
            self.say('The last console chip job had already finished.')
            return EXIT_OK
        if not self.journal.writes_started:
            self.journal.append('close', 'nothing-written')
            self.journal.archive()
            self.say('The last console chip job stopped before writing anything. Nothing to do.')
            return EXIT_OK
        self.say('Finishing the unfinished console chip job.')
        return self.recover()

    def abandon(self):
        self.journal.load()
        if not self.journal.exists or self.journal.closed:
            self.say('No unfinished console chip job.')
            return EXIT_OK
        self.journal.append('close', 'abandoned-by-user')
        self.journal.archive()
        self.say('The unfinished console chip job is closed. Nothing was written.')
        return EXIT_OK

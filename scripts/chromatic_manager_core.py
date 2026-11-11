# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Chromatic Manager core: the one place jobs run, shared by the window, the local API
and the MCP server. No Qt here, so the MCP server can run it without a window.

Every action calls the unchanged Manager methods the text menu calls, inside the same
operating-system lock. The core never decides on its own to write: a write starts only
from a front end where a person approved it (the window's Approve dialog, after a button
or a request the window put to the owner). The local API can only *request* a write
(`request_action`); there is no flag, token or setting that starts one directly.

Nothing here returns device identifiers, MACs, flash contents, Wi-Fi details or
Jellyfin settings: the manager's own messages never name them, and everything that
leaves through the API is passed through `redact` as well.
"""
from __future__ import annotations

from collections import deque
import dataclasses
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import threading
import time

import chromatic_manager_owner as owner_actions

ROOT = Path(__file__).resolve().parents[1]
from user_paths import PRIVATE  # noqa: E402  (your per-user Jellymatic folder; see user_paths.py)
LOG_FOLDER = 'chromatic-manager-gui/logs'  # in your per-user Jellymatic folder (user_paths.PRIVATE)
OWNER = 'the owner (in the window)'
APPROVED_IN_WINDOW = 'the owner, approved in the window'
# The one-step Pocket link update, started by the owner's own press of Update now: nothing is written
# on that press. Its one Approve comes inside the job, with the new image running (chromatic_fpga
# FpgaJobs.update_link), so the job's record says exactly that.
STARTED_IN_WINDOW = 'the owner, who pressed Update now in the window (the write inside asks for Approve)'
AI_GRANT = "the owner's AI permission (allowed in the window)"
GRANT_LOG = "allowed by the owner's AI permission"
REASON_MAX = 120  # a program's reason, shown labelled "The AI says:", never longer
SETTINGS_FILE = 'chromatic-manager-gui/settings.json'  # in your per-user Jellymatic folder
USAGE_SNAPSHOT = 'chromatic-tools/usage-snapshot.json'
LATEST_BACKUP = 'chromatic-tools/latest-backup-path.txt'


class AppSettings:
    """The window's own settings (so far only the owner's remembered AI choice), in a small
    JSON file in the Git-ignored private folder. Read when needed, written atomically.
    A file that cannot be read counts as empty, which means "ask the owner"."""

    def __init__(self, path):
        self.path = Path(path)

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, key, default=None):
        return self._load().get(key, default)

    def set(self, key, value):
        """None removes the key."""
        data = self._load()
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, indent=1), encoding='utf-8')
        os.replace(temporary, self.path)


def hardware_rule(root=PRIVATE):
    """The project's hardware gate (scripts/hardware_preflight.py, same checks in the same
    order as its main()) for a write the AI starts under the owner's permission: a fresh usage
    reading with the recovery reserve left (90% for reporter "claude", 75% otherwise), the
    stock checkpoint, and both saved stock backup copies matching. None when it passes, or one
    plain reason. Never prints or returns anything read from those files."""
    root = Path(root)
    try:
        import hardware_preflight as preflight
    except Exception:
        return 'The hardware usage check (scripts/hardware_preflight.py) could not be loaded.'
    try:
        preflight.check_usage(json.loads((root / USAGE_SNAPSHOT).read_text(encoding='utf-8')))
        folder = Path((root / LATEST_BACKUP).read_text(encoding='utf-8').strip())
        preflight.check_checkpoint(json.loads((folder / 'recovery-validation.json').read_text(encoding='utf-8')))
        receipt = json.loads((folder / 'completion-receipt.json').read_text(encoding='utf-8'))
        preflight.check_backup_copies(folder, Path(receipt['secondary_bundle']))
    except FileNotFoundError:
        return ('The hardware usage check found no fresh usage reading or backup record. Refresh '
                f'{USAGE_SNAPSHOT} (less than 5 minutes old), then ask again.')
    except ValueError as error:
        return f'The hardware usage check stopped it: {error}'
    except Exception as error:
        return f'The hardware usage check could not run ({type(error).__name__}).'
    return None


# ---- actions -------------------------------------------------------------------------
@dataclass(frozen=True)
class Action:
    key: str
    label: str         # button text
    title: str         # what the confirmation and the working screen call it
    tab: str           # 'console' or 'recovery'
    confirm: str | None  # word the person types before it starts, None for read-only
    pending_ok: bool   # allowed while an unfinished job waits
    kind: str = ''     # button colour: '', 'go' or 'stop'
    help: str = ''


ACTIONS = (  # key, button, title, tab, confirm, pending_ok, kind, help (plain words: tooltips and Approve)
    Action('status', 'Check again', 'Check the console', 'console', None, True, '',
           'Looks at what is on your Chromatic. Takes about 20 seconds (about 7 minutes the first time after an '
           'official update). Changes nothing, but a game that is playing stops.'),
    Action('backup', 'Back up', 'Back up the console', 'recovery', None, False, '',
           'Saves a full copy of your Chromatic to this PC, with a second copy. The first time it copies both chips '
           '(about 15 minutes; Install and the updates do this by themselves), later the Wi-Fi chip (about 7 '
           'minutes). Changes nothing on the console.'),
    Action('install', 'Repair menu', 'Install the start-up menu', 'recovery', 'yes', False, '',
           'Puts the start-up menu on your Chromatic, updates it, or turns it back on after an official update. '
           'Takes about 2 minutes. New firmware is backed up first. If Wi-Fi and Jellyfin are not set up yet, '
           'they are set up too.'),
    Action('update', 'Official update', 'Official ModRetro update (guided)', 'console', 'yes', False, '',
           'Helps you run ModRetro\'s own updater: the menu is turned off, you run the updater, then this app '
           'checks the result, backs it up and turns the menu back on. This app never starts the updater.'),
    Action('wifi', 'Wi-Fi', 'Set up Wi-Fi and Jellyfin', 'console', 'yes', False, '',
           'Adds a Wi-Fi network to your Chromatic or gives one a new password. The networks already saved on the '
           'console stay, passwords and all: you see the list (names only) and can change the order or remove some '
           'before anything is written. It keeps at most 4. Also pairs it with this PC and signs it in to Jellyfin '
           'with its own sign-in. No password is shown.'),
    Action('jellyfin', 'New Jellyfin sign-in', 'Give the console a new Jellyfin sign-in', 'console', 'yes', False, '',
           'Signs your Chromatic in to Jellyfin again and signs the old sign-in out. Your password stays on this '
           'PC. Wi-Fi stays as it is.'),
    Action('remove', 'Remove menu', 'Remove the start-up menu', 'recovery', 'yes', False, 'stop',
           'Takes the start-up menu off completely. The console goes back to how ModRetro made it. This also '
           'erases the menu\'s setup: every Wi-Fi network (also ones added on the console), the PC pairing and the '
           'console\'s Jellyfin sign-in, which is signed out on the server.'),
    Action('disable', 'Switch menu off', 'Switch the start-up menu off', 'recovery', 'yes', False, '',
           'Turns the start-up menu off. Games still work. Safe even if the power goes off. Wi-Fi and Jellyfin '
           'stay saved for when you switch it back on.'),
    Action('restore', 'Restore original', 'Restore the original firmware', 'recovery', 'yes', False, 'stop',
           'Puts ModRetro\'s original firmware back from your newest backup. ModRetro\'s own settings stay, but the '
           'start-up menu\'s setup is erased: every Wi-Fi network (also ones added on the console), the PC pairing '
           'and the console\'s Jellyfin sign-in, which is signed out on the server. If the version would change, '
           'you are asked again and the console is backed up first.'),
    Action('jellyfin_forget', 'Forget Jellyfin', 'Forget Jellyfin on the console', 'recovery', 'yes', False, 'stop',
           'Signs your Chromatic out of Jellyfin and removes its sign-in. Wi-Fi stays.'),
    Action('fpga_check', 'Check FPGA', 'Check the console chip (FPGA)', 'recovery', 'yes', False, '',
           'Reads the console chip\'s flash over the Chromatic\'s JTAG bridge and says whether it holds your original '
           'image, the sound image or the Pocket link image. Takes about 1 minute; the screen goes dark, then the '
           'console restarts. Writes nothing.'),
    Action('fpga_restore', 'Restore FPGA', 'Put the original console chip image back (FPGA)', 'recovery', 'yes', False,
           'go',
           'Writes your original console chip (FPGA) image from your backup, reads all of it back and compares it, '
           'then restarts the console. Done over the original image first, as the rehearsal; it is also the undo for '
           'FPGA sound. Takes about 3 minutes; the screen stays dark. Games, saves and the Wi-Fi chip are untouched.'),
    Action('fpga_media', 'FPGA sound', 'Install the sound (stock+media) console chip image (FPGA)', 'recovery', 'yes',
           False, 'go',
           'Writes the stock+media console chip image that gives Jellyfin sound: over your original image only after '
           'Restore FPGA has succeeded in this session, over the Pocket link image at once (it goes back to sound '
           'only); reads it all back and compares, then restarts. Takes about 2 minutes; the screen stays dark. Games '
           'still run on it. Restore FPGA undoes it.'),
    Action('fpga_trial', 'Try FPGA image (temporary)', 'Try the sound console chip image temporarily (FPGA RAM only)',
           'recovery', 'yes', False, '',
           'Loads the stock+media console chip image into the chip\'s RAM over the JTAG bridge, not into its flash, '
           'and restarts the console on it so you can check the start-up menu and Jellyfin picture and sound before '
           'anything is written. Needs Check FPGA (within 24 hours) or Restore FPGA (this session) to have shown the '
           'original image in the flash first. Takes about 1 minute; the screen goes dark. Switching the console off '
           'and on returns to the flash image. Games, saves and the Wi-Fi chip are untouched.'),
    # The sound + Pocket link image (18.41): owner-present, through "Update the Pocket link" (its Step by step)
    # only (no Fix-a-problem button of their own, see GUIDE_ONLY_KEYS); the write needs a try of the
    # same image that the owner confirmed with every check answered All good.
    Action('fpga_link_trial', 'Try Pocket link (temporary)',
           'Try the sound + Pocket link console chip image temporarily (FPGA RAM only)', 'recovery', 'yes', False, '',
           'Loads the sound + Pocket link console chip image (18.41) into the chip\'s RAM over the JTAG bridge, not '
           'into its flash, and restarts the console on it so you can check games, a link-cable game, Jellyfin sound '
           'and the Pocket link row before anything is written. Needs your verified backup and Check FPGA (within 24 '
           'hours) first. Takes about 1 minute; the screen goes dark. Switching the console off and on returns to the '
           'flash image. Games, saves and the Wi-Fi chip are untouched.'),
    Action('fpga_link', 'FPGA Pocket link', 'Install the sound + Pocket link console chip image (FPGA)', 'recovery',
           'yes', False, 'go',
           'Writes the sound + Pocket link console chip image (18.41) over the sound image, only after you tried it '
           'from the chip\'s RAM within the last 24 hours and answered All good to every check; reads all 4 MiB '
           'back and compares, then '
           'restarts. Takes about 2 minutes; the screen stays dark. FPGA sound writes the sound image back, Restore '
           'FPGA your original.'),
    # The same image in one step, with no question rounds: the app's own checks on the
    # try stand in for the owner's four answers, for this job only, and the one Approve is asked with the
    # image running, before the write (chromatic_fpga FpgaJobs.update_link).
    Action('fpga_link_update', 'Update Pocket link', 'Update the Pocket link (console chip, FPGA)', 'recovery',
           'yes', False, 'go',
           'Checks the console chip, then puts the sound + Pocket link image (18.41) on it in one job: the image '
           'runs from the chip\'s RAM first and this app checks it by itself (it loaded, USB came back, not in '
           'download mode, the start-up menu reports 18.41 and reached its menu). Then, with the new image still '
           'running, it asks you once: start a game, look, and press Approve to keep it. Only then is it written; '
           'all 4 MiB are read back and compared, and the console is heard reporting 18.41 from its flash. A '
           'failed check or Not right writes nothing. About 6 minutes; the screen goes dark several times. FPGA '
           'sound puts the previous image back, Restore FPGA your original.'),
    Action('resume', 'Finish job', 'Finish the unfinished job', 'recovery', 'yes', True, 'go',
           'Finishes or undoes a job that was cut off, for example by a power cut.'),
    Action('abandon', 'Close job', 'Close the unfinished job', 'recovery', 'abandon', True, 'stop',
           'Closes a cut-off job without changing anything. Only if Check again says the console is fine.'),
)
ROW_TEXT = {  # one short line next to each Fix-a-problem button
    'install': 'Install the start-up menu again.',
    'backup': 'Save a full copy of your console here.',
    'disable': 'Turn the menu off. Games still work.',
    'remove': 'Take the menu off completely. Erases Wi-Fi and Jellyfin too.',
    'restore': 'Put ModRetro\'s firmware back. Erases Wi-Fi and Jellyfin too.',
    'jellyfin_forget': 'Sign the console out of Jellyfin.',
    'fpga_check': 'Read the console chip: original or sound image?',
    'fpga_restore': 'Original console chip image back (rehearsal and undo).',
    'fpga_media': 'Sound image on the console chip (after Restore FPGA).',
    'fpga_trial': 'Try the sound image from the chip\'s RAM; power off and on undoes it.',
    'fpga_link_trial': 'Try the Pocket link image from the chip\'s RAM; power off and on undoes it.',
    'fpga_link': 'Pocket link image on the console chip (after a confirmed try).',
    'fpga_link_update': 'Pocket link image on the console chip in one step: checked by this app, kept by you.',
    'resume': 'Finish a job that was cut off.',
    'abandon': 'Close a cut-off job, change nothing.',
}
BY_KEY = {a.key: a for a in ACTIONS}
ACTION_HELP = {a.key: a.help for a in ACTIONS}
WRITE_KEYS = tuple(a.key for a in ACTIONS if a.confirm)  # need a person's approval at the window (or typed in the text menu)
# A program may only *ask* for any of these. Status writes nothing, but it holds the console
# in download mode (a game in progress stops; up to minutes) and ends on the start-up menu, so
# it is confirmed by the person at the window like a write.
GUIDE_ONLY_KEYS = ('fpga_link_trial', 'fpga_link')  # no button of their own: the Step by step guide runs them
# What a program (API, MCP, Remote Pilot) may ask for: the guide-only jobs are not among them, so
# no request can run the Pocket link try or write outside the owner's guide.
# The one-step Pocket link update: a program may ask for it only through its own entry
# (ManagerService.request_link_update), and it starts only on the owner's Approve click in the
# window: the owner's AI permission never starts it.
OWNER_APPROVE_KEYS = ('fpga_link_update',)
PROGRAM_WRITE_KEYS = tuple(key for key in WRITE_KEYS if key not in GUIDE_ONLY_KEYS + OWNER_APPROVE_KEYS)
REQUESTABLE_KEYS = PROGRAM_WRITE_KEYS + ('status',)
OWNER_APPROVE_WHY = ('is asked for with chromatic_update_pocket_link (its own entry), starts only when the owner '
                     'presses Approve in the Chromatic Manager window, and writes only after the owner\'s second '
                     'Approve there, given with the new image running.')
GUIDE_ONLY_WHY = ('runs only inside Update the Pocket link (its Step by step) in the Chromatic Manager window, which '
                  'the owner starts there. It cannot be requested.')
FPGA_KEYS = ('fpga_check', 'fpga_restore', 'fpga_media', 'fpga_trial', 'fpga_link_trial',
             'fpga_link', 'fpga_link_update')  # console chip jobs (chromatic_fpga.py), JTAG not ESP32


SETUP_CHOICES = ('auto', 'change', 'never')  # install: set up Wi-Fi/Jellyfin when missing, always, or not


def install_setup(cm, manager, ask, secret, phase=None):
    """The rest of an install: Wi-Fi, PC pairing and the console's own Jellyfin sign-in
    (cm.wifi_setup: networks from the owner's private file or typed in the window, password
    hidden; Jellyfin by Quick Connect, which never signs anything else out), written only
    after the launcher was verified. The person approved both together, so the question
    about this PC's own private LAN address is answered from the address found here.
    `phase(True/False)` marks the part whose messages are kept out of logs and the API."""
    manager.say('Next, as part of this install: Wi-Fi, PC pairing and the console\'s Jellyfin sign-in '
                '(nothing to type on the console).')
    try:
        address = cm.lan_ipv4()
    except OSError:
        address = None

    def answer(prompt):
        if address and str(prompt).startswith(f'Use this PC at {address} for Jellyfin?'):
            return 'yes'
        return ask(prompt)
    if phase:
        phase(True)
    try:
        result = cm.wifi_setup(manager, answer, secret)
    finally:
        if phase:
            phase(False)
    if result == cm.EXIT_REFUSED:
        manager.say('The start-up menu is installed, but Wi-Fi and Jellyfin were not set up (nothing else was '
                    'written). Press Wi-Fi on the Home tab to try again.')
        return cm.EXIT_OK
    if result == cm.EXIT_OK:
        manager.say('Wi-Fi and Jellyfin setup written as part of this install and checked on the chip.')
    return result


def fpga_jobs(manager, approved_media=None):
    """The console chip (FPGA) jobs for this manager: same messages, same Stop hook. Under the
    owner's AI permission `approved_media` is the sound image fingerprint the owner saw (or
    '' for no approval at all), which the job compares with the built image; None means an
    owner-approved job, which takes the built image the Approve dialog named. The Pocket link
    jobs compare the same fingerprint, so under the grant they are refused (the owner approved
    a sound image, never the Pocket link image)."""
    import chromatic_fpga
    jobs = chromatic_fpga.FpgaJobs(say=manager.say, stop_point=manager.stop_point,
                                   owner=getattr(manager, 'owner', None))
    jobs.approved_media = approved_media
    return jobs


def run_action(cm, manager, key, ask, secret, lock, setup='auto', phase=None, fpga=None):
    """One action, exactly as `chromatic_manager.main` and `menu` run it. Never raises.
    Install then sets up Wi-Fi and Jellyfin when the console has none yet (`setup='auto'`),
    or always (`'change'`); a console's existing setup is otherwise kept untouched.
    `fpga`: the console chip jobs (fpga_jobs(manager) by default); an unfinished one blocks
    the other writes just as an unfinished ESP32 job does, and Finish/Close act on it."""
    from chromatic_lock import Busy
    action = BY_KEY[key]
    try:
        with lock:
            if fpga is None:
                fpga = fpga_jobs(manager)
            fpga_pending = fpga.pending()
            if not action.pending_ok and (manager.pending() or fpga_pending):
                manager.say('Refused: an earlier job did not finish. Finish it, or check status and abandon it.')
                return cm.EXIT_REFUSED
            if key in FPGA_KEYS and hasattr(cm, 'first_full_backup'):
                # No full backup on this PC yet: the job takes it first (it writes nothing to the console).
                code = cm.first_full_backup(manager, fpga)
                if code is not None:
                    return code
            if key == 'fpga_check':
                return fpga.check()
            if key == 'fpga_restore':
                return fpga.restore_original(confirm=ask)
            if key == 'fpga_media':
                return fpga.write_media(approved_fingerprint=getattr(fpga, 'approved_media', None))
            if key == 'fpga_trial':
                return fpga.try_media(approved_fingerprint=getattr(fpga, 'approved_media', None))
            if key == 'fpga_link_trial':
                return fpga.try_link(approved_fingerprint=getattr(fpga, 'approved_media', None))
            if key == 'fpga_link':
                return fpga.write_link(approved_fingerprint=getattr(fpga, 'approved_media', None))
            if key == 'fpga_link_update':
                return fpga.update_link(approved_fingerprint=getattr(fpga, 'approved_media', None), confirm=ask)
            if key == 'resume' and fpga_pending:
                return fpga.resume()
            if key == 'abandon' and fpga_pending:
                return fpga.abandon()
            if key == 'status':
                return manager.guarded(manager.status)
            if key == 'backup':
                return manager.guarded(manager.backup)
            if key == 'install':
                code = manager.install()
                if code != cm.EXIT_OK or setup == 'never':
                    return code
                if setup != 'change' and manager.console_has_setup() is not False:
                    return code  # it has a setup already (or that is unknown): kept as it is
                return install_setup(cm, manager, ask, secret, phase)
            if key == 'update':
                return manager.guided_update(ask)
            if key == 'wifi':
                return cm.wifi_setup(manager, ask, secret)
            if key == 'jellyfin':
                return cm.jellyfin_relink(manager)
            if key == 'jellyfin_forget':
                return cm.jellyfin_forget(manager)
            if key == 'remove':
                return manager.remove()
            if key == 'disable':
                return manager.disable()
            if key == 'restore':
                return manager.restore_stock(confirm=ask)
            if key == 'resume':
                return manager.resume()
            if key == 'abandon':
                return manager.abandon()
            raise ValueError(key)
    except Busy as error:
        manager.say(str(error))
        return cm.EXIT_REFUSED
    except cm.Refused as error:
        manager.say(f'Refused: {error}')
        return cm.EXIT_REFUSED
    except KeyboardInterrupt:
        manager.close_device()
        manager.say('Cancelled.')
        return cm.EXIT_REFUSED
    except Exception as error:
        manager.close_device()
        manager.say(f'Unexpected error ({type(error).__name__}); nothing further was attempted.')
        return cm.EXIT_ERROR


# ---- reading the manager's messages ----------------------------------------------------
STEP_LINE = re.compile(r'Step \d+ of \d+:')  # a job's own numbered steps, whatever their count


class Progress:
    """Phase and percentage from the manager's own progress lines."""

    def __init__(self):
        self.phase, self.percent = 'Starting', None

    def feed(self, line):
        text = line.strip()
        step = re.match(r'(Erasing|Writing) step (\d+) of (\d+)', text)
        read = re.match(r'read (\d+)%', text)
        if text.startswith('Identifying the console'):
            self.phase, self.percent = 'Identifying the console', None
        elif text.startswith('Reading the console'):
            self.phase, self.percent = 'Reading the console', 0
        elif read:
            self.percent = int(read.group(1))
        elif step:
            done, total = int(step.group(2)), int(step.group(3))
            self.phase, self.percent = f'{step.group(1)} step {done} of {total}', (done - 1) * 100 // total
        elif text.startswith('Waiting for the console'):
            self.phase, self.percent = 'Waiting for the console', None
        elif text.startswith('Please do not press'):
            self.phase = 'Writing; then watching the console start'
        elif text.startswith('Backing up') or text.startswith('Backed up'):
            self.phase = 'Saving a backup and its second copy'
        elif STEP_LINE.match(text):  # "Step 2 of 4: ...", "Step 3 of 5: ..."
            self.phase, self.percent = text.split(':')[0], None
        elif text.startswith('Finishing the unfinished job'):
            self.phase, self.percent = 'Finishing the unfinished job', None
        return self


def summarize(lines):
    """What a status run said, for the summary card. Never more than the manager printed."""
    found = {}
    for line in lines:
        console = re.match(r'Console: ([\w-]+)\. Boots: (.*)\.$', line.strip())
        stock = re.match(r'Stock firmware: version (\S+) \(ESP-IDF (\S+)\)\.', line.strip())
        setup = re.match(r'Console setup: (.*)\.$', line.strip())
        if setup:
            found['setup'] = setup.group(1)
        launcher = re.match(r'Launcher in flash: version (\S+); this tool installs (\S+?)(?: \(|\.$)', line.strip())
        current = re.match(r'Launcher in flash: version (\S+), the one this tool installs', line.strip())
        if console:
            found['state'], found['boots'] = console.group(1), console.group(2)
        elif stock:
            found['stock'], found['idf'] = stock.group(1), stock.group(2)
        elif launcher:
            found['launcher'], found['release'] = launcher.group(1), launcher.group(2)
        elif current:
            found['launcher'] = found['release'] = current.group(1)
        chip = re.match(r'Console chip \(FPGA\): (?:version (\d+)\.(\d+)( with the debug bit)?, as the start-up menu '
                        r'reports it|(not reported)|(no version reply))', line.strip())
        if chip:
            found['fpga'] = (f'{chip.group(1)}.{chip.group(2)}' + (' debug' if chip.group(3) else '') if chip.group(1)
                             else 'not reported' if chip.group(4) else 'no reply')
    return found


# The console chip (FPGA) image, as the start-up menu reports it at the last status check (no JTAG
# read). The sound image keeps stock's major with the debug bit and its own minor (launcher_logic.h
# PJ_FPGA_MEDIA_MAJOR / PJ_FPGA_MEDIA_MINOR); ModRetro's stock image this tool knows is 18.14.
SOUND_FPGA = (18, 40)
STOCK_FPGA = (18, 14)
LINK_FPGA = (18, 41)  # the sound + Pocket link image (launcher_logic.h PJ_FPGA_LINK_MINOR), with the debug bit


def built_sound_version(text):
    """(18, 40) from 'sound build 18.40 (image 1a2b3c4d)', or None."""
    found = re.match(r'sound build (\d+)\.(\d+)\b', text or '')
    return (int(found.group(1)), int(found.group(2))) if found else None


def sound_state(summary, built=None):
    """'installed' (this app's sound image), 'outdated' (an older sound image of this app), 'none'
    (ModRetro's stock 18.14 image), 'unknown' (anything else), or None when the last check did not
    learn it (an older start-up menu, or no check yet). `built`: the built sound image's version."""
    value = (summary or {}).get('fpga')
    if not value or value == 'not reported':
        return None
    if value in ('original', 'sound', 'link'):  # what a console chip job itself found or wrote, byte for byte
        return 'none' if value == 'original' else 'installed'
    found = re.fullmatch(r'(\d+)\.(\d+)( debug)?', value)
    if not found:
        return 'unknown'
    major, minor, debug = int(found.group(1)), int(found.group(2)), bool(found.group(3))
    if debug and (major, minor) == LINK_FPGA:  # the Pocket link image carries the sound image too
        return 'installed'
    target = built or SOUND_FPGA
    if debug and major == target[0] and minor >= SOUND_FPGA[1]:
        return 'installed' if minor == target[1] else 'outdated' if minor < target[1] else 'unknown'
    return 'none' if not debug and (major, minor) == STOCK_FPGA else 'unknown'


def link_state(summary):
    """'ready' when the console chip runs the sound + Pocket link image (the start-up menu reported
    18.41 with the debug bit, or a console chip job found or wrote it byte for byte), else None."""
    value = (summary or {}).get('fpga')
    if value == 'link':
        return 'ready'
    found = re.fullmatch(r'(\d+)\.(\d+) debug', value or '')
    return 'ready' if found and (int(found.group(1)), int(found.group(2))) == LINK_FPGA else None


# chromatic_fpga FpgaJobs.recover's own result line: "The unfinished console chip job was undone (your
# original image is back); ..." or "... was finished; ..." (the flash already read as the job's own
# image: it is on and checked). Both exit 2 (EXIT_RECOVERED), or 5 when not seen restarting.
SAID_RECOVERED_UNDONE = 'console chip job was undone (your original image is back)'
SAID_RECOVERED_FINISHED = 'console chip job was finished'
RECOVERED_HOLDS = {'fpga_media': 'sound', 'fpga_link': 'link', 'fpga_link_update': 'link',
                   'fpga_restore': 'original'}  # 'finished': the job's image
# chromatic_fpga's own words in the one-step Pocket link update (FpgaJobs.update_link).
SAID_AUTO_FAILED = 'Automatic checks failed'
SAID_AUTO_PASSED = 'Automatic checks passed'
SAID_LINK_FROM_FLASH = 'Confirmed: the console runs the Pocket link image (18.41) from its flash'
SAID_LINK_NOT_FROM_FLASH = 'Not confirmed yet: the console was not heard reporting 18.41 from its flash'
SAID_LINK_ALREADY = 'the flash already holds this Pocket link image'
SAID_FLASH_WRITING = "Writing the console chip's flash"  # _run_job, as the write begins
SAID_UPDATE_NEEDS_SOUND = 'Set up Jellyfin sound first'
SAID_UPDATE_UNKNOWN = 'The console chip holds an image this tool does not know'
SAID_NOT_KEPT = 'Not kept'                                   # the owner did not approve the image they saw running
SAID_TRY_ENDED = 'The new image was no longer running'       # Approve came after the console was switched off and on
SAID_KEPT = 'You approved the new image while it was running'
SAID_NO_ASKER = 'This update needs the app window for its Approve'
SAID_SWITCHED_OFF = 'Your Chromatic was switched off'        # its USB port left as for a switch-off while the Approve waited
SAID_LATE_REFUSAL = 'The write did not start after your Approve'  # refused or stopped after Approve, before any write
# update_link's own words after such a stop (chromatic_fpga back_words, FLASH_WORDS_FOR).
SAID_STILL_HOLDS = 'The flash still holds '
SAID_AS_IT_WAS = 'the console is as it was'
HELD_WORDS = ('the sound image (18.40)', 'your original image')


def late_refusal_note(text):
    """Where the console is after a write that did not start after the Approve, from update_link's
    own words: the image its flash holds, and whether the chip was seen restarting from it."""
    held = next((words for words in HELD_WORDS if SAID_STILL_HOLDS + words in text), 'the image it had')
    if SAID_AS_IT_WAS in text:
        return f'Nothing was written. Your Chromatic is back on {held}: it was restarted from its flash.'
    return (f'Nothing was written, and the flash still holds {held}. The console was not seen restarting: switch it '
            'off and on, and it starts from that image.')


def recovered_outcome(lines):
    """'undone', 'finished', or None: how a cut-off console chip job was put right, from recover's
    own words. 'undone': the original image was written back and checked. 'finished': the flash
    already read as the job's target, so that image is on and checked (nothing written back)."""
    text = '\n'.join(lines)
    if SAID_RECOVERED_UNDONE in text:
        return 'undone'
    if SAID_RECOVERED_FINISHED in text:
        return 'finished'
    return None


def fpga_summary(key, code, lines, before):
    """After a console chip (FPGA) job: the ESP32's facts stay (these jobs never touch its flash);
    what the chip holds comes from the job's own words, or is forgotten until the next check."""
    if not before:
        return before
    after, text, holds = dict(before), '\n'.join(lines), None
    if key in ('fpga_trial', 'fpga_link_trial') or code == 1:  # RAM only, or refused: the flash is as it was
        return after
    if code in (0, 5):
        if key == 'fpga_check':
            holds = ('original' if SAID_ORIGINAL in text else 'sound' if SAID_SOUND in text
                     else 'link' if SAID_LINK in text else 'unknown' if SAID_NEITHER in text else None)
        elif key == 'fpga_restore' and (SAID_REHEARSAL in text or SAID_ORIGINAL_BACK in text):
            holds = 'original'
        elif key == 'fpga_media' and (SAID_SOUND_WRITTEN in text or SAID_SOUND_THERE in text):
            holds = 'sound'
        elif key == 'fpga_link' and (SAID_LINK_WRITTEN in text or SAID_LINK_THERE in text):
            holds = 'link'
        elif key == 'fpga_link_update' and (SAID_LINK_FROM_FLASH in text or SAID_LINK_NOT_FROM_FLASH in text):
            holds = 'link'  # said only after the flash was read to hold it byte for byte (its check or its write)
    if code in (2, 5) and holds is None:  # cut off, then put right: undone (the original) or finished (its own image)
        outcome = recovered_outcome(lines)
        holds = 'original' if outcome == 'undone' else RECOVERED_HOLDS.get(key) if outcome == 'finished' else None
    if holds:
        after['fpga'] = holds
    else:
        after.pop('fpga', None)
    return after


SETUP_KEYS = ('wifi', 'jellyfin', 'jellyfin_forget')  # change only the console setup sectors
CHANGED = {'state': 'changed', 'boots': 'unknown until the next status check',
           'note': 'A job changed (or may have changed) the console since the last status check.'}


def summary_after_job(key, code, lines, before):
    """The last-status summary once a job other than status has finished, so neither the
    window's side panel nor the API keeps showing the console as it was before the job:
    a verified install states what it put there itself; a job that wrote nothing leaves the
    summary as it was; anything else is marked changed until the next status check."""
    if key not in WRITE_KEYS or key == 'abandon':
        return before
    if key in FPGA_KEYS:
        return fpga_summary(key, code, lines, before)
    if code == 1 and key != 'update':  # refused or stopped before any write (the guided update may have written)
        return before
    for line in lines:
        done = (re.match(r'Start-up menu (\S+) installed\. The whole-flash check matches', line.strip())
                or re.match(r'Launcher (\S+) is already installed\. Nothing written\.', line.strip()))
        if key == 'install' and code == 0 and done:
            version, before = done.group(1), before or {}
            return dict(before, state='installed', boots='start-up menu on power-on, stock otherwise',
                        launcher=version, release=version, checked=time.strftime('%Y-%m-%d %H:%M:%S'),
                        by='this install (whole-flash check on the chip)',
                        setup=('written by this install (Wi-Fi, PC pairing and Jellyfin when linked)'
                               if any('setup written as part of this install' in text for text in lines)
                               else before.get('setup', 'kept as it was; check status to see it')))
    if code == 0 and key != 'update' and any('Nothing written' in line for line in lines):
        return before
    if key in SETUP_KEYS and code == 0 and before:
        return dict(before, setup='changed by the last job; check status to see it')
    return dict(CHANGED, checked=time.strftime('%Y-%m-%d %H:%M:%S'))


RESULTS = {  # exit code: (headline, colour role)
    0: ('Done', 'ok'),
    1: ('Nothing was changed', 'warn'),
    2: ('Fixed: the unfinished job was put right', 'ok'),
    3: ('Stopped: this needs you', 'error'),
    4: ('Something went wrong', 'error'),
    5: ('Written, but not seen starting', 'warn'),
}
STATUS_DONE = 'Checked. Nothing was changed.'

# Ready-made answers for the manager's known questions; anything can still be typed.
QUICK_ANSWERS = (
    ('Type done once', ('done', 'failed', 'cancel')),
    ("The console's firmware did not change", ('yes', 'no')),
    ('Use this PC at', ('yes', 'no')),
)


def quick_answers(prompt):
    return next((answers for start, answers in QUICK_ANSWERS if prompt.startswith(start)), ())


# ---- what may leave this program -------------------------------------------------------
_MAC = re.compile(r'\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b')
_HEX = re.compile(r'\b[0-9A-Fa-f]{16,}\b')  # hashes, serials, raw bytes
WIFI_WITHHELD = '(Wi-Fi setup message, withheld)'


def redact(text, wifi=False):
    """Logs and the API carry steps, timings and results, never identifiers or Wi-Fi data."""
    if wifi:
        return WIFI_WITHHELD
    return _HEX.sub('[withheld]', _MAC.sub('[withheld]', str(text)))


# ---- what the window shows while private details are masked (the default) -----------------
# Display only: the masked text is what the window and its log panel show; log files, the
# API and every job keep the real text (they are already passed through `redact`). Each
# private character becomes '*'; dots, colons and slashes stay, so an address still reads
# as an address ("***.***.*.**") and a path as a path.
MASK_CHAR = '*'
_KEEP = re.compile(r'[^.:/\\\s-]')
_URL = re.compile(r'\b(?:https?|wss?|tcp|udp|rtsp)://[^\s"\'<>]+', re.IGNORECASE)
_LABELLED = re.compile(
    r'\b(?P<key>(?:wi-?fi\s+)?(?:network(?:\s+name)?|ssid)|serial(?:\s+(?:number|no\.?))?|device\s+id|chip\s+id|'
    r'mac(?:\s+address)?|user(?:\s*name)?|account|server(?:\s+address)?|host(?:\s*name)?|address|ip|port|'
    r'token|api\s+key|access\s+key|password|pass\s*phrase|psk|pairing(?:\s+(?:code|key|secret))?|pin)'
    r'(?P<sep>\s*[:=]\s*|\s+(?:named\s+|called\s+)?(?=["\']))'
    r'(?P<value>"[^"\n]*"|\'[^\'\n]*\'|[^\s,;)\]]+)', re.IGNORECASE)
_CODE = re.compile(r'\b(?P<key>(?:pairing\s+)?code\s+)(?P<value>\d{4,8})\b', re.IGNORECASE)
_QUOTED_WIFI = re.compile(r'(?P<lead>\b(?:wi-?fi|network|ssid|joined|connected\s+to|saved)\b[^"\n]{0,24})"(?P<name>[^"\n]+)"',
                          re.IGNORECASE)
_IPV6 = re.compile(r'(?<![\w:.])(?:(?:[0-9A-Fa-f]{1,4}:){3,7}[0-9A-Fa-f]{1,4}|'
                   r'(?:[0-9A-Fa-f]{1,4}:){1,7}:(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6})?|'
                   r'::(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6}))(?:%\w+)?(?![\w:])')
_IPV4 = re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?![\w.])')
_HOST_PORT = re.compile(r'(?<![\w.*])(?=[\w.*-]*[A-Za-z*])[A-Za-z0-9*][\w.*-]*:\d{2,5}(?![\w:])')
_PORT = re.compile(r'\b(?P<key>ports?\s+)(?P<value>\d{2,5})\b', re.IGNORECASE)
_LOCAL_HOST = re.compile(r'(?<![\w.-])[A-Za-z0-9][\w-]*(?:\.[\w-]+)*\.(?:local|lan|home|internal|localdomain|'
                         r'home\.arpa)(?![\w.-])', re.IGNORECASE)
# serials, device IDs, tokens: 12+ hex digits with a letter (or 16+), not 40/64-digit content hashes
_LONG_HEX = re.compile(r'\b(?=[0-9]*[A-Fa-f]|[0-9A-Fa-f]{16})[0-9A-Fa-f]{12,}\b')
_TOKEN = re.compile(r'\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{28,}\b')
_HOME_DIR = re.compile(r'(?<=[\\/])(?:Users|home)[\\/](?P<name>[^\\/\s"\'<>|,;)]+)', re.IGNORECASE)
_PRIVATE_DIR = re.compile(r'(?<![\w-])(?:private|Jellymatic|jellymatic|Jellymatic-Backups|ChromaticBackups|chromatic-manager-backups)[\\/]'
                          r'(?P<rest>[^\s"\'<>|,;)]+)', re.IGNORECASE)


def _stars(text):
    return _KEEP.sub(MASK_CHAR, text)


def _local_names():
    """This PC's account names (as they appear in paths and messages), longest first."""
    names = set()
    for value in (os.environ.get('USERNAME'), os.environ.get('USER'), Path.home().name,
                  Path(os.environ.get('USERPROFILE') or '').name):
        if value and len(value.strip()) >= 3 and value.strip().lower() not in ('public', 'default', 'root'):
            names.add(value.strip())
    return sorted(names, key=len, reverse=True)


def mask_private(text, names=None):
    """`text` as the window shows it while private details are masked: IP addresses (v4 and
    v6, with ports), MACs, serial numbers and device IDs, Wi-Fi network names, server
    addresses, hosts and ports, user names, tokens and pairing details, and paths that name
    the PC's user or the private folders (backups included) are starred. `names`: the
    account names to hide (default: this PC's). Everything else is left as it is."""
    text = '' if text is None else str(text)
    if not text:
        return text
    text = _URL.sub(lambda m: m.group(0).split('://', 1)[0] + '://' + _stars(m.group(0).split('://', 1)[1]), text)
    text = _LABELLED.sub(lambda m: m.group(0) if '://' in m.group('value') else
                         m.group('key') + m.group('sep') + _stars(m.group('value')), text)
    text = _CODE.sub(lambda m: m.group('key') + _stars(m.group('value')), text)
    text = _QUOTED_WIFI.sub(lambda m: m.group('lead') + '"' + _stars(m.group('name')) + '"', text)
    for name in (_local_names() if names is None else names):
        text = re.sub(re.escape(name), lambda m: MASK_CHAR * len(m.group(0)), text, flags=re.IGNORECASE)
    text = _HOME_DIR.sub(lambda m: m.group(0)[:m.start('name') - m.start(0)] + _stars(m.group('name')), text)
    text = _PRIVATE_DIR.sub(lambda m: m.group(0)[:m.start('rest') - m.start(0)] + _stars(m.group('rest')), text)
    text = _MAC.sub(lambda m: _stars(m.group(0)), text)
    text = _IPV6.sub(lambda m: _stars(m.group(0)), text)
    text = _IPV4.sub(lambda m: _stars(m.group(0)), text)
    text = _LOCAL_HOST.sub(lambda m: _stars(m.group(0)), text)
    text = _HOST_PORT.sub(lambda m: _stars(m.group(0)), text)
    text = _PORT.sub(lambda m: m.group('key') + _stars(m.group('value')), text)
    text = _LONG_HEX.sub(lambda m: m.group(0) if len(m.group(0)) in (40, 64) else _stars(m.group(0)), text)
    return _TOKEN.sub(lambda m: _stars(m.group(0)), text)


def mask_name(text):
    """A name that is private as a whole (a Wi-Fi network, a server's name) as the window shows
    it while private details are hidden: every character a '*', so only its length shows."""
    return _stars(str(text or ''))


def requester_name(text, default='a local program'):
    """Who asked, as shown on the owner's prompt: printable, short, never empty."""
    cleaned = re.sub(r'[^A-Za-z0-9 ._()/+\[\]-]', '', str(text or '')).strip()[:90]
    return cleaned or default


API_SUFFIX = ' [program via local API]'


def api_requester(text):
    """A caller of the local API, always marked as a program, so no name it chooses can
    pass for the owner at the window."""
    return requester_name(text, 'a local program')[:60] + API_SUFFIX


# ---- watching the device steps ----------------------------------------------------------
FLASH_SIZE = 4 * 1024 * 1024
READ_RATE = FLASH_SIZE / 384  # bytes/s: one full read measured at 384 s on this console (115200 baud)
MD5_RATE = FLASH_SIZE / 5.5   # bytes/s: the stub's on-chip MD5 of all 4 MiB measured at 5.5 s


class ObservedDevice:
    """Wraps the programmer the manager builds without changing what it does. It reports
    which device step runs, how far it is (byte counts from esptool where it gives them),
    and whether the console sits in ROM download mode (frozen screen) or runs its firmware.
    Reports carry step names, sizes, port names and timings only, never device data.

    An on-console progress screen would hook in here (for example a serial command sent
    before `connect` resets the chip); there is none yet."""

    mode = 'none'

    def __init__(self, inner, report, should_stop=None):
        self._inner, self._report = inner, report
        self._should_stop = should_stop
        self._last = 0.0
        if hasattr(inner, 'progress'):
            inner.progress = self._bytes

    def _stop_point(self):
        """The owner pressed Stop on a job the AI started: stop here, but only before anything
        was written. Reads and checks are safe points; once a write or erase has begun, the
        service never asks to stop (stopping then could leave the console unable to start)."""
        if self._should_stop is not None and self._should_stop():
            raise StoppedByOwner(STOPPED_BY_OWNER)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def _bytes(self, done, total):
        now = time.monotonic()
        if done >= total or now - self._last >= 0.15:  # a few updates a second is plenty
            self._last = now
            self._report('bytes', {'done': done, 'total': total})

    def _step(self, kind, call, **info):
        self._report('step', dict(info, kind=kind))
        start = time.monotonic()
        try:
            value = call()
        except BaseException as error:
            self._report('step-end', {'kind': kind, 'ok': False, 'seconds': time.monotonic() - start,
                                      'error': type(error).__name__})
            raise
        self._report('step-end', {'kind': kind, 'ok': True, 'seconds': time.monotonic() - start})
        return value

    def _open(self):
        """False only when the programmer says it holds no connection (esptool's `esp`)."""
        return getattr(self._inner, 'esp', True) is not None

    def _mode(self, mode):
        self.mode = mode
        self._report('mode', {'mode': mode})

    def connect(self):
        self._stop_point()
        before = self.mode
        self._mode('download')  # esptool's default reset puts the chip in ROM download mode
        try:
            value = self._step('connect', self._inner.connect)
        except BaseException:
            if not self._open():  # it never got as far as opening the port: the chip was not reset
                self._mode(before)
            raise
        try:
            ports = self._inner.ports() if hasattr(self._inner, 'ports') else []
        except Exception:
            ports = []
        if len(ports) == 1:
            self._report('port', {'port': str(ports[0])})
        return value

    def read_all(self, say=lambda message: None):
        self._stop_point()
        return self._step('read', lambda: self._inner.read_all(say), size=FLASH_SIZE)

    def read(self, offset, size):
        self._stop_point()
        return self._step('read-part', lambda: self._inner.read(offset, size), size=size)

    def write(self, offset, data):
        return self._step('write', lambda: self._inner.write(offset, data), size=len(data))

    def erase(self, offset, size):
        return self._step('erase', lambda: self._inner.erase(offset, size), size=size)

    def md5(self, offset, size):
        self._stop_point()
        return self._step('md5', lambda: self._inner.md5(offset, size), size=size)

    def watch(self, seconds, handover, quiet):
        self._mode('restarting')
        try:
            return self._step('watch', lambda: self._inner.watch(seconds, handover, quiet), seconds=seconds)
        finally:
            self._mode('running')  # watch resets the chip into its firmware first

    def close(self, reset=True):
        resets = reset and self._open()  # closing an already-closed programmer resets nothing
        try:
            return self._inner.close(reset)
        finally:
            if resets and self.mode != 'none':
                self._mode('running')


class StoppedByOwner(KeyboardInterrupt):
    """The owner pressed Stop. A KeyboardInterrupt, so the manager handles it exactly like a
    cancelled job: before any write it stops with nothing changed (`owner_stop` lets the
    recovery loop end at once instead of retrying)."""
    owner_stop = True


def short_release(release):
    """'1.2.0 (image 48b11f9a)' for the owner's prompts."""
    return f'{release.version} (image {release.fingerprint()[:8]})'


def usage_note(root=PRIVATE, now=None):
    """What the AI reported about its usage, for the owner to see before an AI-started write:
    'Usage check: 62% used, reported by the AI (claude) at 14:02 (3 min ago).' None when the
    reading is missing or unreadable. The Manager cannot fetch the AI's usage itself, so this
    is shown, not trusted (hardware_rule refuses a missing or stale reading)."""
    from datetime import datetime, timezone
    try:
        snapshot = json.loads((Path(root) / USAGE_SNAPSHOT).read_text(encoding='utf-8'))
        observed = datetime.fromisoformat(snapshot['observed_at'])
        used = max(float(v) for v in snapshot['used_percent_by_window'])
        reporter = re.sub(r'[^A-Za-z0-9 ._-]', '', str(snapshot.get('reporter') or 'unknown'))[:20]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if observed.tzinfo is None:
        return None
    age = ((now or datetime.now(timezone.utc)) - observed).total_seconds()
    return (f'Usage check: {used:g}% used, reported by the AI ({reporter}) at '
            f'{observed.astimezone().strftime("%H:%M")} ({max(0, int(age // 60))} min ago).')


def observe(manager, report, should_stop=None):
    """Every programmer this manager builds from now on is watched. With `should_stop`, the
    owner's Stop also takes effect right before a job's first write (Manager.stop_point)."""
    build = manager.factory
    manager.factory = lambda: ObservedDevice(build(), report, should_stop)
    if should_stop is not None:
        def stop_point():
            if should_stop():
                raise StoppedByOwner(STOPPED_BY_OWNER)
        manager.stop_point = stop_point
    return manager


STEP_NAMES = {
    'connect': 'Connecting: restarting the console into download mode',
    'read': 'Reading the flash ({mib} MiB, each 256 KiB checked on the chip)',
    'read-part': 'Reading {kib} KiB of the flash (checked on the chip)',
    'write': 'Writing {kib} KiB to the flash',
    'erase': 'Erasing {kib} KiB of flash',
    'md5': 'Checking {kib} KiB on the chip (MD5)',
    'watch': 'Restarting the console and watching it start',
}


def clock_text(seconds):
    seconds = max(0, int(round(seconds)))
    return f'{seconds // 60}:{seconds % 60:02d}'


class Tracker:
    """What the working screen shows: the device step, how far it is, how long it has
    taken and roughly how long is left, and the console's mode."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.job_start = clock()
        self.step, self.mode, self.port, self.connected = None, 'none', None, False

    def event(self, kind, info):
        now = self.clock()
        if kind == 'step':
            self.step = dict(info, start=now, done=None, total=info.get('size'))
            if info['kind'] == 'connect':
                self.connected = True
        elif kind == 'bytes' and self.step is not None:
            self.step['done'], self.step['total'] = info['done'], info['total']
        elif kind == 'step-end':
            self.step = None
        elif kind == 'mode':
            self.mode = info['mode']
        elif kind == 'port':
            self.port = info['port']

    def view(self, fallback_percent=None):
        """{'name', 'percent' (None: animate), 'detail'} for the current step."""
        now, step = self.clock(), self.step
        job = clock_text(now - self.job_start)
        if step is None:  # between device steps (saving a backup, waiting for an answer): animate
            return {'name': '', 'percent': None, 'detail': f'Whole job {job}'}
        size = step.get('size') or 0
        name = STEP_NAMES[step['kind']].format(kib=size // 1024, mib=round(size / 1048576, 1))
        elapsed = now - step['start']
        percent = left = None
        prefix = 'about'
        if step['kind'] in ('read', 'read-part', 'write') and step.get('total'):
            done, total = step['done'] or 0, step['total']
            percent = min(100, done * 100 // total)
            rate = done / elapsed if done and elapsed >= 3 else READ_RATE
            left = (total - done) / rate
            if step['kind'] == 'read':
                name += f': {done / 1048576:.1f} of {total / 1048576:.1f} MiB'
        elif step['kind'] == 'md5' and size:
            left = max(0.0, size / MD5_RATE - elapsed)
        elif step['kind'] == 'watch' and step.get('seconds'):
            percent = min(99, int(elapsed * 100 / step['seconds']))
            left, prefix = step['seconds'] - elapsed, 'at most'
        elif fallback_percent is not None and step['kind'] == 'read':
            percent = fallback_percent
        parts = [f'Step {clock_text(elapsed)}']
        if left is not None:
            parts.append(f'{prefix} {clock_text(left)} left')
        parts.append(f'whole job {job}')
        return {'name': name, 'percent': percent, 'detail': '  |  '.join(parts)}


MODE_NAMES = {'download': 'in ROM download mode (screen frozen)', 'restarting': 'restarting into its firmware',
              'running': 'running its own firmware', 'none': 'not touched'}

BANNERS = {  # console mode: (headline, line under it)
    'download': ("Working. Don't unplug or switch off your Chromatic.",
                 'Its screen stays frozen until this finishes. That is normal.'),
    'restarting': ("Your Chromatic is restarting. Don't press its buttons.",
                   'This app is watching it start. Keep it plugged in and switched on.'),
}


def what_next(key, code, mode, found, lines):
    """One sentence for the result screen: what the console does now."""
    if mode == 'none':
        return 'The console was not touched.'
    if mode in ('download', 'restarting'):
        return ('The console is still in download mode (frozen screen). Unplug USB, switch it off and on: '
                'it then starts normally.')
    state, menu, version = found.get('state'), None, None
    if key == 'status' and state == 'damaged':
        return 'It restarted, but its start-up code is damaged, so it cannot start. Press Fix it.'
    if key == 'status' and state:
        menu, version = state in ('installed', 'outdated'), found.get('launcher')
    elif code == 0 and key == 'install':
        menu = True
        for line in lines:  # the version this install put there decides how its menu behaves
            done = (re.match(r'Start-up menu (\S+) installed\.', line.strip())
                    or re.match(r'Launcher (\S+) is already installed\.', line.strip()))
            if done:
                version = done.group(1)
    elif code == 0 and key in ('remove', 'disable'):
        menu = False
    elif code == 0 and key == 'update':
        menu = any('start-up menu is back on' in line for line in lines)
    if menu is True and version in ('1.1.0', '1.1.1'):
        return ('It has restarted: the start-up menu shows, then stock firmware (games) starts by itself after '
                'its countdown.')
    if menu is True:
        return 'It has restarted into the start-up menu, which waits for you: choose Games to play.'
    if menu is False:
        return 'It has restarted into stock firmware (games). The start-up menu does not appear.'
    return 'It has restarted and runs its own firmware.'


def fpga_job(key, lines):
    """Was this a console chip (FPGA) job? Finish it / Close job act on an unfinished one too."""
    return key in FPGA_KEYS or (key in ('resume', 'abandon') and any('console chip job' in line for line in lines))


def fpga_result(key, code, lines):
    """(headline, what the console does now) for a console chip (FPGA) job, from what the job
    itself said. The ESP32 wording ("The console was not touched", from the programmer watch)
    cannot be used: these jobs run over the JTAG bridge, so that watch never sees them, and
    after a write it would claim nothing happened."""
    text = '\n'.join(lines)
    seen = code != 5
    back = ' The console restarted from it.' if seen else (' The console was not seen coming back: switch it off '
                                                          'and on.')
    outcome = recovered_outcome(lines) if code in (2, 5) else None
    if outcome is not None:  # cut off, then put right (recover): exit 2, or 5 when not seen restarting
        head = 'Fixed: the cut-off job was put right' if seen else 'Put right, not seen restarting'
        if outcome == 'finished' and key in ('fpga_media', 'fpga_link', 'fpga_link_update'):
            image = 'sound' if key == 'fpga_media' else 'Pocket link'
            return head, (f'The cut-off write was finished: the flash already held the {image} image, checked (all 4 '
                          'MiB read back identical). Nothing was written back.' + back)
        if outcome == 'undone':
            return head, 'Your original FPGA image was written back and checked (all 4 MiB read back identical).' + back
        return head, 'The console chip was put right: its flash was written back and checked.' + back
    if code == 0 or code == 5:
        if key == 'fpga_check':
            holds = ('your original FPGA image' if 'Its flash holds your original image' in text else
                     'the sound image' if 'Its flash holds the sound' in text else
                     'the Pocket link image' if SAID_LINK in text else
                     'an image this app does not know' if 'Its flash holds neither' in text else 'an unknown image')
            return (('Checked. Nothing was written' if seen else 'Checked, not seen restarting'),
                    f'Nothing was written. The console chip holds {holds}.'
                    + (' The console restarted from it.' if seen else ' The console was not seen coming back: '
                                                                      'switch it off and on.'))
        if key == 'fpga_restore':
            return (('Original FPGA image written back' if seen else 'Original written, not seen restarting'),
                    'Your original FPGA image was written back and checked (all 4 MiB read back identical).' + back)
        if key == 'fpga_media':
            if 'already holds this sound image' in text:
                return 'Sound image already there', 'Nothing was written: the console chip already holds it.'
            return (('Sound image written and checked' if seen else 'Sound image written, not seen restarting'),
                    'The sound image was written and checked (all 4 MiB read back identical).'
                    + (' The console restarted on it.' if seen else ' The console was not seen coming back: switch '
                                                                    'it off and on.'))
        if key == 'fpga_trial':
            return (('Sound image running from RAM' if seen else 'Trial loaded, not seen restarting'),
                    'Nothing was written: the sound image runs from the chip\'s memory only. Switching the console '
                    'off and on goes back to your original image.')
        if key == 'fpga_link_trial':
            if code == 0 and SAID_LINK_RUNNING in text:
                return ('Pocket link confirmed running',
                        'Nothing was written: the console reports 18.41, the Pocket link image, running from the '
                        'chip\'s memory only. Switching the console off and on goes back to the image in its flash.')
            return (('Pocket link running from RAM' if seen else 'Trial loaded, not seen restarting'),
                    'Nothing was written: the Pocket link image runs from the chip\'s memory only. Switching the '
                    'console off and on goes back to the image in its flash.')
        if key == 'fpga_link':
            if SAID_LINK_THERE in text:
                return 'Pocket link already there', 'Nothing was written: the console chip already holds it.'
            return (('Pocket link written and checked' if seen else 'Pocket link written, not seen restarting'),
                    'The Pocket link image was written and checked (all 4 MiB read back identical).'
                    + (' The console restarted on it.' if seen else ' The console was not seen coming back: switch '
                                                                    'it off and on.'))
        if key == 'fpga_link_update':
            wrote = SAID_FLASH_WRITING in text  # no recovery (handled above): the write ran and read back identical
            if code == 0 and SAID_LINK_FROM_FLASH in text:
                if wrote:
                    return ('Pocket link updated',
                            'The Pocket link image was written and checked (all 4 MiB read back identical), and the '
                            'console reports 18.41 from its flash.')
                return ('Pocket link already up to date',
                        'Nothing was written: the console chip already holds this Pocket link image, and the console '
                        'reports 18.41 from its flash.')
            return ('Pocket link written, not heard from yet' if wrote else 'Pocket link there, not heard from yet',
                    'The Pocket link image is in the flash (read back identical), but the console was not heard '
                    'reporting 18.41 from it. Switch it off and on, then press Update the Pocket link again: it only '
                    'checks.')
        if 'No unfinished console chip job' in text or 'already finished' in text or 'Nothing to do' in text:
            return 'Nothing was changed', 'Nothing was written to the console chip.'
        if key == 'abandon':
            return 'Job closed', 'Nothing was written to the console chip; the cut-off job is closed.'
        return RESULTS[code][0], 'The console chip job finished; Show details has its messages.'
    if code == 1 and key == 'fpga_link_update' and SAID_AUTO_FAILED in text:
        return ('Automatic checks failed',
                'Nothing was written. The new image only ran from the chip\'s memory; the console was restarted from '
                'the image in its flash.')
    if code == 1 and key == 'fpga_link_update' and SAID_SWITCHED_OFF in text:
        return ('Switched off. Nothing was written',
                'Your Chromatic was switched off while the new image waited for your Approve, which ends the try. '
                'Start again, and leave it switched on and plugged in until this app says Done.')
    if code == 1 and key == 'fpga_link_update' and SAID_LATE_REFUSAL in text:
        return 'Stopped. Nothing was written', late_refusal_note(text)
    if code == 1 and key == 'fpga_link_update' and SAID_NOT_KEPT + ':' in text:
        return ('Not kept. Nothing was written',
                'You did not approve the new image. It only ran from the chip\'s memory; the console was restarted '
                'from the image in its flash.')
    if code == 1 and key == 'fpga_link_update' and SAID_TRY_ENDED in text:
        return ('Approve came too late. Nothing was written',
                'The console was no longer running the new image when you pressed Approve (switching it off and on '
                'ends the try). Leave it on between the try and Approve, then start again.')
    if code == 1 and key in ('fpga_link_trial', 'fpga_link_update') and SAID_LINK_NOT_RUNNING in text:
        return ('Pocket link NOT running',
                'Nothing was written. The console does not report the Pocket link image (18.41), so nothing on it can '
                'be checked. Switch the console off and on, then start again.')
    if code == 1:
        return 'Nothing was changed', 'Nothing was written to the console chip.'
    if code == 2:
        return ('Fixed: the cut-off job was put right',
                'The console chip was put right: its flash was written back and checked.')
    if code == 3:
        return 'Stopped: this needs you', 'The console chip job stopped and needs you; nothing more is written.'
    return RESULTS.get(code, ('Something went wrong', 'error'))[0], 'Something went wrong; see Show details.'


# A failure's own words -> the one thing to do about it (first match wins).
STOPPED_BY_OWNER = 'Stopped by you before anything was changed.'
TROUBLE = (
    ((STOPPED_BY_OWNER,), 'Nothing else is needed; the AI now needs your OK for each change.'),
    (('did not appear on USB', 'No Chromatic serial port', 'Expected exactly one Chromatic'),
     'Plug in your Chromatic (only one), switch it on, then press the button again.'),
    (('Another Chromatic tool',), 'Close the other Chromatic tool or wait for it to finish, then try again.'),
    (('not the console the backups came from', 'not the console the job was for'),
     'Connect your own Chromatic (the one your backups came from), then try again.'),
    (('No verified backup',), 'Make a backup first: Fix a problem tab, Back up.'),
    (('Expected exactly one Chromatic (its GWU2X bridge)',),
     'Plug in your Chromatic (only one), switch it on, then press the button again.'),
    (('not the expected console chip',), 'This is not a Chromatic this tool knows. Nothing was changed.'),
    (('has not succeeded in this session yet',), 'Press Restore FPGA first (the rehearsal), then FPGA sound.'),
    (('does not hold your original image',), 'Press Restore FPGA to put your original console chip image back.'),
    (('unknown image and that was not confirmed',), 'Press Check FPGA, then see docs/Recovery.md (Restore the original FPGA image).'),
    (('is not the one approved in the window',), 'Approve the new sound image in the window, then ask again.'),
    (('has not been confirmed by you',), 'Press Update the Pocket link on Fix a problem: it tries the image first.'),
    (('NEVER-INSTALL',), 'Nothing to do: that image is not cleared for your console yet.'),
    (('unfinished console chip job',), 'Press Finish it on the Home tab.'),
    (('earlier job did not finish', 'unfinished job exists'), 'Press Finish it on the Home tab.'),
    (('passed its deadline', 'failed its on-chip checksum', 'wrong size'),
     'Unplug your Chromatic, switch it off and on, plug it back in, then try again.'),
    (('different ESP-IDF major version', 'does not support it yet', 'layout the start-up menu does not support'),
     'Nothing to do: your console keeps working normally with its own firmware.'),
    (('is damaged',), 'Press Fix it on the Home tab.'),
    (('Wi-Fi and Jellyfin were not set up',), 'Press Wi-Fi on the Home tab to try the Wi-Fi and Jellyfin setup again.'),
    (('could not sign in to Jellyfin with its private settings', 'has no private Jellyfin settings file',
      "Jellyfin settings file could not be read"),
     'Check the server, user name and password on Settings > Jellyfin server, then press New Jellyfin sign-in.'),
    (('Jellyfin server reachable: no',),
     'Is the Jellyfin server on? If its address changed, fix it on Settings > Jellyfin server (Find servers).'),
    (('the Wi-Fi list was not confirmed',), 'Nothing was written. Press Wi-Fi again when you are ready.'),
)


NOTHING_CHANGED_TODO = 'Nothing was changed; open Show details to see the last message, then try again.'


def what_to_do(key, code, mode, found, lines):
    """One sentence for the result screen: what the person does now. Never needs anyone to
    explain the tool: it names a button, or a physical step."""
    if mode in ('download', 'restarting'):
        return 'Unplug your Chromatic, switch it off and on, and plug it back in: it then starts normally.'
    if code == 3:
        return ('Unplug your Chromatic, switch it off, wait 10 seconds, plug it in, switch it on, then press '
                'Finish it on the Home tab.')
    if code == 5:
        return 'Unplug your Chromatic, switch it off and on, and check that a game plays.'
    text = '\n'.join(lines)
    if code == 2 and recovered_outcome(lines) is not None:  # its words say "unfinished console chip job", but it is closed
        return 'Nothing: the cut-off job was put right. Use your console as usual.'
    if code != 0:
        for needles, todo in TROUBLE:
            if any(needle in text for needle in needles):
                return todo
        if code == 4:
            return 'Unplug your Chromatic, switch it off and on, plug it back in, then press Check now.'
        if code == 2:
            return 'Nothing: the cut-off job was put right. Use your console as usual.'
        return NOTHING_CHANGED_TODO
    if 'Wi-Fi and Jellyfin were not set up' in text:
        return 'Press Wi-Fi on the Home tab to try the Wi-Fi and Jellyfin setup again.'
    if key == 'status' and found.get('state') == 'damaged':
        return 'Press Fix it on the Home tab.'
    if key in ('install', 'update') or (key == 'status' and found.get('state') == 'installed'):
        return 'Insert a cartridge, then choose Games on the console to play.'
    return 'Nothing else is needed.'


@dataclass(frozen=True)
class Guide:
    """The window's one next step: a short headline, one or two plain lines, its single
    primary button (empty when there is nothing to press), and a tone for the colour:
    'ok' (up to date), 'update' (something to install), 'error' (needs fixing) or 'info'.
    `detail` is the longer plain explanation, shown as a tooltip."""
    headline: str
    steps: tuple
    button: str
    key: str | None   # the action the button starts; None: nothing to press now
    tone: str = 'info'
    detail: str = ''


KEEP_ON = 'Keep your Chromatic plugged in and switched on until this app says Done.'


def guide(summary, pending=False, consoles=None, release=None):
    """What the Home tab shows: one plain status and at most one thing to press.
    `consoles`: how many are plugged in (None when this front end cannot tell)."""
    summary = summary or {}
    state = summary.get('state')
    if consoles == 0:
        steps = ('Connect it to this PC with its USB cable and switch it on.',)
        if pending:
            steps += ('A job did not finish. You can finish it once the console is connected.',)
        return Guide('Plug in your Chromatic', steps, '', None, 'info',
                     'This app notices it by itself and checks it (about 20 seconds). While it checks, the '
                     'console\'s screen freezes and a game that is playing stops.')
    if consoles is not None and consoles > 1:
        return Guide('Connect only one Chromatic', ('Unplug the others. This app then checks yours.',), '', None,
                     'update', 'This app works with one console at a time, so it never changes the wrong one.')
    if pending:
        return Guide('A job did not finish', ('Press Finish it. ' + KEEP_ON,), 'Finish it', 'resume', 'error',
                     'A job was cut off, for example by a power cut or an unplugged cable. Finish it puts the '
                     'console right (it finishes or undoes the job) and checks the result.')
    if state in (None, 'changed'):
        return Guide('Check your Chromatic' if state is None else 'Check it again',
                     ('Takes about 20 seconds. Nothing is changed.',), 'Check now', 'status', 'info',
                     ACTION_HELP['status'])
    if state in ('stock', 'dormant', 'partial'):
        return Guide('Start-up menu not installed', ('Takes about 2 minutes. ' + KEEP_ON,), 'Install', 'install',
                     'update', 'The start-up menu lets you choose Games or Media when the console starts. Games '
                     'work exactly as before. ' + ACTION_HELP['install'])
    if state == 'outdated':
        version = summary.get('release') or release or 'the new version'
        return Guide('Update available', (f'Version {version} is ready. Takes about 2 minutes. ' + KEEP_ON,),
                     f'Update to {version}', 'install', 'update',
                     'Installs the new start-up menu. Wi-Fi, Jellyfin, games and saves stay as they are.')
    if state == 'installed':
        version = summary.get('launcher')
        return Guide('Up to date', ((f'Start-up menu {version}. ' if version else '')
                                    + 'To play, insert a cartridge and choose Games.',), '', None, 'ok',
                     'Nothing to do. Wi-Fi, Jellyfin and the official ModRetro update are under More.')
    if state == 'damaged':
        return Guide('Your Chromatic can\'t start', ('Press Fix it to put the original firmware back from your '
                                                    'backup. ' + KEEP_ON,), 'Fix it', 'restore', 'error',
                     'Its start-up code is damaged, so its screen stays dark, but this PC can still reach it. '
                     + ACTION_HELP['restore'])
    return Guide('Nothing to do', ('Your console runs firmware this app does not change. It works normally.',), '',
                 None, 'info', 'For official updates use ModRetro\'s updater (Official update under More).')


@dataclass(frozen=True)
class SetupSign:
    """The Home tab's second line: what the console's own Wi-Fi and
    Jellyfin setup looks like, so "Up to date" never hides a console that cannot play.
    `tone`: 'ok' (green), 'update' (yellow: something to set up or check), 'error' (red: it
    no longer works); `button`/`key`: the one thing to press ('jellyfin_settings' opens the
    Jellyfin page on this PC, not a console job); `detail`: the tooltip."""
    text: str
    tone: str
    button: str = ''
    key: str | None = None
    detail: str = ''


JELLYFIN_PAGE = 'jellyfin_settings'
_SETUP = re.compile(r'(\d+) Wi-Fi network\(s\), PC pairing (yes|no), Jellyfin (.+)$')


def setup_sign(summary, pc_jellyfin=True):
    """The Home tab's Wi-Fi and Jellyfin line from what the last status said (summary['setup'],
    the manager's "Console setup: ..." line, which already tests the console's own sign-in);
    None when there is nothing to say. `pc_jellyfin`: does this PC have its private Jellyfin
    settings (only whether the file exists)? Without them New Jellyfin sign-in cannot work, so
    the button opens the Jellyfin page first."""
    summary = summary or {}
    setup = summary.get('setup')
    if summary.get('state') not in ('installed', 'outdated') or not setup:
        return None
    sign_in = (('New Jellyfin sign-in', 'jellyfin') if pc_jellyfin else ('Jellyfin server', JELLYFIN_PAGE))
    sign_in_tip = ('Gives the console its own new Jellyfin sign-in by Quick Connect (your password stays on this PC).'
                   if pc_jellyfin else 'First tell this PC your Jellyfin server and sign-in (Settings > Jellyfin '
                                       'server); then New Jellyfin sign-in.')
    if setup.startswith('none'):
        return SetupSign('Wi-Fi and Jellyfin: not set up yet', 'update', 'Wi-Fi', 'wifi',
                         'The console has no Wi-Fi network and no Jellyfin sign-in, so Jellyfin cannot open on it. '
                         'Wi-Fi gives it your network and its own Jellyfin sign-in.')
    found = _SETUP.match(setup)
    if not found:  # "written by this install ...", "changed by the last job ...": not checked since
        return SetupSign('Wi-Fi and Jellyfin: press Check again to see them', 'info', '', None,
                         'They changed since the last check.')
    networks, jellyfin = int(found.group(1)), found.group(3)
    if networks == 0:
        return SetupSign('Wi-Fi: no network saved', 'update', 'Wi-Fi', 'wifi',
                         'The console has no Wi-Fi network, so Jellyfin cannot reach your server. Wi-Fi adds one.')
    nets = f'Wi-Fi: {networks} network{"s" if networks != 1 else ""}'
    if jellyfin == 'not set':
        return SetupSign(f'{nets}. Jellyfin: not signed in', 'update', *sign_in,
                         'The console has no Jellyfin sign-in. ' + sign_in_tip)
    if jellyfin.startswith('sign-in no longer works'):
        return SetupSign(f'{nets}. Jellyfin: sign-in no longer works', 'error', *sign_in,
                         'The Jellyfin server turned the console\'s sign-in down (for example it was removed in '
                         'Dashboard > Devices). ' + sign_in_tip)
    if jellyfin.startswith('linked, not checked'):
        return SetupSign(f'{nets}. Jellyfin: signed in, server not reached', 'update', '', None,
                         'This PC could not reach the Jellyfin server to check the console\'s sign-in. Is the server '
                         'on? Then press Check again. If its address changed: Settings > Jellyfin server, then New '
                         'Jellyfin sign-in.')
    if jellyfin.startswith('linked'):
        return SetupSign(f'{nets}. Jellyfin: signed in', 'ok', '', None,
                         'The console\'s own Jellyfin sign-in works (checked by this PC at the last check).')
    return None


def sound_after_update(lines, before=None, built=None):
    """The last step of the guided official update: ModRetro's updater
    writes its own console chip (FPGA) image, so a Jellyfin sound image is gone afterwards.
    None, or (text, offer): offer is True when the sound setup should be offered. `before`: the
    summary of the last check before the update; `built`: the built sound image's version."""
    text = '\n'.join(lines)
    if 'official update applied and the start-up menu is back on' not in text:
        return None
    now = sound_state(summarize(lines), built)
    was = sound_state(before or {}, built)
    if now == 'unknown':
        return ('The update put a newer ModRetro console chip image on. This app\'s sound image is made for '
                f'{STOCK_FPGA[0]}.{STOCK_FPGA[1]}, so Jellyfin plays without sound until a new sound image is built.',
                False)
    if was in ('installed', 'outdated') and now in ('none', None):
        return 'The update removed Jellyfin sound. Set it up again?', True
    if now == 'none':
        return 'Jellyfin plays without sound: the console chip runs ModRetro\'s own image. Set up Jellyfin sound?', True
    return None


# ---- the guided Jellyfin sound setup ---------------------------------------
# "It really needs to be a 1 button guided setup in the PC app." The guide only puts the existing
# console chip jobs in order (Check FPGA, Restore FPGA as the rehearsal, Try FPGA image, FPGA
# sound) and asks the owner what they see on the console between them. Every job keeps its own
# Approve dialog and every check inside it; the guide never starts a job without that Approve,
# never skips one, and can be stopped on any screen.
SOUND_GUIDE_FILE = 'chromatic-manager-gui/sound-guide.json'  # per-user folder: screen names only
SOUND_STEPS = 5
SOUND_MINUTES = {1: 1, 2: 4, 3: 4, 4: 3, 5: 3}  # check; rehearsal + game; trial + look; power cycle + write; checks
# What the console chip jobs say (chromatic_fpga.py); the tests check each one is still its wording.
SAID_ORIGINAL = 'Its flash holds your original image'
SAID_SOUND = 'Its flash holds the sound'
SAID_NEITHER = 'Its flash holds neither'
SAID_REHEARSAL = 'Rehearsal recorded for this session.'
SAID_TRIAL = "is running from the console chip's RAM"
SAID_SOUND_WRITTEN = 'installed: all 4 MiB read back and identical'
SAID_SOUND_THERE = 'The flash already holds this sound image.'
SAID_NO_REHEARSAL = 'has not succeeded in this session yet'
SAID_LINK = 'Its flash holds the Pocket link image'
SAID_LINK_WRITTEN = SAID_SOUND_WRITTEN  # the same words after a verified write; the job's key tells them apart
SAID_LINK_THERE = 'The flash already holds this Pocket link image.'
SAID_NO_LINK_TRY = 'has not been confirmed by you'
SAID_LINK_RUNNING = 'Confirmed on the console: its start-up menu reports version 18.41 with the debug bit'
SAID_LINK_NOT_RUNNING = 'Not confirmed: the Pocket link image is not running'
SAID_ORIGINAL_BACK = 'Original console chip image written back'
KEEP_PLUGGED = 'Keep your Chromatic plugged in and switched on.'


@dataclass(frozen=True)
class GuideScreen:
    """One screen of the sound setup: `step` 1..5 (0: no step, such as Done or Stopped), a short
    headline, plain lines, the physical thing to do now (`todo`), the console sprite's screen
    (`tone`), and its buttons as (label, kind, choice), the main one first. `detail` is the
    longer explanation, shown as a tooltip."""
    key: str
    step: int
    headline: str
    lines: tuple
    todo: str
    tone: str
    buttons: tuple
    detail: str = ''


STOP = ('Stop setup', '', 'stop')
CLOSE = ('Close', '', 'close')

GUIDE_SCREENS = {
    'check': GuideScreen(
        'check', 1, 'Check the console chip',
        ('Jellyfin sound needs a new image on the console chip (FPGA). This setup takes about 15 minutes in 5 '
         'steps. Stay at your Chromatic the whole time.',
         'First this app reads the chip. It writes nothing.'),
        'Plug in your Chromatic, switch it on and put a game cartridge in: one whose save you do not mind losing, '
        'not the EverDrive, until the new image is proven. Then press Check now.',
        'info', (('Check now', 'go', 'job:fpga_check'), STOP),
        'Check FPGA: reads all 4 MiB of the console chip\'s flash over its JTAG bridge and compares it with your '
        'backup. The screen goes dark for about half a minute, then the console restarts.'),
    'rehearse': GuideScreen(
        'rehearse', 2, 'Practise the undo',
        ('Your chip holds your original image. Now this app writes that same image back and checks it. Nothing '
         'changes, but it proves the undo works before anything new is written.',
         'This only counts while Chromatic Manager stays open and for 6 hours: the sound image must be written in '
         'this same run, so keep the app open until the end.'),
        KEEP_PLUGGED + ' The screen stays dark for about 3 minutes.',
        'info', (('Write original back', 'go', 'job:fpga_restore'), STOP),
        'Restore FPGA over your original image (the rehearsal): the same bytes, command and checks as a real '
        'rescue. All 4 MiB are read back and compared. You approve it in the next window.'),
    'game_test': GuideScreen(
        'game_test', 2, 'Play a game',
        ('The undo worked: your original image was written back and checked.',),
        'Switch your Chromatic off, then on. Choose Games and play for a few seconds. Does the game look right, '
        'with sound? Turn the volume wheel.',
        'info', (('Game looks and sounds normal', 'go', 'game_ok'), ('Something is wrong', 'stop', 'game_wrong'),
                 STOP),
        'This proves your console works on its original image after a real write.'),
    'trial': GuideScreen(
        'trial', 3, 'Try sound, nothing written',
        ('Next the sound image runs from the chip\'s memory only. Nothing is written. Switching the console off '
         'and on undoes it.',),
        KEEP_PLUGGED + ' The screen goes dark for about a minute, then the start-up menu comes back on the trial '
        'image.',
        'sound', (('Try sound image', 'go', 'job:fpga_trial'), STOP),
        'Try FPGA image (temporary): loads the sound image into the console chip\'s RAM over JTAG, never into its '
        'flash. You approve it in the next window.'),
    'trial_look': GuideScreen(
        'trial_look', 3, 'Look at your Chromatic',
        ('The sound image is running from the chip\'s memory. Nothing was written.',),
        'Look at the Chromatic: is the start-up menu clear?\nOpen Jellyfin and play something: do you hear sound?\n'
        'Choose Games: does the game play? Let it run a few seconds before you switch the console off later.',
        'sound', (('All good', 'go', 'trial_ok'), ('Something is wrong', 'stop', 'trial_wrong'), STOP),
        'The start-up menu must be drawn right: one header, no repeats, no slant. Press Menu in Jellyfin: its menu '
        'opens; B closes it.'),
    'power_cycle': GuideScreen(
        'power_cycle', 4, 'Switch off and on',
        ('Now the sound image is written for good. Restore FPGA can always undo it.',),
        'If a game is up under the menu, choose Games and let it run a few seconds first. Then switch your Chromatic '
        'off, then on, and wait for the start-up menu. Then press Write sound image. ' + KEEP_PLUGGED,
        'power', (('Write sound image', 'go', 'job:fpga_media'), STOP),
        'FPGA sound: writes the image, reads all 4 MiB back and compares them, then restarts the console. About '
        '2 minutes; the screen stays dark. You approve it in the next window, which names the same image.'),
    'final': GuideScreen(
        'final', 5, 'Final checks',
        ('The sound image was written and checked.',),
        'Switch your Chromatic off and on, then check:\n1. Games: the game plays, with sound.\n2. Media, Jellyfin: '
        'play something, you hear sound.\n3. The Menu button: the Jellyfin menu opens.',
        'sound', (('All good', 'go', 'final_ok'), ('Something is wrong', 'stop', 'final_wrong'), STOP),
        'Also try the volume wheel and headphones in a game. B on Jellyfin\'s home goes back to the start-up menu.'),
    'final_wrong': GuideScreen(
        'final_wrong', 5, 'Put your original back?',
        ('Restore FPGA writes your original image back and checks it. About 3 minutes.',),
        KEEP_PLUGGED + ' Then press Restore FPGA.',
        'error', (('Restore FPGA', 'go', 'job:fpga_restore'), ('Not now', '', 'close')),
        'Your games keep working on the sound image too; Restore FPGA puts ModRetro\'s original image back exactly. '
        'You approve it in the next window.'),
    'done': GuideScreen(
        'done', 0, 'Jellyfin sound is ready',
        ('Games run as before, and Jellyfin has sound.',
         'Restore FPGA on Fix a problem puts your original image back at any time.'),
        'After an official ModRetro update, run this setup again.',
        'ok', (('OK', 'ok', 'close'),)),
    'restored': GuideScreen(
        'restored', 0, 'Your original is back',
        ('Your original FPGA image was written back and checked.',),
        'Switch your Chromatic off and on, then play a game.',
        'ok', (('OK', 'ok', 'close'),)),
}

# Why the setup stopped: (headline, lines, what to do, tone, extra button or None).
RESTORE_OFFER = ('Restore FPGA', 'go', 'job:fpga_restore')
FIX_OFFER = ('Fix a problem', 'error', 'fix')
AGAIN_OFFER = ('Start again', 'go', 'again')
GUIDE_STOPS = {
    'cancelled': ('Sound setup stopped', ('Nothing more will run.',), '', 'info', None),
    'already_sound': ('Sound is already installed', ('The console chip already holds the sound image, so there is '
                                                      'nothing to set up.',),
                      'To start again from your original image, press Restore FPGA.', 'ok', RESTORE_OFFER),
    'unknown': ('Stopped: unknown chip image', ('The console chip holds an image this app does not know, for example '
                                                'after an official ModRetro update. Nothing was written.',),
                'Don\'t go on. Your Chromatic works as before. Ask for help before you try again.', 'error', None),
    'not_back': ('Console not seen restarting', ('Nothing was written.',),
                 'Switch your Chromatic off and on. If it starts normally, start the setup again.', 'error', None),
    'refused': ('Stopped. Nothing was written', (), '', 'update', None),
    'rehearsal_lost': ('Start again from step 1', ('The practice undo only counts while Chromatic Manager stays '
                                                   'open, and this run has none yet. Nothing was written.',),
                       'Press Start again.', 'update', AGAIN_OFFER),
    'rehearsal_not_back': ('Undo written, not seen', ('Your original image was written back and checked, but the '
                                                      'console was not seen restarting.',),
                           'Switch your Chromatic off and on. If a game plays normally, start the setup again.',
                           'error', None),
    'needs_you': ('Stopped: this needs you', (), '', 'error', FIX_OFFER),
    'game_wrong': ('Stopped: game not right', ('Your original image was written back and checked, but the game does '
                                               'not look or sound right.',),
                   'Press Restore FPGA to write your original image again. If that does not help, stop and ask for '
                   'help.', 'error', RESTORE_OFFER),
    'trial_not_back': ('Stopped: trial did not start', ('Nothing was written. The trial only ran from the chip\'s '
                                                        'memory.',),
                       'Switch your Chromatic off and on: it starts from its original image.', 'error', None),
    'trial_wrong': ('Stopped: trial not right', ('Nothing was written. The sound image only ran from the chip\'s '
                                                 'memory.',),
                    'Switch your Chromatic off and on: it starts from its original image, as before.', 'error', None),
    'sound_undone': ('Sound not installed', ('The sound image could not be written and checked, so your original '
                                             'image was written back.',),
                     'Switch your Chromatic off and on, and check that a game plays.', 'update', None),
}
RECOVERED_FINISHED_NOTE = ('The write was cut off, then finished: the flash already held the {image}, checked (all '
                           '4 MiB read back identical). Nothing was written back.')
RECOVERED_NOT_SEEN_TODO = ('It was not seen restarting: switch your Chromatic off and on, and check that a game plays.')
TRIAL_SCREENS = ('trial_look', 'power_cycle')  # the trial image may still run: off and on undoes it
# Update sound (the start-up menu reported an older sound image of this app): the same five steps
# and jobs. Only two screens read differently: step 1, and step 2 when Check found the older image
# (neither the original nor the built sound image), where Restore FPGA writes the original back over
# it after its own Approve for an image it does not know.
UPDATE_SCREENS = {
    'check': dataclasses.replace(
        GUIDE_SCREENS['check'], headline='Update the sound image',
        lines=('Your console chip has an older sound image. Updating takes about 15 minutes in 5 steps: your '
               'original image goes back first, then the new sound image. Stay at your Chromatic the whole time.',
               'First this app reads the chip. It writes nothing.')),
    'rehearse': dataclasses.replace(
        GUIDE_SCREENS['rehearse'],
        lines=('Your chip holds the older sound image. Now this app writes your original image back over it and '
               'checks it, so the undo is proven before the new image is written.',
               'This only counts while Chromatic Manager stays open and for 6 hours: keep the app open until the end.'),
        detail='Restore FPGA: writes your original image from your backup, reads all 4 MiB back and compares them. '
               'The chip holds an image this app does not know byte for byte (the older sound image), so the next '
               'window asks you to approve writing over it.'),
}


class SoundGuide:
    """The guided sound setup's state: which screen shows, why it stopped, and whether the sound
    image was written. Saved to a small private file (screen names and yes/no only), so if the
    Manager is closed mid-way the next start offers to resume or to start again. The rehearsal
    is credited per run of the Manager (chromatic_fpga.SESSION), so after a restart the guide
    always starts again at step 1, unless the sound image is already written (then only the
    final checks are left)."""
    # What the window draws around the screens (the Pocket link guide has its own).
    TITLE, TAG, STEPS, MINUTES = 'JELLYFIN SOUND', 'SOUND SETUP', SOUND_STEPS, SOUND_MINUTES
    IMAGE_SCREENS = ('trial', 'power_cycle')  # the screens that name the image (to try, to write)
    NAME = 'sound'

    def __init__(self, path, session):
        self.path, self.session = Path(path), str(session)
        self.screen, self.reason, self.note, self.todo = None, None, '', ''
        self.sound_written, self.trial_loaded, self.saved, self.job = False, False, None, None
        self.update, self.older = False, False  # Update sound; Check found the older sound image

    @property
    def active(self):
        return self.screen is not None

    # persistence ---------------------------------------------------------------------------
    def _save(self):
        if self.screen in (None, 'done', 'restored', 'stopped'):
            self._forget()
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'screen': self.screen, 'sound_written': self.sound_written,
                                             'trial_loaded': self.trial_loaded, 'session': self.session,
                                             'update': self.update,
                                             'at': time.strftime('%Y-%m-%d %H:%M:%S')}), encoding='utf-8')
            os.replace(temporary, self.path)
        except OSError:
            pass  # never stops the setup; a restart then simply starts again

    def _forget(self):
        try:
            self.path.unlink()
        except OSError:
            pass

    def resume_on_open(self):
        """At start-up: a setup left unfinished by an earlier run shows the resume screen."""
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(saved, dict) or saved.get('session') == self.session:
                return False
        except (OSError, ValueError):
            return False
        self.saved = saved
        self.sound_written = saved.get('sound_written') is True
        self.update, self.older = saved.get('update') is True, False
        self.trial_loaded = saved.get('trial_loaded') is True or saved.get('screen') in TRIAL_SCREENS
        self.screen, self.reason, self.note, self.todo = 'resume', None, '', ''
        return True

    # moving between screens ---------------------------------------------------------------
    def _go(self, screen, note=''):
        self.screen, self.note = screen, note
        if screen != 'stopped':
            self.reason, self.todo = None, ''
        self._save()

    def _stop(self, reason, note='', todo=''):
        self.screen, self.reason, self.note, self.todo = 'stopped', reason, note, todo
        self.job = None
        self._save()

    def start(self, update=None):
        """`update`: Update sound (an older sound image is on the chip); None keeps the current mode."""
        self.sound_written, self.trial_loaded, self.saved, self.job = False, False, None, None
        self.update, self.older = (self.update if update is None else bool(update)), False
        self._go('check')

    def stop(self):
        """The owner's Stop setup: nothing more runs. Says how to undo a trial still running."""
        trial = self.screen in TRIAL_SCREENS or (self.screen == 'resume' and self.trial_loaded)
        self._stop('cancelled', todo=('Switch your Chromatic off and on: it goes back to its original image.' if trial
                                      else 'You can start the setup again any time.'))
        self._forget()

    def close(self):
        self.screen, self.reason, self.note, self.todo, self.job = None, None, '', '', None
        self._forget()

    def choose(self, choice):
        """A button on the current screen. Returns ('job', key) for a job the window then starts
        with its own Approve dialog, ('tab', name), ('close',) or None (the screen changed)."""
        if choice.startswith('job:'):
            return 'job', choice[4:]
        if choice == 'stop':
            self.stop()
        elif choice == 'close':
            self.close()
            return ('close',)
        elif choice == 'fix':
            self.close()
            return 'tab', 'recovery'
        elif choice == 'again':
            self.start()
        elif choice == 'resume':
            self._go('final' if self.sound_written else 'check')
        elif choice == 'game_ok':
            self._go('trial')
        elif choice == 'game_wrong':
            self._stop('game_wrong')
        elif choice == 'trial_ok':
            self._go('power_cycle')
        elif choice == 'trial_wrong':
            self.trial_loaded = False
            self._stop('trial_wrong')
            self._forget()
        elif choice == 'final_ok':
            self._go('done')
        elif choice == 'final_wrong':
            self._go('final_wrong')
        return None

    def cancelled(self, key):
        """The owner pressed Cancel on the job's Approve dialog: the screen stays, nothing ran."""
        self.job = None
        self.note = f'{BY_KEY[key].title}: cancelled. Nothing was done. Press the button again when you are ready.'

    def job_done(self, key, code, lines, headline='', todo=''):
        """A job the guide started has ended: the next screen, from what the job said."""
        self.job = None
        text = '\n'.join(lines)
        done = f'{headline}.' if headline else ''
        # The sound write cut off and put right says which (recover's line also says "unfinished
        # console chip job", but that job is closed): undone, or finished with the sound image on.
        outcome = recovered_outcome(lines) if key == 'fpga_media' and code in (2, 5) else None
        if outcome is None and ('earlier job did not finish' in text or 'unfinished console chip job' in text):
            return self._stop('needs_you', done, 'A job did not finish. Press Finish it on the Home tab.')
        if key == 'fpga_check':
            if code == 0 and SAID_ORIGINAL in text:
                return self._go('rehearse', 'Checked: your original image, byte for byte. Nothing was written.')
            if self.update and code == 0 and SAID_NEITHER in text:  # the older sound image, as the menu said
                self.older = True
                return self._go('rehearse', 'Checked: the older sound image, as expected. Nothing was written.')
            if SAID_SOUND in text:
                return self._stop('already_sound', done)
            if SAID_NEITHER in text:
                return self._stop('unknown', done)
            if code == 5:
                return self._stop('not_back', done)
            return self._stop('refused', done, todo)
        if key == 'fpga_restore' and self.screen == 'rehearse':
            if code == 0 and SAID_REHEARSAL in text:
                return self._go('game_test')  # the screen itself says the undo worked
            if code == 5:
                return self._stop('rehearsal_not_back', done)
            if code == 1:
                return self._stop('refused', done, todo)
            return self._stop('needs_you', done, todo)
        if key == 'fpga_restore':  # the undo offered on a stop screen or after the final checks
            if code == 0:
                self.sound_written = False
                return self._go('restored')
            if code == 1:
                return self._stop('refused', done, todo)
            return self._stop('needs_you', done, todo)
        if key == 'fpga_trial':
            if code == 0 and SAID_TRIAL in text:
                self.trial_loaded = True
                return self._go('trial_look')
            if code == 5:
                self.trial_loaded = True
                return self._stop('trial_not_back', done)
            return self._stop('refused', done, todo)
        if key == 'fpga_media':
            if code in (0, 5) and (SAID_SOUND_WRITTEN in text or SAID_SOUND_THERE in text):
                self.sound_written, self.trial_loaded = True, False
                return self._go('final', 'It was not seen restarting: switch it off and on first.' if code == 5 else '')
            if outcome == 'finished':  # the flash already read as the sound image: it is on and checked
                self.sound_written, self.trial_loaded = True, False
                return self._go('final', RECOVERED_FINISHED_NOTE.format(image='sound image')
                                + (' It was not seen restarting: switch it off and on first.' if code == 5 else ''))
            if outcome == 'undone':
                self.sound_written = False
                return self._stop('sound_undone', done, RECOVERED_NOT_SEEN_TODO if code == 5 else '')
            if SAID_NO_REHEARSAL in text:
                return self._stop('rehearsal_lost', done)
            if code == 1:
                return self._stop('refused', done, todo)
            return self._stop('needs_you', done, todo)
        return self._stop('refused', done, todo)

    # what the window shows -------------------------------------------------------------------
    def minutes_left(self, step):
        return sum(m for s, m in self.MINUTES.items() if s >= step) if step else 0

    def view(self, image=None):
        """The current screen, with the result note and (step 4) the exact image to be written."""
        if self.screen == 'stopped':
            head, lines, todo, tone, offer = GUIDE_STOPS[self.reason]
            buttons = ((offer,) if offer else ()) + (CLOSE,)
            screen = GuideScreen('stopped', 0, head, lines, self.todo or todo or 'Nothing else is needed.', tone, buttons)
        elif self.screen == 'resume':
            if self.sound_written:
                screen = GuideScreen(
                    'resume', 5, 'Finish the sound setup',
                    ('Chromatic Manager was closed during the sound setup. The sound image is already written; '
                     'only the final checks are left.',),
                    'Press Continue for the final checks.', 'sound', (('Continue', 'go', 'resume'), STOP))
            else:
                screen = GuideScreen(
                    'resume', 1, 'Sound setup not finished',
                    ('Chromatic Manager was closed during the sound setup. The practice undo (step 2) only counts '
                     'while the app stays open, so the setup starts again at step 1. Nothing is lost.',),
                    ('Switch your Chromatic off and on first, so it leaves the trial image. ' if self.trial_loaded
                     else '') + 'Then press Start again.',
                    'info', (('Start again', 'go', 'resume'), STOP))
        elif self.update and (self.screen == 'check' or (self.screen == 'rehearse' and self.older)):
            screen = UPDATE_SCREENS[self.screen]
        else:
            screen = GUIDE_SCREENS[self.screen]
        lines = ((self.note,) if self.note else ()) + tuple(screen.lines)
        if screen.key == 'power_cycle':
            lines += (f'Image to write: {image or "none built yet (the job will refuse)"}',)
        if screen.key == 'trial':
            lines += (f'Image to try: {image or "none built yet (the job will refuse)"}',)
        return GuideScreen(screen.key, screen.step, screen.headline, lines, screen.todo, screen.tone, screen.buttons,
                           screen.detail)


# ---- the guided Pocket link setup (the combined image 18.41; owner-present trial) ---------
# The sound guide's shape, for the reviewed 18.41 image: Check FPGA (it must hold the sound image),
# Try Pocket link (SRAM only), four looks at the console on the trial image (games, a link-cable
# game or a plain skip, Jellyfin sound, the Pocket link row in the Jellyfin menu), a power cycle
# back to the sound image in the flash, FPGA Pocket link (the write, refused by the job itself
# unless this try was confirmed with every check All good), and the final checks. Every job keeps
# its own Approve dialog; "Something is wrong" stops, and after the write offers the sound image
# back (FPGA sound) or the original (Restore FPGA).
LINK_GUIDE_FILE = 'chromatic-manager-gui/link-guide.json'  # per-user folder: screen names only
LINK_STEPS = 5
LINK_MINUTES = {1: 1, 2: 2, 3: 5, 4: 3, 5: 3}  # check; try; four looks; power cycle + write; final checks
# The order matters: Games hands the console over to stock firmware, and the start-up
# menu, so Jellyfin, only comes back on a power-on, which reloads the flash image and ends the try.
# So the Jellyfin looks come first and games last, and the try itself proves 18.41 runs first.
LINK_LOOKS = ('look_link_row', 'look_sound', 'look_games', 'look_link_game')
LINK_LOOK_CHECK = {'look_link_row': 'pocket_link', 'look_sound': 'sound', 'look_games': 'games',
                   'look_link_game': 'link_game'}  # chromatic_fpga.LINK_CHECKS, in the order asked
SAID_CONFIRMED_NOTE = 'Confirmed: the console reports 18.41, the Pocket link image.'


def link_answers_good(answers):
    """Every look answered All good (the link-cable game may be skipped): chromatic_fpga's own rule
    (FpgaJobs.answers_all_good), used only to word a stop, never to open the write."""
    checks = tuple(LINK_LOOK_CHECK.values())
    return (isinstance(answers, dict) and set(answers) == set(checks)
            and all(answers[k] == 'good' or (k == 'link_game' and answers[k] == 'skipped') for k in checks))


LINK_TRIAL_SCREENS = LINK_LOOKS + ('power_cycle',)  # the trial image may still run: off and on undoes it
SOUND_BACK_OFFER = ('Sound image back', 'go', 'job:fpga_media')
SOUND_SETUP_OFFER = ('Set up sound', 'go', 'sound_guide')
LOOK_ANSWERS = (('All good', 'go', 'look_ok'), ('Something is wrong', 'stop', 'look_wrong'), STOP)
LINK_SCREENS = {
    'check': GuideScreen(
        'check', 1, 'Add the Pocket link',
        ('The Pocket link lets your Chromatic share Jellyfin with an Analogue Pocket over the link cable. It needs a '
         'new image on the console chip (FPGA): the sound image plus the link. About 15 minutes in 5 steps. Stay at '
         'your Chromatic the whole time.',
         'First this app reads the chip. It writes nothing.'),
        'Plug in your Chromatic, switch it on and put a game cartridge in: one whose save you do not mind losing, '
        'not the EverDrive, until the new image is proven. Then press Check now.',
        'link', (('Check now', 'go', 'job:fpga_check'), STOP),
        'Check FPGA: reads all 4 MiB of the console chip\'s flash over its JTAG bridge. It must hold the sound image '
        '(18.40). The screen goes dark for about half a minute, then the console restarts.'),
    'trial': GuideScreen(
        'trial', 2, 'Try it, nothing written',
        ('Your chip holds the sound image. Next the Pocket link image runs from the chip\'s memory only. Nothing is '
         'written. Switching the console off and on undoes it.',),
        KEEP_PLUGGED + ' The screen goes dark for about a minute, then the start-up menu comes back on the trial '
        'image, and once more while this app reads which image runs. If a try already ran in the last 3 hours, this '
        'app first asks you to switch the console off and on: do that only when it asks, while it watches (it '
        'waits up to 90 s); switching off and on beforehand is not seen and it asks again.',
        'link', (('Try Pocket link', 'go', 'job:fpga_link_trial'), STOP),
        'Try Pocket link (temporary): loads the image into the console chip\'s RAM over JTAG, never into its flash, '
        'then reads the version the start-up menu reports over USB: it must be 18.41 before any check. '
        'You approve it in the next window.'),
    'look_link_row': GuideScreen(
        'look_link_row', 3, 'Check 1 of 4: Pocket link',
        ('The Pocket link image is running from the chip\'s memory. Nothing was written. Do not switch the console '
         'off until the four checks are done: that ends the try.',),
        'On the start-up menu choose Jellyfin. In Jellyfin, press Select to open its menu. Is there a row Pocket '
        'link, with a plug icon? Choose it: a screen with a yellow light says waiting. Press B to go back.',
        'link', LOOK_ANSWERS,
        'Only this image shows the Pocket link row. No Pocket or cable is needed for this check.'),
    'look_sound': GuideScreen(
        'look_sound', 3, 'Check 2 of 4: Jellyfin sound',
        ('Still in Jellyfin, on the trial image.',),
        'Play something. Do you hear sound? Press Menu in Jellyfin: its menu opens; B closes it.',
        'link', LOOK_ANSWERS,
        'Sound works as on the sound image: this image carries it too.'),
    'look_games': GuideScreen(
        'look_games', 3, 'Check 3 of 4: games',
        ('Games come last: once a game runs, Jellyfin only comes back after switching off and on, which ends the '
         'try.',),
        'Press B until the start-up menu shows, then choose Games and play for a minute. Does the game look right, '
        'with sound? Turn the volume wheel and press the buttons.',
        'link', LOOK_ANSWERS,
        'Games must run exactly as before: the Pocket link only starts when you open it in Jellyfin.'),
    'look_link_game': GuideScreen(
        'look_link_game', 3, 'Check 4 of 4: link cable',
        ('Games that use the link cable must still work on this image. Last check before anything is written.',),
        'Have a second console and a game that uses the link cable? Connect them and trade or play together for a '
        'minute. No link-cable game? Press Skip.',
        'link', (('All good', 'go', 'look_ok'), ('Skip', '', 'look_skip'), ('Something is wrong', 'stop', 'look_wrong'),
                 STOP),
        'The link cable belongs to the game unless you open the Pocket link in Jellyfin. Skip is fine: it is noted '
        'with your answers.'),
    'power_cycle': GuideScreen(
        'power_cycle', 4, 'Switch off and on',
        ('Every check passed. Now the Pocket link image is written for good. FPGA sound puts the sound image back at '
         'any time; Restore FPGA your original.',),
        'Let the game run a few seconds first. Then switch your Chromatic off, then on, and wait for the start-up '
        'menu: it is back on the sound image. Then press Write Pocket link. ' + KEEP_PLUGGED,
        'power', (('Write Pocket link', 'go', 'job:fpga_link'), STOP),
        'FPGA Pocket link: writes the image you tried, reads all 4 MiB back and compares them, then restarts the '
        'console. About 2 minutes; the screen stays dark. You approve it in the next window, which names the same '
        'image.'),
    'final': GuideScreen(
        'final', 5, 'Final checks',
        ('The Pocket link image was written and checked.',),
        'Switch your Chromatic off and on, then check:\n1. Games: the game plays, with sound.\n2. Media, Jellyfin: '
        'play something, you hear sound.\n3. Select in Jellyfin: the menu has Pocket link.',
        'link', (('All good', 'go', 'final_ok'), ('Something is wrong', 'stop', 'final_wrong'), STOP),
        'Also try the volume wheel and headphones in a game.'),
    'final_wrong': GuideScreen(
        'final_wrong', 5, 'Go back?',
        ('Write the sound image back, as it was before this setup, or your original image. Each takes about 3 '
         'minutes and is checked.',),
        KEEP_PLUGGED + ' Then press one.',
        'error', (SOUND_BACK_OFFER, ('Restore FPGA', '', 'job:fpga_restore'), ('Not now', '', 'close')),
        'Sound image back: FPGA sound, the sound image (18.40) you had. Restore FPGA: ModRetro\'s original image, '
        'exactly. You approve either in the next window.'),
    'done': GuideScreen(
        'done', 0, 'The Pocket link is ready',
        ('Games run as before, Jellyfin has sound, and its menu has Pocket link.',
         'FPGA sound on Fix a problem puts the sound image back; Restore FPGA your original.'),
        'To link: plug the cable into both consoles, then open Jellyfin, Select, Pocket link.',
        'ok', (('OK', 'ok', 'close'),)),
    'sound_back': GuideScreen(
        'sound_back', 0, 'Sound image is back',
        ('The sound image was written back and checked.',),
        'Switch your Chromatic off and on, then play a game.',
        'ok', (('OK', 'ok', 'close'),)),
    'restored': GuideScreen(
        'restored', 0, 'Your original is back',
        ('Your original FPGA image was written back and checked.',),
        'Switch your Chromatic off and on, then play a game.',
        'ok', (('OK', 'ok', 'close'),)),
}
# After a Pocket link write that failed and was put right: exactly where the console is.
LINK_UNDONE_LINES = ('The Pocket link image could not be written and checked, so your original image was written back '
                     'and checked.',
                     'Your Chromatic is now on ModRetro\'s original image (18.14): games work, and Jellyfin has no sound '
                     'until the sound image (18.40) is back.')
LINK_STOPS = {
    'cancelled': ('Link setup stopped', ('Nothing more will run.',), '', 'info', None),
    'no_image': ('Pocket link not ready', ('The Pocket link image is not cleared for your console on this PC yet. '
                                           'Nothing was done.',),
                 'Nothing to do now. This button works once the image is cleared.', 'info', None),
    'already_link': ('Pocket link is already on', ('The console chip already holds the Pocket link image, so there '
                                                   'is nothing to set up.',),
                     'To go back to the sound image, press Sound image back.', 'ok', SOUND_BACK_OFFER),
    'needs_sound': ('Set up sound first', ('Your console chip holds ModRetro\'s original image. The Pocket link '
                                           'builds on the sound image. Nothing was written.',),
                    'Press Set up sound, then come back here.', 'update', SOUND_SETUP_OFFER),
    'unknown': GUIDE_STOPS['unknown'],
    'not_back': ('Console not seen restarting', ('Nothing was written.',),
                 'Switch your Chromatic off and on. If it starts normally, start the setup again.', 'error', None),
    'refused': ('Stopped. Nothing was written', (), '', 'update', None),
    'trial_not_back': ('Stopped: trial did not start', ('Nothing was written. The trial only ran from the chip\'s '
                                                        'memory.',),
                       'Switch your Chromatic off and on: it starts from the sound image in its flash.', 'error', None),
    'trial_not_running': ('NOT running the Pocket link', ('Nothing was written. The console was asked which image '
                                                         'runs, and it is not the Pocket link image (18.41), so the '
                                                         'checks would test the wrong image.',),
                          'Switch your Chromatic off and on, then press Start again (the next Try waits for that off '
                          'and on and watches the USB port go away and come back). Show details says what the '
                          'console reported.', 'error', AGAIN_OFFER),
    'look_wrong': ('Stopped: trial not right', ('Nothing was written. The Pocket link image only ran from the chip\'s '
                                                'memory, and your answer is kept, so it cannot be written.',),
                   'Choose Games and let it run a few seconds, then switch your Chromatic off and on: it starts from '
                   'the sound image, as before.', 'error', None),
    'confirm_failed': ('Stopped: answers not saved', ('Your answers could not be saved with the try, so the image '
                                                      'cannot be written. Nothing was written.',),
                       'Switch your Chromatic off and on, then start again.', 'error', AGAIN_OFFER),
    'not_confirmed': ('Try not confirmed', ('Every answer was All good, but they could not confirm this try (for '
                                            'example this PC\'s clock was moved back). Nothing was written.',),
                      'Choose Games and let it run a few seconds, then switch your Chromatic off and on, then press '
                      'Start again.', 'update', AGAIN_OFFER),
    'try_lost': ('Try it again first', ('The write needs a try of this exact image that you confirmed. None is '
                                        'recorded, or the image changed. Nothing was written.',),
                 'Press Start again.', 'update', AGAIN_OFFER),
    'needs_you': ('Stopped: this needs you', (), '', 'error', FIX_OFFER),
    'link_undone': ('Pocket link not installed', LINK_UNDONE_LINES,
                    'Press Sound image back to put the sound image (18.40) on again: about 3 minutes, one Approve.',
                    'update', SOUND_BACK_OFFER),
}


class LinkGuide(SoundGuide):
    """The guided Pocket link setup's state, kept like the sound guide's (a small private file of
    screen names, yes/no and the answers given so far). `confirmer(answers)` records the owner's
    answers with the recorded try (chromatic_fpga FpgaJobs.confirm_link_try: True when they confirm
    it, False when not, Refused when there is no try to answer for); `try_confirmed()` says whether
    a confirmed try of the current image is recorded, so a Manager restart after the looks can go
    straight to the write (the try is recorded on disk, unlike the sound guide's rehearsal)."""
    TITLE, TAG, STEPS, MINUTES = 'POCKET LINK', 'LINK SETUP', LINK_STEPS, LINK_MINUTES
    IMAGE_SCREENS = ('trial', 'power_cycle')
    NAME = 'link'

    def __init__(self, path, session, confirmer=None, try_confirmed=None):
        super().__init__(path, session)
        self.confirmer = confirmer or (lambda answers: False)
        self.try_confirmed = try_confirmed or (lambda: False)
        self.link_written, self.answers, self.answered = False, {}, False

    def _save(self):
        if self.screen in (None, 'done', 'restored', 'sound_back', 'stopped'):
            self._forget()
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'screen': self.screen, 'link_written': self.link_written,
                                             'trial_loaded': self.trial_loaded, 'answers': self.answers,
                                             'session': self.session,
                                             'at': time.strftime('%Y-%m-%d %H:%M:%S')}), encoding='utf-8')
            os.replace(temporary, self.path)
        except OSError:
            pass

    def resume_on_open(self):
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(saved, dict) or saved.get('session') == self.session:
                return False
        except (OSError, ValueError):
            return False
        self.saved = saved
        self.link_written = saved.get('link_written') is True
        self.trial_loaded = saved.get('trial_loaded') is True or saved.get('screen') in LINK_TRIAL_SCREENS
        self.answers = {}
        self.answered = saved.get('screen') == 'power_cycle'  # the answers were saved with the try before closing
        self.screen, self.reason, self.note, self.todo = 'resume', None, '', ''
        return True

    def start(self, update=None, ready=True):
        """`ready`: is the reviewed Pocket link image there to try (chromatic_fpga.link_ready_text)?"""
        self.link_written, self.trial_loaded, self.saved, self.job, self.answers = False, False, None, None, {}
        if not ready:
            return self._stop('no_image')
        self._go('check')

    def stop(self):
        trial = self.screen in LINK_TRIAL_SCREENS or (self.screen == 'resume' and self.trial_loaded)
        self._stop('cancelled', todo=('Choose Games, wait a few seconds, then switch your Chromatic off and on: it goes '
                                      'back to the image in its flash.' if trial
                                      else 'You can start the setup again any time.'))
        self._forget()

    def _record(self, check, answer):
        self.answers = dict(self.answers, **{check: answer})

    def _confirm(self):
        """All four looks answered: record them with the try. Only a True from the job's own check
        moves on to the write; anything else stops."""
        try:
            good = self.confirmer(dict(self.answers)) is True
        except Exception:
            return self._stop('confirm_failed')
        if not good and link_answers_good(self.answers):
            return self._stop('not_confirmed')  # every answer All good, yet the job would not confirm the try
        if not good:
            return self._stop('look_wrong')
        self._go('power_cycle', 'Your answers are saved with this try.')

    def choose(self, choice):
        if choice.startswith('job:'):
            return 'job', choice[4:]
        if choice == 'stop':
            self.stop()
        elif choice == 'close':
            self.close()
            return ('close',)
        elif choice == 'fix':
            self.close()
            return 'tab', 'recovery'
        elif choice == 'sound_guide':
            self.close()
            return 'guide', 'sound'
        elif choice == 'again':
            self.start()
        elif choice == 'resume':
            if self.link_written:
                self._go('final')
            elif self.answered and self.try_confirmed():
                self._go('power_cycle')
            else:
                self._go('check', 'The saved try no longer counts (or there was none), so the setup starts again.'
                         if self.answered else '')
        elif choice in ('look_ok', 'look_skip', 'look_wrong') and self.screen in LINK_LOOKS:
            check = LINK_LOOK_CHECK[self.screen]
            if choice == 'look_skip' and self.screen != 'look_link_game':
                return None
            self._record(check, {'look_ok': 'good', 'look_skip': 'skipped', 'look_wrong': 'wrong'}[choice])
            if choice == 'look_wrong':
                try:
                    self.confirmer(dict(self.answers))  # kept with the try: it cannot be written
                except Exception:
                    pass
                return self._stop('look_wrong')
            at = LINK_LOOKS.index(self.screen)
            if at + 1 < len(LINK_LOOKS):
                self._go(LINK_LOOKS[at + 1], 'Skipped: no link-cable game. That is noted with your answers.'
                         if choice == 'look_skip' else '')
            else:
                self._confirm()
        elif choice == 'final_ok':
            self._go('done')
        elif choice == 'final_wrong':
            self._go('final_wrong')
        return None

    def job_done(self, key, code, lines, headline='', todo=''):
        self.job = None
        text = '\n'.join(lines)
        done = f'{headline}.' if headline else ''
        # The Pocket link write cut off and put right says which: undone (the original is back) or
        # finished (18.41 is on and checked). Recover's line says "unfinished console chip job" too.
        outcome = recovered_outcome(lines) if key == 'fpga_link' and code in (2, 5) else None
        if outcome is None and ('earlier job did not finish' in text or 'unfinished console chip job' in text):
            return self._stop('needs_you', done, 'A job did not finish. Press Finish it on the Home tab.')
        if key == 'fpga_check':
            if code == 0 and SAID_SOUND in text:
                return self._go('trial', 'Checked: the sound image, byte for byte. Nothing was written.')
            if SAID_LINK in text:
                return self._stop('already_link', done)
            if SAID_ORIGINAL in text:
                return self._stop('needs_sound', done)
            if SAID_NEITHER in text:
                return self._stop('unknown', done)
            if code == 5:
                return self._stop('not_back', done)
            return self._stop('refused', done, todo)
        if key == 'fpga_link_trial':
            # The looks open only when the job proved 18.41 runs (the start-up menu's own version line).
            if code == 0 and SAID_TRIAL in text and SAID_LINK_RUNNING in text:
                self.trial_loaded, self.answers = True, {}
                return self._go(LINK_LOOKS[0], SAID_CONFIRMED_NOTE)
            if SAID_LINK_NOT_RUNNING in text or (code == 0 and SAID_TRIAL in text):
                self.trial_loaded = True
                return self._stop('trial_not_running', done)
            if code == 5:
                self.trial_loaded = True
                return self._stop('trial_not_back', done)
            return self._stop('refused', done, todo)
        if key == 'fpga_link':
            if code in (0, 5) and (SAID_LINK_WRITTEN in text or SAID_LINK_THERE in text):
                self.link_written, self.trial_loaded = True, False
                return self._go('final', 'It was not seen restarting: switch it off and on first.' if code == 5 else '')
            if outcome == 'finished':  # the flash already read as 18.41: it is on and checked
                self.link_written, self.trial_loaded = True, False
                return self._go('final', RECOVERED_FINISHED_NOTE.format(image='Pocket link image')
                                + (' It was not seen restarting: switch it off and on first.' if code == 5 else ''))
            if outcome == 'undone':
                self.link_written = False
                return self._stop('link_undone', done, RECOVERED_NOT_SEEN_TODO.replace(
                    'and check that a game plays.', 'then press Sound image back.') if code == 5 else '')
            if SAID_NO_LINK_TRY in text:
                return self._stop('try_lost', done)
            if code == 1:
                return self._stop('refused', done, todo)
            return self._stop('needs_you', done, todo)
        if key == 'fpga_media':  # Sound image back (after the write, or on a stop screen)
            if code in (0, 5):
                self.link_written = False
                return self._go('sound_back', 'It was not seen restarting: switch it off and on.' if code == 5 else '')
            if code == 1:
                return self._stop('refused', done, todo)
            return self._stop('needs_you', done, todo)
        if key == 'fpga_restore':
            if code == 0:
                self.link_written = False
                return self._go('restored')
            if code == 1:
                return self._stop('refused', done, todo)
            return self._stop('needs_you', done, todo)
        return self._stop('refused', done, todo)

    def view(self, image=None):
        if self.screen == 'stopped':
            head, lines, todo, tone, offer = LINK_STOPS[self.reason]
            buttons = ((offer,) if offer else ()) + (CLOSE,)
            screen = GuideScreen('stopped', 0, head, lines, self.todo or todo or 'Nothing else is needed.', tone, buttons)
        elif self.screen == 'resume':
            if self.link_written:
                screen = GuideScreen(
                    'resume', 5, 'Finish the link setup',
                    ('Chromatic Manager was closed during the Pocket link setup. The image is already written; only '
                     'the final checks are left.',),
                    'Press Continue for the final checks.', 'link', (('Continue', 'go', 'resume'), STOP))
            elif self.answered:
                screen = GuideScreen(
                    'resume', 4, 'Link setup not finished',
                    ('Chromatic Manager was closed during the Pocket link setup. Your try and your answers are saved, '
                     'so only the write and the final checks are left.',),
                    ('Switch your Chromatic off and on first, so it leaves the trial image. ' if self.trial_loaded
                     else '') + 'Then press Continue.',
                    'link', (('Continue', 'go', 'resume'), STOP))
            else:
                screen = GuideScreen(
                    'resume', 1, 'Link setup not finished',
                    ('Chromatic Manager was closed during the Pocket link setup, before your answers were saved, so '
                     'it starts again at step 1. Nothing was written.',),
                    ('Switch your Chromatic off and on first, so it leaves the trial image. ' if self.trial_loaded
                     else '') + 'Then press Start again.',
                    'info', (('Start again', 'go', 'resume'), STOP))
        else:
            screen = LINK_SCREENS[self.screen]
        lines = ((self.note,) if self.note else ()) + tuple(screen.lines)
        if screen.key == 'power_cycle':
            lines += (f'Image to write: {image or "none ready (the job will refuse)"}',)
        if screen.key == 'trial':
            lines += (f'Image to try: {image or "none ready (the job will refuse)"}',)
        return GuideScreen(screen.key, screen.step, screen.headline, lines, screen.todo, screen.tone, screen.buttons,
                           screen.detail)


# ---- Update the Pocket link in one step ("simply on the device, then I plug it in") ----
# One press, no question rounds: the console chip job fpga_link_update (chromatic_fpga FpgaJobs.update_link:
# flash check, RAM try, the app's own checks, the ONE Approve asked with the new image running (the
# owner starts a game and looks;, the flash write with its read-back, then
# the console heard reporting 18.41 from its flash) and after it this tool's start-up menu, when the
# console's is older (the ordinary install of the reviewed build, with its own Approve). Update now
# itself shows no Approve dialog: nothing is written before the Approve inside the job. Every device
# write keeps its one Approve click; any failed check, or Not right, stops with a plain next step and
# nothing written. The question-based guide above stays as "Step by step".
LINK_UPDATE_FILE = 'chromatic-manager-gui/link-update.json'  # never written: this guide keeps no file
LINK_UPDATE_KEY = 'fpga_link_update'
PREVIOUS_OFFER = ('Put the previous image back', '', 'job:fpga_media')
STEP_BY_STEP = ('Step by step', '', 'step_by_step')
UPDATE_AGAIN = ('Try again', 'go', 'run')
CHECK_AGAIN = ('Check again', 'go', 'run')
# Back to the sound image (18.40) from the original, with the two verified jobs and nothing else: Restore
# FPGA (the rehearsal the sound write needs in every session), then FPGA sound by itself. Each keeps its
# own Approve dialog; no questions.
RESTORE_THEN_SOUND = 'restore_then_sound'
RESTORE_FIRST_OFFER = ('Restore FPGA', 'go', RESTORE_THEN_SOUND)
SOUND_CHAIN_OFFER = ('Sound image back', '', RESTORE_THEN_SOUND)
# The automatic checks read which image runs from the start-up menu's own "fpga version=" line. Menus before
# 1.3.5 do not say it by themselves (1.3.5 says it every 5 s), so with an older one the check fails (safe:
# nothing written) however good the image is: the plain next step is the menu update, then the update again.
MENU_SAYS_IMAGE_SINCE = (1, 3, 5)


def menu_cannot_say(facts):
    """Does the last status say the console has an older start-up menu, from before the first one that
    names the running image by itself? False when the status is missing, stale or not readable."""
    facts = facts or {}
    if facts.get('state') != 'outdated':
        return False
    parts = str(facts.get('launcher') or '').split('.')
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return False
    return tuple(int(part) for part in parts) < MENU_SAYS_IMAGE_SINCE


LINK_UPDATE_NOT_CHECKED = ('Not checked by a machine: the link with a Pocket over the cable, Jellyfin sound, and '
                           'games. You test those now, by plugging in.')
LINK_UPDATE_SCREENS = {
    'start': GuideScreen(
        'start', 0, 'Update the Pocket link',
        ('One press: this app checks the console chip, runs the new image from the chip\'s memory and checks it by '
         'itself. Then it shows you the new image running: start a game, look, and press Approve once. It writes '
         'the image, reads it back, and brings the start-up menu up to date. About 6 minutes. No question rounds.',
         'Nothing is written before your Approve. If a check fails, or you press Not right, nothing is written.'),
        'Plug in your Chromatic with a game cartridge in it and switch it on, then press Update now. Stay near it: '
        'what you must do shows in big letters, and this app waits.',
        'link', (('Update now', 'go', 'run'), STEP_BY_STEP, STOP),
        'Update the Pocket link: Check FPGA (reads all 4 MiB), the image loaded into the chip\'s RAM only, automatic '
        'checks (it loaded, USB came back, not in download mode, the start-up menu reports 18.41 and reached its '
        'menu), your one Approve with the new image running (start a game and look first), then the flash write '
        'with a full read-back, and the console heard reporting 18.41 from its flash. Step by step is the same '
        'with you answering four checks on the console instead.'),
    'menu': GuideScreen(
        'menu', 0, 'Now the start-up menu',
        ('The Pocket link image is on the console chip, and the console reports it from its flash.',
         'Your start-up menu is older than the one this app installs. It is updated next. Wi-Fi, Jellyfin, games '
         'and saves stay as they are.'),
        KEEP_PLUGGED + ' Press Approve in the next window.',
        'link', (('Update the menu', 'go', 'job:install'), STOP),
        'Install: writes this tool\'s reviewed start-up menu and checks the whole flash. About 2 minutes.'),
    'menu_check': GuideScreen(
        'menu_check', 0, 'Checking the start-up menu',
        ('The Pocket link image is on the console chip, and the console reports it from its flash.',
         'This app now reads which start-up menu the console has. It writes nothing.'),
        KEEP_PLUGGED,
        'link', (('Check now', 'go', 'job:status'), STOP),
        'Check: about 20 seconds; the console\'s screen freezes meanwhile.'),
    'ready': GuideScreen(
        'ready', 0, 'Ready: plug in the cable',
        ('The Pocket link image is on your Chromatic: written, read back, and reported by the console from its '
         'flash. The start-up menu is up to date.',
         LINK_UPDATE_NOT_CHECKED),
        'Plug the link cable into both consoles. On the Chromatic choose Jellyfin, press Select, then Pocket link. '
        'On the Pocket open Jellyfin.',
        'ok', (('OK', 'ok', 'close'), PREVIOUS_OFFER),
        'Put the previous image back: FPGA sound, the sound image (18.40) you had, written and checked. About 3 '
        'minutes; you approve it in the next window.'),
    'sound_next': GuideScreen(
        'sound_next', 0, 'Now the sound image',
        ('Restore FPGA succeeded: your original image was written back and checked, so the way back is proven.',
         'Next the sound image (18.40) is written and checked. About 3 minutes.'),
        KEEP_PLUGGED + ' Press Approve in the next window.',
        'link', (SOUND_BACK_OFFER, STOP),
        'FPGA sound: writes the sound image (18.40), reads all 4 MiB back and compares them, then restarts the '
        'console. You approve it in the next window.'),
    'sound_back': LINK_SCREENS['sound_back'],
    'restored': LINK_SCREENS['restored'],
}
# reason: (headline, lines, what to do, tone, offers before Close; at most two)
LINK_UPDATE_STOPS = {
    'cancelled': ('Update stopped', ('Nothing more will run.',), 'You can start it again any time.', 'info', ()),
    'no_image': LINK_STOPS['no_image'][:4] + ((),),
    'no_menu': ('Start-up menu first', ('The Pocket link is opened from this tool\'s start-up menu, and the automatic '
                                        'checks read what that menu reports. Nothing was written.',),
                'Press Close, then Install on the Home tab. Then come back here.', 'update', ()),
    'needs_sound': ('Set up sound first',
                    ('Your console chip holds ModRetro\'s original image (18.14): games work, and Jellyfin has no '
                     'sound. The Pocket link builds on the sound image (18.40). Nothing was written.',),
                    'First time: press Set up sound, then come back here. Had the sound image before (a Pocket link '
                    'write was undone, say)? Press Sound image back: Restore FPGA, then the sound image by itself, '
                    'one Approve each, about 6 minutes.',
                    'update', (SOUND_SETUP_OFFER, SOUND_CHAIN_OFFER)),
    'unknown': ('Stopped: unknown chip image',
                ('The console chip holds an image this app does not know, for example an earlier Pocket link image or '
                 'an official ModRetro update. Nothing was written.',),
                'Open Fix a problem and press Restore FPGA (it asks before writing over an image it does not know), '
                'then Set up Jellyfin sound, then come back here.', 'error', (FIX_OFFER,)),
    'auto_failed': ('Automatic checks failed',
                    ('Nothing was written. The new image only ran from the chip\'s memory; the console was restarted '
                     'from the image in its flash. Show details lists each check.',),
                    'Press Try again: this app asks you to switch the console off and on first, and watches. If it '
                    'fails again, use Step by step, or Fix a problem.', 'error', (UPDATE_AGAIN, STEP_BY_STEP)),
    'menu_too_old': ('Update the menu first',
                     ('Nothing was written. The new image only ran from the chip\'s memory; the console was restarted '
                      'from the image in its flash.',
                      'The automatic checks read which image runs from the start-up menu, and the menu on your '
                      'Chromatic is older than 1.3.5, which does not say it by itself. So the check could not pass.'),
                     'Press Update the menu: about 2 minutes, one Approve. Then press Update now again: this app asks '
                     'you to switch the console off and on first, and watches.',
                     'update', (('Update the menu', 'go', 'job:install'),)),
    'not_kept': ('Not kept. Nothing was written',
                 ('You did not approve the new image. It only ran from the chip\'s memory; the console was '
                  'restarted from the image in its flash and is as it was.',),
                 'If the picture or the sound was wrong, say so to whoever built the image. Try again asks you to '
                 'switch the console off and on first, and watches. Step by step asks about each thing in turn.',
                 'update', (UPDATE_AGAIN, STEP_BY_STEP)),
    'switched_off': ('Switched off: nothing written',
                     ('Your Chromatic was switched off: nothing was written.',
                      'The new image only ran from the chip\'s memory, and switching off ends that. Your console '
                      'starts from the image in its flash, as before.'),
                     'Press Update now to start again. Leave your Chromatic switched on and plugged in until this app '
                     'says Done. If you did not switch it off, check the USB cable.',
                     'update', (('Update now', 'go', 'run'),)),
    'late_refused': ('Stopped. Nothing was written',
                     ('You pressed Approve, but the write did not start: a last check before it refused, or it was '
                      'stopped. The new image is not installed. Show details says why.',),
                     'Press Try again: this app asks you to switch the console off and on first, and watches.',
                     'update', (UPDATE_AGAIN,)),
    'sound_needs_restore': ('Restore FPGA comes first',
                            ('Nothing was written. The sound image goes on only after Restore FPGA has succeeded '
                             'since this app was opened: it writes your original image back and checks it, which '
                             'proves the way back.',),
                            'Press Restore FPGA: about 3 minutes, one Approve. The sound image (18.40) follows by '
                            'itself, with its own Approve.',
                            'update', (RESTORE_FIRST_OFFER,)),
    'try_ended': ('Approve came too late',
                  ('Nothing was written. When you pressed Approve the console was no longer running the new image: '
                   'switching it off and on ends the try and returns to the image in its flash.',),
                  'Press Try again, and leave your Chromatic switched on between the try and Approve.',
                  'update', (UPDATE_AGAIN, STEP_BY_STEP)),
    'not_running': ('NOT running the Pocket link',
                    ('Nothing was written. The console was asked which image runs, and it is not the Pocket link '
                     'image (18.41).',),
                    'Switch your Chromatic off and on, then press Try again.', 'error', (UPDATE_AGAIN, STEP_BY_STEP)),
    'not_back': ('Console not seen restarting', ('Nothing was written.',),
                 'Switch your Chromatic off and on. If it starts normally, press Try again.', 'error', (UPDATE_AGAIN,)),
    'refused': ('Stopped. Nothing was written', (), '', 'update', (UPDATE_AGAIN,)),
    'not_heard': ('Written, not heard from yet',
                  ('The Pocket link image is in the flash, read back and identical. The console was not heard '
                   'reporting 18.41 from it yet.',),
                  'Switch your Chromatic off and on, then press Check again: it only checks, nothing is written.',
                  'update', (CHECK_AGAIN, PREVIOUS_OFFER)),
    'needs_you': ('Stopped: this needs you', (), '', 'error', (FIX_OFFER,)),
    'link_undone': ('Pocket link not installed', LINK_UNDONE_LINES,
                    'Press Sound image back: the sound image (18.40) is written and checked, about 3 minutes, one '
                    'Approve. If this app is closed first, it asks for Restore FPGA before it.',
                    'update', (SOUND_BACK_OFFER,)),
    'menu_failed': ('Start-up menu not updated',
                    ('The Pocket link image is on the console chip and checked. The start-up menu update did not '
                     'finish, so the Pocket link may not open yet.',),
                    'Press Close, then follow the Home tab.', 'update', (PREVIOUS_OFFER,)),
    'menu_unknown': ('Check the start-up menu',
                     ('The Pocket link image is on the console chip and checked. This app could not read which '
                      'start-up menu the console has.',),
                     'Press Close, then Check now on the Home tab.', 'update', (PREVIOUS_OFFER,)),
}
NO_MENU_STATES = ('stock', 'dormant', 'partial')  # the console has no start-up menu of this tool


class LinkUpdateGuide(SoundGuide):
    """Update the Pocket link in one step: which screen shows between its jobs, and which job comes
    next. `facts()` gives the last status summary ({} when none, or when it is stale). After a job,
    `auto` names the job the window starts next by itself (through that job's own Approve dialog);
    None when the owner must press something or the update has ended. It keeps no file: a restart
    simply starts again, and the console chip job then only checks what is already done."""
    TITLE, TAG, STEPS, MINUTES = 'POCKET LINK', 'ONE-STEP UPDATE', 0, {}
    IMAGE_SCREENS = ('start',)
    NAME = 'link_update'

    def __init__(self, path, session, facts=None):
        super().__init__(path or LINK_UPDATE_FILE, session)  # the path is never read or written
        self.facts = facts or (lambda: {})
        self.link_written, self.menu_checked, self.auto, self.asked_by = False, False, None, None
        self.chain = None  # 'restore': FPGA sound follows a Restore FPGA that succeeds; 'sound': that FPGA sound

    def _save(self):
        pass

    def _forget(self):
        pass

    def resume_on_open(self):
        return False

    def start(self, update=None, ready=True, asked_by=None):
        """`ready`: is the reviewed Pocket link image there (chromatic_fpga.link_ready_text)?
        `asked_by`: the program that asked for the update (None: the owner pressed the button)."""
        self.link_written, self.menu_checked, self.auto, self.job = False, False, None, None
        self.trial_loaded, self.saved, self.asked_by, self.chain = False, None, asked_by, None
        if not ready:
            return self._stop('no_image')
        self._go('start')

    def stop(self):
        self.auto, self.chain = None, None
        self._stop('cancelled', todo='You can start it again any time.')

    def close(self):
        self.auto, self.asked_by, self.chain = None, None, None
        super().close()

    def _first_job(self):
        """Update now: the console chip job, unless the last status says there is no start-up menu
        (its checks read that menu's own report, so the job could only fail)."""
        self.link_written, self.menu_checked, self.auto = False, False, None
        if (self.facts() or {}).get('state') in NO_MENU_STATES:
            self._stop('no_menu')
            return None
        return LINK_UPDATE_KEY

    def choose(self, choice):
        self.auto = None
        chain, self.chain = self.chain, None
        if choice == RESTORE_THEN_SOUND:
            self.chain = 'restore'
            return 'job', 'fpga_restore'
        if choice.startswith('job:'):
            if chain == 'sound' and choice == 'job:fpga_media':
                self.chain = 'sound'  # the FPGA sound that follows the Restore FPGA (also after Cancel, pressed again)
            return 'job', choice[4:]
        if choice == 'run':
            key = self._first_job()
            return ('job', key) if key else None
        if choice == 'step_by_step':
            self.close()
            return 'guide', 'link'
        if choice == 'sound_guide':
            self.close()
            return 'guide', 'sound'
        if choice == 'stop':
            self.stop()
        elif choice == 'close':
            self.close()
            return ('close',)
        elif choice == 'fix':
            self.close()
            return 'tab', 'recovery'
        return None

    def cancelled(self, key):
        self.auto = None
        super().cancelled(key)

    def _menu_step(self, note=''):
        """The console chip is done: is this tool's start-up menu the one it installs?"""
        state = (self.facts() or {}).get('state')
        if state == 'outdated':
            self._go('menu', note)
            self.auto = 'install'
        elif state == 'installed':
            self._go('ready', note)
        elif state in NO_MENU_STATES:
            self._stop('no_menu', note)
        elif not self.menu_checked:
            self._go('menu_check', note)
            self.auto = 'status'
        else:
            self._stop('menu_unknown', note)

    def job_done(self, key, code, lines, headline='', todo=''):
        self.job, self.auto = None, None
        text = '\n'.join(lines)
        done = f'{headline}.' if headline else ''
        outcome = recovered_outcome(lines) if key == LINK_UPDATE_KEY and code in (2, 5) else None
        if outcome is None and ('earlier job did not finish' in text or 'unfinished console chip job' in text):
            return self._stop('needs_you', done, 'A job did not finish. Press Finish it on the Home tab.')
        if key == LINK_UPDATE_KEY:
            if code == 0 and SAID_LINK_FROM_FLASH in text:
                self.link_written = True
                return self._menu_step('Written, read back, and reported by the console from its flash.'
                                       if SAID_FLASH_WRITING in text else
                                       'Already there: checked byte for byte, and reported by the console. Nothing '
                                       'was written.')
            if outcome == 'undone':
                return self._stop('link_undone', done, RECOVERED_NOT_SEEN_TODO.replace(
                    'and check that a game plays.', 'then press Sound image back.') if code == 5 else '')
            if outcome == 'finished' or (code == 5 and SAID_LINK_NOT_FROM_FLASH in text):
                self.link_written = True
                return self._stop('not_heard', RECOVERED_FINISHED_NOTE.format(image='Pocket link image')
                                  if outcome == 'finished' else '')
            if SAID_AUTO_FAILED in text:
                return self._stop('menu_too_old' if menu_cannot_say(self.facts()) else 'auto_failed')
            if SAID_SWITCHED_OFF in text:
                return self._stop('switched_off')
            if code == 1 and SAID_LATE_REFUSAL in text:
                hint = '' if todo == NOTHING_CHANGED_TODO else todo
                if SAID_AS_IT_WAS not in text:
                    hint = (f'Switch your Chromatic off and on. {hint}' if hint else
                            'Switch your Chromatic off and on. If it starts normally, press Try again.')
                return self._stop('late_refused', late_refusal_note(text), hint)
            if SAID_NOT_KEPT + ':' in text:
                return self._stop('not_kept')
            if SAID_TRY_ENDED in text:
                return self._stop('try_ended')
            if SAID_LINK_NOT_RUNNING in text:
                return self._stop('not_running')
            if SAID_UPDATE_NEEDS_SOUND in text:
                return self._stop('needs_sound')
            if SAID_UPDATE_UNKNOWN in text or SAID_NEITHER in text:
                return self._stop('unknown')
            if code == 5:
                return self._stop('not_back', done)
            if code == 1:
                return self._stop('refused', done, todo)
            return self._stop('needs_you', done, todo)
        if key == 'status':
            self.menu_checked = True
            return self._menu_step()
        if key == 'install':
            if not self.link_written:  # the menu update that comes first (an older menu failed the checks)
                if code == 0:
                    return self._go('start', 'Start-up menu updated and checked. Now the Pocket link: press Update now.')
                return self._stop('refused' if code == 1 else 'needs_you', done, todo)
            if code == 0:
                return self._go('ready', 'Start-up menu updated and checked.')
            return self._stop('menu_failed' if code == 1 else 'needs_you', done, todo)
        chain, self.chain = self.chain, None
        if key == 'fpga_media':  # Put the previous image back, or Sound image back
            if code in (0, 5):
                self.link_written = False
                return self._go('sound_back', 'It was not seen restarting: switch it off and on.' if code == 5 else '')
            if code == 1 and SAID_NO_REHEARSAL in text and chain != 'sound':
                # The flash holds the original and this session has no rehearsal (the app was restarted
                # since): Restore FPGA first, then this again by itself. Once only: no loop.
                return self._stop('sound_needs_restore')
            return self._stop('refused' if code == 1 else 'needs_you', done, todo)
        if key == 'fpga_restore':
            if code == 0 and chain == 'restore':
                self.link_written, self.chain = False, 'sound'
                self._go('sound_next')
                self.auto = 'fpga_media'
                return None
            if code == 0:
                self.link_written = False
                return self._go('restored')
            return self._stop('refused' if code == 1 else 'needs_you', done, todo)
        return self._stop('refused', done, todo)

    def minutes_left(self, step):
        return 0

    def view(self, image=None):
        if self.screen == 'stopped':
            head, lines, todo, tone, offers = LINK_UPDATE_STOPS[self.reason]
            if self.link_written and PREVIOUS_OFFER not in offers and len(offers) < 2:
                offers = tuple(offers) + (PREVIOUS_OFFER,)  # once written, the way back stays one click away
            screen = GuideScreen('stopped', 0, head, lines, self.todo or todo or 'Nothing else is needed.', tone,
                                 tuple(offers) + (CLOSE,))
        else:
            screen = LINK_UPDATE_SCREENS[self.screen]
        lines = ((self.note,) if self.note else ()) + tuple(screen.lines)
        if screen.key == 'start':
            if self.asked_by:
                lines = (f'Asked for by {self.asked_by}. Nothing is written unless you press Approve.',) + lines
            lines += (f'Image: {image or "none ready (the job will refuse)"}',)
        return GuideScreen(screen.key, screen.step, screen.headline, lines, screen.todo, screen.tone, screen.buttons,
                           screen.detail)

    def public_view(self):
        """For the API: the screen's fixed wording only (no image name, no requester's words)."""
        if not self.active:
            return None
        view = self.view()
        return {'guide': 'Update the Pocket link', 'screen': self.reason if view.key == 'stopped' else view.key,
                'headline': view.headline, 'what_to_do': view.todo, 'tone': view.tone,
                'starts_next': self.auto, 'buttons': [b[0] for b in view.buttons]}


class RunLog:
    """A timestamped log of one job, in a Git-ignored private folder. Steps, port, timings
    and results only: Wi-Fi setup messages and typed answers are never written."""
    KEEP = 100

    def __init__(self, folder, key):
        self.path, self.handle = None, None
        try:
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime('%Y%m%d-%H%M%S')
            self.path = folder / f'chromatic-manager-{stamp}-{key}.log'
            self.handle = open(self.path, 'a', encoding='utf-8')
            for old in sorted(folder.glob('chromatic-manager-*.log'))[:-self.KEEP]:
                old.unlink()
        except OSError:
            self.handle = None  # a log that cannot be written never stops the job

    def write(self, text):
        if self.handle is None:
            return
        try:
            self.handle.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")}  {redact(text)}\n')
            self.handle.flush()
        except OSError:
            pass

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


def append_request_log(folder, text):
    """Who asked the API for what, and how it ended: one line each, in the private log folder."""
    try:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / 'api-requests.log', 'a', encoding='utf-8') as handle:
            handle.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")}  {redact(text)}\n')
    except OSError:
        pass


UPDATE_GUIDE = (
    'Guided update (Chromatic Manager, choice 6). It never starts ModRetro\'s updater itself.',
    'Step 1 of 4: the tool switches the start-up menu off (erases only its own area; power-safe).',
    'Step 2 of 4: the owner runs ModRetro\'s updater: firmware update only (not Cart Clinic), console plugged '
    'in and on, PC awake, never closed while working; on success close it and answer done in the window.',
    'Step 3 of 4: the tool reads the console, checks the new firmware and backs it up.',
    'Step 4 of 4: the tool switches the start-up menu back on.',
    'Answers to its questions come only from the person at the window. More: docs/Recovery.md, '
    '"Official updates and restoring Chromatic OS".',
)


# ---- the service --------------------------------------------------------------------------
class ManagerService:
    """Runs one job at a time and keeps what the window and the API show.

    Front ends register as listeners (called as `listener(event, data)` from whichever
    thread produced the event: 'started', 'line', 'device', 'question', 'request',
    'request-update' and 'done'). `asker` answers the manager's questions; without one
    every question gets an empty answer, which the manager treats as no.

    The write gate: `request_action` only records a request. It can turn into a job only
    through `resolve_request`, called on the thread of the window that registered itself
    with `attach_confirmer`; that window shows the person its Approve dialog.
    Without such a window (headless), every write request is refused.

    AI control (owner opt-in): the window asks the owner once per run, on the first request,
    whether the AI may drive the app. Only the window's thread can record that answer
    (`set_ai_control`), remember it in the private settings file, or forget it. With it
    allowed, a request still goes through `resolve_request` on the window's thread, after
    the project's hardware rule (`hardware_gate`), and every manager safety check still runs.
    `stop_ai` (the owner's Stop button) takes the permission back and stops the AI's job at
    the next point before anything is written."""

    def __init__(self, cm, manager_factory=None, lock_factory=None, log_folder=None, root=PRIVATE, host='headless',
                 settings_path=None, hardware_gate=None):
        self.cm, self.root, self.host = cm, Path(root), host
        self.settings = AppSettings(settings_path or self.root / SETTINGS_FILE)
        self.hardware_gate = hardware_gate or (lambda: hardware_rule(self.root))
        self.usage_note = lambda: usage_note(self.root)
        self.release_peek = lambda: cm.Release.load(repin=False)  # what an install would write, read only
        self.ai_session = None  # this run: None (not asked yet), 'allow' or 'deny'; in memory only
        self.ai_release = None  # fingerprint of the launcher image the owner approved for the AI
        self.ai_fpga_image = None  # fingerprint of the sound (FPGA) image the owner approved for the AI, if any
        self.fpga_factory = fpga_jobs  # (manager, approved_media) -> the console chip jobs
        self._stop, self._wrote, self._stop_said = False, False, False
        self.ui = {'log_panel_open': False}
        self.manager_factory = manager_factory or (lambda say: cm.Manager(say=say))
        if lock_factory is None:
            from chromatic_lock import DeviceLock
            lock_factory = DeviceLock
        self.lock_factory = lock_factory
        self.log_folder = Path(log_folder) if log_folder else self.root / LOG_FOLDER
        self.listeners, self.asker = [], None
        self._confirmer_thread = None
        self._lock = threading.RLock()
        self.job, self.request, self.requests = None, None, deque(maxlen=20)
        self.tracker, self.progress, self.run_lines = Tracker(), Progress(), []
        # What the running job needs the owner to do right now (chromatic_manager_owner): the window
        # shows it big with a countdown, the API reports it. None when nothing is asked.
        self.owner_action, self._job_owner, self._job_owners = None, None, []
        self.guide_view = None  # the one-step update's screen in the window, for the API (note_guide)
        self.tail = deque(maxlen=400)
        self.run_log, self.last_log_path, self.last_result = None, None, None
        self.summary, self.question = {}, None
        self._sequence = 0
        self._setup_phase, self._withheld = False, set()
        self.port_handoff = None  # remote_pilot_link.UsbHandoff: set by the window; None = no Remote Pilot hand-off
        self._code = None

    # front ends ------------------------------------------------------------------------
    def attach_confirmer(self):
        """Called by the window, on its own thread: from now on write requests are put to
        the person at that window, and only that thread can turn one into a job."""
        self._confirmer_thread = threading.get_ident()

    def detach_confirmer(self):
        self._confirmer_thread = None

    def _on_window_thread(self):
        return self._confirmer_thread is not None and threading.get_ident() == self._confirmer_thread

    # AI control (owner opt-in) ------------------------------------------------------------
    def remembered_ai_control(self):
        value = self.settings.get('ai_control')
        return value if value in ('allow', 'deny') else None

    def ai_decision(self):
        """'allow' or 'deny' for this run, or None when the owner must be asked. A grant only
        ever comes from the owner's click in this run (`set_ai_control`), never from a file:
        a remembered Allow is only a preference (the window then asks "Keep allowing it this
        time?"); a remembered Don't allow is honoured at once (denying is always safe).
        Headless (no window), the AI is never allowed."""
        if self._confirmer_thread is None:
            return 'deny'
        if self.ai_session:
            return self.ai_session
        return 'deny' if self.remembered_ai_control() == 'deny' else None

    def set_ai_control(self, allow, remember=False, release=None, fpga_image=None):
        """The owner's answer to a pop-up. Window thread only; anything else is ignored.
        `release`: the fingerprint of the launcher image the owner saw in that pop-up; an
        AI-started write may only use that image. `fpga_image`: likewise the sound (FPGA)
        image's fingerprint, when the pop-up showed one; without it an AI-started FPGA sound
        write is refused (the owner approves it in the window). `remember` stores a preference only."""
        if not self._on_window_thread():
            return False
        choice = 'allow' if allow else 'deny'
        with self._lock:
            self.ai_session = choice
            self.ai_release = release if allow else None
            self.ai_fpga_image = fpga_image if allow else None
        if remember:
            try:
                self.settings.set('ai_control', choice)
            except OSError:
                pass  # not remembered; this run still uses the answer
        append_request_log(self.log_folder, f'AI control: the owner chose {choice}'
                                            + (' (remembered)' if remember else ' (this run only)'))
        return True

    def forget_ai_control(self):
        """The Settings tab's Forget button: the next AI request asks the owner again."""
        if not self._on_window_thread():
            return False
        with self._lock:
            self.ai_session, self.ai_release, self.ai_fpga_image = None, None, None
        try:
            self.settings.set('ai_control', None)
        except OSError:
            pass
        append_request_log(self.log_folder, 'AI control: the owner cleared the remembered choice')
        return True

    def ai_gate(self, key):
        """The hardware rule for an AI-started write; None when it may start."""
        if key not in WRITE_KEYS:
            return None
        try:
            return self.hardware_gate()
        except Exception as error:
            return f'The hardware usage check could not run ({type(error).__name__}).'

    def ai_release_loader(self, key):
        """The launcher release for a job the AI started under the grant: read without
        re-pinning, and for a write only the image the owner approved in the window."""
        approved = self.ai_release

        def load():
            release = self.release_peek()
            if key in WRITE_KEYS and release.fingerprint() != approved:
                raise self.cm.Refused('the start-up menu image is not the one you approved for the AI. Approve the '
                                      'new one in the window first.')
            return release
        return load

    def ai_job(self):
        job = self.job
        return job is not None and job.get('confirmed_by') == AI_GRANT

    def stop_ai(self):
        """The owner's Stop button (window thread only): the AI loses its permission now (a
        remembered Allow is forgotten too), a waiting request is withdrawn, and a job the AI
        started stops at the next safe point. Once something is being written the job is
        never cut off: it finishes, with all its checks, and nothing new starts."""
        if not self._on_window_thread():
            return None
        with self._lock:
            self.ai_session, self.ai_release, self.ai_fpga_image = 'deny', None, None
            request = self.request
            if request is not None and request['state'] == 'waiting':
                request['state'], request['outcome'] = 'withdrawn', 'Stopped by the owner.'
            stopping = self.ai_job()
            if stopping:
                self._stop = True
            wrote = self._wrote
        if self.remembered_ai_control() == 'allow':
            try:
                self.settings.set('ai_control', None)
            except OSError:
                pass
        append_request_log(self.log_folder, 'AI control: stopped by the owner'
                                            + (' (job running)' if stopping else ''))
        if stopping and wrote:
            self._line('Stop pressed. A change is being written, so the job finishes safely first; nothing new '
                       'starts after it.')
        elif stopping:
            self._line('Stop pressed. Stopping before anything is changed.')
        return {'stopping_job': stopping, 'writing': bool(stopping and wrote)}

    def _should_stop(self):
        """Asked by the device wrapper before each read or check: True only when the owner
        pressed Stop and nothing has been written in this job."""
        with self._lock:
            stop = self._stop and not self._wrote
            say = stop and not self._stop_said
            if say:
                self._stop_said = True
        if say:
            self._line(STOPPED_BY_OWNER)
        return stop

    # the window's log panel, for the AI to show the owner ---------------------------------
    def set_log_panel(self, open_):
        if self._confirmer_thread is None:
            return {'state': 'refused', 'why': 'No Chromatic Manager window is open.'}
        with self._lock:
            self.ui['log_panel_open'] = bool(open_)
        self.emit('ui', dict(self.ui))
        return {'log_panel_open': bool(open_)}

    def read_log(self, lines=100):
        lines = max(0, min(int(lines), 400))
        with self._lock:
            return {'log_panel_open': self.ui['log_panel_open'] if self._confirmer_thread is not None else None,
                    'lines': list(self.tail)[-lines:] if lines else [],
                    'note': "The same lines as the window's log, with identifiers and Wi-Fi details withheld."}

    @property
    def confirmations(self):
        return 'window' if self._confirmer_thread is not None else 'none'

    def emit(self, event, data=None):
        for listener in list(self.listeners):
            try:
                listener(event, data or {})
            except Exception:
                pass  # a front end's display problem never stops a job

    def busy(self):
        return self.job is not None

    def wait(self, timeout=None):
        """Until the running job (if any) has ended."""
        thread = getattr(self, '_thread', None)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def pending(self):
        try:
            manager = self.manager_factory(lambda message: None)
            return bool(manager.pending() or self.fpga_factory(manager).pending())
        except Exception:
            return False

    # running jobs ----------------------------------------------------------------------
    def start(self, key, requested_by=OWNER, confirmed_by=None, _request=None, setup='auto', note=None):
        """Start an action now; False if one is running. Front ends call this for a write
        only after their own approval by a person; the API never calls it for a write.
        `setup` (install only): see run_action."""
        action = BY_KEY[key]
        with self._lock:
            if self.job is not None:
                return False
            self._setup_phase = False
            self._stop, self._wrote, self._stop_said = False, False, False
            self.job = {'key': key, 'title': action.title, 'requested_by': requested_by,
                        'confirmed_by': confirmed_by, 'started': time.strftime('%Y-%m-%d %H:%M:%S'),
                        'setup': setup if setup in SETUP_CHOICES else 'auto', 'note': note}
            if _request is not None:
                _request['state'], _request['job'] = 'running', self.job
            self.tracker, self.progress, self.run_lines, self.question = Tracker(), Progress(), [], None
            self.owner_action, self._job_owner, self._job_owners = None, None, []
            self._withheld = set()  # indexes of run_lines from Wi-Fi setup: never in logs or the API
            self.run_log = RunLog(self.log_folder, key)
            self.last_log_path = self.run_log.path if self.run_log.handle else None
            self.run_log.write(f'Started: {action.title}. Requested by {requested_by}.'
                               + (f' Confirmed by {confirmed_by}.' if confirmed_by else ''))
            thread = self._thread = threading.Thread(target=self._run, args=(key,), name=f'chromatic-job-{key}')
        self.emit('started', {'key': key, 'requested_by': requested_by})
        if note:
            self._line(note)  # the window, the log file and the API all show it
        thread.start()
        return True

    def start_status(self, requested_by, reason=''):
        """A program asking for the read-only status: it waits for the person at the window
        to confirm (it stops a game and holds the console in download mode: about 20 seconds,
        or about 7 minutes when its stock firmware is new to this tool)."""
        return self.request_action('status', requested_by, reason)

    def _run(self, key):
        code = self.cm.EXIT_ERROR
        handoff = self.port_handoff
        if handoff is not None and not self._pause_pilot(handoff, key):
            self._resume_pilot(handoff)
            self._finish(key, self.cm.EXIT_REFUSED)
            return
        try:
            self._job_body(key)
            code = self._code
        finally:
            if handoff is not None:
                self._resume_pilot(handoff)  # before 'done': no next job can start until USB is handed back
        self._finish(key, code)

    # Remote Pilot's USB hand-off (remote_pilot_link.UsbHandoff): the console has one USB port.
    # Before a job opens it, Remote Pilot is asked to let go (a hold file on disk first, then its
    # local API closes the port); after the job it gets it back, unless a job is left unfinished.
    def _pause_pilot(self, handoff, key):
        try:
            outcome = handoff.pause(BY_KEY[key].title)
        except Exception as error:  # never start a job when the hand-off itself failed
            self._line(f'Remote Pilot could not be asked to let go of the USB cable ({type(error).__name__}). '
                       'Nothing was done; try again.')
            return False
        if outcome.line:
            self._line(outcome.line)
        if outcome.paused:
            self.emit('pilot', {'state': 'paused', 'title': BY_KEY[key].title})
        return outcome.ok

    def _resume_pilot(self, handoff):
        try:
            keep = self.pending()
            line = handoff.resume(keep)
        except Exception as error:
            keep, line = True, (f'Remote Pilot\'s USB link stays paused ({type(error).__name__}); it comes back the next '
                                'time Chromatic Manager starts.')
        if line:
            self._line(line)
        self.emit('pilot', {'state': 'kept' if keep else 'back'})

    def _job_body(self, key):
        self._code = code = self.cm.EXIT_ERROR
        try:
            manager = observe(self.manager_factory(self._say), self._device_event, self._should_stop)
            by_grant = self.job.get('confirmed_by') == AI_GRANT
            if by_grant:
                manager.release_loader = self.ai_release_loader(key)
            # Under the grant the sound image must be the one the owner saw ('' : none approved);
            # an owner-approved job takes the built image (the Approve dialog named it).
            fpga = self.fpga_factory(manager, (self.ai_fpga_image or '') if by_grant else None)
            # Whatever either job layer needs the owner to do now reaches the window and the API.
            owners = []
            for jobs in (manager, fpga):
                prompts = getattr(jobs, 'owner', None)
                if prompts is not None and hasattr(prompts, 'listen') and all(prompts is not p for p in owners):
                    prompts.listen(self._owner_event)
                    owners.append(prompts)
            self._job_owner = owners[0] if owners else None
            self._job_owners = owners
            code = run_action(self.cm, manager, key, self._ask, lambda prompt: self._ask(prompt, True),
                              self.lock_factory(), setup=self.job.get('setup', 'auto'), phase=self._set_setup_phase,
                              fpga=fpga)
        except Exception as error:  # building the manager failed: report, never crash
            self._say(f'Unexpected error ({type(error).__name__}); nothing was attempted.')
        self._code = code

    def _set_setup_phase(self, on):
        with self._lock:
            self._setup_phase = bool(on)

    def _wifi(self):
        """Wi-Fi setup messages and answers (the Wi-Fi action, or that part of an install)
        are kept out of the log file, the log tail and the API."""
        return self.job is not None and (self.job['key'] == 'wifi' or getattr(self, '_setup_phase', False))

    def _say(self, message):
        for part in str(message).strip('\n').split('\n'):
            self._line(part)

    def _line(self, text):
        with self._lock:
            wifi = self._wifi()
            self.run_lines.append(text)
            if wifi:
                self._withheld.add(len(self.run_lines) - 1)
            self.progress.feed(text)
            self.tail.append(f'{time.strftime("%H:%M:%S")}  {redact(text, wifi)}')
            if self.run_log is not None:
                self.run_log.write(WIFI_WITHHELD if wifi else text.strip())
        self.emit('line', {'text': text})

    def _owner_event(self, event):
        """A job layer announced, or ended, something the owner must do (chromatic_manager_owner).
        Titles and instructions are fixed wording from the code: no private detail is in them."""
        with self._lock:
            if self.job is None:
                return
            action = dict(event, shown=time.strftime('%Y-%m-%d %H:%M:%S'), at=time.monotonic())
            self.owner_action = action
            if self.run_log is not None:
                state = action['state']
                if state == owner_actions.WAITING:
                    limit = '' if action['seconds'] is None else f' (within {int(action["seconds"])} s)'
                    self.run_log.write(f'[owner] asked on screen: {action["title"]}{limit}')
                elif state == owner_actions.DONE:
                    self.run_log.write(f'[owner] done: {action["title"]}')
                elif state == owner_actions.TIMEOUT:
                    self.run_log.write(f'[owner] time ran out: {action["title"]}')
        self.emit('owner', dict(action))

    def owner_view(self):
        """What the owner is asked to do now, for the window and the API: None, or the title, the
        instruction, the state (waiting / done / timeout) and the seconds left when it is timed."""
        with self._lock:
            action = self.owner_action
            if action is None or action['state'] == owner_actions.CLOSED:
                return None
            left = None
            if action['seconds'] is not None and action['state'] == owner_actions.WAITING:
                left = max(0, int(round(action['seconds'] - (time.monotonic() - action['at']))))
            return {'state': action['state'], 'kind': action['kind'], 'title': action['title'],
                    'instruction': action['instruction'], 'seconds_allowed': action['seconds'],
                    'seconds_left': left, 'next_step': action['next'] or None, 'shown': action['shown']}

    def _ask(self, prompt, secret=False):
        with self._lock:
            wifi = self._wifi()
            self.question = {'prompt': '(a Wi-Fi setup question)' if wifi else redact(prompt), 'hidden': secret,
                             'answered_by': 'the person at the window only',
                             'since': time.strftime('%Y-%m-%d %H:%M:%S')}
            if self.run_log is not None:
                self.run_log.write('[question] waiting for an answer in the window')
        # A question is something the owner must do too: unless the job already said what it needs
        # (the guided update's "run the updater now"), the main screen says a small window is asking.
        prompts, asked = self._job_owner, False
        if prompts is not None and not any(p.waiting for p in self._job_owners):
            prompts.need(owner_actions.QUESTION_TITLE, owner_actions.QUESTION_TEXT, log='',
                         kind=owner_actions.QUESTION)
            asked = True
        self.emit('question', {})
        try:
            answer = self.asker(prompt, secret) if self.asker is not None else ''
        finally:
            with self._lock:
                self.question = None
            if asked:
                prompts.close()
        if not secret:
            shown = '(answered)' if wifi and answer else answer
            self._line(f'> {shown}' if answer else '> (no answer)')
        return answer

    def _device_event(self, kind, info):
        with self._lock:
            self.tracker.event(kind, info)
            if kind == 'step' and info.get('kind') in ('write', 'erase'):
                self._wrote = True  # from here on Stop never cuts the job off
            log = self.run_log
            if log is not None:
                if kind == 'step':
                    log.write(f'[step] {info["kind"]} started' +
                              (f' ({info["size"] // 1024} KiB)' if info.get('size') else ''))
                elif kind == 'step-end':
                    log.write(f'[step] {info["kind"]} {"done" if info["ok"] else "failed (" + info["error"] + ")"}'
                              f' after {info["seconds"]:.1f} s')
                elif kind == 'mode':
                    log.write(f'[console] {MODE_NAMES.get(info["mode"], info["mode"])}')
                elif kind == 'port':
                    log.write(f'[console] serial port {info["port"]}')
        self.emit('device', {'kind': kind, 'info': info})

    def _finish(self, key, code):
        for prompts in self._job_owners:
            if prompts.waiting:
                prompts.close()  # the job ended another way (Stop, an error): nothing stays asked
        with self._lock:
            job = self.job
            if self.owner_action is not None and self.owner_action['state'] != owner_actions.TIMEOUT:
                self.owner_action = None  # done or closed: nothing is asked; a timed-out request keeps its next step
            found = summarize(self.run_lines) if key == 'status' and code == self.cm.EXIT_OK else {}
            if found:
                self.summary = dict(found, checked=time.strftime('%Y-%m-%d %H:%M:%S'))
            elif key != 'status':
                self.summary = summary_after_job(key, code, self.run_lines, self.summary) or {}
            head, role = RESULTS.get(code, ('FINISHED', 'warn'))
            if key == 'status' and code == self.cm.EXIT_OK:
                head = STATUS_DONE
            next_text = what_next(key, code, self.tracker.mode, found, self.run_lines)
            if fpga_job(key, self.run_lines):  # written over JTAG: say what was written, never "not touched"
                head, next_text = fpga_result(key, code, self.run_lines)
            todo = what_to_do(key, code, self.tracker.mode, found, self.run_lines)
            took = self.tracker.clock() - self.tracker.job_start
            wifi = key == 'wifi'
            withheld = self._withheld
            self.last_result = {
                'action': key, 'title': job['title'], 'exit_code': code, 'headline': head, 'role': role,
                'next': next_text, 'what_to_do': todo, 'took': clock_text(took), 'requested_by': job['requested_by'],
                'confirmed_by': job['confirmed_by'], 'started': job['started'],
                'finished': time.strftime('%Y-%m-%d %H:%M:%S'),
                'messages': [redact(line, wifi or n in withheld) for n, line in enumerate(self.run_lines)
                             if line.strip()][-20:],
                'log_file': str(self.last_log_path) if self.last_log_path else None,
            }
            if self.run_log is not None:
                self.run_log.write(f'Finished: exit {code}, {head} Took {clock_text(took)}.')
                self.run_log.write(f'Next: {next_text}')
                self.run_log.write(f'What to do: {todo}')
                self.run_log.close()
            request = self.request
            if request is not None and request.get('job') is job:
                request['state'], request['exit_code'] = 'finished', code
                request['outcome'] = head
                append_request_log(self.log_folder, f'{request["id"]} {request["action"]} requested by '
                                                    f'{request["requested_by"]}: finished, exit {code}, {head}')
            self.job = None
        self.emit('done', {'code': code})

    # the write gate --------------------------------------------------------------------
    def request_link_update(self, requested_by, reason=''):
        """A program asks for the one-step Pocket link update. Returns at once. The window opens
        Update the Pocket link and shows the job's Approve dialog; nothing is loaded or written
        unless the owner presses Approve there. The owner's AI permission never starts it."""
        return self.request_action(LINK_UPDATE_KEY, requested_by, reason, _owner_approves=True)

    def note_guide(self, view):
        """The window says which guided screen shows (fixed wording only), or None when it closed."""
        with self._lock:
            self.guide_view = dict(view) if view else None

    def request_action(self, key, requested_by, reason='', _owner_approves=False):
        """Ask for a write. Returns at once; nothing is written unless the owner then approves
        it in the window, or has allowed AI control there (and the hardware rule passes).
        No argument, token or setting reachable from here can grant either."""
        requested_by = requester_name(requested_by)
        action = BY_KEY.get(key)
        if key in GUIDE_ONLY_KEYS:
            append_request_log(self.log_folder, f'{key} requested by {requested_by}: refused (guide only)')
            return {'state': 'refused', 'action': key, 'why': f'{action.title} {GUIDE_ONLY_WHY}'}
        owner_only = key in OWNER_APPROVE_KEYS
        if owner_only and not _owner_approves:
            append_request_log(self.log_folder, f'{key} requested by {requested_by}: refused (its own entry only)')
            return {'state': 'refused', 'action': key, 'why': f'{action.title} {OWNER_APPROVE_WHY}'}
        if action is None or (key not in REQUESTABLE_KEYS and not owner_only):
            return {'state': 'refused', 'why': f'{key!r} cannot be requested.'}
        why = None
        with self._lock:
            if self._confirmer_thread is None:
                why = ('No Chromatic Manager window is open to confirm it. This needs the owner at the window: '
                       'open Chromatic-Manager-App.cmd and ask again (or the owner uses the text menu).')
            elif self.job is not None:
                why = 'A job is running; ask again when it has finished.'
            elif self.request is not None and self.request['state'] == 'waiting':
                why = 'Another request is already waiting for the owner.'
            if why is None:
                self._sequence += 1
                request = {'id': f'r{self._sequence}', 'action': key, 'title': action.title,
                           'requested_by': requested_by, 'reason': redact(str(reason or '')[:REASON_MAX]),
                           'state': 'waiting', 'asked': time.strftime('%Y-%m-%d %H:%M:%S'),
                           'confirmation': ('the owner presses Approve in the Chromatic Manager window to start '
                                            'it (RAM only, nothing written), then Approve again with the new image '
                                            'running before it is written; AI control never starts this or answers '
                                            'for the owner' if owner_only else
                                            "starts under the owner's AI permission once the hardware usage check "
                                            'passes' if self.ai_decision() == 'allow' else
                                            'the owner answers in the Chromatic Manager window (the first request '
                                            'of a run may ask whether the AI may use the app)'
                                            if self.ai_decision() is None else
                                            'the owner presses Approve in the Chromatic Manager window')}
                self.request = request
                self.requests.append(request)
        if why is not None:
            append_request_log(self.log_folder, f'{key} requested by {requested_by}: refused ({why})')
            return {'state': 'refused', 'action': key, 'why': why}
        append_request_log(self.log_folder, f'{request["id"]} {key} requested by {requested_by}: waiting for the owner')
        self.emit('request', self.public_request(request))
        return self.public_request(request)

    def resolve_request(self, request_id, allowed, note='', by_grant=False, usage=None):
        """The window's answer after the person saw the Approve dialog, or (`by_grant`) under
        the owner's AI permission once the hardware rule passed. Only the window's own thread
        may give it; anything else is ignored."""
        if self._confirmer_thread is None or threading.get_ident() != self._confirmer_thread:
            return False
        if allowed and by_grant and self.ai_decision() != 'allow':
            return False
        with self._lock:
            request = self.request
            if request is None or request['id'] != request_id or request['state'] != 'waiting':
                return False
            if allowed and by_grant and request['action'] in OWNER_APPROVE_KEYS:
                return False  # only the owner's own Approve click starts it, never the AI permission
            if not allowed:
                request['state'], request['outcome'] = 'declined', note or 'The owner did not confirm it.'
        if not allowed:
            append_request_log(self.log_folder, f'{request_id} {request["action"]} requested by '
                                                f'{request["requested_by"]}: declined ({request["outcome"]})')
            self.emit('request-update', self.public_request(request))
            return True
        started = self.start(request['action'], requested_by=request['requested_by'],
                             confirmed_by=AI_GRANT if by_grant else APPROVED_IN_WINDOW, _request=request,
                             note=usage if by_grant else None)
        if not started:
            with self._lock:
                request['state'], request['outcome'] = 'not started', 'Another job was running.'
        append_request_log(self.log_folder, f'{request_id} {request["action"]} requested by '
                                            f'{request["requested_by"]}: '
                                            f'{GRANT_LOG if by_grant else "confirmed by the owner"}, '
                                            f'{"started" if started else "not started"}')
        self.emit('request-update', self.public_request(request))
        return started

    def cancel_request(self, request_id, requested_by):
        with self._lock:
            request = self.request
            if request is None or request['id'] != request_id or request['state'] != 'waiting':
                return {'cancelled': False, 'why': 'No waiting request with that id.'}
            request['state'], request['outcome'] = 'withdrawn', f'Withdrawn by {requester_name(requested_by)}.'
        append_request_log(self.log_folder, f'{request_id} withdrawn by {requester_name(requested_by)}')
        self.emit('request-update', self.public_request(request))
        return {'cancelled': True, 'request': self.public_request(request)}

    @staticmethod
    def public_request(request):
        return {k: v for k, v in request.items() if k != 'job'} if request else None

    # what the API may read -----------------------------------------------------------
    def state(self):
        with self._lock:
            job = dict(self.job) if self.job else None
            mode = self.tracker.mode if job else None
            summary = dict(self.summary)
            request = self.public_request(self.request)
            guided = dict(self.guide_view) if self.guide_view else None
        pending = self.pending()
        step = guide(summary, pending)
        sign = setup_sign(summary, self._pc_jellyfin())
        return {
            'app': 'Chromatic Manager', 'host': self.host,
            'next_step': {'headline': step.headline, 'steps': list(step.steps), 'button': step.button,
                          'action': step.key, 'tone': step.tone},
            'console_setup': sign and {'text': sign.text, 'tone': sign.tone, 'button': sign.button,
                                       'action': sign.key},
            'write_confirmations': self._confirmations_text(),
            'ai_control': self.ai_control_view(),
            'log_panel_open': self.ui['log_panel_open'] if self.confirmations == 'window' else None,
            'busy': job is not None,
            'running': job and {k: job[k] for k in ('key', 'title', 'requested_by', 'started')},
            'console_mode_now': mode and MODE_NAMES.get(mode, mode),
            'last_status': summary or None,
            'unfinished_job_waiting': pending,
            'request': request,
            'guided_setup': guided,
            'last_result': self.last_result and {k: self.last_result[k] for k in
                                                 ('action', 'headline', 'next', 'what_to_do', 'finished',
                                                  'requested_by')},
        }

    def _pc_jellyfin(self):
        """Does this PC have its private Jellyfin settings file? (Existence only; never read here.)"""
        try:
            return bool(self.cm.jellyfin_settings_available())
        except Exception:
            return False

    def ai_control_view(self):
        decision = self.ai_decision()
        return {'this_run': {'allow': 'allowed', 'deny': 'ask the owner for each change'}.get(decision,
                                                                                              'not asked yet'),
                'remembered': self.remembered_ai_control() if self.confirmations == 'window' else None,
                'note': 'Only the owner can allow it, in the window. Stop in the window takes it back at once.'}

    def _confirmations_text(self):
        if self.confirmations != 'window':
            return 'no window open: write requests are refused'
        if self.ai_decision() == 'allow':
            return ('the owner allowed AI control: requests start without a per-change Approve, after the '
                    'hardware usage check, with every safety check')
        return 'the owner approves each write in the open window'

    def progress_view(self, lines=40):
        lines = max(0, min(int(lines), 400))
        with self._lock:
            job = self.job
            view = self.tracker.view(self.progress.percent) if job else None
            banner = BANNERS.get(self.tracker.mode) if job else None
            return {
                'busy': job is not None,
                'action': job and job['key'], 'title': job and job['title'],
                'requested_by': job and job['requested_by'],
                'under_ai_permission': bool(job) and job.get('confirmed_by') == AI_GRANT,
                'stop_pressed': bool(job) and self._stop,
                'phase': self.progress.phase if job else None,
                'step': view and view['name'], 'percent': view and view['percent'],
                'times': view and view['detail'],
                'console_mode': job and MODE_NAMES.get(self.tracker.mode, self.tracker.mode),
                'warning_shown': banner and banner[0],
                'waiting_for_owner_answer': dict(self.question) if self.question else None,
                'owner_action_needed': self.owner_view(),
                'log_tail': list(self.tail)[-lines:] if lines else [],
            }

    def result(self):
        with self._lock:
            return dict(self.last_result) if self.last_result else {'result': None, 'note': 'No job has finished yet.'}

    def update_guide(self):
        with self._lock:
            job = self.job
            running = job is not None and job['key'] == 'update'
            return {'guided_update_running': running,
                    'step': self.progress.phase if running else None,
                    'waiting_for_owner_answer': dict(self.question) if running and self.question else None,
                    'request': self.public_request(self.request) if self.request and
                    self.request['action'] == 'update' else None,
                    'guide': list(UPDATE_GUIDE)}

    def backups(self):
        """Backup folders by path only: nothing inside them is opened."""
        found = []
        for name, folder in (('H', getattr(self.cm, 'BACKUPS', None)), ('C', getattr(self.cm, 'SECONDARY', None))):
            if folder is None or not Path(folder).is_dir():
                continue
            for entry in sorted(Path(folder).iterdir(), reverse=True):
                if entry.is_dir():
                    files = sorted(p.name for p in entry.iterdir() if p.is_file())
                    found.append({'disk': name, 'path': str(entry), 'files': files})
        return {'backups': found, 'note': 'Paths only; contents, hashes and identities are never returned.'}


def acquire_window_lock(root=PRIVATE):
    """One Chromatic Manager window at a time (an OS lock, freed if the window dies)."""
    from chromatic_lock import DeviceLock
    lock = DeviceLock(Path(root) / 'chromatic-manager-gui/window.lock')
    lock.__enter__()  # raises chromatic_lock.Busy if another window holds it
    return lock

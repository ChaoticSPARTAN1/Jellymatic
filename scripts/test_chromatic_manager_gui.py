# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Chromatic Manager window against the simulated console of test_chromatic_manager.py
(built from the real stock backup and the real launcher build). Every button runs the
real Manager through the window's worker thread, with scripted approvals and answers. No device
access, no real Wi-Fi file, no real gateway identity, nothing written outside a temporary
folder. Prints only pass/fail.

Run: private/chromatic-manager-gui/venv/Scripts/python.exe -B scripts/test_chromatic_manager_gui.py
"""
import dataclasses
import inspect
import json
import os
from pathlib import Path
from user_paths import PRIVATE  # noqa: E402  (per-user folder, see user_paths.py)
import re
import tempfile
import threading
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_QPA_FONTDIR', os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts'))  # real fonts

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QLabel

import chromatic_manager as cm
import chromatic_manager_gui as gui
import chromatic_theme as theme
from chromatic_lock import DeviceLock
from compose_chromatic_image import stock_dump
import time

from test_chromatic_manager import (Device, Harness, IDENTITY, check, crash_mid_install, official_update,
                                    with_app)

ROOT = Path(__file__).resolve().parents[1]
USAGE = 'Usage check: 12% used, reported by the AI (claude) at 10:00 (1 min ago).'
OFFICIAL_STYLES = PRIVATE / 'updater-analysis/extract1/carchive/flasherUI/styles'


class Scripted:
    """Typed answers in order; an answer may be a function of the prompt. `ai` answers the
    AI-control pop-up in order as (allow, remember); with none left it answers Don't allow."""

    def __init__(self, confirms=(), answers=(), ai=(), keep=()):
        self.confirms, self.answers, self.ai, self.keep = list(confirms), list(answers), list(ai), list(keep)
        self.asked, self.secrets, self.infos, self.ai_asked, self.keep_asked = [], [], [], [], []

    def ai_control(self, requested_by, action=None, image=None):
        self.ai_asked.append((requested_by, action.key if action else None))
        self.image = image
        return self.ai.pop(0) if self.ai else (False, False)

    def ai_keep(self, requested_by, action=None, image=None, changed=False):
        """"Keep allowing it this time?": scripted Keep (True) or Turn off (default)."""
        self.keep_asked.append((requested_by, action.key if action else None, image, changed))
        return self.keep.pop(0) if self.keep else False

    def confirm(self, action, requested_by=None, reason=''):
        """A scripted 'yes' (or the action's own word) stands for pressing Approve in the
        word correctly; anything else for a wrong word or Cancel."""
        word = self.confirms.pop(0)
        return word.strip().lower() == (action.confirm or 'yes')

    def ask(self, prompt, secret):
        self.asked.append(prompt)
        if secret:
            self.secrets.append(prompt)
        if not self.answers:
            return ''  # closing a dialog: the manager treats it as no
        answer = self.answers.pop(0)
        return answer(prompt) if callable(answer) else answer

    def info(self, title, text):
        self.infos.append(text)


def factory(h):
    def make(say):
        manager = h.manager()
        manager.say = lambda message: (h.log.append(message), say(message))
        return manager
    return make


class Rig:
    def __init__(self, image, release, original=None, confirms=(), answers=(), detect=None, ai=(), gate=None):
        """`gate`: the hardware usage rule for AI-started writes (default: passes). Settings
        (the remembered AI choice) go to the temporary folder, never the real private one."""
        self.h = Harness(image, release, original)
        self.dialogs = Scripted(confirms, answers, ai)
        self.lock_path = self.h.folder / 'device.lock'
        self.logs = self.h.folder / 'logs'
        self.settings = self.h.folder / 'settings.json'
        self.window = gui.MainWindow(cm, factory(self.h), lambda: DeviceLock(self.lock_path), self.dialogs,
                                     root=ROOT, frameless=False, log_folder=self.logs, detect=detect,
                                     settings_path=self.settings, hardware_gate=gate or (lambda: None),
                                     sound_image=lambda: None,  # no sound image unless a test gives one
                                     link_image=lambda: None)  # nor a Pocket link image
        # What an install would write (the test's release, never the real build folder) and a
        # synthetic usage reading.
        self.window.service.release_peek = lambda: self.h.release
        self.window.service.usage_note = lambda: USAGE

    def log_text(self):
        return '\n'.join(f.read_text(encoding='utf-8') for f in sorted(self.logs.glob('*.log')))

    def run(self, key, confirmed=False, seconds=120):
        """Press a button and wait for the job. Returns the exit code, or None if it did not start."""
        loop, result = QEventLoop(), {}

        def done(code):
            result['code'] = code
            loop.quit()
        self.window.finished.connect(done)
        try:
            if confirmed:
                started = self.window.start(key, confirmed=True)
            else:
                self.window.buttons[key].click()
                started = self.window.busy()
            if not started:
                return None
            QTimer.singleShot(seconds * 1000, loop.quit)
            loop.exec()
        finally:
            self.window.finished.disconnect(done)
        check('code' in result, f'{key}: finished in time')
        check(not self.window.busy(), f'{key}: window free again')
        return result['code']

    def state(self):
        return self.h.state()


class SlowDevice(Device):
    """The simulated console with esptool-like byte counts and slow I/O: a silent stretch
    first (the window must keep animating while the worker waits), then the read in steps."""
    progress = None

    def __init__(self, console, quiet=1.2, steps=8, pause=0.12):
        super().__init__(console)
        self.quiet, self.steps, self.pause = quiet, steps, pause

    def read_all(self, say=None):
        time.sleep(self.quiet)
        for n in range(1, self.steps + 1):
            time.sleep(self.pause)
            if self.progress:
                self.progress(n * cm.SIZE // self.steps, cm.SIZE)
        return bytes(self.c.flash)

    def write(self, offset, data):
        super().write(offset, data)
        if self.progress:
            self.progress(len(data), len(data))


def slow_factory(h):
    def make(say):
        manager = h.manager()
        manager.say = lambda message: (h.log.append(message), say(message))
        manager.factory = lambda: SlowDevice(h.console)
        manager.timing = dataclasses.replace(manager.timing, read=30)
        return manager
    return make


def run_sampled(rig, key, every=0.08):
    """Run a job and record what the working screen showed, several times a second."""
    samples, w = [], rig.window
    timer = QTimer()
    timer.setInterval(int(every * 1000))

    def take():
        if w.busy():
            samples.append({'banner': not w.banner.isHidden(), 'head': w.banner_head.text(),
                            'percent': w.work_percent.text(), 'step': w.work_step.text(),
                            'time': w.work_time.text(), 'tick': w.bar.tick, 'lit': w.bar.lit,
                            'animating': w.bar.timer.isActive(), 'mode': w.tracker.mode})
    timer.timeout.connect(take)
    timer.start()
    try:
        code = rig.run(key)
    finally:
        timer.stop()
    return code, samples


class FakeEsp:
    """Just enough of esptool's loader for ManagerProgrammer's read and write loops."""
    FLASH_WRITE_SIZE = 0x400

    def __init__(self):
        self.flash = bytearray(b'\x5a' * cm.SIZE)
        self.progress_fns = []

    def read_flash(self, offset, length, progress_fn=None):
        self.progress_fns.append(progress_fn)
        if progress_fn:
            progress_fn(length // 2, length)
            progress_fn(length, length)
        return bytes(self.flash[offset:offset + length])

    def flash_md5sum(self, offset, size):
        import hashlib
        return hashlib.md5(bytes(self.flash[offset:offset + size])).hexdigest()

    def flash_begin(self, size, offset):
        self.at = offset
        return -(-size // self.FLASH_WRITE_SIZE)

    def flash_block(self, block, number, timeout=None):
        start = self.at + number * self.FLASH_WRITE_SIZE
        self.flash[start:start + len(block)] = block

    def read_reg(self, address, timeout=None):
        return 0


def settle(ms=gui.ApproveDialog.SETTLE_MS + 150):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def dialog_checks():
    """The window's approval: a plain dialog saying what will be
    written, APPROVE and CANCEL. Only a click on APPROVE approves: Enter and Escape cancel,
    and APPROVE ignores input for its first second."""
    import chromatic_manager_core as core
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    Rejected = gui.QDialog.DialogCode.Rejected
    # Enter presses the default button, which is CANCEL.
    dialog = gui.ApproveDialog(None, 'Install the start-up menu.')
    check(dialog.cancel.isDefault() and not dialog.approve.isDefault() and not dialog.approve.autoDefault(),
          'Cancel is the default button')
    dialog.show()
    settle()
    QTest.keyClick(dialog, Qt.Key.Key_Return)
    check(not dialog.isVisible() and dialog.result() == Rejected and not dialog.approved, 'Enter cancels')
    # Escape cancels.
    dialog = gui.ApproveDialog(None, 'Install the start-up menu.')
    dialog.show()
    settle()
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    check(not dialog.isVisible() and not dialog.approved, 'Escape cancels')
    # A click on APPROVE in the first second does nothing; after it, it approves.
    dialog = gui.ApproveDialog(None, 'Install the start-up menu.')
    dialog.show()
    check(not dialog.approve.isEnabled(), 'Approve ignores input when the dialog opens')
    QTest.mouseClick(dialog.approve, Qt.MouseButton.LeftButton)
    check(dialog.isVisible() and not dialog.approved, 'an early click does not approve')
    settle()
    QTest.mouseClick(dialog.approve, Qt.MouseButton.LeftButton)
    check(not dialog.isVisible() and dialog.approved and dialog.result() == gui.QDialog.DialogCode.Accepted,
          'a click on Approve approves')
    dialog = gui.ApproveDialog(None, 'Install the start-up menu.')
    dialog.show()
    settle()
    QTest.mouseClick(dialog.cancel, Qt.MouseButton.LeftButton)
    check(not dialog.isVisible() and not dialog.approved, 'Cancel cancels')

    # Dialogs.confirm shows what will be written; a program's request names the program, and
    # still only the person's click approves it.
    seen, mode = [], {}
    real_exec = gui.ApproveDialog.exec

    def fake_exec(self):  # the person reads the dialog, then presses one of its buttons
        seen.append(self.findChildren(gui.QLabel)[0].text())
        (self.approve if mode['press'] == 'approve' else self.cancel).setEnabled(True)
        (self.approve if mode['press'] == 'approve' else self.cancel).click()
        return self.result()
    gui.ApproveDialog.exec = fake_exec
    try:
        dialogs = gui.Dialogs(None)
        mode['press'] = 'cancel'
        check(not dialogs.confirm(core.BY_KEY['install']), 'Cancel: not approved')
        mode['press'] = 'approve'
        check(dialogs.confirm(core.BY_KEY['install']) and dialogs.confirm(core.BY_KEY['abandon']), 'Approve approves')
        check(dialogs.confirm(core.BY_KEY['status'], requested_by=core.api_requester('the owner (in the window)'),
                              reason='check the version'), 'a status request is approved the same way')
        # The version-changing restore's second question is an Approve dialog too.
        dialogs.lines = lambda: ['This changes the stock firmware from v4.6 to 9.9.9 (backup "newer").']
        check(dialogs.ask('Type change to continue with this version change: ', False) == 'change',
              'version change approved: answered change')
        mode['press'] = 'cancel'
        check(dialogs.ask('Type change to continue with this version change: ', False) == '',
              'version change cancelled: no answer, so nothing is written')
    finally:
        gui.ApproveDialog.exec = real_exec
    check(core.BY_KEY['install'].help in seen[0] and 'Keep your Chromatic plugged in' in seen[0]
          and 'Type' not in seen[0], 'the dialog says what will be written; nothing to type')
    check('Request from the owner (in the window) [program via local API]' in seen[3]
          and 'The AI says: "check the version"' in seen[3]
          and 'about 20 seconds' in seen[3] and 'the program cannot approve it' in seen[3],
          'a program request names the program, even one posing as the owner, and says it cannot approve')
    check(seen[4].startswith('The original firmware version will change.') and 'from v4.6 to 9.9.9' in seen[4],
          'the second dialog says the stock version will change, and to what')


def feedback_checks(stock, release, installed):
    """Progress, banner, finish message and log file."""
    # The programmer's byte counts: esptool's read progress and one count per written block.
    programmer = cm.ManagerProgrammer()
    programmer.esp = FakeEsp()
    check(programmer.read_all() == bytes(programmer.esp.flash) and not any(programmer.esp.progress_fns),
          'no progress hook: esptool gets no callback (command line unchanged)')
    counts = []
    programmer.progress = lambda done, total: counts.append((done, total))
    check(programmer.read_all() == bytes(programmer.esp.flash), 'read with the progress hook')
    check(counts[-1] == (cm.SIZE, cm.SIZE) and len(counts) == 2 * cm.SIZE // programmer.CHUNK
          and all(a[0] <= b[0] for a, b in zip(counts, counts[1:])), 'read progress counts the whole flash in order')
    counts.clear()
    programmer.write(0x10000, b'\x11' * 8192)
    check(counts == [(n * 1024, 8192) for n in range(1, 9)], 'write progress: one count per block')
    programmer.progress = lambda done, total: 1 / 0
    check(programmer.read_all() == bytes(programmer.esp.flash), 'a failing progress display never stops a read')
    programmer.write(0x10000, b'\x22' * 4096)
    check(programmer.esp.flash[0x10000:0x11000] == b'\x22' * 4096, 'or a write')
    # A partial read (identification): checked per piece, progress counted within the part.
    counts = []
    programmer.progress = lambda done, total: counts.append((done, total))
    programmer.esp.flash[0x3F000:0x3F000 + 0x82000] = bytes(range(256)) * (0x82000 // 256)
    check(programmer.read(0x3F000, 0x82000) == bytes(programmer.esp.flash[0x3F000:0xC1000])
          and counts[-1] == (0x82000, 0x82000) and all(a[0] <= b[0] for a, b in zip(counts, counts[1:])),
          'partial read across 256 KiB pieces, in order, with progress')
    check(programmer.read(cm.SETTINGS[0], 0x7000) == bytes(programmer.esp.flash[0x9000:0x10000]), 'settings-sized read')
    programmer.progress = None

    # The tracker: byte steps get a percent and time left; steps without counts animate.
    now = [100.0]
    tracker = gui.Tracker(clock=lambda: now[0])
    view = tracker.view(fallback_percent=100)
    check(view['percent'] is None and 'Whole job 0:00' in view['detail'], 'between device steps: animated')
    tracker.event('mode', {'mode': 'download'})
    tracker.event('step', {'kind': 'connect'})
    check(tracker.view()['percent'] is None and 'download mode' in tracker.view()['name'], 'connect: animated')
    tracker.event('step-end', {'kind': 'connect', 'ok': True, 'seconds': 2})
    tracker.event('step', {'kind': 'read', 'size': cm.SIZE})
    view = tracker.view()
    check(view['percent'] == 0 and 'about 6:24 left' in view['detail'], 'read: starts at 0% with the measured estimate')
    now[0] += 96
    tracker.event('bytes', {'done': cm.SIZE // 4, 'total': cm.SIZE})
    view = tracker.view()
    check(view['percent'] == 25 and '1.0 of 4.0 MiB' in view['name'] and 'about 4:48 left' in view['detail']
          and 'Step 1:36' in view['detail'], 'read: percent, MiB, elapsed and time left from the real rate')
    tracker.event('step-end', {'kind': 'read', 'ok': True, 'seconds': 96})
    tracker.event('step', {'kind': 'watch', 'seconds': 20})
    now[0] += 5
    view = tracker.view()
    check(view['percent'] == 25 and 'at most 0:15 left' in view['detail'], 'watch: by time, with its limit')
    tracker.event('step-end', {'kind': 'watch', 'ok': True, 'seconds': 5})
    tracker.event('step', {'kind': 'md5', 'size': cm.SIZE})
    check('about 0:06 left' in tracker.view()['detail'], 'whole-flash MD5: the measured 5.5 s estimate')
    tracker.event('step-end', {'kind': 'md5', 'ok': True, 'seconds': 5})
    tracker.event('step', {'kind': 'read-part', 'size': 0x7000})
    view = tracker.view()
    check(view['name'] == 'Reading 28 KiB of the flash (checked on the chip)' and view['percent'] == 0
          and 'about 0:03 left' in view['detail'], 'partial read: KiB, percent and a seconds estimate')

    # What the console does next.
    check(gui.what_next('status', 1, 'none', {}, []) == 'The console was not touched.', 'next: untouched')
    check('still in download mode' in gui.what_next('status', 4, 'download', {}, []), 'next: left in download mode')
    check('waits for you: choose Games' in gui.what_next('status', 0, 'running', {'state': 'outdated', 'launcher': '1.1.2'}, []) and 'countdown' in gui.what_next('status', 0, 'running', {'state': 'outdated', 'launcher': '1.1.0'}, []),
          'next: menu installed')
    check('does not appear' in gui.what_next('remove', 0, 'running', {}, []), 'next: menu removed')
    check('countdown' in gui.what_next('install', 0, 'running', {}, ['Start-up menu 1.1.0 installed. The whole-flash '
                                                                       'check matches, and the menu was seen running.'])
          and 'waits for you' in gui.what_next('install', 0, 'running', {}, ['Start-up menu 1.1.2 installed. The '
                                                                             'whole-flash check matches.'])
          and 'countdown' in gui.what_next('install', 0, 'running', {}, ['Launcher 1.1.1 is already installed. '
                                                                         'Nothing written.']),
          'next after install: from the version installed (1.1.0/1.1.1 count down, 1.1.2 waits)')
    check('Fix it' in gui.what_next('status', 0, 'running', {'state': 'damaged'}, []), 'next: damaged')

    # The device watcher: a connect that never opened the port did not reset the chip, and
    # closing an already-closed programmer does not claim a restart.
    class NoPort:
        esp = None

        def connect(self):
            raise cm.StepFailed('No Chromatic serial port')

        def close(self, reset=True):
            pass
    modes = []
    watched = gui.ObservedDevice(NoPort(), lambda kind, info: modes.append(info.get('mode')) if kind == 'mode' else None)
    try:
        watched.connect()
    except cm.StepFailed:
        pass
    watched.close(True)
    check(watched.mode == 'none' and modes == ['download', 'none'], 'no port: console not touched')
    stuck = NoPort()
    stuck.esp = object()
    watched = gui.ObservedDevice(stuck, lambda kind, info: None)
    try:
        watched.connect()
    except cm.StepFailed:
        pass
    stuck.esp = None  # a deadline closed it without a reset
    watched.close(True)
    check(watched.mode == 'download', 'closed without a reset: still reported as download mode')

    # A slow status on the window: banner, live percent, animation while the worker blocks,
    # time left, and the finish message. The console's stock firmware is in no backup, so it
    # is read in full (a known console is not: see below).
    newer = with_app(stock, version='9.9.9', idf='v5.4.1')
    rig = Rig(newer, release, stock)
    rig.window.manager_factory = slow_factory(rig.h)
    code, samples = run_sampled(rig, 'status')
    check(code == cm.EXIT_OK and rig.h.flash() == newer and not rig.h.console.in_stub, 'slow status')
    reading = [s for s in samples if s['step'].startswith('Reading the flash')]
    quiet = [s for s in reading if s['percent'] == '0%']
    check(len(quiet) >= 5 and all(s['animating'] for s in quiet) and len({s['tick'] for s in quiet}) > 1,
          'bar keeps animating while the worker waits on the console')
    shown = [int(s['percent'][:-1]) for s in reading if s['percent']]
    check(shown == sorted(shown) and any(0 < p < 100 for p in shown), 'percent climbs during the read')
    check(all('left' in s['time'] and 'whole job' in s['time'] for s in reading), 'elapsed and time left shown')
    check(all(s['banner'] and s['head'].startswith("Working. Don't unplug") for s in reading),
          'download-mode banner while the console is frozen')
    check(rig.window.banner.isHidden(), 'banner gone when the job ends')
    check(rig.window.next_text == 'It has restarted into stock firmware (games). The start-up menu does not appear.'
          and rig.window.result_next.text() == rig.window.next_text, 'finish message says what the console does')
    log = rig.log_text()
    for want in ('Started: Check the console', '[console] in ROM download mode', '[step] connect started',
                 '[step] read started (4096 KiB)', '[step] read done after', '[console] running its own firmware',
                 'Finished: exit 0', 'Next: It has restarted into stock firmware'):
        check(want in log, f'log file: {want}')
    check(re.match(r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d  ', log), 'log file lines are timestamped')
    check(not any(value in log for value in (IDENTITY['chip'], IDENTITY['flash_id'])), 'log file: no identifiers')
    check(all(f.parent == rig.logs for f in rig.logs.glob('*.log')) and rig.logs.is_relative_to(rig.h.folder),
          'test logs stay in the temporary folder')
    check(re.search(r'\d\d:\d\d:\d\d  Reading the console', rig.window.log.toPlainText()) is not None,
          'log pane lines are timestamped')

    # A known console: identified from on-chip checksums, never read in full; the log shows
    # the checksums and the 36 KiB of settings and setup that are always read.
    rig = Rig(stock, release)
    check(rig.run('status') == cm.EXIT_OK and rig.h.console.full_reads == 0
          and rig.window.summary_state.text() == gui.STATE_NAMES['stock'],
          'fast status from the window')
    log = rig.log_text()
    check('Identifying the console' in log and '[step] md5 started (4096 KiB)' in log
          and '[step] read-part started (28 KiB)' in log and '[step] read started (4096 KiB)' not in log,
          'fast status log: checksums and a small read, no full read')

    # A slow install: the finish message points at the menu; the restart is logged.
    rig = Rig(stock, release, confirms=['yes'])
    rig.window.manager_factory = slow_factory(rig.h)
    code, samples = run_sampled(rig, 'install')
    check(code == cm.EXIT_OK and rig.state() == 'installed', 'slow install')
    check('choose Games' in rig.window.next_text, 'install: says the menu waits for a choice')
    log = rig.log_text()
    check('[console] restarting into its firmware' in log and '[step] watch started' in log
          and '[step] write started' in log, 'install log: writes and the restart watch')

    # The default log folder is the Git-ignored private folder.
    check(not gui.LOG_FOLDER.startswith('/') and gui.MainWindow(
        cm, factory(Harness(stock, release)), lambda: None, Scripted(), root=ROOT, frameless=False
    ).service.log_folder == PRIVATE / 'chromatic-manager-gui/logs', 'default log folder is private')


def wait_idle(rig, seconds=60):
    """Until a job the window started by itself (the plug-in check) has ended."""
    limit = time.monotonic() + seconds
    while time.monotonic() < limit:
        QApplication.processEvents()
        if not rig.window.busy() and not rig.window.service.busy():
            return True
        time.sleep(0.02)
    return False


def setup_record(flash):
    import chromatic_provision as cp
    return cp.current(cm.span(flash, cm.PROVISION_A), cm.span(flash, cm.PROVISION_B))[0]


def guided_checks(stock, release):
    """Self-guided window: one obvious next step with one primary button,
    plain physical steps, a plug-in check by itself, a one-sentence what-to-do after every
    result, and install carrying Wi-Fi and Jellyfin in the same approved job."""
    import chromatic_manager_core as core
    # The next step for every console state, from the shared guide.
    for summary, consoles, pending, key, words, tone in (
            ({}, 0, False, None, 'Plug in', 'info'), ({}, 2, False, None, 'only one', 'update'),
            ({}, 1, False, 'status', 'Check your Chromatic', 'info'),
            ({'state': 'changed'}, 1, False, 'status', 'again', 'info'),
            ({'state': 'stock'}, 1, False, 'install', 'Install', 'update'),
            ({'state': 'dormant'}, None, False, 'install', 'Install', 'update'),
            ({'state': 'outdated', 'release': '1.1.2'}, 1, False, 'install', 'Update available', 'update'),
            ({'state': 'installed'}, 1, False, None, 'Up to date', 'ok'),
            ({'state': 'damaged'}, 1, False, 'restore', "can't start", 'error'),
            ({'state': 'unknown'}, 1, False, None, 'Nothing to do', 'info'),
            ({'state': 'stock'}, 1, True, 'resume', 'did not finish', 'error')):
        step = core.guide(summary, pending, consoles)
        check(step.key == key and words.lower() in (step.headline + step.button).lower() and step.steps
              and step.tone == tone and (step.button == '') == (key is None) and step.detail,
              f'guide: {summary or "nothing read"}, {consoles} console(s), pending {pending}: {tone}')
        check(all(len(line) <= 140 for line in step.steps) and len(step.headline) <= 30,
              f'guide: short words ({step.headline})')
    check(core.guide({'state': 'outdated', 'release': '1.1.2'}).button == 'Update to 1.1.2',
          'the update button names the version')
    check(core.guide({'state': 'installed', 'launcher': '1.1.2'}).headline == 'Up to date'
          and '1.1.2' in core.guide({'state': 'installed', 'launcher': '1.1.2'}).steps[0], 'installed: up to date')
    # One plain physical instruction while a job runs.
    check(gui.physical_step('Waiting for the console', 'none', False) == 'Plug in your Chromatic and switch it on.'
          and gui.physical_step('x', 'download', False).startswith("Wait. Don't switch off or unplug")
          and 'restarting' in gui.physical_step('x', 'restarting', False)
          and gui.physical_step('x', 'download', True) == 'Answer the question in the window.', 'physical steps')
    # Errors end with one sentence saying what to do.
    for lines, code, want in ((['Stopped before any write: The console did not appear on USB.'], 1, 'Plug in your Chromatic'),
                              (['Another Chromatic tool is using the console.'], 1, 'other Chromatic tool'),
                              (['Refused, nothing written: This is not the console the backups came from.'], 1,
                               'your own Chromatic'),
                              ([], 3, 'Finish it'), ([], 5, 'check that a game plays'),
                              ([core.STOPPED_BY_OWNER], 1, 'needs your OK'),
                              (['Unexpected error (OSError); nothing further was attempted.'], 4, 'Check now')):
        todo = core.what_to_do('install', code, 'running', {}, lines)
        check(want in todo and todo.count('. ') == 0, f'what to do after: {lines or code}')

    # The window: nothing plugged in, then the console appears and is checked by itself.
    plugged, now, updater = {'n': 0}, [1000.0], {'on': False}
    rig = Rig(stock, release, confirms=['yes'], detect=lambda: plugged['n'])
    w = rig.window
    w.clock, w.updater_running = (lambda: now[0]), (lambda: updater['on'])
    started = []
    w.service.listeners.append(lambda event, data: started.append(data['key']) if event == 'started' else None)

    def poll(n, seconds=0.0):
        plugged['n'] = n
        now[0] += seconds
        w.poll_console()
    poll(0)
    check(w.guide_head.text() == 'Plug in your Chromatic' and w.primary.isHidden()
          and 'Connect it to this PC' in w.guide_steps.text() and w.status_card.tone == 'info',
          'nothing plugged in: says to plug it in, no greyed-out button')
    # The console drops off USB and comes straight back by itself (stock starting after the
    # menu, link play, an updater reboot): never a reason to take the port.
    poll(1, 2)
    poll(0, 1.5)
    poll(1, 3)
    check(started == [] and w.guide_head.text() == 'Check your Chromatic' and w.primary.isEnabled()
          and not w.primary.isHidden() and w.primary.text() == 'Check now',
          '1-0-1 quick re-enumeration: not checked by itself, one button')
    # A real plug-in (gone for 10 s or more): checked by itself, fast, once.
    poll(0, 1)
    poll(0, 10.5)
    poll(1, 1)
    check(started == ['status'] and wait_idle(rig) and rig.h.console.full_reads == 0
          and w.summary_state.text() == gui.STATE_NAMES['stock']
          and any('Chromatic found. Checking it now' in line for line in w.lines), 'plugged in: checked by itself, fast')
    check(rig.dialogs.confirms == ['yes'], 'the read-only check needed no approval')
    check(w.guide_head.text() == 'Start-up menu not installed' and w.primary.text() == 'Install'
          and w.primary.isEnabled() and not w.change_setup.isHidden() and w.status_card.tone == 'update'
          and w.primary.property('kind') == 'update', 'stock: one primary button, Install, in the update colour')
    poll(1, 30)
    check(started == ['status'], 'still plugged in: not checked again')
    # Within 90 s of a job of this tool: not by itself, even after a real unplug.
    poll(0, 1)
    poll(0, 12)
    poll(1, 1)
    check(started == ['status'] and w.guide_head.text() == 'Check your Chromatic'
          and any('Press Check now when it is ready' in line for line in w.lines),
          'post-job plug-in: not checked by itself; the panel asks for a check')
    # While ModRetro's updater runs: never by itself.
    updater['on'] = True
    poll(0, 100)
    poll(0, 11)
    poll(1, 1)
    check(started == ['status'], 'updater running: not checked by itself')
    updater['on'] = False
    w.primary.click()  # Check now: one click, no dialog
    check(started == ['status', 'status'] and wait_idle(rig) and rig.dialogs.confirms == ['yes']
          and w.guide_head.text() == 'Start-up menu not installed', 'one click checks it; the guide is current again')
    check(callable(gui.modretro_updater_running) and gui.modretro_updater_running() in (True, False),
          'the updater check reads process names only and answers yes or no')

    # Install from the primary button: one Approve covers the menu and the Wi-Fi/Jellyfin setup
    # (networks from the private file; nothing typed, nothing shown or logged).
    with tempfile.TemporaryDirectory() as folder:
        wifi = Path(folder) / 'wifi.txt'
        wifi.write_text('ssid = GuidedTestNet\npassword = guided-pass-123\nssid = SecondNet\npassword = second-pass-1\n',
                        encoding='utf-8')
        saved_wifi = cm.WIFI_FILE
        cm.WIFI_FILE = wifi
        try:
            loop, done = QEventLoop(), {}
            w.finished.connect(lambda code: (done.setdefault('code', code), loop.quit()))
            w.primary.click()
            QTimer.singleShot(60000, loop.quit)
            loop.exec()
            check(done.get('code') == cm.EXIT_OK and rig.state() == 'installed', 'primary install')
            record = setup_record(rig.h.flash())
            check(record is not None and record.summary()['networks'] == 2, 'Wi-Fi from the private file written in the same job')
            check(rig.dialogs.confirms == [] and rig.dialogs.asked == [], 'one Approve, no questions')
            # The window shows this PC's own LAN address in its one instruction (start the gateway with
            # --bind ...), as Wi-Fi setup always has; the log file and the API carry none of it.
            kept = rig.log_text() + json.dumps(w.service.result()) + json.dumps(w.service.progress_view(400))
            for where, text, secrets in (('log/API', kept, ('GuidedTestNet', 'guided-pass-123', 'second-pass-1',
                                                             '192.0.2.20')),
                                         ('window', w.log.toPlainText(), ('GuidedTestNet', 'guided-pass-123',
                                                                          'second-pass-1'))):
                check(not any(s in text for s in secrets), f'no network name or password (or address) in the {where}')
            check(w.guide_head.text() == 'Up to date' and w.primary.isHidden() and w.status_card.tone == 'ok'
                  and w.summary_state.text() == gui.UP_TO_DATE
                  and w.summary_setup.text().startswith('written by this install'),
                  'then: up to date, in green, with no greyed-out button')
            check('Insert a cartridge' in w.result_todo.text(), 'the result says what to do: insert a cartridge, play')
            check(w.result_head.text() == 'Done' and w.result_card.tone == 'ok' and w.result_fix.isHidden()
                  and w.result_ok.property('kind') == 'ok', 'Done looks like success (green), not an error')
            # Installing again keeps an existing setup; ticking Change sets it up again.
            before = rig.h.flash()
            rig.dialogs.confirms += ['yes', 'yes']
            check(rig.run('install') == cm.EXIT_OK and rig.h.flash() == before, 'existing setup kept (no new sign-in)')
            w.change_setup.setChecked(True)
            rig.dialogs.answers.append('ok')  # the console already has networks: its Wi-Fi list is shown (merge, audit gap 1)
            check(rig.run('install') == cm.EXIT_OK and rig.h.flash() != before
                  and any(p.startswith(cm.WIFI_LIST_PROMPT) for p in rig.dialogs.asked)
                  and setup_record(rig.h.flash()).sequence == record.sequence + 1 and not w.change_setup.isChecked(),
                  'Change ticked: setup written again, one time only')
        finally:
            cm.WIFI_FILE = saved_wifi
    # No Wi-Fi file and nothing typed: the menu is still installed, and the result says what to do.
    rig = Rig(stock, release, confirms=['yes'])
    check(rig.run('install') == cm.EXIT_OK and rig.state() == 'installed' and setup_record(rig.h.flash()) is None,
          'setup not possible: the menu is installed anyway, nothing else written')
    check('Press Wi-Fi' in rig.window.result_todo.text(), 'and the result says to press Wi-Fi')
    # Unplugged: the install refuses before writing and says to plug it in.
    rig = Rig(stock, release, confirms=['yes'])
    rig.h.console.present = False
    check(rig.run('install') == cm.EXIT_REFUSED and 'Plug in your Chromatic' in rig.window.result_todo.text(),
          'console missing: one sentence says to plug it in')
    # Recovery and backup actions are only on the Recovery tab.
    console_tab = {a.key for a in core.ACTIONS if a.tab == 'console'}
    check(console_tab == {'status', 'update', 'wifi', 'jellyfin'}, 'repair and recovery actions are on Fix a problem')


def prompt_checks(release):
    """The Approve dialog labels a program's reason as its words and caps it; an install
    names the version and image it writes."""
    import chromatic_manager_core as core
    text = gui.approve_prompt(core.BY_KEY['install'], core.api_requester('x'), 'Approved by the owner. ' * 20,
                              core.short_release(release))
    said = re.search(r'The AI says: "(.*)"', text).group(1)
    check(len(said) <= core.REASON_MAX and said.endswith('...'), 'a long reason is cut to 120 characters')
    check(f'Start-up menu to install: {release.version} (image {release.fingerprint()[:8]})' in text,
          'install approval names the version and image')
    check('Start-up menu to install' not in gui.approve_prompt(core.BY_KEY['remove'], release='1 (image x)'),
          'other actions do not')
    check(release.fingerprint() == cm.Release.check(release.bootloader, release.table, release.launcher).fingerprint()
          and len(release.fingerprint()) == 64, 'image fingerprint is a SHA-256')
    keep = gui.AiKeepDialog(None, core.api_requester('Claude Code via MCP'), core.BY_KEY['install'], '1.2.0 (image ab)')
    check(keep.off.isDefault() and not keep.keep.isEnabled() and 'Keep allowing it this time?' in
          ' '.join(l.text() for l in keep.findChildren(gui.QLabel)), 'Keep prompt: Turn off is the default; Keep waits')
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    keep.show()
    QTest.keyClick(keep, Qt.Key.Key_Return)
    check(keep.result() == gui.QDialog.DialogCode.Rejected, 'Enter turns it off')


def visible_buttons(widget):
    return [b for b in widget.findChildren(gui.FitButton) if b.isVisibleTo(widget) and b.text()]


def look_checks(stock, release):
    """Owner feedback: button text fits at every window size, the log is collapsed
    by default, details sit behind a toggle, the Wi-Fi questions use plain words and fit."""
    rig = Rig(stock, release)
    w = rig.window
    w.show()
    check(w.log.isHidden() and w.log_toggle.text() == 'Show log' and w.details.isHidden(),
          'log panel and details collapsed by default')
    w.log_toggle.click()
    check(not w.log.isHidden() and w.log_toggle.text() == 'Hide log' and w.service.ui['log_panel_open'],
          'Show log expands it')
    w.log_toggle.click()
    check(w.log.isHidden() and not w.service.ui['log_panel_open'], 'Hide log collapses it')
    states = ({}, {'state': 'stock'}, {'state': 'outdated', 'release': '1.1.12', 'launcher': '1.1.2'},
              {'state': 'installed', 'launcher': '1.1.2', 'release': '1.1.2'}, {'state': 'damaged'})
    misfits = []
    check((w.minimumSize().width(), w.minimumSize().height()) == tuple(gui.WINDOW) and
          (w.width(), w.height()) == tuple(gui.WINDOW) and w.maximumSize().width() > gui.WINDOW[0],
          'opens compact like the official updater, resizable larger, never smaller')
    for log_open in (False, True):
        w.set_log_open(log_open)
        size = (w.width(), w.height())
        check(size == tuple(gui.WINDOW_LOG if log_open else gui.WINDOW), f'window size with the log open: {log_open}')
        for state in states:
            w.service.summary = dict(state)
            w.refresh_guide()
            for tab in ('console', 'recovery', 'settings', 'about'):
                w.show_tab(tab)
                QApplication.processEvents()
                misfits += [(size, tab, b.text()) for b in visible_buttons(w) if not b.text_fits()]
                head = w.status_card.head
                if tab == 'console' and head.width() > 0:
                    misfits += [(size, 'headline', line) for line in head.lines(head.width())
                                if head._image_of(line).width() > head.width()]
    w.set_log_open(False)
    check(not misfits, f'every button\'s text and every headline fits, log open or closed {misfits[:3]}')
    # Each tab has its own colour, and the console drawing shows the status.
    w.service.summary = {'state': 'installed', 'launcher': '1.1.2', 'release': '1.1.2'}
    w.refresh_guide()
    w.show_tab('console')
    check(w.console_art.tone == 'ok' and w.title.text() == 'START-UP MENU', 'up to date: a tick on the console')
    w.service.summary = {'state': 'outdated', 'launcher': '1.1.1', 'release': '1.1.2'}
    w.refresh_guide()
    check(w.console_art.tone == 'update', 'update available: an arrow on the console')
    w.service.summary = {'state': 'damaged'}
    w.refresh_guide()
    check(w.console_art.tone == 'error', 'a problem: a warning sign on the console')
    check(len({theme.PALETTE[c] for c in theme.TAB_COLOURS.values()}) == len(w.tabs) == 5 and all(
        w.tabs[k].tab_colour == theme.TAB_COLOURS[k] for k in w.tabs), 'five tabs (Remote Pilot added), five colours')
    w.show_tab('recovery')
    check(w.title.text() == 'FIX A PROBLEM' and w.tabs['recovery'].isChecked(), 'the band names the tab')
    w.show_tab('console')
    image = w.grab().toImage()
    border = w.body.geometry().topLeft()
    check(image.pixelColor(border.x() + 1, border.y() + 40).name() == theme.PALETTE['tab_home'],
          'the frame is drawn in the tab\'s colour')
    # A long label in a narrow button shrinks (or wraps) rather than being cut off.
    narrow = gui.FitButton('New Jellyfin sign-in')
    narrow.resize(90, 40)
    font, text = narrow.layout_text()
    check(narrow.text_fits() and (font.pointSizeF() < narrow.base_pt or '\n' in text), 'a narrow button shrinks its text')
    # Owner: the whole header band drags the window, and it resizes like a normal window.
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt as _Qt
    from PySide6.QtGui import QMouseEvent
    fw = gui.MainWindow(cm, factory(Harness(stock, release)), lambda: None, Scripted(), root=ROOT, frameless=True)
    fw.show()
    QApplication.processEvents()
    start = fw.pos()
    inside = QPointF(fw.title.width() / 2, fw.title.height() / 2)

    def mouse(kind, target, local, buttons):
        glob = QPointF(target.mapToGlobal(local.toPoint()))
        button = _Qt.MouseButton.LeftButton if kind != QEvent.Type.MouseMove else _Qt.MouseButton.NoButton
        QApplication.sendEvent(target, QMouseEvent(kind, local, glob, button, buttons, _Qt.KeyboardModifier.NoModifier))
    mouse(QEvent.Type.MouseButtonPress, fw.title, inside, _Qt.MouseButton.LeftButton)
    mouse(QEvent.Type.MouseMove, fw.title, inside + QPointF(40, 25), _Qt.MouseButton.LeftButton)
    mouse(QEvent.Type.MouseButtonRelease, fw.title, inside + QPointF(40, 25), _Qt.MouseButton.NoButton)
    QApplication.processEvents()
    check(fw.pos() - start == QPoint(40, 25), 'dragging the header band moves the window')
    check(fw.minimumSize().width() == gui.WINDOW[0] and fw.maximumSize().width() > gui.WINDOW[0] * 2,
          'the window can be resized larger, never below its compact size')
    check(fw.resize_edges(QPoint(2, fw.height() // 2)) == _Qt.Edge.LeftEdge
          and fw.resize_edges(QPoint(fw.width() - 2, fw.height() - 2)) == (_Qt.Edge.RightEdge | _Qt.Edge.BottomEdge)
          and fw.resize_edges(QPoint(fw.width() // 2, fw.height() // 2)) == _Qt.Edge(0)
          and fw.edge_cursor(_Qt.Edge.RightEdge | _Qt.Edge.BottomEdge) == _Qt.CursorShape.SizeFDiagCursor
          and fw.edge_cursor(_Qt.Edge.LeftEdge | _Qt.Edge.BottomEdge) == _Qt.CursorShape.SizeBDiagCursor,
          'edges and corners resize, with the usual cursors')
    fw.resize(gui.WINDOW[0] + 300, gui.WINDOW[1] + 120)
    QApplication.processEvents()
    fw.set_log_open(True)
    QApplication.processEvents()
    fw.set_log_open(False)
    QApplication.processEvents()
    check(fw.width() == gui.WINDOW[0] + 300 and fw.height() >= gui.WINDOW[1], 'opening the log keeps the size the owner chose')
    for button in fw.findChildren(gui.FitButton):
        if button.isVisible() and not button.text_fits():
            check(False, f'button text fits in a larger window: {button.text()!r}')
    fw.close()
    w.hide()
    # The Wi-Fi questions in plain words, inside the window.
    for prompt, want in (('Use this PC at 192.0.2.20 for Jellyfin? Type yes: ', 'Use this PC (192.0.2.20)'),
                         ("This PC's LAN address (for example 192.0.2.20): ", 'home network'),
                         ('Wi-Fi network name to add or change (2.4 GHz). Leave it empty to keep the console\'s '
                          'networks and only change their order or remove some: ', 'The Wi-Fi network to add'),
                         ('Wi-Fi password (hidden; leave empty for an open network): ', 'Your Wi-Fi password')):
        title, text = gui.plain_prompt(prompt)
        check(title == 'Wi-Fi setup' and want in text and 'LAN' not in text and 'Type yes' not in text,
              f'plain Wi-Fi wording: {want}')
    w.show()
    for prompt, secret in (('Wi-Fi password (hidden; leave empty for an open network): ', True),
                           ('Use this PC at 192.0.2.20 for Jellyfin? Type yes: ', False)):
        dialog = gui.AskDialog(w, prompt, secret)
        dialog.show()
        QApplication.processEvents()
        inside = w.frameGeometry().contains(dialog.frameGeometry())
        check(dialog.width() <= w.width() - 40 and inside and all(b.text_fits() for b in visible_buttons(dialog)),
              f'the Wi-Fi dialog fits inside the smallest window ({"password" if secret else "yes/no"})')
        check(dialog.field.isHidden() != secret, 'a password gets a hidden field; yes/no gets buttons only')
        dialog.close()
    w.hide()


SOUND_IMAGE = 'sound build 18.40 (image 1a2b3c4d)'  # a made-up short name; the synthetic chip gets its own image


def fpga_factory(fr, session=None, watch=None, own_sign=False):
    """The window's console chip jobs on the synthetic chip and JTAG bridge of test_chromatic_fpga
    (temporary folder, synthetic images), speaking through the manager like the real ones.
    `watch` (OwnerWatch): its loader, and it hears what the job asks the owner to do. `own_sign`:
    the chip jobs get their own owner-action object instead of the manager's (the service must
    then still show one sign, never a question over what the job already asks)."""
    import chromatic_fpga as cf
    import test_chromatic_fpga as tf

    def make(manager, approved=None):
        clock = [0.0]

        def tick():
            clock[0] += 1
            return clock[0]
        loader = watch.loader if watch is not None else (lambda: tf.FakeLoader(fr.chip))
        jobs = cf.FpgaJobs(folder=fr.work, loader_factory=loader, backup_loader=fr.backup,
                           media_loader=fr.media, timing=fr.timing, say=manager.say, sleep=lambda s: None, clock=tick,
                           session=session or fr.session, stop_point=manager.stop_point, link_loader=fr.link,
                           owner=None if own_sign else getattr(manager, 'owner', None))  # as core.fpga_jobs
        jobs.approved_media = approved
        if watch is not None:
            jobs.owner.listen(watch.on_owner)
        return jobs
    return make


def sound_window(rig, fr, session='run-a', fpga_session=None, guide_path=None):
    """A window on `rig`'s simulated console and `fr`'s synthetic console chip. `session` is the
    Manager run (a new one stands for closing and reopening it)."""
    w = gui.MainWindow(cm, factory(rig.h), lambda: DeviceLock(rig.lock_path), rig.dialogs, root=ROOT, frameless=False,
                       log_folder=rig.logs, settings_path=rig.settings, hardware_gate=lambda: None,
                       guide_path=guide_path or rig.h.folder / 'sound-guide.json', guide_session=session,
                       sound_image=lambda: SOUND_IMAGE, link_image=lambda: None)
    w.service.release_peek = lambda: rig.h.release
    w.service.usage_note = lambda: USAGE
    w.service.fpga_factory = fpga_factory(fr, fpga_session)
    return w


def press_guide(w, n, seconds=60):
    """Press the n-th answer button on the guide screen (or 'stop'); wait for the job it starts.
    Returns the job's exit code, or None when no job ran."""
    loop, result = QEventLoop(), {}

    def done(code):
        result['code'] = code
        loop.quit()
    w.finished.connect(done)
    try:
        target = w.guide_stop if n == 'stop' else w.guide_buttons[n]
        check(not target.isHidden() and target.isEnabled(), f'guide: button {n} is there to press ({target.text()})')
        target.click()
        if w.busy():
            QTimer.singleShot(seconds * 1000, loop.quit)
            loop.exec()
            check('code' in result and not w.busy(), 'guide job finished in time')
    finally:
        w.finished.disconnect(done)
    return result.get('code')


def guide_fits(w, where):
    """Every visible guide button's text fits, the headline fits, and the screen fits the bubble
    (at the window's compact size, the smallest it can be)."""
    w.resize(*gui.WINDOW)
    w.show()
    QApplication.processEvents()
    page = w.pages['guide']
    misfits = [b.text() for b in visible_buttons(page) if not b.text_fits()]
    head = w.guide_card.head
    misfits += [line for line in head.lines(head.width()) if head._image_of(line).width() > head.width()]
    check(not misfits, f'guide {where}: every button and headline fits {misfits[:3]}')
    check(page.minimumSizeHint().height() <= w.stack.height() + 2,
          f'guide {where}: the screen fits the speech bubble ({page.minimumSizeHint().height()} > {w.stack.height()})')


def pump(condition, seconds=60):
    """Let the window's event loop run until `condition()` holds."""
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        loop = QEventLoop()
        QTimer.singleShot(20, loop.quit)
        loop.exec()
    return condition()


def quiet(w, seconds=120):
    """Wait until the one-step update has nothing more to start by itself: no job runs and no next
    job is queued (press_guide returns when the first job ends; the next one starts from a timer)."""
    return pump(lambda: not w.busy() and not w.service.busy() and w.link_update.auto is None, seconds)


class Approvals(Scripted):
    """Scripted Approve dialogs that also record which job each one was shown for, in order."""

    def __init__(self, confirms=(), answers=(), ai=(), keep=()):
        super().__init__(confirms, answers, ai, keep)
        self.shown = []

    def confirm(self, action, requested_by=None, reason=''):
        self.shown.append(action.key)
        return super().confirm(action, requested_by, reason)


class KeepApprovals(Approvals):
    """Approvals that also answer the one-step update's own Approve, the one asked with the new
    image running (the real window: Dialogs.ask, a dialog with Approve and Not right). `keeps` in
    order: True for Approve, False for Not right, or a function of the prompt giving either; none
    left is Not right. `kept` records each prompt asked."""

    def __init__(self, confirms=(), keeps=(), answers=()):
        super().__init__(confirms, answers)
        self.keeps, self.kept = list(keeps), []

    def ask(self, prompt, secret):
        import chromatic_fpga as cf
        if not secret and str(prompt).startswith(cf.LINK_KEEP_PROMPT):
            self.kept.append(str(prompt))
            answer = self.keeps.pop(0) if self.keeps else False
            if callable(answer):
                answer = answer(prompt)
            return cf.KEEP_ANSWER if answer else ''
        return super().ask(prompt, secret)


class OwnerWatch:
    """Holds a console chip job at the moment it waits for the owner (a watched power cycle), so
    the window's sign can be looked at while the job really waits; then lets it go on, with the
    power cycle done or not. `quiet_after_write`: the start-up menu is not heard after the flash
    write (what makes the one-step update ask for the power cycle)."""

    def __init__(self, fr, quiet_after_write=True):
        import test_chromatic_fpga as tf
        self.fr, self.quiet = fr, quiet_after_write
        self.go, self.waiting, self.armed = threading.Event(), threading.Event(), False
        watch = self

        class Loader(tf.FakeLoader):
            def write(self, source):
                super().write(source)
                if watch.quiet:
                    self.c.reported = None

            def present(self):
                if watch.armed and not watch.go.is_set():
                    watch.waiting.set()
                    watch.go.wait(60)
                return super().present()
        self.loader = lambda: Loader(fr.chip)

    def on_owner(self, event):
        import chromatic_fpga as cf
        if event['state'] == 'waiting' and event['title'] == cf.POWER_CYCLE_TITLE:  # not the Approve with the image running
            self.armed = True

    def release(self, power_cycle):
        """The owner did it (the console goes off and on, and reports again), or did not."""
        if power_cycle:
            self.fr.chip.reported = ...
            self.fr.chip.power_cycle()
        self.go.set()


def owner_fits(w, where):
    """The owner-action sign fits the speech bubble at the window's compact size: its buttons, its
    big pixel headline and its whole instruction."""
    QApplication.processEvents()
    page, banner = w.pages['owner'], w.owner_banner
    misfits = [b.text() for b in visible_buttons(page) if not b.text_fits()]
    head = banner.head
    misfits += [line for line in head.lines(head.width()) if head._image_of(line).width() > head.width()]
    check(not misfits, f'owner sign {where}: every button and the headline fit {misfits[:3]}')
    check(page.minimumSizeHint().height() <= w.stack.height() + 2,
          f'owner sign {where}: fits the speech bubble ({page.minimumSizeHint().height()} > {w.stack.height()})')
    check(banner.text.isHidden() or banner.text.height() >= banner.text.heightForWidth(banner.text.width()),
          f'owner sign {where}: the whole instruction shows')


def sound_guide_checks(stock, release):
    """The one-button guided Jellyfin sound setup: the console chip jobs in
    order, each with its own Approve and every check inside it, with the owner's answers about
    what the console shows between them; every "Something is wrong" stops; Stop works on every
    screen; a Manager restart resumes safely; a refusal when Check does not find the original."""
    import chromatic_fpga as cf
    import chromatic_manager_core as core
    import test_chromatic_fpga as tf

    # What the guide reads from the jobs is still what they say (chromatic_fpga.py is not changed
    # here): run the real jobs on the synthetic chip and look for each phrase in their messages.
    said = []
    probe = tf.Rig()
    jobs = probe.jobs()
    jobs.say = said.append
    jobs.check()
    jobs.media_loader().expected(tf.ORIGINAL)
    jobs.write_media()                       # before any rehearsal: refused
    jobs.restore_original()
    jobs.try_media()
    tf.FakeLoader(probe.chip).reset()
    jobs.write_media()
    jobs.write_media()                       # already there
    jobs.check()                             # the sound image
    probe.chip.flash = bytearray(tf.synthetic_bitstream(9) + b'\xff' * (tf.SIZE - 900_000))
    jobs.check()                             # neither
    text = '\n'.join(said)
    for phrase in (core.SAID_ORIGINAL, core.SAID_SOUND, core.SAID_NEITHER, core.SAID_REHEARSAL, core.SAID_TRIAL,
                   core.SAID_SOUND_WRITTEN, core.SAID_SOUND_THERE, core.SAID_NO_REHEARSAL):
        check(phrase in text, f'the guide follows the job wording: {phrase!r}')
    check(all(ch in theme.PIXEL_GLYPHS or ch == ' ' for ch in gui.MainWindow.GUIDE_TITLE), 'guide title in pixel letters')
    check(all(core.SOUND_MINUTES[s] > 0 for s in range(1, 6)) and sum(core.SOUND_MINUTES.values()) == 15,
          'every step has a time; about 15 minutes in all')
    for screen in core.GUIDE_SCREENS.values():
        check(len(screen.headline) <= 30 and screen.todo and screen.buttons, f'guide screen {screen.key}: short, says what to do')
        check(all(b[2] in ('stop', 'close') or b[2].startswith('job:') or b[2] in (
            'game_ok', 'game_wrong', 'trial_ok', 'trial_wrong', 'final_ok', 'final_wrong') for b in screen.buttons),
              f'guide screen {screen.key}: every button is a known answer')
        check(screen.step == 0 or any(b[2] == 'stop' for b in screen.buttons) or screen.key == 'final_wrong',
              f'guide screen {screen.key}: Stop setup is offered')
    for reason, (head, _, _, _, _) in core.GUIDE_STOPS.items():
        check(len(head) <= 30, f'stop screen {reason}: short headline')

    # The headline after console chip jobs says what was written (owner: it said "THE CONSOLE WAS
    # NOT TOUCHED" after the rehearsal and the sound write).
    restore_lines = ['The flash already holds your original image: this write is the rehearsal (same bytes, same '
                     'command, same checks as a real rescue).', 'Original console chip image written back (backup of '
                     'x), all 4 MiB read back and identical, console restarted. Rehearsal recorded for this session.']
    for key, code, lines, head, words in (
            ('fpga_restore', 0, restore_lines, 'Original FPGA image written back', 'was written back and checked'),
            ('fpga_media', 0, ['sound build 18.40 (image 1a2b3c4d) installed: all 4 MiB read back and identical to '
                               'the expected flash, console restarted.'], 'Sound image written and checked',
             'written and checked'),
            ('fpga_media', 0, ['The flash already holds this sound image. Nothing written.'], 'Sound image already there',
             'Nothing was written'),
            ('fpga_check', 0, ['Console chip: GW5A-25 confirmed. Its flash holds your original image (the backup of x), '
                               'byte for byte. Nothing was written.'], 'Checked. Nothing was written', 'original'),
            ('fpga_trial', 0, ['sound build is running from the console chip\'s RAM'], 'Sound image running from RAM',
             'Nothing was written'),
            ('fpga_restore', 5, restore_lines, 'Original written, not seen restarting', 'switch it off and on'),
            ('fpga_check', 5, [], 'Checked, not seen restarting', 'Nothing was written'),
            ('fpga_media', 1, ['Refused, nothing written: x'], 'Nothing was changed', 'Nothing was written'),
            ('fpga_restore', 2, [], 'Fixed: the cut-off job was put right', 'written back and checked'),
            ('resume', 2, ['Finishing the unfinished console chip job.'], 'Fixed: the cut-off job was put right',
             'console chip')):
        got_head, got_next = core.fpga_result(key, code, lines)
        check(core.fpga_job(key, lines) and got_head == head and words in got_next and 'not touched' not in got_next,
              f'FPGA result headline: {key} exit {code} -> {got_head}')
    check(not core.fpga_job('resume', ['Finishing the unfinished job.']) and core.what_next('status', 1, 'none', {}, [])
          == 'The console was not touched.', 'ESP32 jobs keep their own wording')

    # Home: the button shows only on an up-to-date console with a built sound image.
    fr = tf.Rig()
    rig = Rig(stock, release, confirms=['yes'] * 4)
    w = sound_window(rig, fr)
    w.show()
    installed = {'state': 'installed', 'launcher': release.version, 'release': release.version, 'fpga': '18.14'}
    w.service.summary = dict(installed)
    w.refresh_guide()
    check(not w.primary.isHidden() and w.primary.text() == gui.MainWindow.SOUND_BUTTON and w.primary.isEnabled()
          and w.guide_head.text() == 'Up to date', 'Home: up to date on the stock chip image offers Set up Jellyfin '
                                                   'sound as its one button')
    check(not w.fix_sound.isHidden() and w.fix_sound.text() == gui.MainWindow.SOUND_BUTTON and
          'fix_sound' not in w.buttons, 'Fix a problem has the same button too')
    bare = Rig(stock, release)
    bare.window.service.summary = dict(installed)
    bare.window.refresh_guide()
    check(bare.window.primary.isHidden(), 'no sound image built: Home keeps no button')
    bare.window.service.summary = {'state': 'outdated', 'launcher': '1.1.0', 'release': release.version}
    bare.window.refresh_guide()
    check(bare.window.primary.text().startswith('Update to'), 'an update still comes first')

    # The happy path: five steps, each job approved once, the flash only written by the rehearsal
    # (same bytes) and the sound write.
    w.primary.click()
    check(w.stack.currentWidget() is w.pages['guide'] and w.sound.screen == 'check'
          and w.guide_step.text() == 'STEP 1 OF 5' and '15 min left' in w.guide_time.text()
          and w.title.text() == gui.MainWindow.GUIDE_TITLE and not any(t.isEnabled() for t in w.tabs.values())
          and not w.guide_stop.isHidden(), 'Set up Jellyfin sound: step 1 of 5, with times and Stop setup')
    check(rig.dialogs.confirms == ['yes'] * 4 and fr.chip.reads == 0, 'opening the guide runs nothing')
    guide_fits(w, 'step 1')
    shown = {}

    def screen_of():
        shown[w.sound.screen] = (w.guide_card.head.text(), w.guide_todo.text(),
                                 [b.text() for b in w.guide_buttons if not b.isHidden()])
        return w.sound.screen
    screen_of()
    check(press_guide(w, 0) == cf.EXIT_OK and screen_of() == 'rehearse' and fr.chip.writes == 0
          and len(rig.dialogs.confirms) == 3, 'step 1: Check FPGA (approved), original found, nothing written')
    check('stays open' in ' '.join(w.sound.view().lines) and w.guide_step.text() == 'STEP 2 OF 5',
          'step 2 says the rehearsal only counts while the app stays open')
    guide_fits(w, 'step 2')
    check(press_guide(w, 0) == cf.EXIT_OK and screen_of() == 'game_test' and fr.chip.writes == 1
          and fr.flash() == tf.ORIGINAL and len(rig.dialogs.confirms) == 2, 'step 2: the rehearsal, approved and credited')
    result = w.service.result()
    check(result['headline'] == 'Original FPGA image written back' and 'written back and checked' in result['next']
          and 'not touched' not in result['next'].lower(), 'the rehearsal\'s result says what was written')
    check(w.guide_buttons[0].text() == 'Game looks and sounds normal' and w.guide_buttons[1].text() == 'Something is wrong'
          and 'off, then on' in w.guide_todo.text() and 'play' in w.guide_todo.text(),
          'then: power-cycle and play a game, two answers')
    guide_fits(w, 'game test')
    check(press_guide(w, 0) is None and screen_of() == 'trial' and SOUND_IMAGE in w.guide_card.sub.text(),
          'game normal: step 3 names the image to try')
    check(w.console_art.tone == 'sound', 'the console drawing shows the step (sound)')
    check(press_guide(w, 0) == cf.EXIT_OK and screen_of() == 'trial_look' and fr.chip.sram_loads == 1
          and fr.chip.writes == 1 and fr.flash() == tf.ORIGINAL, 'step 3: the trial from RAM only, nothing written')
    check('start-up menu clear' in w.guide_todo.text() and 'sound' in w.guide_todo.text()
          and 'Games' in w.guide_todo.text() and w.guide_buttons[0].text() == 'All good', 'look: menu, sound, games')
    guide_fits(w, 'trial look')
    check(press_guide(w, 0) is None and screen_of() == 'power_cycle' and SOUND_IMAGE in w.guide_card.sub.text()
          and 'off, then on' in w.guide_todo.text() and w.console_art.tone == 'power',
          'step 4: switch off and on, with the image fingerprint shown')
    tf.FakeLoader(fr.chip).reset()  # the owner switches it off and on: the flash image runs again
    check(press_guide(w, 0) == cf.EXIT_OK and screen_of() == 'final' and fr.flash() == fr.expected_media()
          and rig.dialogs.confirms == [], 'step 4: FPGA sound with its own Approve, verified')
    result = w.service.result()
    check(result['headline'] == 'Sound image written and checked' and 'not touched' not in result['next'].lower(),
          'the sound write\'s result says what was written')
    check('Games' in w.guide_todo.text() and 'Jellyfin' in w.guide_todo.text() and 'Menu' in w.guide_todo.text(),
          'step 5: game, Jellyfin with sound, Menu button')
    guide_fits(w, 'final checks')
    check(press_guide(w, 0) is None and screen_of() == 'done' and w.guide_card.tone == 'ok', 'All good: done, in green')
    check(not (rig.h.folder / 'sound-guide.json').exists(), 'done: no progress file left')
    guide_fits(w, 'done')
    check(press_guide(w, 0) is None and not w.sound.active and w.stack.currentWidget() is w.pages['console']
          and all(t.isEnabled() for t in w.tabs.values()), 'OK: back to Home')
    check(rig.h.console.writes == 0 and rig.h.flash() == stock, 'the ESP32 was never written')
    # The same jobs from the Fix a problem buttons: the result card says what was written.
    rig.dialogs.confirms.append('yes')
    loop, done = QEventLoop(), {}
    w.finished.connect(lambda code: (done.setdefault('code', code), loop.quit()))
    w.buttons['fpga_restore'].click()
    QTimer.singleShot(60000, loop.quit)
    loop.exec()
    check(done.get('code') == cf.EXIT_OK and fr.flash() == tf.ORIGINAL and w.stack.currentWidget() is w.pages['result']
          and w.result_head.text() == 'Original FPGA image written back'
          and 'written back and checked' in w.result_next.text() and 'not touched' not in w.result_next.text().lower()
          and w.result_card.tone == 'ok', 'Restore FPGA (the undo) from Fix a problem: the headline says what was written')
    w.hide()

    # Check FPGA not original: stop with plain advice, nothing written, no write offered.
    for flash, reason, offer in ((bytes(tf.synthetic_bitstream(9)) + b'\xff' * (tf.SIZE - 900_000), 'unknown', None),
                                 (None, 'already_sound', 'Restore FPGA')):
        fr2 = tf.Rig()
        if flash is None:
            fr2.chip.flash = bytearray(fr2.expected_media())
        else:
            fr2.chip.flash = bytearray(flash)
        before = fr2.flash()
        rig2 = Rig(stock, release, confirms=['yes'])
        w2 = sound_window(rig2, fr2)
        w2.open_sound_guide()
        check(press_guide(w2, 0) == cf.EXIT_OK and w2.sound.screen == 'stopped' and w2.sound.reason == reason
              and fr2.chip.writes == 0 and fr2.flash() == before, f'check finds {reason}: the guide stops, nothing written')
        texts = [b.text() for b in w2.guide_buttons if not b.isHidden()]
        check(texts == ([offer] if offer else []) + ['Close'] and w2.guide_stop.isHidden(),
              f'{reason}: plain advice, {"offers " + offer if offer else "offers no write"}')
        shown[f'stop-{reason}'] = True
        if reason == 'unknown':
            check("Don't go on" in w2.guide_todo.text() and w2.guide_card.tone == 'error', 'unknown image: do not go on')

    # "Something is wrong" after the rehearsal: stop, offer Restore FPGA in one click (with its Approve).
    fr3 = tf.Rig()
    rig3 = Rig(stock, release, confirms=['yes'] * 3)
    w3 = sound_window(rig3, fr3)
    w3.open_sound_guide()
    press_guide(w3, 0)
    press_guide(w3, 0)
    check(w3.sound.screen == 'game_test' and press_guide(w3, 1) is None and w3.sound.reason == 'game_wrong'
          and w3.guide_buttons[0].text() == 'Restore FPGA', 'game wrong: stop, Restore FPGA offered')
    guide_fits(w3, 'game wrong')
    check(press_guide(w3, 0) == cf.EXIT_OK and w3.sound.screen == 'restored' and fr3.chip.writes == 2
          and fr3.flash() == tf.ORIGINAL and rig3.dialogs.confirms == [], 'Restore FPGA from the stop screen, approved')
    # Cancel on an Approve: the screen stays, nothing runs.
    rig3.dialogs.confirms.append('no')
    w3.open_sound_guide()
    reads = fr3.chip.reads
    check(press_guide(w3, 0) is None and w3.sound.screen == 'check' and 'cancelled' in w3.guide_card.sub.text()
          and fr3.chip.reads == reads, 'Cancel on the Approve: nothing runs, the screen says so')
    check(press_guide(w3, 'stop') is None and w3.sound.reason == 'cancelled' and press_guide(w3, 0) is None
          and not w3.sound.active, 'Stop setup on step 1, then Close')

    # "Something is wrong" in the trial: power off and on, nothing was written, stop.
    fr4 = tf.Rig()
    rig4 = Rig(stock, release, confirms=['yes'] * 3)
    w4 = sound_window(rig4, fr4)
    w4.open_sound_guide()
    for n in (0, 0, 0, 0):
        press_guide(w4, n)
    check(w4.sound.screen == 'trial_look' and press_guide(w4, 1) is None and w4.sound.reason == 'trial_wrong'
          and 'off and on' in w4.guide_todo.text() and fr4.chip.writes == 1 and fr4.flash() == tf.ORIGINAL
          and [b.text() for b in w4.guide_buttons if not b.isHidden()] == ['Close'],
          'trial wrong: switch off and on, nothing written, stop')
    guide_fits(w4, 'trial wrong')

    # Stop mid-way on every screen between jobs: nothing more runs; after the trial it says to power-cycle.
    for screen in ('rehearse', 'game_test', 'trial', 'trial_look', 'power_cycle', 'final'):
        w4.sound.screen, w4.sound.reason, w4.sound.note = screen, None, ''
        w4.show_guide()
        writes, reads, loads = fr4.chip.writes, fr4.chip.reads, fr4.chip.sram_loads
        check(press_guide(w4, 'stop') is None and w4.sound.reason == 'cancelled' and not w4.busy()
              and (fr4.chip.writes, fr4.chip.reads, fr4.chip.sram_loads) == (writes, reads, loads)
              and (('off and on' in w4.guide_todo.text()) == (screen in core.TRIAL_SCREENS))
              and not (rig4.h.folder / 'sound-guide.json').exists(), f'Stop setup on {screen}: nothing more runs')
    press_guide(w4, 0)

    # "Something is wrong" in the final checks: Restore FPGA in one click.
    fr5 = tf.Rig()
    rig5 = Rig(stock, release, confirms=['yes'] * 5)
    w5 = sound_window(rig5, fr5)
    w5.open_sound_guide()
    for n in (0, 0, 0, 0, 0, 0):
        press_guide(w5, n)
    check(w5.sound.screen == 'final' and fr5.flash() == fr5.expected_media(), 'sound written')
    check(press_guide(w5, 1) is None and w5.sound.screen == 'final_wrong' and w5.guide_buttons[0].text() == 'Restore FPGA',
          'final wrong: Restore FPGA offered')
    shown['final_wrong'] = True
    guide_fits(w5, 'final wrong')
    check(press_guide(w5, 0) == cf.EXIT_OK and w5.sound.screen == 'restored' and fr5.flash() == tf.ORIGINAL
          and rig5.dialogs.confirms == [], 'Restore FPGA after the final checks: the original back, approved')

    # A program cannot start anything while the guide is open, and no status check runs by itself.
    fr6 = tf.Rig()
    rig6 = Rig(stock, release, confirms=['yes'] * 4)
    w6 = sound_window(rig6, fr6)
    w6.open_sound_guide()
    w6.service.request_action('fpga_media', 'Claude Code via MCP', 'sound please')
    QApplication.processEvents()
    check(w6.service.request['state'] == 'declined' and not w6.busy() and fr6.chip.reads == 0
          and 'guided sound setup' in w6.service.request['outcome'],
          'a program\'s request is declined while the guide is open')
    w6.consoles, w6._plug_armed, w6.detect = 0, True, lambda: 1
    w6.poll_console()
    check(not w6.busy() and any('sound setup goes on' in line for line in w6.lines), 'no status check by itself')

    # Closing and reopening the Manager mid-way: the rehearsal is per run, so it starts again at step 1.
    for n in (0, 0, 0, 0, 0):
        press_guide(w6, n)
    check(w6.sound.screen == 'power_cycle' and (rig6.h.folder / 'sound-guide.json').exists(), 'progress saved')
    w6.close()
    reopened = sound_window(rig6, fr6, session='run-b', fpga_session='session-b')
    check(reopened.sound.screen == 'resume' and reopened.stack.currentWidget() is reopened.pages['guide']
          and 'step 1' in reopened.guide_card.sub.text() and 'only counts' in reopened.guide_card.sub.text()
          and 'off and on first' in reopened.guide_todo.text() and reopened.guide_buttons[0].text() == 'Start again',
          'reopened: says the rehearsal was per run, power-cycle the trial away, start again')
    shown['resume'] = True
    guide_fits(reopened, 'resume')
    check(press_guide(reopened, 0) is None and reopened.sound.screen == 'check', 'Start again: step 1')
    # The job's own check would refuse the write anyway (this run has no rehearsal).
    reopened.sound.screen = 'power_cycle'
    reopened.show_guide()
    tf.FakeLoader(fr6.chip).reset()
    writes = fr6.chip.writes
    check(press_guide(reopened, 0) == cf.EXIT_REFUSED and reopened.sound.reason == 'rehearsal_lost'
          and fr6.chip.writes == writes and fr6.flash() == tf.ORIGINAL
          and reopened.guide_buttons[0].text() == 'Start again', 'a write without this run\'s rehearsal is refused')
    # Reopened after the sound write: only the final checks are left.
    rig6.dialogs.confirms += ['yes'] * 4
    press_guide(reopened, 0)
    for n in (0, 0, 0, 0, 0, 0):
        press_guide(reopened, n)
    check(reopened.sound.screen == 'final' and fr6.flash() == fr6.expected_media(), 'reopened run: sound written')
    reopened.close()
    third = sound_window(rig6, fr6, session='run-c', fpga_session='session-c')
    check(third.sound.screen == 'resume' and third.sound.sound_written and 'final checks' in third.guide_card.sub.text()
          and third.guide_buttons[0].text() == 'Continue', 'reopened after the write: only the final checks are left')
    check(press_guide(third, 0) is None and third.sound.screen == 'final', 'Continue: the final checks')
    third.close()
    third2 = sound_window(rig6, fr6, session='run-c', fpga_session='session-c')
    check(not third2.sound.active, 'the same run does not resume its own screen twice')
    for key in ('check', 'rehearse', 'game_test', 'trial', 'trial_look', 'power_cycle', 'final', 'done',
                'stop-unknown', 'stop-already_sound', 'final_wrong', 'resume'):
        check(key in shown, f'guide screen visited: {key}')
    for window in (w2, w3, w4, w5, reopened, third, third2):
        window.close()


def home_sound_checks(stock, release, installed):
    """Owner (high priority): Home knows the sound image is already there. A normal
    Check now on the simulated console reads the FPGA version line the start-up menu prints at
    start (no JTAG read) and shows the four states: sound on (status only), older sound image
    (Update sound), stock image (Set up Jellyfin sound), unknown image (a warning, no button)."""
    import chromatic_fpga as cf
    import test_chromatic_fpga as tf
    newer = 'sound build 18.41 (image 5e6f7a8b)'
    cases = (  # (launcher's line, built image, button, sound line starts with, console tone, preview name)
        ('18.40 debug=1', SOUND_IMAGE, None, 'Sound: on', 'sound', 'sound-installed'),
        ('18.40 debug=1', newer, gui.MainWindow.UPDATE_SOUND_BUTTON, 'Sound: older image', 'update', 'sound-outdated'),
        ('18.14 debug=0', SOUND_IMAGE, gui.MainWindow.SOUND_BUTTON, 'Sound: not set up', 'ok', 'sound-none'),
        ('18.20 debug=0', SOUND_IMAGE, None, 'Unknown chip image', 'ok', 'sound-unknown'),
        ('18.41 debug=1', SOUND_IMAGE, None, 'Sound: on  |  Pocket link: ready', 'sound', 'sound-link'),
        ('none', SOUND_IMAGE, None, 'Unknown chip image', 'ok', None),
        (None, SOUND_IMAGE, None, 'Sound: not checked', 'ok', None))
    windows = []
    for fpga, built, button, line, tone, _ in cases:
        rig = Rig(installed, release, stock)
        rig.h.console.fpga = fpga
        w = sound_window(rig, tf.Rig())
        w.sound_image, w._sound_image_text = (lambda b=built: b), ...
        w.resize(*gui.WINDOW)
        w.show()
        windows.append(w)
        rig.window.close()
        writes, flash = rig.h.console.writes, rig.h.flash()
        loop, done = QEventLoop(), {}
        w.finished.connect(lambda code: (done.setdefault('code', code), loop.quit()))
        w.buttons['status'].click()
        QTimer.singleShot(60000, loop.quit)
        loop.exec()
        w.show_tab('console')
        QApplication.processEvents()
        check(done.get('code') == cm.EXIT_OK and rig.h.console.writes == writes and rig.h.flash() == flash,
              f'FPGA {fpga!r}: the normal check, nothing written')
        check(w.guide_head.text() == 'Up to date' and w.sound_line.isVisible() and w.sound_line.text().startswith(line)
              and w.sound_line.toolTip(), f'FPGA {fpga!r}: Home says {line!r}, details in its tooltip')
        if button:
            check(w.primary.isVisible() and w.primary.text() == button and w.primary.isEnabled() and w.primary.text_fits(),
                  f'FPGA {fpga!r}: the one button is {button}')
        else:
            check(w.primary.isHidden(), f'FPGA {fpga!r}: no setup button')
        check(w.console_art.tone == tone, f'FPGA {fpga!r}: the console drawing shows {tone}')
        if line.startswith('Unknown'):
            check('Check FPGA' in w.sound_line.text() + w.sound_line.toolTip() and w.sound_line.property('role') == 'warn',
                  f'FPGA {fpga!r}: a plain warning pointing to Check FPGA')
        page = w.pages['console']
        check(page.minimumSizeHint().height() <= w.stack.height() + 2 and w.sound_line.width() >=
              w.sound_line.fontMetrics().horizontalAdvance(w.sound_line.text()), f'FPGA {fpga!r}: Home fits at the compact size')

    # Update sound runs the guided setup's own jobs and checks: Check finds the older image (not the
    # original, not this build), Restore FPGA writes the original over it after its Approve for an
    # unknown image, then the same trial, write and final checks.
    w = windows[1]
    fr = tf.Rig()
    fr.chip.flash = bytearray(bytes(tf.synthetic_bitstream(9)) + b'\xff' * (tf.SIZE - 900_000))  # the older image
    w.service.fpga_factory = fpga_factory(fr)
    w.dialogs.confirms[:] = ['yes'] * 4
    w.dialogs.answers[:] = ['change']
    w.primary.click()
    check(w.sound.active and w.sound.update and w.sound.screen == 'check'
          and w.guide_card.head.text() == 'Update the sound image' and w.guide_step.text() == 'STEP 1 OF 5'
          and fr.chip.reads == 0, 'Update sound: step 1 of 5 of the same guide, nothing run yet')
    guide_fits(w, 'update step 1')
    check(press_guide(w, 0) == cf.EXIT_OK and w.sound.screen == 'rehearse' and w.sound.older and fr.chip.writes == 0
          and 'older sound image' in w.guide_card.sub.text(), 'Update: Check finds the older image, nothing written')
    guide_fits(w, 'update step 2')
    check(press_guide(w, 0) == cf.EXIT_OK and w.sound.screen == 'game_test' and fr.flash() == tf.ORIGINAL
          and w.dialogs.answers == [] and any(p.startswith(cf.FPGA_UNKNOWN_PROMPT) for p in w.dialogs.asked),
          'Update: the original written back over the older image, after its own Approve for an unknown image')
    for n in (0, 0, 0):
        press_guide(w, n)
    check(w.sound.screen == 'power_cycle' and fr.chip.sram_loads == 1 and fr.flash() == tf.ORIGINAL, 'Update: trial from RAM')
    tf.FakeLoader(fr.chip).reset()
    check(press_guide(w, 0) == cf.EXIT_OK and w.sound.screen == 'final' and fr.flash() == fr.expected_media()
          and w.dialogs.confirms == [], 'Update: the new sound image written and checked, every job approved')
    press_guide(w, 0)
    press_guide(w, 0)
    check(not w.sound.active and w.sound_line.text() == 'Sound: on' and w.primary.isHidden()
          and w.console_art.tone == 'sound', 'after the update Home says Sound: on, from the write itself')
    for window in windows:
        window.close()


LINK_IMAGE = 'sound + Pocket link build 18.41 (image 0123abcd)'  # example short name (no real image's hash)


def link_window(rig, fr, session='run-a', fpga_session=None, link=LINK_IMAGE):
    """A window on the synthetic console chip `fr` (which holds the sound image unless a test
    changes it) with the reviewed Pocket link image offered (`link`; None: not cleared)."""
    w = sound_window(rig, fr, session, fpga_session)
    w.link_image, w._link_image_text = (lambda: link), ...
    return w


def recovered_lines(case, not_seen=False):
    """The real words of chromatic_fpga's recover after a cut-off sound write: 'finished' (cut
    during the readback: the flash already reads as the sound image) or 'undone' (the readback
    did not match: the original written back). Exit code and the job's lines."""
    import test_chromatic_fpga as tf
    fr = tf.Rig()
    jobs = fr.jobs()
    check(jobs.restore_original() == 0, 'rehearsal for the recovery words')
    if case == 'finished':
        fr.chip.cut_at_read = fr.chip.reads + 2  # the readback after the write
    else:
        fr.chip.corrupt_after_write = 5000
    fr.chip.boot_fails = not_seen
    start = len(fr.lines)
    code = jobs.write_media()
    return code, list(fr.lines[start:])


def recovered_outcome_checks(stock, release):
    """Review: a sound or Pocket link write that was cut off and put right says what
    really happened. 'Undone': the original is back (the undone stops). 'Finished': the flash
    already read as the new image, so it is on and checked: the final checks, never "your original
    is back" and never "Press Finish it" (the job is closed)."""
    import chromatic_fpga as cf
    import chromatic_manager_core as core
    import test_chromatic_fpga as tf
    words = {}
    for case in ('finished', 'undone'):
        for not_seen in (False, True):
            code, lines = recovered_lines(case, not_seen)
            check(code == (cf.EXIT_NOT_SEEN if not_seen else cf.EXIT_RECOVERED) and core.recovered_outcome(lines) == case,
                  f'recover said {case} (not seen: {not_seen}), exit {code}')
            words[case, not_seen] = code, lines
    with tempfile.TemporaryDirectory() as folder:
        for guide_class, key, flag, undone, image in (
                (core.SoundGuide, 'fpga_media', 'sound_written', 'sound_undone', 'sound image'),
                (core.LinkGuide, 'fpga_link', 'link_written', 'link_undone', 'Pocket link image')):
            for (case, not_seen), (code, lines) in words.items():
                guide = guide_class(Path(folder) / f'{key}.json', 'run-a')
                guide.start()
                head, _ = core.fpga_result(key, code, lines)
                guide.job_done(key, code, lines, head, core.what_to_do(key, code, 'none', {}, lines))
                view = guide.view()
                said = ' '.join(view.lines) + ' ' + view.todo
                what = f'{key} {case}{" not seen" if not_seen else ""}'
                if case == 'finished':
                    check(guide.screen == 'final' and getattr(guide, flag) is True and not guide.trial_loaded
                          and f'already held the {image}' in said and 'original' not in ' '.join(view.lines[:1])
                          and ('not seen restarting' in said) == not_seen, f'{what}: the final checks, said truly')
                else:
                    check(guide.screen == 'stopped' and guide.reason == undone and getattr(guide, flag) is False
                          and 'original image was written back' in said and 'Finish it' not in said
                          and ('not seen restarting' in view.todo) == not_seen, f'{what}: the undone stop, said truly')
        # The job's own refusal for a job still open keeps "Finish it".
        guide = core.LinkGuide(Path(folder) / 'open.json', 'run-a')
        guide.start()
        guide.job_done('fpga_link', 1, ['Refused, nothing written: an unfinished console chip job exists. Finish or '
                                        'abandon it first.'])
        check(guide.reason == 'needs_you' and 'Finish it' in guide.view().todo, 'a job still open: Finish it')
        # Answers all good that the job would not confirm (the clock moved back): its own stop.
        guide = core.LinkGuide(Path(folder) / 'clock.json', 'run-a', confirmer=lambda answers: False)
        guide.start()
        guide.screen, guide.answers = 'look_link_game', {'pocket_link': 'good', 'sound': 'good', 'games': 'good'}
        guide.choose('look_skip')
        check(guide.reason == 'not_confirmed' and 'clock' in ' '.join(guide.view().lines)
              and guide.view().buttons[0][0] == 'Start again', 'all good but not confirmed: says so, not "trial not right"')
        guide = core.LinkGuide(Path(folder) / 'wrong.json', 'run-a', confirmer=lambda answers: False)
        guide.start()
        guide.screen, guide.answers = 'look_link_game', {'pocket_link': 'good', 'sound': 'good', 'games': 'good'}
        guide.choose('look_wrong')
        check(guide.reason == 'look_wrong', 'something wrong: still the trial-not-right stop')
    # Home and the result say what the flash holds after the recovery.
    before = {'state': 'installed', 'fpga': '18.40 debug'}
    for (case, not_seen), (code, lines) in words.items():
        for key, holds in (('fpga_media', 'sound'), ('fpga_link', 'link')):
            got = core.fpga_summary(key, code, lines, before).get('fpga')
            check(got == (holds if case == 'finished' else 'original'), f'{key} {case}: Home knows the chip holds {got}')
        check(core.what_to_do('fpga_link', code, 'none', {}, lines) != 'Press Finish it on the Home tab.',
              f'{case}: the result never says Finish it for a closed job')
    finished = words['finished', False]
    check('already held the Pocket link image' in core.fpga_result('fpga_link', *finished)[1]
          and 'original' not in core.fpga_result('fpga_link', *finished)[1], 'the finished result: nothing written back')
    check('written back and checked' in core.fpga_result('fpga_link', *words['undone', False])[1], 'the undone result')


def link_guide_checks(stock, release):
    """The guided Pocket link setup (the combined image 18.41, owner-present): Check FPGA must find
    the sound image, the SRAM try, four looks (the link-cable game may be skipped with a note),
    the answers recorded with the try, a power cycle, the write (refused by the job without a
    confirmed try), the final checks; every "Something is wrong" stops and offers the way back;
    Home and the sound line know 18.41; nothing offered while the image is not cleared."""
    import chromatic_fpga as cf
    import chromatic_manager_core as core
    import test_chromatic_fpga as tf
    sound = cf.full_flash_image(tf.MEDIA_BITSTREAM)
    shots = {}

    # The core's view of 18.41: sound on, Pocket link ready, never "unknown".
    for value in ('18.41 debug', 'link'):
        check(core.sound_state({'fpga': value}) == 'installed' and core.link_state({'fpga': value}) == 'ready',
              f'{value}: sound installed, Pocket link ready')
    for value in ('18.40 debug', 'sound', '18.14', '18.41', '18.42 debug', None):
        check(core.link_state({'fpga': value}) is None, f'{value}: no Pocket link')
    check(core.sound_state({'fpga': '18.41 debug'}, built=(18, 40)) == 'installed', '18.41 with the 18.40 build: installed')
    check(core.sound_state({'fpga': '18.41'}) == 'unknown', '18.41 without the debug bit stays unknown')
    check(set(core.GUIDE_ONLY_KEYS) <= set(core.FPGA_KEYS) and all(k in core.WRITE_KEYS for k in core.GUIDE_ONLY_KEYS),
          'the two Pocket link jobs are approved console chip jobs')
    check(all(ch in theme.PIXEL_GLYPHS or ch == ' ' for ch in core.LinkGuide.TITLE + core.LinkGuide.TAG),
          'link guide title in pixel letters')
    check(sum(core.LINK_MINUTES.values()) == 14 and all(core.LINK_MINUTES[s] > 0 for s in range(1, 6)),
          'every step has a time; about 15 minutes in all')
    # The guide says the repeat-try power cycle happens while the Manager watches, and its
    # numbers are chromatic_fpga's (LINK_TRY_POWER_CYCLE_S, FpgaTiming.boot).
    trial_todo = core.LINK_SCREENS['trial'].todo
    check('while it watches' in trial_todo and 'not seen' in trial_todo
          and f'last {cf.LINK_TRY_POWER_CYCLE_S // 3600} hours' in trial_todo
          and f'up to {int(cf.FpgaTiming().boot)} s' in trial_todo,
          'the trial screen says the power cycle must happen while this app watches, with the real window and wait')
    for screen in core.LINK_SCREENS.values():
        check(len(screen.headline) <= 30 and screen.todo and screen.buttons, f'link screen {screen.key}: short, says what to do')
        check(screen.step == 0 or any(b[2] == 'stop' for b in screen.buttons) or screen.key == 'final_wrong',
              f'link screen {screen.key}: Stop setup is offered')
    for reason, (head, _, _, _, _) in core.LINK_STOPS.items():
        check(len(head) <= 30, f'link stop {reason}: short headline')
    for key, code, lines, head in (
            ('fpga_link_trial', 0, ['x is running from the console chip\'s RAM'], 'Pocket link running from RAM'),
            ('fpga_link', 0, ['x installed: all 4 MiB read back and identical'], 'Pocket link written and checked'),
            ('fpga_link', 0, ['The flash already holds this Pocket link image. Nothing written.'], 'Pocket link already there'),
            ('fpga_link', 5, ['x installed: all 4 MiB read back and identical'], 'Pocket link written, not seen restarting'),
            ('fpga_check', 0, ['Console chip: GW5A-25 confirmed. Its flash holds the Pocket link image (x). Nothing was '
                               'written.'], 'Checked. Nothing was written')):
        got_head, got_next = core.fpga_result(key, code, lines)
        check(got_head == head and 'not touched' not in got_next, f'FPGA result headline: {key} exit {code} -> {got_head}')
    check('the Pocket link image' in core.fpga_result('fpga_check', 0, [core.SAID_LINK])[1], 'check result names the image')
    summary = {'state': 'installed', 'fpga': '18.40 debug'}
    check(core.fpga_summary('fpga_link', 0, ['x installed: all 4 MiB read back and identical'], summary)['fpga'] == 'link'
          and core.fpga_summary('fpga_check', 0, [core.SAID_LINK], summary)['fpga'] == 'link'
          and core.fpga_summary('fpga_link_trial', 0, [core.SAID_TRIAL], summary)['fpga'] == '18.40 debug',
          'after a Pocket link job the summary says what the flash holds (the try changes nothing)')
    check('Console chip image to write: none ready (refused)' in gui.approve_prompt(core.BY_KEY['fpga_link'])
          or 'Console chip image to write: sound + Pocket link build' in gui.approve_prompt(core.BY_KEY['fpga_link']),
          'the write\'s Approve names the image (or says none is ready)')
    check('flash is not written' in gui.approve_prompt(core.BY_KEY['fpga_link_trial']), 'the try\'s Approve: RAM only')
    for key in ('fpga_link_trial', 'fpga_link', 'fpga_media', 'fpga_restore', 'restore'):
        dialog = gui.ApproveDialog(None, gui.approve_prompt(core.BY_KEY[key]))
        dialog.show()
        QApplication.processEvents()
        said = dialog.findChildren(QLabel)[0]
        check(said.height() >= said.heightForWidth(said.width()) and 'Keep your Chromatic' in said.text(),
              f'{key}: the Approve dialog shows its whole text (the image line and Keep plugged in are not cut off)')
        dialog.close()

    recovered_outcome_checks(stock, release)

    # Where the button lives: Fix a problem (beside Set up Jellyfin sound), and Home on the sound image.
    fr = tf.Rig(flash=sound)
    rig = Rig(stock, release, confirms=['yes'] * 3)
    w = link_window(rig, fr)
    w.resize(*gui.WINDOW)
    w.show()
    check(w.fix_link.text() == gui.MainWindow.LINK_BUTTON and not any(k in w.buttons for k in core.GUIDE_ONLY_KEYS),
          'Fix a problem: Update the Pocket link; the try and the write have no buttons of their own')
    check(core.LINK_UPDATE_KEY not in w.buttons, 'nor has the one-step job: it runs from its own screen')
    w.show_tab('recovery')
    QApplication.processEvents()
    check(all(b.text_fits() for b in visible_buttons(w.pages['recovery'])), 'Fix a problem: every button fits')
    shots['fix'] = True
    installed = {'state': 'installed', 'launcher': release.version, 'release': release.version, 'fpga': '18.40 debug'}
    w.service.summary = dict(installed)
    w.show_tab('console')
    w.refresh_guide()
    check(w.primary.isVisible() and w.primary.text() == gui.MainWindow.LINK_BUTTON and w.primary.text_fits()
          and w.sound_line.text() == 'Sound: on', 'Home on the sound image: Update the Pocket link, Sound: on')
    w.service.summary = dict(installed, fpga='18.41 debug')
    w.refresh_guide()
    check(w.primary.isHidden() and w.sound_line.text() == 'Sound: on  |  Pocket link: ready'
          and 'unknown' not in (w.sound_line.text() + w.sound_line.toolTip()).lower(),
          'Home on 18.41: Sound: on | Pocket link: ready, no button, never unknown')
    w.service.summary = dict(installed, fpga='18.14')
    w.refresh_guide()
    check(w.primary.text() != gui.MainWindow.LINK_BUTTON, 'Home on the stock image: no Pocket link offer')
    w.service.summary = dict(installed)
    w.link_image, w._link_image_text = (lambda: None), ...
    w.refresh_guide()
    check(w.primary.isHidden(), 'image not cleared (NEVER-INSTALL): Home offers nothing')
    check(w.open_link_guide() and w.sound is w.link_guide and w.sound.reason == 'no_image'
          and w.guide_card.head.text() == 'Pocket link not ready' and fr.chip.reads == 0,
          'image not cleared: the guide says so and runs nothing')
    check(press_guide(w, 0) is None and not w.link_guide.active, 'Close')
    w.link_image, w._link_image_text = (lambda: LINK_IMAGE), ...
    w.refresh_guide()

    # Home's button opens the one-step update; Step by step there is this question-based guide, unchanged.
    w.primary.click()
    check(w.sound is w.link_update and w.sound.screen == 'start' and w.guide_buttons[1].text() == 'Step by step'
          and fr.chip.reads == 0 and rig.dialogs.confirms == ['yes'] * 3,
          'Home, Update the Pocket link: the one-step screen, nothing read, no Approve yet')
    check(press_guide(w, 1) is None and not w.link_update.active, 'Step by step leaves the one-step screen')
    check(w.sound is w.link_guide and w.sound.screen == 'check' and w.guide_step.text() == 'STEP 1 OF 5'
          and '14 min left' in w.guide_time.text() and w.title.text() == core.LinkGuide.TITLE
          and w.console_art.tone == 'link' and not any(t.isEnabled() for t in w.tabs.values()),
          'Add the Pocket link: step 1 of 5, its title, the plug on the console drawing')
    guide_fits(w, 'link step 1')
    check(press_guide(w, 0) == cf.EXIT_OK and w.sound.screen == 'trial' and fr.chip.writes == 0
          and LINK_IMAGE in w.guide_card.sub.text() and 'sound image, byte for byte' in w.guide_card.sub.text(),
          'step 1: Check FPGA finds the sound image; step 2 names the image to try')
    guide_fits(w, 'link step 2')
    check(press_guide(w, 0) == cf.EXIT_OK and w.sound.screen == 'look_link_row' and fr.chip.sram_loads == 1
          and fr.chip.sram == tf.LINK_BITSTREAM and fr.flash() == sound and fr.chip.writes == 0
          and fr.chip.version_reads == 1 and core.SAID_CONFIRMED_NOTE in w.guide_card.sub.text(),
          'step 2: the try from RAM only, nothing written, 18.41 confirmed from the start-up menu before any look')
    check(w.service.result()['headline'] == 'Pocket link confirmed running', "the try's result: confirmed running")
    guide_fits(w, 'look link row')
    check('Select' in w.guide_todo.text() and 'Pocket link' in w.guide_todo.text() and 'Jellyfin' in w.guide_todo.text()
          and w.guide_buttons[0].text() == 'All good' and w.guide_buttons[1].text() == 'Something is wrong'
          and w.guide_buttons[2].isHidden(), 'look 1: the Pocket link row in the Jellyfin menu, first')
    check(press_guide(w, 0) is None and w.sound.screen == 'look_sound' and 'sound' in w.guide_todo.text()
          and 'choose Media' not in w.guide_todo.text(),
          'look 2: Jellyfin sound, still in Jellyfin (no route from a game back to Media is asked for)')
    guide_fits(w, 'look sound')
    check(press_guide(w, 0) is None and w.sound.screen == 'look_games' and 'Games' in w.guide_todo.text(),
          'look 3: games, after the Jellyfin looks')
    guide_fits(w, 'look games')
    check(press_guide(w, 0) is None and w.sound.screen == 'look_link_game'
          and [b.text() for b in w.guide_buttons if not b.isHidden()] == ['All good', 'Skip', 'Something is wrong'],
          'look 4: the link-cable game, with Skip, last')
    guide_fits(w, 'look link game')
    jobs = fpga_factory(fr)(SimpleNamespace(say=lambda m: None, stop_point=lambda: None))
    check(jobs.link_try_confirmed(fr.link().fingerprint) is None, 'before the last answer the write is still shut')
    check(press_guide(w, 1) is None and w.sound.screen == 'power_cycle' and LINK_IMAGE in w.guide_card.sub.text()
          and w.console_art.tone == 'power' and 'off, then on' in w.guide_todo.text(),
          'all good: step 4, switch off and on, the image to write named')
    check(jobs.link_try_confirmed(fr.link().fingerprint) is not None
          and jobs.link_try()['answers'] == {'games': 'good', 'link_game': 'skipped', 'sound': 'good',
                                             'pocket_link': 'good'} and jobs.link_try()['running'] == [18, 41, 1],
          'the answers (link-cable game skipped) are recorded with the try, which proved 18.41')
    guide_fits(w, 'link power cycle')
    tf.FakeLoader(fr.chip).reset()  # the owner switches off and on: the sound image runs again
    check(press_guide(w, 0) == cf.EXIT_OK and w.sound.screen == 'final' and fr.flash() == fr.expected_link()
          and rig.dialogs.confirms == [], 'step 4: the write, approved, verified')
    check(w.service.result()['headline'] == 'Pocket link written and checked', 'the write\'s result says what was written')
    guide_fits(w, 'link final')
    check(press_guide(w, 0) is None and w.sound.screen == 'done' and w.guide_card.head.text() == 'The Pocket link is ready',
          'final checks all good: done')
    guide_fits(w, 'link done')
    check(press_guide(w, 0) is None and not w.sound.active and w.sound_line.text() == 'Sound: on  |  Pocket link: ready'
          and w.primary.isHidden(), 'Home after the write: Sound: on | Pocket link: ready, from the write itself')

    # Something is wrong after the write: Sound image back (one click), or Restore FPGA.
    rig.dialogs.confirms[:] = ['yes']
    w.open_link_guide()
    w.sound.screen, w.sound.link_written = 'final', True
    w.show_guide()
    check(press_guide(w, 1) is None and w.sound.screen == 'final_wrong'
          and [b.text() for b in w.guide_buttons if not b.isHidden()] == ['Sound image back', 'Restore FPGA', 'Not now'],
          'final wrong: the sound image back, or the original, offered')
    guide_fits(w, 'link final wrong')
    check(press_guide(w, 0) == cf.EXIT_OK and w.sound.screen == 'sound_back' and fr.flash() == sound,
          'Sound image back: 18.40 written over 18.41 in one click (no rehearsal in this run)')
    guide_fits(w, 'sound back')
    press_guide(w, 0)

    # Something is wrong on the trial: stopped, recorded, the write stays shut.
    rig.dialogs.confirms[:] = ['yes'] * 3
    w.open_link_guide()
    press_guide(w, 0)
    fr.chip.power_cycle()  # a try already ran in this session: the owner switches off and on before the next (watched by the job)
    press_guide(w, 0)
    press_guide(w, 0)
    check(w.sound.screen == 'look_sound', 'to look 2')
    check(press_guide(w, 1) is None and w.sound.reason == 'look_wrong' and 'off and on' in w.guide_todo.text()
          and jobs.link_try()['confirmed'] is False and jobs.link_try()['answers']['sound'] == 'wrong',
          'look wrong: stopped, the power cycle said, the answer kept with the try')
    guide_fits(w, 'look wrong')
    tf.FakeLoader(fr.chip).reset()
    w.sound.screen, w.sound.reason = 'power_cycle', None  # even if the window were forced on, the job refuses
    w.show_guide()
    writes = fr.chip.writes
    check(press_guide(w, 0) == cf.EXIT_REFUSED and w.sound.reason == 'try_lost' and fr.chip.writes == writes
          and fr.flash() == sound, 'a write without a confirmed try: refused by the job, try again')
    guide_fits(w, 'try lost')
    press_guide(w, 1)

    # Stop on a look screen: says to switch off and on.
    rig.dialogs.confirms[:] = ['yes'] * 2
    w.open_link_guide()
    press_guide(w, 0)
    fr.chip.power_cycle()
    press_guide(w, 0)
    check(w.sound.screen == 'look_link_row' and press_guide(w, 'stop') is None and w.sound.reason == 'cancelled'
          and 'off and on' in w.guide_todo.text(), 'Stop on a look screen: the power cycle said')
    press_guide(w, 0)
    tf.FakeLoader(fr.chip).reset()

    # the try came back on USB but the console ran the flash image (18.40): no look is asked,
    # the guide says so plainly and offers Start again; no answers can be given for it.
    for what, falls_back, reported in (('fell back to 18.40', True, ...), ('no version line', False, None)):
        rig.dialogs.confirms[:] = ['yes'] * 2
        fr.chip.sram_falls_back, fr.chip.reported = falls_back, reported
        w.open_link_guide()
        press_guide(w, 0)
        fr.chip.power_cycle()
        writes = fr.chip.writes
        code = press_guide(w, 0)
        check(code == cf.EXIT_REFUSED and w.sound.reason == 'trial_not_running',
              f'trial {what}: stopped before any look (exit {code}, {w.sound.screen}/{w.sound.reason})')
        check(w.guide_card.head.text() == 'NOT running the Pocket link' and w.guide_buttons[0].text() == 'Start again'
              and 'off and on' in w.guide_todo.text() and w.sound.trial_loaded and fr.chip.writes == writes,
              f'trial {what}: said plainly, Start again')
        check(w.service.result()['headline'] == 'Pocket link NOT running',
              f'trial {what}: the result says NOT running ({w.service.result()["headline"]})')
        guide_fits(w, f'trial not running ({what})')
        check(jobs.link_try_confirmed(fr.link().fingerprint) is None, f'trial {what}: the write stays shut')
        press_guide(w, 1)
        tf.FakeLoader(fr.chip).reset()
    fr.chip.sram_falls_back, fr.chip.reported = False, ...

    # Check finds the original: set up sound first, with a button into the sound guide.
    fr2 = tf.Rig()
    rig2 = Rig(stock, release, confirms=['yes'])
    w2 = link_window(rig2, fr2)
    w2.open_link_guide()
    check(press_guide(w2, 0) == cf.EXIT_OK and w2.sound.reason == 'needs_sound'
          and w2.guide_buttons[0].text() == 'Set up sound' and fr2.chip.writes == 0, 'original: set up sound first')
    guide_fits(w2, 'needs sound')
    check(press_guide(w2, 0) is None and w2.sound is w2.sound_guide and w2.sound.screen == 'check'
          and w2.title.text() == gui.MainWindow.GUIDE_TITLE, 'Set up sound opens the sound guide')
    press_guide(w2, 'stop')
    press_guide(w2, 0)
    # Check finds 18.41 already: nothing to do, the way back offered.
    fr2.chip.flash = bytearray(fr2.expected_link())
    rig2.dialogs.confirms[:] = ['yes']
    w2.open_link_guide()
    check(press_guide(w2, 0) == cf.EXIT_OK and w2.sound.reason == 'already_link'
          and w2.guide_buttons[0].text() == 'Sound image back', 'already 18.41: nothing to set up, the way back offered')
    press_guide(w2, 1)

    # Closing the Manager after the answers: the try is recorded, so the next run goes on to the write.
    fr3 = tf.Rig(flash=sound)
    rig3 = Rig(stock, release, confirms=['yes'] * 3)
    w3 = link_window(rig3, fr3)
    w3.open_link_guide()
    for n in (0, 0, 0, 0, 0, 0):
        press_guide(w3, n)
    check(w3.sound.screen == 'power_cycle' and (rig3.h.folder / 'link-guide.json').exists(), 'progress saved')
    w3.close()
    reopened = link_window(rig3, fr3, session='run-b', fpga_session='session-b')
    check(reopened.sound is reopened.link_guide and reopened.sound.screen == 'resume'
          and 'answers are saved' in reopened.guide_card.sub.text() and reopened.guide_buttons[0].text() == 'Continue'
          and 'off and on first' in reopened.guide_todo.text(), 'reopened: the try and answers are kept, go on')
    guide_fits(reopened, 'link resume')
    check(press_guide(reopened, 0) is None and reopened.sound.screen == 'power_cycle', 'Continue: the write step')
    tf.FakeLoader(fr3.chip).reset()
    check(press_guide(reopened, 0) == cf.EXIT_OK and reopened.sound.screen == 'final' and fr3.flash() == fr3.expected_link(),
          'reopened run: written on the recorded confirmed try')
    reopened.close()
    # Reopened after the answers, but the recorded try no longer counts (here: removed): step 1 again.
    fr5 = tf.Rig(flash=sound)
    rig5 = Rig(stock, release, confirms=['yes'] * 2)
    w5 = link_window(rig5, fr5)
    w5.open_link_guide()
    for n in (0, 0, 0, 0, 0, 0):
        press_guide(w5, n)
    w5.close()
    (fr5.work / cf.LINK_TRY_FILE).unlink()
    gone = link_window(rig5, fr5, session='run-b', fpga_session='session-b')
    check(gone.sound.screen == 'resume' and press_guide(gone, 0) is None and gone.sound.screen == 'check'
          and 'no longer counts' in gone.guide_card.sub.text(), 'reopened, the try no longer recorded: start again')
    gone.close()
    # Reopened before the answers: back to step 1.
    fr4 = tf.Rig(flash=sound)
    rig4 = Rig(stock, release, confirms=['yes'] * 2)
    w4 = link_window(rig4, fr4)
    w4.open_link_guide()
    press_guide(w4, 0)
    press_guide(w4, 0)
    w4.close()
    again = link_window(rig4, fr4, session='run-b', fpga_session='session-b')
    check(again.sound.screen == 'resume' and again.guide_buttons[0].text() == 'Start again'
          and 'off and on first' in again.guide_todo.text(), 'reopened before the answers: start again')
    check(press_guide(again, 0) is None and again.sound.screen == 'check', 'Start again: step 1')
    again.close()
    # A program cannot start anything while the guide is open.
    # The two Pocket link jobs are never requestable by a program (API, MCP): only the guide runs them.
    for key in core.GUIDE_ONLY_KEYS:
        answer = w2.service.request_action(key, 'Claude Code via MCP', 'link please')
        check(answer['state'] == 'refused' and 'Update the Pocket link' in answer['why']
              and 'Add the Pocket link' not in answer['why'] and w2.service.request is None
              and key not in core.REQUESTABLE_KEYS and key not in core.PROGRAM_WRITE_KEYS,
              f'{key}: a program\'s request is refused outright (guide only)')
    w2.open_link_guide()
    w2.service.request_action('fpga_media', 'Claude Code via MCP', 'sound please')
    QApplication.processEvents()
    check(w2.service.request['state'] == 'declined' and 'Pocket link setup' in w2.service.request['outcome']
          and 'sound setup' not in w2.service.request['outcome'],
          'a program\'s request is declined while the link guide is open, naming the link guide')
    for window in (w, w2):
        window.close()


def link_update_checks(stock, release, installed_image):
    """Update the Pocket link in one step (no question rounds): one screen;
    Update now starts the console chip job with no dialog (the try from RAM and the app's own
    checks write nothing); then ONE Approve, asked big on the screen with the new image running
    (start a game, look; Not right changes nothing); then the write, the start-up menu when it is
    older (its own Approve), and Ready; every stop says the next step and writes nothing; and the
    owner-action sign (a watched power cycle) on a job that really waits: yellow with a
    countdown, green when done, red with the next step when the time ran out."""
    import chromatic_fpga as cf
    import chromatic_manager_core as core
    import chromatic_manager_owner as owner_actions
    import test_chromatic_fpga as tf
    sound = cf.full_flash_image(tf.MEDIA_BITSTREAM)
    key = core.LINK_UPDATE_KEY
    current = {'state': 'installed', 'launcher': release.version, 'release': release.version, 'fpga': '18.40 debug'}
    windows = []

    def window(fr, confirms=(), summary=current, image=stock, watch=None, keeps=(), own_sign=False):
        rig = Rig(image, release, None if image is stock else stock)
        rig.dialogs = KeepApprovals(confirms, keeps)
        w = link_window(rig, fr)
        if watch is not None or own_sign:
            w.service.fpga_factory = fpga_factory(fr, None, watch, own_sign)
        w.resize(*gui.WINDOW)
        w.show()
        w.service.summary = dict(summary)
        w.show_tab('console')
        w.refresh_guide()
        windows.append(w)
        return rig, w

    def shown(w):
        return [b.text() for b in w.guide_buttons if not b.isHidden()]

    def from_fix(w):
        w.show_tab('recovery')
        w.fix_link.click()

    def looks(w, fr, seen, then=True):
        # The owner at the one Approve: what the window shows while it is asked (noted in `seen`),
        # then Approve (`then` True), Not right (False), or what `then()` gives.
        def look(prompt):
            QApplication.processEvents()
            b, view = w.owner_banner, w.service.owner_view() or {}
            try:
                owner_fits(w, 'new image running')
                fits = True
            except SystemExit as error:
                fits = str(error)
            seen.append({'page': w.stack.currentWidget() is w.pages['owner'], 'state': b.state, 'head': b.head.text(),
                         'tag': b.tag.text(), 'text': b.text.raw_text(), 'ok': not b.ok.isHidden(),
                         'timed': not b.count.isHidden() or not b.drain.isHidden() or w.owner_left() is not None,
                         'tone': w.console_art.tone, 'fits': fits, 'view': view, 'busy': w.busy(),
                         'ram': fr.chip.sram is not None, 'writes': fr.chip.writes, 'flash': fr.flash(),
                         'tabs': any(t.isEnabled() for t in w.tabs.values()),
                         'progress': w.service.progress_view()['owner_action_needed']})
            return then() if callable(then) else then
        return look
    asked_keep = cf.LINK_KEEP_PROMPT + cf.keep_text('media')

    # ---- the wording and the rules, without a window ----
    action = core.BY_KEY[key]
    check(core.OWNER_APPROVE_KEYS == (key,) and key in core.FPGA_KEYS and key in core.WRITE_KEYS
          and key not in core.REQUESTABLE_KEYS and key not in core.GUIDE_ONLY_KEYS and action.confirm == 'yes',
          'the one-step job: an approved console chip job that no program can request through the general entry')
    check(all(ch in theme.PIXEL_GLYPHS or ch in ' -' for ch in core.LinkUpdateGuide.TITLE + core.LinkUpdateGuide.TAG),
          'one-step screen title in pixel letters')
    for screen in core.LINK_UPDATE_SCREENS.values():
        check(len(screen.headline) <= 30 and screen.todo and screen.buttons,
              f'update screen {screen.key}: short, says what to do')
    for reason, (head, _, _, tone, offers) in core.LINK_UPDATE_STOPS.items():
        check(len(head) <= 30 and len(offers) <= 2 and tone in gui.TONE_INK, f'update stop {reason}: short, at most two offers')
    for name in ('SAID_AUTO_FAILED', 'SAID_AUTO_PASSED', 'SAID_LINK_FROM_FLASH', 'SAID_LINK_NOT_FROM_FLASH',
                 'SAID_UPDATE_NEEDS_SOUND', 'SAID_UPDATE_UNKNOWN', 'SAID_LINK_NOT_RUNNING', 'SAID_NOT_KEPT',
                 'SAID_TRY_ENDED', 'SAID_KEPT', 'SAID_NO_ASKER', 'SAID_SWITCHED_OFF', 'SAID_LATE_REFUSAL'):
        check(getattr(core, name) == getattr(cf, name), f'{name}: the window reads the job\'s own words')
    # A program's request still gets an Approve dialog before anything touches the console: it
    # names the program and says that it only starts the try.
    said = gui.approve_prompt(action, core.api_requester('A Program'), 'update it')
    check('Request from A Program [program via local API]' in said and 'try from RAM and check' in said
          and 'This Approve starts the try; it writes nothing' in said
          and 'asked once more, with the new image running' in said and 'Keep your Chromatic' in said,
          'a program\'s request: its Approve names the program and says it only starts the try')
    dialog = gui.ApproveDialog(None, said)
    dialog.show()
    QApplication.processEvents()
    text = dialog.findChildren(QLabel)[0]
    check(dialog.head is None and text.height() >= text.heightForWidth(text.width()),
          'that Approve dialog shows its whole text')
    dialog.close()
    # The ONE Approve of the update, asked with the new image running: the job's words under a big
    # pixel headline, Approve or Not right, nothing to type.
    keep = cf.keep_text('media')
    check(all(word in keep for word in ('Start a game', 'picture', 'sound', 'Press Approve to keep the new image',
                                        'Not right', 'off and on', 'changes nothing', cf.FLASH_WORDS_FOR['media']))
          and 'If you switched your Chromatic off and on, press Not right.' in keep
          and 'ype ' not in keep and all(ch in theme.PIXEL_GLYPHS or ch == ' ' for ch in cf.KEEP_TITLE),
          'the keep words: start a game, look, Approve keeps it; after an off-and-on press Not right, which changes '
          'nothing')
    said = gui.keep_prompt(keep)
    check(said.startswith(keep) and 'Approve writes this image to the console chip' in said and 'read back' in said
          and 'until this app says Done' in said, 'the keep dialog adds what Approve writes and to keep it plugged in')
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    dialog = gui.ApproveDialog(None, said, 'Approve', cf.KEEP_TITLE, 'Not right')
    dialog.show()
    QApplication.processEvents()
    text, head = dialog.findChildren(QLabel)[0], dialog.head
    check(head is not None and head.text() == cf.KEEP_TITLE and head.height() >= head.heightForWidth(head.width())
          and not [line for line in head.lines(head.width()) if head._image_of(line).width() > head.width()]
          and text.height() >= text.heightForWidth(text.width()),
          'the keep dialog: The new image is running now in big pixel letters, and its whole text')
    check(dialog.cancel.text() == 'Not right' and dialog.approve.text() == 'Approve' and dialog.cancel.isDefault()
          and not dialog.approve.isEnabled() and dialog.cancel.text_fits() and dialog.approve.text_fits(),
          'its buttons: Not right (the default) and Approve, which ignores input for its first second')
    QTest.mouseClick(dialog.approve, Qt.MouseButton.LeftButton)
    check(dialog.isVisible() and not dialog.approved, 'an early click does not keep the image')
    settle()
    QTest.keyClick(dialog, Qt.Key.Key_Return)
    check(not dialog.isVisible() and not dialog.approved, 'Enter is Not right')
    dialog = gui.ApproveDialog(None, said, 'Approve', cf.KEEP_TITLE, 'Not right')
    dialog.show()
    settle()
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    check(not dialog.isVisible() and not dialog.approved, 'Esc is Not right')
    dialog = gui.ApproveDialog(None, said, 'Approve', cf.KEEP_TITLE, 'Not right')
    dialog.show()
    settle()
    QTest.mouseClick(dialog.approve, Qt.MouseButton.LeftButton)
    check(not dialog.isVisible() and dialog.approved, 'only a click on Approve keeps it')
    # The real Dialogs turn the job's question into that dialog, and only its Approve into the answer.
    made, press, real_exec = [], {}, gui.ApproveDialog.exec

    def fake_exec(self):  # the owner reads the dialog, then presses one of its buttons
        made.append((self.head.text() if self.head else None, self.findChildren(QLabel)[0].text(),
                     self.cancel.text(), self.approve.text()))
        pressed = self.approve if press['approve'] else self.cancel
        pressed.setEnabled(True)
        pressed.click()
        return self.result()
    gui.ApproveDialog.exec = fake_exec
    try:
        dialogs = gui.Dialogs(None)
        press['approve'] = True
        check(dialogs.ask(asked_keep, False) == cf.KEEP_ANSWER, 'the real dialogs: Approve answers the job keep')
        press['approve'] = False
        check(dialogs.ask(asked_keep, False) == '', 'Not right answers nothing, which the job takes as not kept')
    finally:
        gui.ApproveDialog.exec = real_exec
    check(made == [(cf.KEEP_TITLE, said, 'Not right', 'Approve')] * 2,
          'and the dialog they show is the keep dialog: headline, the job\'s words, Not right, Approve')
    wrote = f'{core.SAID_FLASH_WRITING} now.'
    off = f'{core.SAID_SWITCHED_OFF} while the new image waited for your Approve: nothing was written.'
    late_start = f'{core.SAID_LATE_REFUSAL} (the line above says why): nothing was written. '
    late_back = late_start + ('The flash still holds the sound image (18.40), and the console chip was restarted from '
                              'it: the console is as it was.')
    late_blind = late_start + ('The flash still holds your original image. Switch the console off and on: it starts '
                               'from that image, as before.')
    for code, lines, head in (
            (0, [wrote, core.SAID_LINK_FROM_FLASH], 'Pocket link updated'),
            (0, [core.SAID_LINK_FROM_FLASH], 'Pocket link already up to date'),
            (5, [wrote, core.SAID_LINK_NOT_FROM_FLASH], 'Pocket link written, not heard from yet'),
            (5, [core.SAID_LINK_NOT_FROM_FLASH], 'Pocket link there, not heard from yet'),
            (1, [f'{core.SAID_AUTO_FAILED}: nothing was written.'], 'Automatic checks failed'),
            (1, [f'{core.SAID_NOT_KEPT}: you did not approve the new image, so nothing was written.'],
             'Not kept. Nothing was written'),
            (1, [f'{core.SAID_TRY_ENDED} when you pressed Approve: x. Nothing was written.'],
             'Approve came too late. Nothing was written'),
            (1, [core.SAID_LINK_NOT_RUNNING], 'Pocket link NOT running'),
            (1, [off], 'Switched off. Nothing was written'),
            (1, [late_back], 'Stopped. Nothing was written'),
            (1, [late_blind], 'Stopped. Nothing was written'),
            (1, ['Refused, nothing written: x.'], 'Nothing was changed')):
        got, then = core.fpga_result(key, code, lines)
        check(got == head and 'not touched' not in then, f'one-step result headline: exit {code} -> {got}')
    check(core.HELD_WORDS == (cf.FLASH_WORDS_FOR['media'], cf.FLASH_WORDS_FOR['original'])
          and core.fpga_result(key, 1, [late_back])[1] == core.late_refusal_note(late_back)
          and 'back on the sound image (18.40)' in core.late_refusal_note(late_back)
          and 'restarted from its flash' in core.late_refusal_note(late_back)
          and 'still holds your original image' in core.late_refusal_note(late_blind)
          and 'not seen restarting: switch it off and on' in core.late_refusal_note(late_blind),
          'a write that did not start after Approve: the result says which image the console is back on, or to '
          'switch it off and on')
    before = {'state': 'installed', 'fpga': '18.40 debug'}
    check(core.fpga_summary(key, 0, [wrote, core.SAID_LINK_FROM_FLASH], before)['fpga'] == 'link'
          and core.fpga_summary(key, 5, [wrote, core.SAID_LINK_NOT_FROM_FLASH], before)['fpga'] == 'link'
          and core.fpga_summary(key, 1, [core.SAID_AUTO_FAILED], before)['fpga'] == '18.40 debug'
          and core.fpga_summary(key, 0, [core.SAID_LINK_FROM_FLASH], {}) == {},
          'after the one-step job the summary says what the flash holds; a refusal changes nothing')

    # ---- the screen's steps, from what the jobs said (no window, no file) ----
    facts = {}
    with tempfile.TemporaryDirectory() as folder:
        guide = core.LinkUpdateGuide(Path(folder) / 'never-written.json', 'run-a', facts=lambda: dict(facts))

        def begin(state=None):
            facts.clear()
            if state:
                facts['state'] = state
            guide.start()
            check(guide.choose('run') == ('job', key), f'Update now starts the console chip job (menu: {state})')
            return guide

        def stop_of(code, lines, todo=''):
            begin('installed').job_done(key, code, lines, 'Head', todo)
            return guide.reason if guide.screen == 'stopped' else guide.screen
        good = [wrote, core.SAID_LINK_FROM_FLASH]
        begin('installed').job_done(key, 0, good)
        check(guide.screen == 'ready' and guide.link_written and guide.auto is None
              and guide.view().buttons == (('OK', 'ok', 'close'), core.PREVIOUS_OFFER)
              and core.LINK_UPDATE_NOT_CHECKED in ' '.join(guide.view().lines + (guide.view().todo,)),
              'written and the menu is current: Ready, the way back one click away, what no machine checked')
        begin('installed').job_done(key, 0, [core.SAID_LINK_FROM_FLASH])
        check(guide.screen == 'ready' and 'Nothing was written' in guide.note, 'already there: Ready, nothing written')
        begin('outdated').job_done(key, 0, good)
        check(guide.screen == 'menu' and guide.auto == 'install' and guide.view().buttons[0] == ('Update the menu', 'go', 'job:install'),
              'an older start-up menu: its update is next, by itself')
        guide.cancelled('install')
        check(guide.screen == 'menu' and guide.auto is None, 'Cancel on the menu\'s Approve: the screen keeps its button')
        check(guide.choose('job:install') == ('job', 'install'), 'Update the menu')
        guide.job_done('install', 0, [])
        check(guide.screen == 'ready' and 'Start-up menu updated' in guide.note, 'menu installed: Ready')
        for code, reason in ((1, 'menu_failed'), (3, 'needs_you')):
            begin('outdated').job_done(key, 0, good)
            guide.job_done('install', code, [], 'Head', 'Do this.')
            check(guide.screen == 'stopped' and guide.reason == reason and guide.todo == 'Do this.'
                  and core.PREVIOUS_OFFER in guide.view().buttons and guide.view().buttons[-1] == core.CLOSE,
                  f'menu install exit {code}: {reason}, the way back still offered')
        begin().job_done(key, 0, good)
        check(guide.screen == 'menu_check' and guide.auto == 'status', 'menu version not known: checked next, by itself')
        facts['state'] = 'installed'
        guide.job_done('status', 0, [])
        check(guide.screen == 'ready' and guide.auto is None, 'the check finds the current menu: Ready')
        begin().job_done(key, 0, good)
        guide.job_done('status', 5, [])
        check(guide.screen == 'stopped' and guide.reason == 'menu_unknown' and guide.auto is None,
              'the check tells nothing: stops, never checks in a loop')
        for state in core.NO_MENU_STATES:
            begin().job_done(key, 0, good)
            facts['state'] = state
            guide.job_done('status', 0, [])
            check(guide.screen == 'stopped' and guide.reason == 'no_menu' and core.PREVIOUS_OFFER in guide.view().buttons,
                  f'the check finds no start-up menu ({state}): says so, the way back offered')
            facts.clear()
            facts['state'] = state
            guide.start()
            check(guide.choose('run') is None and guide.reason == 'no_menu' and core.PREVIOUS_OFFER not in guide.view().buttons,
                  f'no start-up menu at the start ({state}): no job, nothing to put back')
        for code, lines, reason in (
                (1, [f'{core.SAID_AUTO_FAILED}: nothing was written.'], 'auto_failed'),
                (1, [f'{core.SAID_NOT_KEPT}: you did not approve the new image, so nothing was written.'], 'not_kept'),
                (1, [f'{core.SAID_TRY_ENDED} when you pressed Approve: x. Nothing was written.'], 'try_ended'),
                (1, [f'Refused: {core.SAID_NO_ASKER}.'], 'refused'),
                (1, [core.SAID_LINK_NOT_RUNNING], 'not_running'),
                (1, [off], 'switched_off'),
                (1, [late_back], 'late_refused'),
                (1, [late_blind], 'late_refused'),
                (1, [f'Refused: {core.SAID_UPDATE_NEEDS_SOUND} (Home).'], 'needs_sound'),
                (1, [f'Refused: {core.SAID_UPDATE_UNKNOWN}.'], 'unknown'),
                (1, [core.SAID_NEITHER], 'unknown'),
                (5, ['The console was not seen coming back.'], 'not_back'),
                (1, ['Refused: x.'], 'refused'),
                (3, ['x'], 'needs_you'),
                (1, ['Refused: an unfinished console chip job exists.'], 'needs_you')):
            check(stop_of(code, lines) == reason and not guide.link_written and guide.auto is None,
                  f'one-step job exit {code} ({lines[0][:40]}): {reason}')
        check('Finish it' in guide.todo, 'an unfinished job: Finish it on the Home tab')
        check(stop_of(1, ['Refused: x.'], 'Do this now.') == 'refused' and guide.todo == 'Do this now.',
              'a refusal shows the job\'s own next step')
        # Switched off while the Approve waited: said plainly, and Update now starts again.
        check(stop_of(1, [off]) == 'switched_off'
              and guide.view().lines[0] == 'Your Chromatic was switched off: nothing was written.'
              and 'Press Update now to start again.' in guide.view().todo
              and guide.view().buttons == (('Update now', 'go', 'run'), core.CLOSE)
              and guide.choose('run') == ('job', key),
              'switched off while the Approve waited: said plainly, nothing written, Update now starts again')
        # A refusal after the Approve: nothing written, and where the console is now.
        check(stop_of(1, [late_back], core.NOTHING_CHANGED_TODO) == 'late_refused'
              and 'back on the sound image (18.40)' in guide.view().lines[0] and 'Try again' in guide.view().todo
              and [b[0] for b in guide.view().buttons] == ['Try again', 'Close'],
              'a refusal after Approve: nothing written, back on the image it had, Try again')
        check(stop_of(1, [late_back], 'Do this now.') == 'late_refused' and guide.view().todo == 'Do this now.',
              'and the job\'s own next step is shown when it has one')
        check(stop_of(1, [late_blind]) == 'late_refused'
              and guide.view().todo.startswith('Switch your Chromatic off and on')
              and 'not seen restarting' in guide.view().lines[0] and 'your original image' in guide.view().lines[0],
              'the same when the restart was not seen: switch it off and on first')
        # A start-up menu from before 1.3.5 does not say which image runs by itself, so the
        # automatic checks fail (nothing written). The plain next step: update the menu, then Update now again.
        failed = [f'{core.SAID_AUTO_FAILED}: nothing was written.']
        for launcher, state, reason in (('1.0.0', 'outdated', 'menu_too_old'), ('1.3.4', 'outdated', 'menu_too_old'),
                                        ('1.3.5', 'outdated', 'auto_failed'), ('1.10.0', 'outdated', 'auto_failed'),
                                        ('1.0.0', 'installed', 'auto_failed'), (None, 'outdated', 'auto_failed'),
                                        ('old', 'outdated', 'auto_failed')):
            begin(state)
            facts['launcher'] = launcher
            guide.job_done(key, 1, failed)
            check(guide.screen == 'stopped' and guide.reason == reason and not guide.link_written and guide.auto is None,
                  f'failed checks with start-up menu {launcher} ({state}): {reason}')

        def older_menu():
            begin('outdated')
            facts['launcher'] = '1.0.0'
            guide.job_done(key, 1, failed)
            return guide.view()
        view = older_menu()
        check(view.headline == 'Update the menu first' and 'Nothing was written' in view.lines[0]
              and 'older than 1.3.5' in view.lines[1] and 'Update the menu' in view.todo and 'Update now again' in view.todo
              and view.buttons == (('Update the menu', 'go', 'job:install'), core.CLOSE)
              and guide.choose('job:install') == ('job', 'install'),
              'an older menu failed the checks: why, and one button (Update the menu)')
        guide.cancelled('install')
        check(guide.screen == 'stopped' and guide.reason == 'menu_too_old' and guide.auto is None,
              'Cancel on that menu update: the stop keeps its button')
        guide.job_done('install', 0, [])
        check(guide.screen == 'start' and 'Start-up menu updated' in guide.note and 'press Update now' in guide.note
              and not guide.link_written and guide.auto is None and core.PREVIOUS_OFFER not in guide.view().buttons
              and guide.choose('run') == ('job', key),
              'the menu updated first: back at the start, Update now runs the update again')
        older_menu()
        guide.job_done('install', 1, [], 'Head', 'Do this.')
        check(guide.reason == 'refused' and guide.todo == 'Do this.' and not guide.link_written
              and core.PREVIOUS_OFFER not in guide.view().buttons,
              'that menu update refused: said so; no Pocket link image was written, so no way back is offered')
        check(stop_of(5, [wrote, core.SAID_LINK_NOT_FROM_FLASH]) == 'not_heard' and guide.link_written
              and [b[0] for b in guide.view().buttons] == ['Check again', 'Put the previous image back', 'Close'],
              'written but not heard: Check again, the way back, Close')
        for case, reason in (('undone', 'link_undone'), ('finished', 'not_heard')):
            code, lines = recovered_lines(case)
            check(stop_of(code, lines) == reason and guide.link_written == (case == 'finished'),
                  f'a cut-off write put right ({case}): {reason}, in recover\'s real words')
        # A failed Pocket link write lands on the original image (recover, unchanged). The stop
        # says where the console is and gives the one button back to the sound image: one Approve in the same
        # session; after this app was closed, Restore FPGA first and the sound image by itself (two Approves).
        code, lines = recovered_lines('undone')
        check(stop_of(code, lines) == 'link_undone', 'a failed write, undone')
        view = guide.view()
        said = ' '.join(view.lines)
        check('original image (18.14)' in said and 'games work' in said and 'Jellyfin has no sound' in said
              and 'sound image (18.40)' in said and 'Sound image back' in view.todo and 'Restore FPGA' in view.todo
              and view.buttons == (core.SOUND_BACK_OFFER, core.CLOSE),
              'a failed write, undone: the original image (18.14), games work, no Jellyfin sound; one button back')
        check(guide.choose('job:fpga_media') == ('job', 'fpga_media'), 'Sound image back is one job')
        guide.job_done('fpga_media', 0, [])
        check(guide.screen == 'sound_back' and not guide.link_written and guide.auto is None,
              'same session: the sound image is back after one Approve')
        code, lines = recovered_lines('undone', True)
        check(stop_of(code, lines) == 'link_undone' and guide.todo.startswith('It was not seen restarting: switch your '
                                                                              'Chromatic off and on')
              and guide.todo.endswith('then press Sound image back.'),
              'undone and not seen restarting: off and on first, then the same button')
        refused_sound = [f'Refused, nothing written: {cf.NO_REHEARSAL}']
        check(core.SAID_NO_REHEARSAL in cf.NO_REHEARSAL, 'the window reads the job\'s own words for a missing rehearsal')

        def restore_first():
            code, lines = recovered_lines('undone')
            stop_of(code, lines)
            guide.choose('job:fpga_media')
            guide.job_done('fpga_media', 1, refused_sound, 'Head', 'Do this.')
        restore_first()
        check(guide.screen == 'stopped' and guide.reason == 'sound_needs_restore' and guide.auto is None
              and guide.view().headline == 'Restore FPGA comes first' and 'Nothing was written' in guide.view().lines[0]
              and guide.view().buttons == (core.RESTORE_FIRST_OFFER, core.CLOSE),
              'the sound image refused for want of a rehearsal (the app was restarted): Restore FPGA comes first, one button')
        check(guide.choose(core.RESTORE_THEN_SOUND) == ('job', 'fpga_restore') and guide.chain == 'restore',
              'Restore FPGA, from that stop')
        guide.cancelled('fpga_restore')
        check(guide.screen == 'stopped' and guide.reason == 'sound_needs_restore' and guide.auto is None,
              'Cancel on its Approve: the stop keeps its button')
        check(guide.choose(core.RESTORE_THEN_SOUND) == ('job', 'fpga_restore'), 'pressed again')
        guide.job_done('fpga_restore', 0, [])
        check(guide.screen == 'sound_next' and guide.auto == 'fpga_media' and guide.chain == 'sound'
              and guide.view().buttons[0] == core.SOUND_BACK_OFFER and not guide.link_written,
              'Restore FPGA succeeded: the sound image is next, by itself')
        guide.cancelled('fpga_media')
        check(guide.screen == 'sound_next' and guide.auto is None, 'Cancel on the sound image\'s Approve: the screen keeps its button')
        check(guide.choose('job:fpga_media') == ('job', 'fpga_media') and guide.chain == 'sound', 'Sound image back, pressed')
        guide.job_done('fpga_media', 1, refused_sound, 'Head', 'Do this.')
        check(guide.screen == 'stopped' and guide.reason == 'refused' and guide.todo == 'Do this.' and guide.auto is None
              and guide.chain is None, 'refused even then: a plain stop, never Restore FPGA in a loop')
        restore_first()
        guide.choose(core.RESTORE_THEN_SOUND)
        guide.job_done('fpga_restore', 0, [])
        guide.choose('job:fpga_media')
        guide.job_done('fpga_media', 0, [])
        check(guide.screen == 'sound_back' and not guide.link_written and guide.auto is None and guide.chain is None,
              'Restore FPGA, then the sound image: 18.40 is back')
        restore_first()
        guide.choose(core.RESTORE_THEN_SOUND)
        guide.job_done('fpga_restore', 1, [], 'Head', 'Do this.')
        check(guide.screen == 'stopped' and guide.reason == 'refused' and guide.todo == 'Do this.' and guide.auto is None
              and guide.chain is None, 'Restore FPGA refused in that chain: said so, nothing follows by itself')
        begin('installed')
        guide.job_done('fpga_restore', 0, [])
        check(guide.screen == 'restored' and guide.auto is None, 'a Restore FPGA outside the chain: nothing follows it')
        begin('installed').job_done(key, 0, good)
        check(guide.choose('job:fpga_media') == ('job', 'fpga_media'), 'Put the previous image back is one job')
        guide.job_done('fpga_media', 0, [])
        check(guide.screen == 'sound_back' and not guide.link_written, 'the sound image is back')
        begin('installed').job_done(key, 0, good)
        guide.job_done('fpga_media', 1, [], 'Head', 'Do this.')
        check(guide.reason == 'refused' and guide.link_written, 'the way back refused: said so, still offered')
        public = guide.public_view()
        check(set(public) == {'guide', 'screen', 'headline', 'what_to_do', 'tone', 'starts_next', 'buttons'}
              and public['screen'] == 'refused' and public['guide'] == 'Update the Pocket link',
              'what the API may see of the screen: fixed wording only')
        guide.start(asked_by='A Program')
        check('Asked for by A Program' in guide.view('IMAGE-X').lines[0] and 'Image: IMAGE-X' in guide.view('IMAGE-X').lines[-1]
              and 'A Program' not in json.dumps(guide.public_view()) and 'IMAGE-X' not in json.dumps(guide.public_view()),
              'the start screen names who asked and the image; the API view names neither')
        check(guide.choose('step_by_step') == ('guide', 'link') and not guide.active and guide.public_view() is None,
              'Step by step: closes this screen for the question-based guide')
        guide.start(ready=False)
        check(guide.reason == 'no_image' and guide.view().headline == 'Pocket link not ready', 'no reviewed image: says so')
        begin('installed').job_done(key, 1, [f'Refused: {core.SAID_UPDATE_NEEDS_SOUND}.'])
        view = guide.view()
        check(view.buttons == (core.SOUND_SETUP_OFFER, core.SOUND_CHAIN_OFFER, core.CLOSE)
              and 'original image (18.14)' in view.lines[0] and 'Jellyfin has no sound' in view.lines[0]
              and 'Nothing was written' in view.lines[0] and 'Sound image back' in view.todo and 'Restore FPGA' in view.todo,
              'the original image (after a restart, too): where the console is; Set up sound, or Sound image back')
        check(guide.choose(core.RESTORE_THEN_SOUND) == ('job', 'fpga_restore') and guide.chain == 'restore',
              'Sound image back there: Restore FPGA first (the sound image follows by itself)')
        begin('installed').job_done(key, 1, [f'Refused: {core.SAID_UPDATE_NEEDS_SOUND}.'])
        check(guide.choose('sound_guide') == ('guide', 'sound') and not guide.active, 'Set up sound: the sound guide')
        begin('installed').job_done(key, 1, [core.SAID_NEITHER])
        check(guide.choose('fix') == ('tab', 'recovery') and not guide.active, 'Fix a problem: that tab')
        guide.start()
        guide.choose('stop')
        check(guide.reason == 'cancelled' and guide.choose('close') == ('close',) and not guide.active, 'Stop, then Close')
        check(not list(Path(folder).iterdir()), 'the one-step screen keeps no file')

    # ---- in the window: an older start-up menu (Fix a problem's button; Home offers the menu update there) ----
    fr = tf.Rig(flash=sound)
    seen = []
    # (The menu on the console is 1.3.5, the first that says which image runs by itself; this PC has a newer one.)
    rig, w = window(fr, ['no', 'yes', 'yes'], dict(current, state='outdated', launcher='1.3.5', release='1.3.6'))
    rig.dialogs.keeps.append(looks(w, fr, seen))
    check(w.primary.text() != gui.MainWindow.LINK_BUTTON and w.fix_link.text() == gui.MainWindow.LINK_BUTTON,
          'an older menu: Home keeps its own button; Fix a problem has Update the Pocket link')
    from_fix(w)
    card = w.guide_card.sub.text()
    check(w.sound is w.link_update and w.sound.screen == 'start' and w.title.text() == core.LinkUpdateGuide.TITLE
          and w.guide_step.text() == core.LinkUpdateGuide.TAG and w.guide_strip.isHidden() and w.guide_time.text() == ''
          and shown(w) == ['Update now', 'Step by step'] and not w.guide_stop.isHidden()
          and f'Image: {LINK_IMAGE}' in card and 'No question rounds' in card and 'press Approve once' in card
          and 'Nothing is written before your Approve' in card
          and w.console_art.tone == 'link' and not any(t.isEnabled() for t in w.tabs.values())
          and fr.chip.reads == 0 and rig.dialogs.shown == [],
          'the one-step screen: one button, the image named, no step strip, nothing read or asked yet')
    check(w.service.state()['guided_setup'] == dict(w.link_update.public_view(), screen='start')
          and w.service.state()['guided_setup']['buttons'] == ['Update now', 'Step by step', 'Stop setup'],
          'the API sees which screen the owner is on')
    guide_fits(w, 'update start')
    check(press_guide(w, 0, 240) == cf.EXIT_OK and fr.flash() == fr.expected_link() and fr.chip.sram_loads == 1
          and fr.chip.writes == 1, 'Update now: tried from RAM once, approved while it ran, written, read back')
    check(rig.dialogs.kept == [asked_keep] and rig.dialogs.asked == [] and len(seen) == 1,
          'Update now showed no dialog; the job asked for its one Approve, in the job\'s own words')
    at = seen[0]
    check(at['ram'] and at['writes'] == 0 and at['flash'] == sound and at['busy'],
          'that Approve is asked with the new image running from RAM and the flash not yet written')
    check(at['page'] and at['state'] == 'waiting' and at['tag'] == 'YOUR TURN: DO THIS NOW' and at['head'] == cf.KEEP_TITLE
          and at['text'] == cf.keep_text('media') and not at['timed'] and not at['ok'] and at['tone'] == 'update'
          and not at['tabs'], 'meanwhile the sign fills the screen: The new image is running now, start a game and '
          'look, no countdown')
    check(at['fits'] is True, f'and it fits the speech bubble ({at["fits"]})')
    view = at['view']
    check(view.get('state') == 'waiting' and view.get('kind') == 'action' and view.get('title') == cf.KEEP_TITLE
          and view.get('instruction') == cf.keep_text('media') and view.get('seconds_allowed') is None
          and view.get('seconds_left') is None and view.get('shown') and at['progress'] == view,
          'the API says the owner is asked to look and approve, with no time limit')
    check(quiet(w) and w.sound.screen == 'menu' and rig.dialogs.shown == ['install'] and shown(w) == ['Update the menu']
          and w.link_update.auto is None and rig.h.flash() == stock,
          'the menu update asked for by itself with its own Approve; Cancel there writes nothing and keeps the button')
    log = rig.log_text()
    check(cf.SAID_AUTO_PASSED in log and all(f'Step {n} of 5' in log for n in (1, 2, 3, 4, 5)) and cf.SAID_LINK_FROM_FLASH in log
          and f'Confirmed by {core.STARTED_IN_WINDOW}' in log and log.count(f'[owner] asked on screen: {cf.KEEP_TITLE}') == 1
          and log.count(f'[owner] done: {cf.KEEP_TITLE}') == 1 and core.SAID_KEPT in log
          and cf.POWER_CYCLE_TITLE not in log and log.count('[owner] asked on screen') == 1,
          'the log: five steps, the automatic checks passed, the Approve asked and given as the one owner action, '
          'confirmed from flash')
    check(w.service.result()['headline'] == 'Pocket link updated' and w.service.summary['fpga'] == 'link',
          'the job\'s result and the summary: Pocket link updated')
    guide_fits(w, 'update menu')
    check(press_guide(w, 0, 240) == cm.EXIT_OK and w.sound.screen == 'ready' and rig.dialogs.shown == ['install', 'install']
          and rig.h.flash() != stock and w.guide_card.head.text() == 'Ready: plug in the cable'
          and shown(w) == ['OK', 'Put the previous image back'] and w.guide_stop.isHidden()
          and 'Not checked by a machine' in w.guide_card.sub.text() + w.guide_todo.text()
          and w.console_art.tone == 'ok' and w.service.state()['guided_setup']['screen'] == 'ready',
          'Update the menu, its Approve: installed, then Ready: plug in the cable, with the way back')
    guide_fits(w, 'update ready')
    check(press_guide(w, 1, 240) == cf.EXIT_OK and fr.flash() == sound and w.sound.screen == 'sound_back'
          and rig.dialogs.shown[-1] == 'fpga_media' and rig.dialogs.confirms == [],
          'Put the previous image back: one click, one Approve, the sound image again')
    check(press_guide(w, 0) is None and not w.link_update.active and w.service.state()['guided_setup'] is None
          and all(t.isEnabled() for t in w.tabs.values()), 'OK closes the screen')

    # ---- a start-up menu from before 1.3.5: it never says which image runs by itself ----
    # The automatic checks fail safe (nothing written, nothing asked); the screen says why and offers the menu
    # update; after it, Update now runs the whole update.
    fr = tf.Rig(flash=sound)
    fr.chip.reported = None  # no "fpga version=" line arrives from such a menu
    watch = OwnerWatch(fr, quiet_after_write=False)
    rig, w = window(fr, ['yes'], dict(current, state='outdated', launcher='1.0.0'), watch=watch, keeps=[True])
    from_fix(w)
    check(press_guide(w, 0, 240) == cf.EXIT_REFUSED and fr.chip.writes == 0 and fr.flash() == sound and fr.chip.sram is None
          and fr.chip.sram_loads == 1 and rig.dialogs.kept == [] and rig.dialogs.shown == []
          and w.sound.reason == 'menu_too_old' and w.guide_card.head.text() == 'Update the menu first'
          and 'Nothing was written' in w.guide_card.sub.text() and 'older than 1.3.5' in w.guide_card.sub.text()
          and shown(w) == ['Update the menu', 'Close'] and w.link_update.auto is None
          and w.service.result()['headline'] == 'Automatic checks failed' and '[owner]' not in rig.log_text()
          and rig.h.flash() == stock,
          'an older start-up menu (1.0.0): the check fails safe, nothing written or asked; next step Update the menu')
    guide_fits(w, 'update menu too old')
    check(press_guide(w, 0, 240) == cm.EXIT_OK and rig.dialogs.shown == ['install'] and rig.h.flash() != stock
          and w.sound.screen == 'start' and 'Start-up menu updated' in w.guide_card.sub.text()
          and shown(w) == ['Update now', 'Step by step'] and fr.chip.writes == 0 and fr.flash() == sound
          and w.link_update.auto is None,
          'Update the menu, its Approve: installed; back at the start with Update now')
    guide_fits(w, 'update start after the menu')
    fr.chip.reported = ...  # the new menu says which image runs
    codes = []
    w.finished.connect(codes.append)
    w.guide_buttons[0].click()
    check(pump(lambda: bool(codes) or (watch.waiting.is_set() and w._owner is not None), 240), 'Update now, again')
    if not codes:
        watch.release(True)  # the watched power cycle before the image is loaded a second time
    check(pump(lambda: bool(codes), 240) and codes == [cf.EXIT_OK] and fr.flash() == fr.expected_link()
          and fr.chip.writes == 1 and quiet(w, 240) and w.sound.screen == 'ready' and rig.dialogs.kept == [asked_keep]
          and rig.dialogs.shown == ['install'],
          'then the update runs through: tried, checked, approved while it ran, written, Ready (the menu is current)')
    w.finished.disconnect(codes.append)

    # ---- the menu's version not known: checked by itself (a read, no Approve) ----
    fr, seen = tf.Rig(flash=sound), []
    rig, w = window(fr, [], {}, image=installed_image, own_sign=True)
    rig.dialogs.keeps.append(looks(w, fr, seen))
    from_fix(w)
    check(press_guide(w, 0, 240) == cf.EXIT_OK and fr.flash() == fr.expected_link(), 'unknown menu: the chip job first')
    check(len(seen) == 1 and seen[0]['page'] and seen[0]['head'] == cf.KEEP_TITLE and seen[0]['view']['kind'] == 'action'
          and seen[0]['text'] == cf.keep_text('media'),
          'chip jobs with a sign of their own: the Approve dialog does not replace what the job asks with '
          '"Answer the question"')
    check(quiet(w, 240) and w.sound.screen == 'ready' and rig.dialogs.shown == [] and rig.dialogs.kept == [asked_keep]
          and w.link_update.menu_checked and rig.h.flash() == installed_image,
          'then the menu is read by itself (no Approve), found current: Ready with one Approve in all')
    fr = tf.Rig(flash=sound)
    rig, w = window(fr, [], {}, keeps=[True])
    from_fix(w)
    press_guide(w, 0, 240)
    check(quiet(w, 240) and w.sound.screen == 'stopped' and w.sound.reason == 'no_menu' and fr.flash() == fr.expected_link()
          and w.guide_card.head.text() == 'Start-up menu first' and 'Put the previous image back' in shown(w)
          and rig.h.flash() == stock and rig.dialogs.shown == [] and len(rig.dialogs.kept) == 1,
          'the read finds no start-up menu: says so, installs nothing by itself, the way back offered')
    guide_fits(w, 'update no menu')

    # ---- the stops ----
    # Not right at the one Approve (the real dialog's Not right, Esc, Enter or closing it): nothing written.
    fr = tf.Rig(flash=sound)
    rig, w = window(fr, [], keeps=[False])
    w.primary.click()
    check(press_guide(w, 0, 240) == cf.EXIT_REFUSED and fr.chip.writes == 0 and fr.flash() == sound and fr.chip.sram is None
          and fr.chip.sram_loads == 1 and rig.dialogs.kept == [asked_keep] and rig.dialogs.shown == []
          and w.sound.reason == 'not_kept' and w.guide_card.head.text() == 'Not kept. Nothing was written'
          and shown(w) == ['Try again', 'Step by step', 'Close'] and w.link_update.auto is None
          and w.service.result()['headline'] == 'Not kept. Nothing was written',
          'Not right: nothing written, the chip back on its flash image, Try again or Step by step')
    log = rig.log_text()
    check(pump(lambda: w._owner is None) and w.stack.currentWidget() is w.pages['guide'] and w.service.owner_view() is None
          and f'[owner] asked on screen: {cf.KEEP_TITLE}' in log and '[owner] done' not in log and core.SAID_NOT_KEPT in log
          and core.SAID_KEPT not in log, 'the sign is taken down without a thank-you, and the log says Not kept')
    guide_fits(w, 'update not kept')
    # Approve after the console was switched off and on while the sign waited: it reports 18.40 again.
    fr = tf.Rig(flash=sound)

    def off_and_on(prompt, fr=fr):
        fr.chip.configured, fr.chip.sram = True, None  # off and on between two looks at the USB list: back at its menu
        return True
    rig, w = window(fr, [], keeps=[off_and_on])
    w.primary.click()
    check(press_guide(w, 0, 240) == cf.EXIT_REFUSED and fr.chip.writes == 0 and fr.flash() == sound
          and w.sound.reason == 'try_ended' and w.guide_card.head.text() == 'Approve came too late'
          and 'Nothing was written' in w.guide_card.sub.text() and shown(w) == ['Try again', 'Step by step', 'Close']
          and w.service.result()['headline'] == 'Approve came too late. Nothing was written'
          and core.SAID_TRY_ENDED in rig.log_text() and core.SAID_KEPT not in rig.log_text(),
          'Approve after an off-and-on: the console no longer runs the new image, so nothing is written')
    guide_fits(w, 'update approve too late')
    # Switched off and on while the sign waited, then a game started (stock firmware: silent),
    # then Approve: the watch on the USB list saw the port leave and the chip configure again, so nothing is
    # written, whatever the version read could not hear.
    fr = tf.Rig(flash=sound)
    watched = []

    def off_on_then_game(prompt, fr=fr):
        chip = fr.chip

        def after_looks(count):
            target, deadline = chip.usb_looks + count, time.monotonic() + 20
            while chip.usb_looks < target and time.monotonic() < deadline:
                time.sleep(0.02)
            return chip.usb_looks >= target
        ok = after_looks(1)        # seen there, running the new image at its menu (0x0100)
        chip.usb_off = True        # switched off ...
        ok = after_looks(2) and ok
        chip.configured, chip.sram, chip.player, chip.usb_off = True, None, 0, False  # ... and on: the flash image
        ok = after_looks(1) and ok  # (the watch stops looking once it has seen that)
        chip.player, chip.reported = 1, None  # a game is started: stock firmware, which says nothing
        watched.append(ok)
        return True
    rig, w = window(fr, [], keeps=[off_on_then_game])
    w.primary.click()
    code = press_guide(w, 0, 240)
    check(code == cf.EXIT_REFUSED and watched == [True] and fr.chip.writes == 0 and fr.flash() == sound
          and fr.chip.sram is None and rig.dialogs.kept == [asked_keep]
          and core.SAID_SWITCHED_OFF in rig.log_text() and core.SAID_KEPT not in rig.log_text(),
          'off and on during the Approve, then a silent game, then Approve: the watch on the USB list saw it; nothing '
          f'written (exit {code}, watched {watched}, writes {fr.chip.writes})')
    check(w.sound.reason == 'switched_off' and w.guide_card.head.text() == 'Switched off: nothing written'
          and 'Your Chromatic was switched off: nothing was written.' in w.guide_card.sub.text()
          and 'Press Update now to start again.' in w.guide_todo.text() and shown(w) == ['Update now', 'Close']
          and w.service.result()['headline'] == 'Switched off. Nothing was written' and w.link_update.auto is None,
          f'and the screen says switched off, with Update now to start again (stop {w.sound.reason}: '
          f'{w.guide_card.head.text()} / {w.service.result()["headline"]} / {shown(w)})')
    guide_fits(w, 'update switched off')
    # A refusal after the Approve and before any write (here the backup check fails by then):
    # the chip is restarted from its flash and the screen says the console is back on the image it had.
    fr = tf.Rig(flash=sound)

    def backup_breaks(prompt, fr=fr):
        fr.backup_fails = 'the second copy of the original FPGA image (other disk) differs.'
        return True
    rig, w = window(fr, [], keeps=[backup_breaks])
    w.primary.click()
    code = press_guide(w, 0, 240)
    check(code == cf.EXIT_REFUSED and fr.chip.writes == 0 and fr.flash() == sound and fr.chip.sram is None
          and fr.chip.sram_loads == 1 and core.SAID_LATE_REFUSAL in rig.log_text() and core.SAID_KEPT in rig.log_text(),
          'a refusal after the Approve: nothing written, and the chip is back on its flash image '
          f'(exit {code}, writes {fr.chip.writes}, loads {fr.chip.sram_loads})')
    result = w.service.result()
    check(w.sound.reason == 'late_refused' and w.guide_card.head.text() == 'Stopped. Nothing was written'
          and 'Your Chromatic is back on the sound image (18.40)' in w.guide_card.sub.text()
          and shown(w) == ['Try again', 'Close'] and w.guide_todo.text() and w.link_update.auto is None
          and result['headline'] == 'Stopped. Nothing was written' and 'back on the sound image (18.40)' in result['next'],
          f'and the screen says so: nothing written, back on 18.40, Try again (stop {w.sound.reason}: '
          f'{w.guide_card.head.text()} / {result["headline"]} / {shown(w)})')
    guide_fits(w, 'update refused after approve')
    # A Pocket link write that fails its read-back: the original is written back and checked
    # (recover, unchanged). The screen says where the console is, and one button and one Approve bring the
    # sound image back in the same session.
    fr = tf.Rig(flash=sound)
    fr.chip.corrupt_after_write = 5000
    rig, w = window(fr, ['yes'], keeps=[True])
    w.primary.click()
    check(press_guide(w, 0, 240) == cf.EXIT_RECOVERED and fr.flash() == tf.ORIGINAL and w.sound.reason == 'link_undone'
          and w.guide_card.head.text() == 'Pocket link not installed' and shown(w) == ['Sound image back', 'Close']
          and 'original image (18.14)' in w.guide_card.sub.text() and 'games work' in w.guide_card.sub.text()
          and 'Jellyfin has no sound' in w.guide_card.sub.text() and 'Sound image back' in w.guide_todo.text()
          and w.link_update.auto is None and rig.dialogs.shown == [],
          'a failed Pocket link write: the original is back, and the screen says so: 18.14, games work, no Jellyfin sound')
    guide_fits(w, 'update write undone')
    check(press_guide(w, 0, 240) == cf.EXIT_OK and fr.flash() == sound and w.sound.screen == 'sound_back'
          and rig.dialogs.shown == ['fpga_media'] and rig.dialogs.confirms == [],
          'Sound image back, same session: one click, one Approve, 18.40 again')
    fr = tf.Rig(flash=sound)
    fr.timing.boot = 90
    fr.chip.phase = 'boot'  # the start-up menu answers, but never reaches its menu
    watch = OwnerWatch(fr, quiet_after_write=False)
    rig, w = window(fr, [], watch=watch, keeps=[True])
    check(w.primary.isVisible() and w.primary.text() == gui.MainWindow.LINK_BUTTON, 'Home on the sound image offers it')
    w.primary.click()
    check(press_guide(w, 0, 240) == cf.EXIT_REFUSED and fr.chip.writes == 0 and fr.flash() == sound and fr.chip.sram is None
          and w.sound.reason == 'auto_failed' and w.guide_card.head.text() == 'Automatic checks failed'
          and w.console_art.tone == 'error' and shown(w) == ['Try again', 'Step by step', 'Close']
          and w.service.result()['headline'] == 'Automatic checks failed' and w.link_update.auto is None
          and rig.dialogs.kept == [] and '[owner]' not in rig.log_text(),
          'a failed automatic check: red, nothing written, nothing asked, the chip back on its flash image, Try again '
          'or Step by step')
    guide_fits(w, 'update checks failed')
    fr.chip.phase = 'menu'
    codes = []
    w.finished.connect(codes.append)
    w.guide_buttons[0].click()
    check(pump(lambda: watch.waiting.is_set() and w._owner is not None, 240) and w.busy()
          and w.stack.currentWidget() is w.pages['owner'] and w.owner_banner.head.text() == cf.POWER_CYCLE_TITLE
          and w.owner_banner.count.text() in ('90', '89') and fr.chip.sram_loads == 1 and fr.flash() == sound,
          'Try again: before the image is loaded again the sign asks for the power cycle, and the job watches for it')
    watch.release(True)  # the owner switches it off and on while the job watches
    check(pump(lambda: bool(codes), 240) and codes == [cf.EXIT_OK] and fr.flash() == fr.expected_link()
          and fr.chip.sram_loads == 2 and quiet(w) and w.sound.screen == 'ready' and pump(lambda: w._owner is None)
          and rig.dialogs.kept == [asked_keep],
          'switched off and on: tried again, checked, approved while it ran, written, Ready')
    log = rig.log_text()
    check(log.count(f'[owner] asked on screen: {cf.POWER_CYCLE_TITLE}') == 1 and log.count(f'[owner] done: {cf.POWER_CYCLE_TITLE}') == 1
          and 'Switch the console OFF and ON now' in log,
          'the second try asked for the watched power cycle once, as an owner action, and still wrote it in the log')
    fr = tf.Rig()  # the original image, no rehearsal
    rig, w = window(fr, [], dict(current, fpga='18.14'), keeps=[True])
    from_fix(w)
    check(press_guide(w, 0, 240) == cf.EXIT_REFUSED and fr.chip.writes == 0 and fr.chip.sram_loads == 0
          and w.sound.reason == 'needs_sound' and shown(w) == ['Set up sound', 'Sound image back', 'Close']
          and 'original image (18.14)' in w.guide_card.sub.text() and rig.dialogs.kept == [],
          'the original image without the sound setup: refused, nothing loaded; Set up sound, or Sound image back')
    guide_fits(w, 'update needs sound')
    check(press_guide(w, 0) is None and w.sound is w.sound_guide and w.sound.active, 'Set up sound opens the sound guide')
    press_guide(w, 'stop')
    # The same place after a failed Pocket link write once this app was closed and opened again:
    # the original image, no rehearsal on record. Sound image back: Restore FPGA, then the sound image by
    # itself, one Approve each and no questions.
    fr = tf.Rig()
    rig, w = window(fr, ['yes', 'yes'], dict(current, fpga='18.14'), keeps=[True])
    from_fix(w)
    check(press_guide(w, 0, 240) == cf.EXIT_REFUSED and w.sound.reason == 'needs_sound' and fr.chip.writes == 0,
          'the original image after a restart: Update now is refused, nothing written')
    check(press_guide(w, 1, 240) == cf.EXIT_OK and rig.dialogs.shown[0] == 'fpga_restore',
          'Sound image back: Restore FPGA first, with its own Approve')
    check(quiet(w, 240) and w.sound.screen == 'sound_back' and fr.flash() == sound
          and rig.dialogs.shown == ['fpga_restore', 'fpga_media'] and rig.dialogs.confirms == [] and rig.dialogs.kept == []
          and w.link_update.auto is None and w.link_update.chain is None,
          'then the sound image by itself, with its own Approve: 18.40 is back after two Approves and no questions')
    guide_fits(w, 'update sound back')
    fr = tf.Rig(flash=sound)
    rig, w = window(fr, [], dict(current, state='stock'))
    from_fix(w)
    check(press_guide(w, 0) is None and w.sound.reason == 'no_menu' and fr.chip.reads == 0 and rig.dialogs.shown == []
          and w.guide_card.head.text() == 'Start-up menu first',
          'no start-up menu on the console: no job, no Approve, nothing read')
    check(press_guide(w, 0) is None and not w.link_update.active, 'Close')
    w.link_image, w._link_image_text = (lambda: None), ...
    from_fix(w)
    check(w.sound is w.link_update and w.sound.reason == 'no_image' and w.guide_card.head.text() == 'Pocket link not ready'
          and shown(w) == ['Close'] and 'not cleared' in ' '.join(w.log_lines),
          'image not cleared (NEVER-INSTALL): the screen says so and offers no job')
    press_guide(w, 0)

    # ---- the owner-action sign, on a job that really waits for the power cycle ----
    banner = gui.OwnerBanner()
    banner.show_action({'id': 1, 'kind': 'action', 'state': 'waiting', 'title': 'Do it', 'instruction': 'Now.',
                        'seconds': 90, 'note': '', 'next': ''})
    banner.set_left(90, 90)
    check(banner.count.text() == '90' and banner.drain.lit() == gui.DrainBar.BLOCKS and banner.drain.ink == theme.PALETTE['warn'],
          'the sign alone: full time, every block lit, yellow')
    banner.set_left(10, 90)
    check(banner.count.text() == '10' and banner.drain.lit() == 3 and banner.drain.ink == theme.PALETTE['error'],
          'ten seconds left: the count and the blocks turn red')
    check(gui.OWNER_TONES == {'waiting': 'update', 'done': 'ok', 'timeout': 'error'}
          and all(ch in theme.PIXEL_GLYPHS or ch == ' ' for ch in ''.join(gui.OWNER_TAGS.values()) + owner_actions.THANKS
                  + owner_actions.TIMED_OUT + cf.POWER_CYCLE_TITLE + cf.KEEP_TITLE + owner_actions.QUESTION_TITLE),
          'the sign\'s tones, and its big words in pixel letters')

    def held():
        """A window whose one-step job is held at the power cycle it asks for after the write."""
        fr = tf.Rig(flash=sound)
        fr.timing.boot = 90  # the real wait
        watch = OwnerWatch(fr)
        rig, w = window(fr, watch=watch, keeps=[True])
        seen, codes, real = [], [], w.owner_banner.show_action

        def record(action):
            real(action)
            b = w.owner_banner
            seen.append((b.state, b.head.text(), b.tag.text(), b.text.raw_text(), b.tone(), b.ok.isHidden()))
        w.owner_banner.show_action = record
        w.finished.connect(codes.append)
        w.primary.click()
        w.guide_buttons[0].click()
        check(pump(lambda: watch.waiting.is_set() and (w._owner or {}).get('title') == cf.POWER_CYCLE_TITLE, 240)
              and w.busy(), 'the job waits for the power cycle, and the window knows')
        return fr, watch, rig, w, seen, codes

    fr, watch, rig, w, seen, codes = held()
    b = w.owner_banner
    check(w.stack.currentWidget() is w.pages['owner'] and b.state == 'waiting' and b.tag.text() == 'YOUR TURN: DO THIS NOW'
          and b.head.text() == cf.POWER_CYCLE_TITLE and b.text.raw_text() == cf.POWER_CYCLE_TEXT
          and not b.count.isHidden() and not b.drain.isHidden() and b.count.text() in ('90', '89')
          and b.ok.isHidden() and b.asked.isHidden() and w.console_art.tone == 'update'
          and w.owner_calls == 2 and w.owner_timer.isActive() and fr.flash() == fr.expected_link(),
          'the sign fills the screen: YOUR TURN, Switch your Chromatic OFF, then ON, the countdown from 90')
    check(not w.guide_card.isVisible() and not any(t.isEnabled() for t in w.tabs.values()),
          'nothing else to press while it waits')
    owner_fits(w, 'waiting')
    at, clock = w._owner['at'], w.clock
    for after, count, lit, ink in ((30.2, '60', 16, 'warn'), (81, '9', 3, 'error'), (500, '0', 0, 'error')):
        w.clock = lambda: at + after
        w.refresh_owner()
        check(b.count.text() == count and b.drain.lit() == lit and b.drain.ink == theme.PALETTE[ink] and b.state == 'waiting',
              f'{after} s later the sign says {count} seconds left, {lit} blocks, {ink}; the job decides when time is up')
    w.clock = clock
    lit = b.lit
    b.tick()
    check(b.lit != lit, 'the border blinks while it waits')
    view = w.service.owner_view()
    check(view['state'] == 'waiting' and view['kind'] == 'action' and view['title'] == cf.POWER_CYCLE_TITLE
          and view['instruction'] == cf.POWER_CYCLE_TEXT and view['seconds_allowed'] == 90
          and 0 <= view['seconds_left'] <= 90 and view['next_step'] is None and view['shown']
          and w.service.progress_view()['owner_action_needed'] == dict(view, seconds_left=w.service.progress_view()[
              'owner_action_needed']['seconds_left']),
          'the API says what the owner must do now, with the seconds allowed and left')
    log = rig.log_text()
    check(f'[owner] asked on screen: {cf.POWER_CYCLE_TITLE} (within 90 s)' in log
          and 'Switch the console OFF and ON now, while this tool watches (up to 90 s)' in log,
          'the same request is still written in the log')
    w.on_owner({'id': 999, 'kind': 'action', 'state': 'done', 'title': 'x', 'instruction': '', 'seconds': None,
                'note': '', 'next': ''})
    check(b.state == 'waiting', 'an event for another request changes nothing')
    watch.release(True)
    check(pump(lambda: bool(codes), 240) and codes == [cf.EXIT_OK] and quiet(w), 'switched off and on: the job goes on and ends well')
    check(('done', owner_actions.THANKS, 'ALL GOOD', 'Your Chromatic was switched off and on.', 'ok', True) in seen
          and ('done', owner_actions.THANKS, 'ALL GOOD', 'You approved the new image.', 'ok', True) in seen
          and [s[:2] for s in seen] == [('waiting', cf.KEEP_TITLE), ('done', owner_actions.THANKS),
                                        ('waiting', cf.POWER_CYCLE_TITLE), ('done', owner_actions.THANKS)],
          'the sign turned green twice: for the Approve with the image running, then for the power cycle')
    check(pump(lambda: w._owner is None) and not w.owner_timer.isActive() and w.stack.currentWidget() is w.pages['guide']
          and w.sound.screen == 'ready' and w.service.owner_view() is None
          and f'[owner] done: {cf.POWER_CYCLE_TITLE}' in rig.log_text() and rig.dialogs.shown == []
          and rig.dialogs.kept == [asked_keep],
          'then the screen moves on by itself: Ready, with nothing left asked')

    fr, watch, rig, w, seen, codes = held()
    b = w.owner_banner
    watch.release(False)
    check(pump(lambda: bool(codes), 240) and codes == [cf.EXIT_NOT_SEEN] and fr.flash() == fr.expected_link(),
          'not switched off and on in time: the job ends, the image written and read back')
    check(w.stack.currentWidget() is w.pages['owner'] and b.state == 'timeout' and b.head.text() == owner_actions.TIMED_OUT
          and b.tag.text() == 'THIS NEEDS YOU' and b.text.raw_text().startswith('What to do now: The Pocket link image is in the flash')
          and 'it only checks' in b.text.raw_text() and b.asked.text() == f'You were asked: {cf.POWER_CYCLE_TITLE}'
          and not b.asked.isHidden() and not b.ok.isHidden() and b.count.isHidden() and b.drain.isHidden()
          and w.console_art.tone == 'error' and not w.owner_timer.isActive(),
          'the red sign stays in front after the job: Time ran out, what you were asked, what to do now, OK')
    owner_fits(w, 'timed out')
    view = w.service.owner_view()
    check(f'[owner] time ran out: {cf.POWER_CYCLE_TITLE}' in rig.log_text() and view['state'] == 'timeout'
          and view['seconds_left'] is None and view['next_step'].startswith('The Pocket link image is in the flash')
          and w.service.progress_view()['owner_action_needed'] == view,
          'the log says the time ran out; the API keeps the missed request with its next step')
    w.link_update.auto = 'install'
    w.guide_follow('install')
    check(not w.busy() and rig.dialogs.shown == [], 'nothing starts by itself behind the red sign')
    w.link_update.auto = None
    b.ok.click()
    check(w._owner is None and w.stack.currentWidget() is w.pages['guide'] and w.sound.reason == 'not_heard'
          and w.guide_card.head.text() == 'Written, not heard from yet'
          and shown(w) == ['Check again', 'Put the previous image back', 'Close'] and w.console_art.tone != 'ok',
          'OK: the screen the job ended on, with Check again and the way back')
    guide_fits(w, 'update not heard')
    fr.chip.reported = ...
    writes = fr.chip.writes
    check(press_guide(w, 0, 240) == cf.EXIT_OK and fr.chip.writes == writes and quiet(w) and w.sound.screen == 'ready'
          and w.service.result()['headline'] == 'Pocket link already up to date' and len(rig.dialogs.kept) == 1,
          'Check again once the console reports: only a check, nothing written or asked again, Ready')

    # A request to wait that arrives while no job runs is not shown (nothing could end it).
    rig, w = window(tf.Rig(flash=sound))
    w.on_owner({'id': 1, 'kind': 'action', 'state': 'waiting', 'title': 'x', 'instruction': 'y', 'seconds': 5,
                'note': '', 'next': ''})
    check(w._owner is None and w.stack.currentWidget() is not w.pages['owner'], 'no job, no sign')
    for window_ in windows:
        window_.close()


def credit_checks(stock, release):
    """Owner: "Created by ChaoticSPARTAN1" at the bottom of the window (small, subtle,
    never clipped at the compact size) and on the About tab."""
    rig = Rig(stock, release)
    w = rig.window
    w.resize(*gui.WINDOW)
    w.show()
    w.show_tab('console')
    QApplication.processEvents()
    credit = w.credit
    need = credit.fontMetrics().horizontalAdvance(gui.CREDIT)
    spot = credit.geometry()
    logo = w.logo.geometry()
    check(credit.isVisible() and credit.text() == gui.CREDIT and credit.width() >= need
          and credit.mapTo(w, credit.rect().bottomRight()).x() <= w.width()
          and credit.mapTo(w, credit.rect().bottomRight()).y() <= w.height() and spot.right() < logo.left(),
          'Home at the compact size: the credit shows whole, beside the logo, inside the window')
    smallest = min(b.base_pt for b in visible_buttons(w))
    check(credit.font().pointSizeF() < smallest and credit.property('role') == 'dim',
          f'the credit is smaller than the button text ({credit.font().pointSizeF()} < {smallest}) and subtle')
    w.set_log_open(True)
    QApplication.processEvents()
    check(credit.isVisible() and credit.width() >= need, 'the credit stays whole with the log open')
    w.set_log_open(False)
    w.show_tab('about')
    QApplication.processEvents()
    check(w.about_credit.isVisible() and w.about_credit.text() == gui.CREDIT, 'the About tab shows the credit too')
    w.close()


PRIVATE_SAMPLE = {  # one made-up value per kind the window must hide
    'IPv4 address': ('Use this PC at 192.0.2.20 for Jellyfin', '192.0.2.20'),
    'IPv6 address': ('Gateway fe80::1c2b:3aff:fe4d:5e6f%12 answered', 'fe80::1c2b'),
    'MAC': ('Wi-Fi MAC 24:6f:28:aa:bb:cc', '24:6f:28:aa:bb:cc'),
    'serial number': ('Serial: CHR0012345X', 'CHR0012345X'),
    'device ID': ('device id=0a1b2c3d4e5f6071', '0a1b2c3d4e5f6071'),
    'Wi-Fi network name': ('Wi-Fi network "Maple Home 5G" saved; SSID: Cafe_Guest', 'Maple Home'),
    'Wi-Fi name (labelled)': ('SSID: Cafe_Guest', 'Cafe_Guest'),
    'Jellyfin server address': ('Jellyfin server: http://192.0.2.30:8096/web', '192.0.2.30'),
    'server host and port': ('Jellyfin at media-box.local:8920', 'media-box'),
    'port': ('listening on port 8096', '8096'),
    'user name': ('Jellyfin user: sam_b', 'sam_b'),
    'token': ('token=0123456789abcdef0123456789abcdef', '0123456789abcdef0123456789abcdef'),
    'pairing code': ('Pairing code 482913: check the console', '482913'),
    'Wi-Fi name (joined)': ('wifi: joined "Cafe Corner" at 198.51.100.9', 'Cafe Corner'),
    'path with a user name': (r'Saved in C:\Users\someone\Documents\x.txt', 'someone'),
    'this PC\'s user name': (f'Second copy ({Path.home() / "Jellymatic-Backups" / "manager"})', Path.home().name),
    'private folder': (r'H:\work\private\chromatic-manager-backups\20261108-101500', 'chromatic-manager-backups'),
    'backup path': ('Backup kept in private/chromatic-tools/latest', 'chromatic-tools'),
}


def privacy_checks(stock, release):
    """Owner: a small pixel eye at the top of the window hides private details (as
    ****), on by default at every start, with a plain tooltip. Display only: log files and the
    job keep the real text, and the toggle shows everything again."""
    from chromatic_manager_core import mask_private
    for kind, (text, secret) in PRIVATE_SAMPLE.items():
        check(secret.lower() not in mask_private(text).lower() and '*' in mask_private(text), f'masked: {kind}')
    plain = ('17:39:01  Stock firmware: version v4.6 (ESP-IDF 5.3.1). Using COM3. Step 2 of 4. Took 6:30. '
             'Start-up menu 1.1.0 installed; sha256 ' + 'ab' * 32)
    check(mask_private(plain) == plain, 'ordinary steps, versions, times, COM ports and content hashes stay readable')
    rig = Rig(stock, release)
    w = rig.window
    w.resize(*gui.WINDOW)
    w.show()
    QApplication.processEvents()
    check(gui.Privacy.on and w.privacy.isChecked() and w.privacy.isVisible() and not w.privacy.text()
          and w.privacy.toolTip() == gui.PRIVACY_TIP_ON and 'Click to show' in w.privacy.toolTip(),
          'private details are hidden by default: the pixel eye is on, with a plain tooltip')
    top = w.close_box.mapTo(w, w.close_box.rect().topLeft())
    eye = w.privacy.mapTo(w, w.privacy.rect().topLeft())
    check(abs(eye.y() - top.y()) < 12 and eye.x() < top.x() and top.x() - eye.x() < 60
          and eye.x() > w.tabs['about'].mapTo(w, w.tabs['about'].rect().topRight()).x(),
          'the eye sits at the top, between the tabs and the close box')
    picture = w.privacy.grab().toImage()
    inks = {picture.pixelColor(x, y).name() for x in range(picture.width()) for y in range(picture.height())}
    check({theme.PALETTE['button'], theme.PALETTE['button_hi'], theme.PALETTE['outline']} <= inks,
          'the eye is drawn as a bevelled grey pixel button in the theme\'s colours')
    sample = '\n'.join(text for text, _ in PRIVATE_SAMPLE.values())
    w.summary_setup.setText(sample)
    w.result_head.setText('Saved at 192.0.2.20')
    w.log_line(sample)
    link = next(b for b in w.findChildren(gui.FitButton) if b.text() == 'Open the backups folder')
    hidden = (w.summary_setup.text(), w.result_head.text(), w.log.toPlainText(), gui.shown_tooltip(link))
    for kind, (_, secret) in PRIVATE_SAMPLE.items():
        check(all(secret.lower() not in shown.lower() for shown in hidden[:3]), f'window and log panel hide: {kind}')
    check('chromatic-manager-backups' not in gui.shown_tooltip(link) and '*' in gui.shown_tooltip(link),
          'tooltips are masked too (the backups folder link)')
    check(w.summary_setup.raw_text() == sample and w.result_head.raw_text() == 'Saved at 192.0.2.20',
          'the real text is kept behind the mask')
    old = picture
    w.privacy.click()
    QApplication.processEvents()
    check(not gui.Privacy.on and not w.privacy.isChecked() and w.privacy.toolTip() == gui.PRIVACY_TIP_OFF
          and w.privacy.grab().toImage() != old, 'one click shows private details; the eye icon changes')
    check(w.summary_setup.text() == sample and w.result_head.text() == 'Saved at 192.0.2.20'
          and sample in w.log.toPlainText() and gui.shown_tooltip(link) == link.toolTip(),
          'shown again: labels, headlines, the log panel and tooltips have the real text')
    w.privacy.click()
    QApplication.processEvents()
    check(gui.Privacy.on and '192.0.2.20' not in w.summary_setup.text() + w.log.toPlainText(),
          'a second click hides them again')
    w.close()
    # Masking changes the screen only: a job's own messages reach its log file unchanged.
    line = 'Jellyfin server: http://192.0.2.30:8096/web, Wi-Fi network "Maple Home 5G"'
    rig = Rig(stock, release)
    make = factory(rig.h)

    def chatty(say):
        manager = make(say)
        say(line)
        return manager
    rig.window.manager_factory = chatty
    check(rig.run('status') == cm.EXIT_OK, 'a check runs the same while private details are hidden')
    check(line in rig.log_text() and line in rig.window.run_lines, 'the log file and the job keep the real text')
    shown = rig.window.log.toPlainText() + rig.window.result_text.text() + rig.window.work_line.text()
    check('192.0.2.30' not in shown and 'Maple Home' not in shown and 'Jellyfin server: http://' in shown,
          'the window shows the same line masked')
    rig.window.set_privacy(False)
    check(line in rig.window.log.toPlainText() and line in rig.window.result_text.text(),
          'and the real line once shown')
    rig.window.close()
    gui.Privacy.set(True)
    w2 = Rig(stock, release).window
    check(gui.Privacy.on and w2.privacy.isChecked(), 'every new start hides private details again')
    w2.close()


def ai_dialog_checks():
    """The AI-control pop-up: Don't allow is the default (Enter, Escape), Allow ignores input
    for its first second, Always remember is only kept with a button press."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    import chromatic_manager_core as core
    who = core.api_requester('Claude Code via MCP')
    dialog = gui.AiControlDialog(None, who, core.BY_KEY['install'])
    text = ' '.join(l.text() for l in dialog.findChildren(gui.QLabel))
    check('Claude Code via MCP (a program on this PC)' in text and 'Install the start-up menu' in text
          and 'Stop' in text and 'never sees your Wi-Fi passwords' in text and '[program via local API]' not in text,
          'the pop-up names the AI and says what it may do, in plain words')
    check(dialog.deny.isDefault() and not dialog.allow.isDefault() and not dialog.allow.isEnabled(),
          "Don't allow is the default; Allow waits a second")
    dialog.show()
    settle()
    dialog.remember.setChecked(True)
    QTest.keyClick(dialog, Qt.Key.Key_Return)
    check(dialog.answer() == (False, True), "Enter: don't allow (remembered, as ticked)")
    dialog = gui.AiControlDialog(None, who)
    dialog.show()
    settle()
    dialog.remember.setChecked(True)
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    check(dialog.answer() == (False, False), 'Escape: no, and nothing remembered')
    dialog = gui.AiControlDialog(None, who)
    dialog.show()
    QTest.mouseClick(dialog.allow, Qt.MouseButton.LeftButton)
    check(dialog.isVisible() and dialog.answer()[0] is False, 'an early click on Allow does nothing')
    settle()
    QTest.mouseClick(dialog.allow, Qt.MouseButton.LeftButton)
    check(not dialog.isVisible() and dialog.answer() == (True, False), 'Allow allows, for this run')
    dialog = gui.AiControlDialog(None, who)
    dialog.show()
    settle()
    dialog.remember.setChecked(True)
    QTest.mouseClick(dialog.allow, Qt.MouseButton.LeftButton)
    check(dialog.answer() == (True, True), 'Allow with Always remember')


def main():
    app = QApplication.instance() or QApplication([])
    theme.apply(app)
    stock = stock_dump(allow_synthetic=True)
    release = cm.Release.load()
    counts = {'actions': 0}

    # Theme: every name resolved, every colour cited, one meaning per colour (
    # success must look like success; the old mostly-red theme read as errors).
    sheet = theme.stylesheet()
    check('@' not in re.sub(r'/\*.*?\*/', '', sheet, flags=re.S), 'every palette name filled in')
    check(all(re.fullmatch(r'#[0-9a-f]{6}', v) and s for v, s in theme.PALETTE_SOURCES.values())
          and all(re.fullmatch(r'#[0-9a-f]{6}', v) and s for v, s in theme.OFFICIAL_UPDATER.values()),
          'every palette value is a colour with a source; the official values stay recorded')
    from PySide6.QtGui import QColor
    hue = {name: QColor(theme.PALETTE[name]).hsvHue() for name in ('ok', 'warn', 'error')}
    check(90 <= hue['ok'] <= 160 and 30 <= hue['warn'] <= 55 and hue['error'] <= 25,
          f'up to date green, update available amber/yellow, problem red {hue}')
    official = {k: v for k, (v, _) in theme.OFFICIAL_UPDATER.items()}
    check(theme.PALETTE['window'] == official['window'] and theme.PALETTE['tab_home'] == official['accent_system']
          and theme.PALETTE['tab_fix'] == official['accent_cart'] and theme.PALETTE['tab_about'] == official['accent_about']
          and theme.PALETTE['warn'] == official['warn'] and theme.PALETTE['ok'] == official['ok']
          and theme.PALETTE['button'] == official['button'] and theme.PALETTE['button_go'] == official['button_go'],
          'the official updater\'s recorded colours: black body, red/orange/green tabs, yellow notices, grey/green buttons')
    check('NOT copied' in theme.__doc__ and 'MODRETRO' in theme.__doc__, 'the theme says no ModRetro artwork or wordmark is copied')
    from PySide6.QtGui import QImage
    logo = theme.pixel_text_image(gui.APP_NAME, gui.LOGO_COLOURS, 1)
    check(logo.width() > 50 and len({logo.pixelColor(x, y).name() for x in range(logo.width()) for y in range(9)
                                     if logo.pixelColor(x, y).alpha()}) >= 5, 'our own multicolour pixel logo')
    check(all(ch in theme.PIXEL_GLYPHS or ch == ' ' for title in gui.MainWindow.TITLES.values() for ch in title),
          'every band title is in our own pixel letters')
    folder_fonts = theme.load_fonts()
    check(set(folder_fonts) == {'heading', 'body'} and (folder_fonts['heading'] is None) == (
        not any(p.suffix.lower() in ('.ttf', '.otf') for p in theme.FONT_FOLDER.glob('*'))
        if theme.FONT_FOLDER.is_dir() else True), 'pixel fonts load from chromatic_gui_fonts/, with a fallback')
    if OFFICIAL_STYLES.exists():  # cross-check the recorded values against the updater's own files where present
        for name, keys in (('yes_button.qss', ('yes', 'yes_hover', 'yes_pressed')),
                           ('no_button.qss', ('no', 'no_hover', 'no_pressed'))):
            official = (OFFICIAL_STYLES / name).read_text(encoding='utf-8').lower()
            check(all(theme.OFFICIAL_UPDATER[k][0] in official for k in keys), f'{name}: recorded values match')
        counts['official_styles'] = True

    # Parsers and quick answers follow the manager's real wording.
    source = inspect.getsource(cm)
    for start, _ in gui.QUICK_ANSWERS:
        check(start in source or start.replace("'", "\\'") in source, f'quick answers match the manager prompt "{start}"')
    p = gui.Progress()
    p.feed('Identifying the console from on-chip checksums (about 20 seconds; nothing is written)...')
    check(p.phase == 'Identifying the console' and p.percent is None, 'progress: identification')
    p.feed('Reading the console (all 4 MiB, about 6.5 minutes; each chunk is checked on the chip)...')
    check(p.percent == 0, 'progress: read starts')
    p.feed('  read 50%')
    check(p.percent == 50, 'progress: read percent')
    p.feed('Writing step 3 of 4 (12 KiB)...')
    check(p.phase == 'Writing step 3 of 4' and p.percent == 50, 'progress: write steps')
    found = gui.summarize(['Console: installed. Boots: start-up menu on power-on, stock otherwise.',
                           'Stock firmware: version v4.6 (ESP-IDF v5.1.2).',
                           'Launcher in flash: version 1.1.0; this tool installs 1.1.0.'])
    check(found == {'state': 'installed', 'boots': 'start-up menu on power-on, stock otherwise',
                    'stock': 'v4.6', 'idf': 'v5.1.2', 'launcher': '1.1.0', 'release': '1.1.0'}, 'status summary')
    for line, want in (('Launcher in flash: version 1.1.2, the one this tool installs (up to date).', ('1.1.2', '1.1.2')),
                       ('Launcher in flash: version 1.1.0; this tool installs 1.1.2 (choose Install to update).',
                        ('1.1.0', '1.1.2'))):
        got = gui.summarize([line])
        check((got.get('launcher'), got.get('release')) == want, f'status summary of: {line}')
    # Status lines the manager really prints are the ones the summary reads.
    check('the one this tool installs (up to date)' in source and '(choose Install to update)' in source,
          'summary follows the manager wording')

    # Every action the text menu has, plus the power-safe switch-off, has a button.
    menu_keys = {'status', 'backup', 'install', 'remove', 'restore', 'update', 'wifi', 'resume', 'abandon'}
    rig = Rig(stock, release)
    check(menu_keys | {'disable', 'jellyfin', 'jellyfin_forget', 'fpga_check', 'fpga_restore', 'fpga_media', 'fpga_trial'}
          == set(rig.window.buttons), 'a button for every manager action (console chip jobs included)')
    check(not rig.window.buttons['resume'].isEnabled() and not rig.window.buttons['abandon'].isEnabled(),
          'finish/abandon only offered when a job waits')

    # Status: reads, writes nothing, leaves the chip running, fills the summary.
    check(rig.run('status') == cm.EXIT_OK and rig.h.flash() == stock and not rig.h.console.in_stub, 'status')
    check(rig.window.summary_state.text() == gui.STATE_NAMES['stock'], 'summary shows the state')
    counts['actions'] += 1

    # A write needs the person's Approve; Cancel starts nothing.
    rig = Rig(stock, release, confirms=['y', 'yes'])
    check(rig.run('install') is None and rig.h.flash() == stock, 'install not started when cancelled')
    check(rig.run('install') == cm.EXIT_OK and rig.state() == 'installed' and not rig.h.console.in_stub,
          'install from the window')
    # The side panel showed the console before the install; it must not stay stale (owner saw
    # OUTDATED after a good install).
    check(rig.window.summary_state.text() == gui.UP_TO_DATE
          and release.version in rig.window.summary_menu.text(), 'summary updated by the install itself')
    check(rig.window.service.state()['last_status']['state'] == 'installed'
          and rig.window.service.state()['last_status']['launcher'] == release.version,
          'the API\'s last status is updated by the install too')
    installed = rig.h.flash()
    check(rig.run('status') == cm.EXIT_OK and rig.window.summary_menu.text() == f'{release.version} (up to date)'
          and rig.window.summary_state.text() == gui.UP_TO_DATE, 'status after install: up to date, never "installs"')
    check(rig.h.console.full_reads == 0, 'install and status on a backed-up console: no full read')
    rig.dialogs.confirms.append('yes')
    check(rig.run('install') == cm.EXIT_OK and rig.window.summary_state.text() == gui.UP_TO_DATE
          and any('already installed' in line for line in rig.window.lines), 'second install: still up to date')
    saved = len(rig.h.saved)
    check(rig.run('backup') == cm.EXIT_OK and len(rig.h.saved) == saved + 1 and rig.h.flash() == installed,
          'backup from the window writes nothing to the console')
    counts['actions'] += 3

    # Switch-off, install again, remove: back to the exact original.
    rig.dialogs.confirms += ['yes', 'yes', 'yes']
    check(rig.run('disable') == cm.EXIT_OK and rig.state() == 'partial' and rig.h.console.boots() == 'stock',
          'switch-off from the window')
    check(rig.run('install') == cm.EXIT_OK and rig.state() == 'installed', 'switch back on')
    check(rig.run('remove') == cm.EXIT_OK and rig.h.flash() == stock, 'remove from the window: exact original')
    check(rig.window.summary_state.text() == gui.CHANGED_TEXT, 'summary not stale after a removal')
    counts['actions'] += 2

    # Restore stock with a version change: a second approval (scripted as change) only.
    updated = with_app(stock, version='9.9.9', idf='v5.4.1')
    rig = Rig(installed, release, stock, confirms=['yes', 'yes'], answers=['nope', 'change'])
    rig.h.backup_list.insert(0, ('newer', updated, dict(IDENTITY)))
    check(rig.run('restore') == cm.EXIT_REFUSED and rig.h.flash() == installed, 'restore: wrong word refused')
    check(rig.run('restore') == cm.EXIT_OK and cm.span(rig.h.flash(), cm.APP) == cm.span(updated, cm.APP)
          and rig.state() == 'stock', 'restore with the version change approved')
    check(len(rig.h.saved) == 1 and rig.h.saved[0] == installed, 'console backed up before the version change')
    check(any(p.startswith('Type change') for p in rig.dialogs.asked), 'the change prompt reached the person')
    counts['actions'] += 1

    # Guided update: the menu is off while the person runs the updater, and comes back on.
    def person(prompt):
        check(rig.h.console.boots() == 'stock', 'menu off while the updater runs')
        rig.window.close()  # closing mid-job is refused
        rig.h.console.flash = official_update(rig.h.console.flash, stock, updated)
        return 'done'
    rig = Rig(installed, release, stock, confirms=['yes'], answers=[person])
    rig.window.show()
    check(rig.run('update') == cm.EXIT_OK and rig.state() == 'installed', 'guided update from the window')
    check(cm.span(rig.h.flash(), cm.APP) == cm.span(updated, cm.APP), 'guided update kept the new firmware')
    check(rig.window.isVisible() and any('job is running' in t for t in rig.dialogs.infos),
          'closing while a job runs is refused')
    rig.window.hide()
    rig = Rig(installed, release, stock, confirms=['yes'], answers=['', 'cancel'])
    check(rig.run('update') == cm.EXIT_OK and rig.h.console.boots() == 'stock' and len(rig.dialogs.asked) == 2,
          'guided update: empty answer re-asked, cancel leaves the menu off')
    counts['actions'] += 1

    # A question in a small window is something the owner must do too: while it waits
    # the main screen says so, big; the guided update's question keeps the sign the job itself put
    # up ("run ModRetro's updater now"). Looked at from inside the question, as the owner sees it.
    import chromatic_manager_owner as owner_actions
    looked = {}

    def looks(name, reply, act=None):
        def answer(prompt):
            w = rig.window
            looked[name] = {'view': w.service.owner_view(), 'page': w.stack.currentWidget() is w.pages['owner'],
                            'state': w.owner_banner.state, 'head': w.owner_banner.head.text(),
                            'text': w.owner_banner.text.text(), 'count': w.owner_banner.count.isHidden(),
                            'answer': w.service.progress_view()['waiting_for_owner_answer']}
            if act is not None:
                act()
            return reply
        return answer
    rig = Rig(installed, release, stock, confirms=['yes'], answers=[looks('restore', 'change')])
    rig.h.backup_list.insert(0, ('newer', updated, dict(IDENTITY)))
    rig.window.show()
    check(rig.run('restore') == cm.EXIT_OK and rig.state() == 'stock', 'restore, with its question answered')
    seen = looked['restore']
    check(seen['page'] and seen['state'] == owner_actions.WAITING and seen['head'] == owner_actions.QUESTION_TITLE
          and seen['text'] == owner_actions.QUESTION_TEXT and seen['count'] and seen['answer']
          and seen['view']['kind'] == owner_actions.QUESTION and seen['view']['seconds_left'] is None
          and seen['view']['title'] == owner_actions.QUESTION_TITLE,
          'a question in a small window: the main screen says a window is asking, with no countdown')
    check(rig.window.service.owner_view() is None and rig.window.stack.currentWidget() is not rig.window.pages['owner']
          and '[owner] asked on screen: ' + owner_actions.QUESTION_TITLE in rig.log_text()
          and '[owner] done: ' not in rig.log_text() and '[owner] time ran out' not in rig.log_text(),
          'answered: the sign is taken down (never called done or late), and the run log has the request')
    rig.window.hide()

    def updater_runs():
        rig.h.console.flash = official_update(rig.h.console.flash, stock, updated)
    rig = Rig(installed, release, stock, confirms=['yes'], answers=[looks('update', 'done', updater_runs)])
    rig.window.show()
    check(rig.run('update') == cm.EXIT_OK and rig.state() == 'installed', 'guided update, looked at from its question')
    seen = looked['update']
    check(seen['page'] and seen['state'] == owner_actions.WAITING and seen['head'] == cm.UPDATER_TITLE
          and seen['text'] == cm.UPDATER_TEXT and seen['count'] and seen['answer']
          and seen['view']['kind'] == owner_actions.ACTION and seen['view']['title'] == cm.UPDATER_TITLE
          and seen['view']['seconds_allowed'] is None,
          "guided update: the main screen says to run ModRetro's updater now, with no countdown, while it asks")
    log = rig.log_text()
    check(log.count('[owner] asked on screen: ' + cm.UPDATER_TITLE) == 1 and '[owner] done: ' + cm.UPDATER_TITLE in log
          and owner_actions.QUESTION_TITLE not in log and "Step 2 of 4: run ModRetro's updater now" in log
          and rig.window.service.owner_view() is None,
          'the updater step: asked once, done, the same step line in the log, no second sign for its question')
    rig.window.hide()

    # An unfinished job: only status, finish and abandon; other writes refused before anything.
    rig = Rig(stock, release, confirms=['yes', 'yes', 'abandon'])
    crash_mid_install(rig.h, 1)
    rig.window.refresh_pending()
    torn = rig.h.flash()
    check(rig.window.pending and not rig.window.buttons['install'].isEnabled()
          and rig.window.buttons['resume'].isEnabled() and rig.window.buttons['abandon'].isEnabled(),
          'pending job: only finish/abandon/status offered')
    check(rig.run('install', confirmed=True) == cm.EXIT_REFUSED and rig.h.flash() == torn, 'pending: install refused')
    check(rig.run('backup', confirmed=True) == cm.EXIT_REFUSED, 'pending: backup refused, as on the command line')
    check(rig.run('status') == cm.EXIT_OK and rig.h.flash() == torn, 'pending: status writes nothing')
    check(rig.run('resume') == cm.EXIT_RECOVERED and rig.h.flash() == stock and not rig.window.pending,
          'finish job from the window undoes the install')
    crash_mid_install(rig.h, 1)
    rig.window.refresh_pending()
    check(rig.run('abandon') is None and rig.window.pending, 'abandon needs approval')
    check(rig.run('abandon') == cm.EXIT_OK and not rig.window.pending, 'abandon from the window')
    counts['actions'] += 2

    # The operating-system lock: another tool holding it means nothing runs.
    rig = Rig(stock, release)
    with DeviceLock(rig.lock_path):
        check(rig.run('status') == cm.EXIT_REFUSED and any('Another Chromatic tool' in l for l in rig.window.lines),
              'lock held elsewhere: refused')
    check(rig.run('status') == cm.EXIT_OK, 'lock free again')

    # Wi-Fi setup: temporary identity and paths only; the password is never shown or logged.
    rig = Rig(installed, release, stock, confirms=['yes'],
              answers=['yes', 'TestNetwork', 'test-passphrase-123'])
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        identity = folder / 'identity'
        identity.mkdir()
        (identity / 'server.der').write_bytes(b'test certificate')
        (identity / 'pairing.bin').write_bytes(bytes(32))
        import wifi_gateway
        saved_globals = {name: getattr(cm, name) for name in ('WIFI_FILE', 'PROVISION_COPY', 'REMOTE_PILOT_PAIRING',
                                                                'lan_ipv4')}
        saved_identity = wifi_gateway.IDENTITY
        try:
            cm.WIFI_FILE = folder / 'no-wifi-file.txt'
            cm.PROVISION_COPY = folder / 'provision.json'
            cm.REMOTE_PILOT_PAIRING = folder / 'pairing.json'
            cm.lan_ipv4 = lambda: '192.0.2.20'
            wifi_gateway.IDENTITY = identity
            check(rig.run('wifi') == cm.EXIT_OK, 'Wi-Fi setup from the window')
            check((folder / 'provision.json').exists() and (folder / 'pairing.json').exists(),
                  'pairing copies written to the temporary folder only')
        finally:
            for name, value in saved_globals.items():
                setattr(cm, name, value)
            wifi_gateway.IDENTITY = saved_identity
    flash = rig.h.flash()
    check(cm.span(flash, cm.PROVISION) != b'\xff' * (cm.PROVISION[1] - cm.PROVISION[0])
          and flash[:cm.PROVISION[0]] == installed[:cm.PROVISION[0]], 'Wi-Fi wrote only the provisioning sector')
    check(len(rig.dialogs.secrets) == 1, 'the password was asked in a hidden field')
    check(not any('test-passphrase-123' in line for line in rig.window.lines)
          and 'test-passphrase-123' not in rig.window.log.toPlainText(), 'the password is never shown')
    check('TestNetwork' not in rig.window.log.toPlainText(), 'the typed network name is not echoed in the window')
    wifi_log = rig.log_text()
    check(wifi_log and not any(secret in wifi_log for secret in ('TestNetwork', 'test-passphrase-123', '192.0.2.20')),
          'the Wi-Fi log file holds no network name, password or address')
    counts['actions'] += 1

    # A windowed exe has no stdout; esptool prints anyway. quiet_console gives it a sink.
    import sys
    saved_streams = sys.stdout, sys.stderr
    try:
        sys.stdout = sys.stderr = None
        gui.quiet_console()
        print('Connecting....', end='')
        print('Hard resetting via RTS pin...', file=sys.stderr)
        check(sys.stdout is not None and sys.stderr is not None, 'windowed build: prints have a sink')
    finally:
        for stream in (sys.stdout, sys.stderr):
            if stream not in saved_streams and stream is not None:
                stream.close()
        sys.stdout, sys.stderr = saved_streams

    # A manager that cannot even start is reported, and the window carries on.
    rig = Rig(stock, release)
    rig.window.manager_factory = lambda say: (_ for _ in ()).throw(OSError('broken'))
    check(rig.run('status') == cm.EXIT_ERROR and not rig.window.busy(), 'startup failure reported')
    check(rig.window.result_head.text() == 'Something went wrong' and rig.window.result_card.tone == 'error'
          and not rig.window.result_fix.isHidden(), 'result screen says so, in orange-red, with the way to fix it')

    feedback_checks(stock, release, installed)
    dialog_checks()
    guided_checks(stock, release)
    look_checks(stock, release)
    ai_dialog_checks()
    prompt_checks(release)
    sound_guide_checks(stock, release)
    home_sound_checks(stock, release, installed)
    link_guide_checks(stock, release)
    link_update_checks(stock, release, installed)
    credit_checks(stock, release)
    privacy_checks(stock, release)

    # The window with its real dialogs: the button's Approve, then the second Approve for the
    # version change, run a restore end to end; cancelling the second writes nothing.
    presses = []
    real_exec = gui.ApproveDialog.exec

    def press(self):
        button = self.approve if presses.pop(0) == 'approve' else self.cancel
        button.setEnabled(True)
        button.click()
        return self.result()
    gui.ApproveDialog.exec = press
    try:
        for second, want in (('cancel', cm.EXIT_REFUSED), ('approve', cm.EXIT_OK)):
            rig = Rig(installed, release, stock)
            rig.window.dialogs = gui.Dialogs(rig.window)
            rig.window.dialogs.lines = lambda w=rig.window: list(w.run_lines)
            rig.h.backup_list.insert(0, ('newer', updated, dict(IDENTITY)))
            presses[:] = ['approve', second]
            check(rig.run('restore') == want and not presses, f'real dialogs: restore, version change {second}')
            check((cm.span(rig.h.flash(), cm.APP) == cm.span(updated, cm.APP)) == (second == 'approve')
                  and (rig.h.flash() == installed) == (second == 'cancel'), f'version change {second}: flash as expected')
        rig = Rig(stock, release)
        rig.window.dialogs = gui.Dialogs(rig.window)
        presses[:] = ['cancel']
        check(rig.run('install') is None and rig.h.flash() == stock and not presses, 'real dialogs: Cancel starts nothing')
    finally:
        gui.ApproveDialog.exec = real_exec

    print(f"PASS: Chromatic Manager window, {counts['actions']} button paths on the simulated console "
          '(status, backup, install, switch-off, remove, restore with approved version change, guided update, Wi-Fi, '
          'finish and abandon a job), Approve/Cancel dialogs (Enter and Esc cancel), pending-job gating, OS lock, close refused while '
          'busy, password hidden, live progress/banner/log file, self-guided Home tab (one plain status and at most '
          'one main button per console state, green Done, no greyed-out button, plain physical steps, plug-in check '
          'by itself, one-sentence what-to-do, install with Wi-Fi/Jellyfin under one Approve), button text fits at '
          'every window size, log and details collapsed by default, plain Wi-Fi dialog that fits, AI-control pop-up '
          '(default no, settle second, remember), guided Jellyfin sound setup (happy path, every "Something is '
          'wrong", Stop on every screen, Cancel, restart mid-way, refusal when Check is not original, FPGA result '
          'headlines), Home sound state from the start-up menu\'s FPGA line (on, older image with Update sound end to '
          'end, stock with the setup, unknown warning, not reported, 18.41 sound on and Pocket link ready), the guided '
          'Pocket link setup (18.41: check, try from RAM, four looks with a skippable link-cable game, answers '
          'recorded with the try, write only on a confirmed try, every "Something is wrong", sound image back in one '
          'click, restart mid-way, nothing offered while the image is not cleared; reached by Step by step), Update '
          'the Pocket link in one step (one screen, no question rounds; Update now starts the try from RAM with no '
          'dialog; ONE Approve asked with the new image running and the flash unwritten, as a big owner action with '
          'no countdown plus the real keep dialog (pixel headline, Not right as default, Approve after its settle '
          'second); Not right and Approve after an off-and-on write nothing; one more Approve for the start-up menu '
          'when it is older; the menu checked by itself when unknown; ready with Put the previous '
          'image back; every stop with its plain next step and nothing written; the owner-action sign for the '
          'watched power cycle on a job that really waits: yellow with a countdown that drains and turns red, '
          'green Done, thank you, red Time ran out with the next step until OK), the same sign for a question '
          'in a small window and for the guided update\'s run-the-updater step (no countdown, taken down when '
          'answered), the owner\'s credit on Home and About, private details hidden by '
          'default (pixel eye toggle, display only), theme values '
          f"{'checked against the official style sheets' if counts.get('official_styles') else 'checked'}. "
          'No hardware.')


if __name__ == '__main__':
    main()

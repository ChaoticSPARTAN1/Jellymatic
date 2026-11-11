# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Chromatic Manager's Remote Pilot tab and the USB hand-off (remote_pilot_link.py).

Three layers, none touching a console or a real serial port:
  * the link on its own with stand-ins: finding the folder, the hold file (also read back with
    Remote Pilot's own chromatic/usb_hold.py when that checkout is on this PC), every hand-off
    answer (not running, new receiver, older receiver with and without USB, a port that stays
    open), stale holds after a crash, the tab's states, and that only a receiver this app
    started is ever stopped;
  * the window: the tab in each state, the launch flow (start, wait, open the viewer once),
    the paused and back signs around a job, a refused job when an older Remote Pilot may hold
    USB, and closing;
  * end to end against Remote Pilot's real receiver, copied to a temporary folder (no local/
    data, so no config, pairing or port of the owner's), started as this app's child on a free
    port, in its simulated auto-sim mode (simulated USB and Wi-Fi): a Manager job on the
    simulated console pauses its USB link while Wi-Fi keeps the session, then gives USB back;
    a hold left by a crashed Manager survives a receiver restart and is cleared on the next
    start only when no job waits; closing the window stops only the receiver it started.

Run: private/chromatic-manager-gui/venv/Scripts/python.exe -B scripts/test_chromatic_manager_remote_pilot.py
"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_QPA_FONTDIR', os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts'))

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

import chromatic_manager as cm
import chromatic_manager_core as core
import chromatic_manager_gui as gui
import chromatic_theme as theme
import remote_pilot_link as link
from compose_chromatic_image import stock_dump
from test_chromatic_manager import check
from test_chromatic_manager_gui import Rig

ROOT = Path(__file__).resolve().parents[1]
REMOTE_PILOT = Path(os.environ.get('REMOTE_PILOT_SOURCE') or (Path(__file__).resolve().parents[1] / 'remote-pilot'))
COPY = ('receiver.py', 'presets.py', 'plugin_runtime.py', 'performance_capture.py', 'capture_api.py', 'touch_api.py',
        'viewer.html', 'viewer.js', 'viewer.css', 'themes.css')


def settle(ms=150):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait(predicate, seconds=10.0, step=50):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        settle(step)
    return predicate()


def fake_home(folder):
    folder = Path(folder)
    (folder / 'chromatic').mkdir(parents=True, exist_ok=True)
    (folder / 'presets/chromatic').mkdir(parents=True, exist_ok=True)
    (folder / 'receiver.py').write_text('import time\nwhile True:\n    time.sleep(1)\n', encoding='utf-8')
    (folder / 'chromatic/runtime.py').write_text('', encoding='utf-8')
    (folder / 'presets/chromatic/manifest.json').write_text('{}', encoding='utf-8')
    return link.Home(folder.resolve())


def dead_pid():
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    child.wait(timeout=30)
    return child.pid


class FakeClient:
    """Remote Pilot's answers, scripted. `statuses` is returned in order (the last one repeats)."""
    port = 38764
    url = 'http://127.0.0.1:38764/'

    def __init__(self, statuses=(None,), release=None, give_back=True, foreign=False):
        self.statuses, self.release, self.give_back, self.foreign = list(statuses), release, give_back, foreign
        self.posts, self.shown = [], 0

    def status(self):
        if self.foreign:
            raise link.Foreign('x')
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def chromatic(self, action, payload=None):
        self.posts.append((action, dict(payload or {})))
        if action == 'usb-release':
            return (True, self.release) if self.release is not None else (False, 'Unknown Chromatic action')
        if action == 'usb-return':
            return self.give_back, {'held': False}
        return False, 'Unknown Chromatic action'

    def show_chromatic(self):
        self.shown += 1
        return True, {'selected': 'chromatic'}


NEW_USB = {'transport': {'kind': 'usb', 'connected': True, 'session': True, 'portOpen': True}, 'usbHold': None,
           'mode': 'auto'}
OLD_USB = {'transport': {'kind': 'usb', 'connected': True, 'session': True, 'portOpen': True}, 'mode': 'auto'}
OLD_LAN = {'transport': {'kind': 'lan', 'connected': True, 'session': True}, 'mode': 'lan'}


def link_checks(temp):
    home = fake_home(temp / 'RemotePilot')
    # finding the folder
    check(link.is_home(home.folder) and not link.is_home(temp) and not link.is_home(temp / 'nope'), 'a Remote Pilot '
          'folder is one with receiver.py, chromatic/runtime.py and the Chromatic preset')
    check(link.locate(temp / 'Manager', env={}).folder == home.folder, 'found next to this project')
    other = fake_home(temp / 'elsewhere' / 'RP')
    check(link.locate(temp / 'Manager', chosen=other.folder, env={}).folder == other.folder, 'the chosen folder wins')
    check(link.locate(temp / 'x' / 'y', env={'REMOTE_PILOT_HOME': str(other.folder)}).folder == other.folder,
          'REMOTE_PILOT_HOME is honoured')
    check(home.python is None, 'no local\\venv: no Python of its own')

    # the hold file, written the way Remote Pilot reads it
    link.write_hold(home, 'a' * 32, link.reason_text('Install the <start-up> menu!'), 4242)
    data = json.loads(link.hold_path(home).read_text(encoding='utf-8'))
    check(data['format'] == 1 and data['holder'] == 'Chromatic Manager' and data['reason'] == 'Install the start-up menu'
          and data['pid'] == 4242, 'hold file: format 1, holder, a plain reason, the process id')
    check(sorted(p.name for p in home.hold_dir.iterdir()) == ['usb-hold.json'], 'hold file written atomically')
    if (REMOTE_PILOT / 'chromatic/usb_hold.py').is_file():
        sys.path.insert(0, str(REMOTE_PILOT))
        try:
            from chromatic import usb_hold  # Remote Pilot's own reader
            theirs = usb_hold.read_hold(home.hold_dir)
            check(theirs and not theirs['unreadable'] and theirs['id'] == 'a' * 32, "Remote Pilot's own reader accepts "
                  "the Manager's hold file")
            usb_hold.write_hold(home.hold_dir, 'b' * 32, 'Chromatic Manager', 'Restore', 77)
            check(link.read_hold(home)['id'] == 'b' * 32, "and the Manager reads Remote Pilot's")
        finally:
            sys.path.remove(str(REMOTE_PILOT))
    link.clear_hold(home, 'c' * 32)
    check(link.read_hold(home) is not None, 'someone else\'s hold is never cleared')
    link.hold_path(home).write_text('{broken', encoding='utf-8')
    check(link.read_hold(home)['unreadable'], 'an unreadable hold is reported as such')
    link.hold_path(home).unlink()

    def handoff(client, pid=None):
        return link.UsbHandoff(lambda: home, lambda: client, pid=pid or os.getpid(), sleep=lambda s: None)

    # Remote Pilot not on this PC: nothing to do
    nowhere = link.UsbHandoff(lambda: None, lambda: FakeClient(), sleep=lambda s: None)
    out = nowhere.pause('Install the start-up menu')
    check(out.ok and not out.paused and out.line is None and nowhere.resume(False) is None, 'no Remote Pilot: no hand-off')

    # not running: the file alone keeps a receiver that starts later away from USB
    client = FakeClient([None])
    h = handoff(client)
    out = h.pause('Back up the console')
    check(out.ok and not out.paused and 'not running' in out.line and link.read_hold(home)['id'] == h.hold_id,
          'not running: job goes ahead, hold on disk for its whole length')
    h.resume(False)
    check(link.read_hold(home) is None, 'not running: hold removed after the job')

    # a new receiver: released at once, returned after
    client = FakeClient([NEW_USB], release={'held': True, 'portOpen': False, 'wifiSession': True})
    h = handoff(client)
    out = h.pause('Install the start-up menu')
    sent = client.posts[0]
    check(out.ok and out.paused and 'Wi-Fi link stays connected' in out.line and sent[0] == 'usb-release'
          and sent[1]['id'] == h.hold_id and sent[1]['holder'] == 'Chromatic Manager' and sent[1]['pid'] == os.getpid(),
          'running receiver: USB released (hold id, holder, process id), Wi-Fi kept')
    check(link.read_hold(home)['id'] == sent[1]['id'], 'the hold was on disk before the receiver was asked')
    hold_id = h.hold_id
    line = h.resume(True)
    check('stays paused' in line and link.read_hold(home)['id'] == hold_id and client.posts[-1][0] == 'usb-release',
          'a job left unfinished: USB stays with the Manager')
    line = h.resume(False)
    check(line == 'Remote Pilot has the USB link back.' and client.posts[-1] == ('usb-return', {'id': hold_id})
          and link.read_hold(home) is None, 'USB given back with the same id, file gone')
    check(h.resume(False) is None, 'given back once only')

    # a port that does not close: refused, nothing kept
    client = FakeClient([NEW_USB, {**NEW_USB}], release={'held': True, 'portOpen': True})
    clock = iter(range(0, 1000))
    h = link.UsbHandoff(lambda: home, lambda: client, clock=lambda: next(clock), sleep=lambda s: None)
    out = h.pause('Restore the original firmware')
    check(not out.ok and 'did not let go' in out.line and link.read_hold(home) is None
          and client.posts[-1][0] == 'usb-return', 'a port that stays open: job refused, hold taken back')
    client = FakeClient([NEW_USB, {'transport': {'kind': 'usb', 'portOpen': False}}], release={'held': True, 'portOpen': True})
    out = handoff(client).pause('Restore the original firmware')
    check(out.ok and out.paused, 'a port that closes a moment later: job goes ahead')
    link.hold_path(home).unlink()

    # an older receiver: refused when its link may use the real USB port, fine when Wi-Fi only
    for status, ok, what in ((OLD_USB, False, 'USB'), ({'transport': {'kind': 'serial'}}, False, 'serial reader'),
                             ({'transport': {'kind': 'lan', 'portOpen': False}}, False, 'automatic with USB allowed'),
                             (OLD_LAN, True, 'Wi-Fi only'),
                             ({'transport': {'kind': 'usb', 'simulated': True, 'portOpen': True}}, True, 'simulator'),
                             ({'transport': {}, 'adapterMissing': True}, True, 'no Chromatic part')):
        client = FakeClient([status])
        out = handoff(client).pause('Install the start-up menu')
        check(out.ok == ok and (ok or out.line == link.OLD_RECEIVER) and (not ok or link.read_hold(home) is not None)
              and (ok or link.read_hold(home) is None), f'older receiver, {what}: {"goes ahead" if ok else "refused"}')
        if link.read_hold(home):
            link.hold_path(home).unlink()

    # stale holds at start-up
    gone = dead_pid()
    check(not link.process_alive(gone) and link.process_alive(os.getpid()) and link.process_alive(None),
          'process check: gone, alive, unknown counts as alive')
    link.write_hold(home, 'd' * 32, 'x', gone)
    client = FakeClient()
    h = handoff(client)
    check('a job did not finish' in h.clear_stale(True) and link.read_hold(home) is not None and h.hold_id == 'd' * 32,
          'crashed run with a job waiting: hold kept until Finish it')
    check('has it back' in h.clear_stale(False) and link.read_hold(home) is None
          and client.posts[-1] == ('usb-return', {'id': 'd' * 32}), 'crashed run, nothing waiting: USB given back')
    link.write_hold(home, 'e' * 32, 'x', os.getppid())
    check(handoff(FakeClient(), pid=12345).clear_stale(False) is None and link.read_hold(home) is not None,
          'a hold whose program still runs is left alone')
    link.hold_path(home).unlink()
    link.hold_path(home).write_text('{bad', encoding='utf-8')
    check(handoff(FakeClient()).clear_stale(False) is None and link.hold_path(home).exists(),
          'an unreadable hold is left for Remote Pilot\'s own "Use USB again"')
    link.hold_path(home).unlink()

    # the tab's state from what is known
    v = link.view_key
    connected = lambda kind, sim=False: {'transport': {'connected': True, 'session': True, 'kind': kind, 'simulated': sim}}
    looking = {'transport': {'connected': False, 'session': False, 'kind': 'lan'}, 'mode': 'auto'}
    table = ((v(False, '', None, 0, None), 'not_found'), (v(True, '', None, 0, None), 'ready'),
             (v(True, 'starting', None, 0, None), 'starting'), (v(True, 'failed', None, 0, None), 'failed'),
             (v(True, 'no_python', None, 0, None), 'no_python'), (v(True, 'foreign', None, 0, None), 'foreign'),
             (v(True, '', looking, 3, None), 'looking'), (v(True, '', looking, 30, None), 'no_console'),
             (v(True, '', {**looking, 'mode': 'off'}, 3, None), 'not_set_up'),
             (v(True, '', connected('usb'), 0, None), 'usb'), (v(True, '', connected('lan'), 0, None), 'wifi'),
             (v(True, '', connected('usb', True), 0, None), 'practice'),
             (v(True, '', looking, 3, {'held': True, 'holderRunning': True}), 'paused'),
             (v(True, '', looking, 3, {'held': True, 'holderRunning': False}), 'paused_kept'),
             (v(True, 'failed', connected('usb'), 0, None), 'usb'))
    check(all(got == want for got, want in table), f'tab states {[(g, w) for g, w in table if g != w]}')
    check(all(tone in ('ok', 'update', 'error', 'busy', 'info') and head and sub.endswith('.')
              for tone, head, sub, step, busy in link.VIEWS.values()), 'every state: a tone, a headline, what to do')
    check(all(link.VIEWS[k][0] == 'ok' for k in ('usb', 'wifi', 'practice'))
          and all(link.VIEWS[k][0] == 'error' for k in ('failed', 'no_python', 'foreign'))
          and all(link.VIEWS[k][0] in ('busy', 'update') for k in ('starting', 'looking', 'paused', 'no_console')),
          'green connected, yellow working or attention, red problem')

    # the launcher: only its own child is ever stopped
    launcher = link.Launcher()
    process = launcher.start(home, 40000)
    other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        check(launcher.running() and launcher.started() and process.poll() is None, 'receiver started as a child')
        check(launcher.start(home, 40000) is process, 'started once only')
        check(launcher.stop() and process.poll() is not None and not launcher.running(), 'its own child is stopped')
        check(not launcher.stop() and other.poll() is None, 'nothing else is ever stopped')
        idle = link.Launcher()
        check(not idle.stop() and other.poll() is None, 'a launcher that started nothing stops nothing')
    finally:
        other.kill()
        other.wait(timeout=10)
    check((home.folder / 'local' / link.LOG_NAME).is_file(), "the child's output goes to Remote Pilot's local/ folder")
    check(link.reason_text('Install the sound (stock+media) console chip image (FPGA)')
          == 'Install the sound (stock+media) console chip image (FPGA)', 'action titles pass as hold reasons')
    check(isinstance(link.webengine_available(), bool) and 'PySide6-Addons 6.12.0' in link.WEBENGINE_NEED,
          'Qt WebEngine is only checked for; what it would need is written down')


class FakeLink(link.PilotLink):
    """A PilotLink whose receiver is a script of statuses, for the window."""

    def __init__(self, home, client, start_result=''):
        super().__init__(lambda: home, client=client, launcher=link.Launcher(popen=lambda *a, **k: FakeProcess()))
        self.start_result, self.started = start_result, 0

    def start(self):
        self.started += 1
        if self.start_result:
            return self.start_result
        if self.client.status() is None:
            self.launcher.start(self.home(), 1)
        return ''


class FakeProcess:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code

    def terminate(self):
        self.code = 1

    def wait(self, timeout=None):
        return self.code


def window_checks(temp, stock, release):
    home = fake_home(temp / 'RemotePilot')
    opened = []
    looking = {'transport': {'connected': False, 'session': False, 'kind': 'lan'}, 'mode': 'auto', 'usbHold': None}
    usb = {'transport': {'connected': True, 'session': True, 'kind': 'usb', 'portOpen': True}, 'mode': 'auto',
           'usbHold': None}
    client = FakeClient([None, None, looking, usb], release={'held': True, 'portOpen': False, 'wifiSession': True})
    fake = FakeLink(home, client)
    rig = Rig(stock, release)
    w = rig.window
    w.pilot, w.pilot_open = fake, lambda where, url: opened.append((where, url))
    w.service.port_handoff = fake.handoff
    w._pilot_where = 'app'
    w.resize(*gui.WINDOW)
    check(list(w.tabs) == ['console', 'pilot', 'recovery', 'settings', 'about'] and w.tabs['pilot'].icon_key == 'pilot'
          and theme.TAB_COLOURS['pilot'] == 'tab_pilot' and len(theme.TAB_ICONS['pilot']) == 9
          and all(len(row) == 9 for row in theme.TAB_ICONS['pilot']), 'Remote Pilot tab: second, its own icon and colour')
    check(theme.PALETTE['tab_pilot'] not in {theme.PALETTE[k] for k in ('tab_home', 'tab_fix', 'tab_settings', 'tab_about')}
          and 'own choice' in theme.PALETTE_SOURCES['tab_pilot'][1], 'a fifth tab colour of our own')
    # opening the tab starts Remote Pilot but opens no window by itself
    w.show_tab('pilot')
    check(w.title.raw_text() == 'REMOTE PILOT' and fake.started == 1, 'opening the tab starts Remote Pilot')
    check(wait(lambda: w._pilot_view == 'looking'), 'starting, then looking for the console')
    check(w.pilot_steps.states == ['done', 'now', 'todo'] and not w.pilot_bar.isHidden() and w.pilot_card.tone == 'busy'
          and w.pilot_card.head.raw_text() == 'Looking for your Chromatic...', 'looking: step 2 amber, bar moving')
    check(opened == [], 'no window opened by itself')
    w.pilot_primary.click()
    check(wait(lambda: opened) and opened == [('window', client.url)] and client.shown >= 1,
          'Open Remote Pilot: its Chromatic page, in its own window, once')
    check(wait(lambda: w._pilot_view == 'usb') and w.pilot_steps.states == ['done'] * 3 and w.pilot_card.tone == 'ok'
          and w.pilot_bar.isHidden() and w.console_art.tone == 'ok', 'connected over USB: all green')
    w.pilot_browser.click()
    check(wait(lambda: len(opened) == 2) and opened[-1][0] == 'browser', 'Open in browser always works')
    check(all(b.text_fits() for b in (w.pilot_primary, w.pilot_browser, w.pilot_folder, w.pilot_pick)),
          'button text fits')
    for widget in (w.pilot_card.head, w.pilot_card.sub, w.pilot_where):
        text = widget.raw_text()
        check('127.0.0.1' not in text and str(home.folder) not in text, 'no address or folder on the tab')
    check(all(b.toolTip() for b in (w.pilot_primary, w.pilot_browser, w.pilot_folder, w.pilot_pick, w.pilot_stop,
                                    w.tabs['pilot'], w.pilot_steps)), 'every control explains itself')

    # a job: the paused sign while it runs, the back sign after
    seen = {}
    real_factory = w.service.manager_factory

    def watching(say):
        if 'hold' not in seen and threading.current_thread().name.startswith('chromatic-job'):
            seen['hold'] = link.read_hold(home)  # what the job itself saw when it began
        return real_factory(say)
    w.service.manager_factory = watching
    code = rig.run('status')
    w.service.manager_factory = real_factory
    lines = '\n'.join(w.run_lines)
    check(code == cm.EXIT_OK and seen['hold'] and seen['hold']['holder'] == 'Chromatic Manager',
          'the job ran with the hold on disk')
    check('Remote Pilot paused its USB link for this job' in lines and 'Remote Pilot has the USB link back.' in lines,
          'the job log says USB was paused and given back')
    check(link.read_hold(home) is None and [a for a, _ in client.posts][-2:] == ['usb-release', 'usb-return'],
          'released before, returned after')
    check(not w.pilot_chip.isHidden() and w.pilot_chip.text() == 'REMOTE PILOT BACK', 'the back sign shows after the job')
    w.show_pilot_sign('paused', 'Install the start-up menu')
    check(w.pilot_chip.text() == 'REMOTE PILOT PAUSED' and 'back in a moment' in w.work_pilot.text(),
          'the paused sign and the working page line')
    w.show_pilot_sign('back')

    # an older Remote Pilot that may hold USB: the job is refused before the console is touched
    old = FakeClient([OLD_USB])
    w.pilot.client = old
    called, real_run = [], core.run_action
    core.run_action = lambda *a, **k: called.append(1) or real_run(*a, **k)
    try:
        code = rig.run('status')
    finally:
        core.run_action = real_run
    check(code == cm.EXIT_REFUSED and not called and link.OLD_RECEIVER in '\n'.join(w.run_lines)
          and link.read_hold(home) is None, 'older Remote Pilot with USB: refused, console untouched, what to do said')
    w.pilot.client = client

    # problems say what to do
    for result, key in (('no_python', 'no_python'), ('foreign', 'foreign'), ('failed', 'failed')):
        bad = FakeLink(home, FakeClient([None]), start_result=result)
        w.pilot, w._pilot = bad, {'launch': '', 'status': None, 'since': None, 'open': None, 'busy': False, 'at': None}
        w.pilot_launch('window')
        check(wait(lambda: w._pilot_view == key) and w.pilot_card.tone == 'error' and w.pilot_primary.text() == 'Try again'
              and 'fail' in w.pilot_steps.states, f'{key}: red, says what to do, Try again')
    # not found: Find Remote Pilot asks for the folder and remembers it
    def chosen_only():  # only the folder chosen on the tab (the real Remote Pilot may be in a usual place here)
        folder = w.service.settings.get(link.SETTING)
        return link.Home(Path(folder)) if folder and link.is_home(folder) else None
    w.pilot = link.PilotLink(chosen_only,
                             client=FakeClient([None]), launcher=link.Launcher(popen=lambda *a, **k: FakeProcess()))
    w._pilot = {'launch': '', 'status': None, 'since': None, 'open': None, 'busy': False, 'at': None}
    w.refresh_pilot()
    check(w._pilot_view == 'not_found' and w.pilot_primary.text() == 'Find Remote Pilot', 'not found: Find Remote Pilot')
    w.pilot_choose = lambda: str(temp)
    w.pilot_primary.click()
    check(w.pilot_card.head.raw_text() == 'That is not Remote Pilot' and w.service.settings.get(link.SETTING) is None,
          'a wrong folder is refused and not remembered')
    w.pilot_choose = lambda: str(home.folder)
    w.pilot_primary.click()
    check(w.service.settings.get(link.SETTING) == str(home.folder) and w._pilot_view != 'not_found',
          'the right folder is remembered (private settings) and used')
    w.pilot_timer.stop()
    w.close()


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def copy_remote_pilot(target):
    """Remote Pilot's code without its local/ folder: no config, pairing key or port of the owner's."""
    target.mkdir(parents=True)
    for name in COPY:
        if (REMOTE_PILOT / name).is_file():
            shutil.copy2(REMOTE_PILOT / name, target / name)
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc', 'local')
    for folder in ('chromatic', 'presets', 'fonts'):
        if (REMOTE_PILOT / folder).is_dir():
            shutil.copytree(REMOTE_PILOT / folder, target / folder, ignore=ignore)
    check(not (target / 'local').exists(), 'the copy has no private local/ folder')
    return link.Home(target.resolve())


def end_to_end(temp, stock, release):
    if not (REMOTE_PILOT / 'chromatic/usb_hold.py').is_file():
        print('SKIP: end to end (no Remote Pilot checkout with the hand-off at', REMOTE_PILOT, ')')
        return False
    home = copy_remote_pilot(temp / 'RemotePilotCopy')
    port = free_port()
    client = link.Client(port, timeout=5)
    pilot = link.PilotLink(lambda: home, client=client)
    check(pilot.start() == '' and pilot.launcher.running(), 'the real receiver starts as this app\'s child')
    try:
        check(wait(lambda: client.status() is not None, 40), 'and answers on 127.0.0.1')
        status = client.status()
        check(status['mode'] == 'off' and 'usbHold' in status, 'a fresh copy: nothing configured, hand-off supported')
        check(client.show_chromatic()[0], 'the viewer is put on the Chromatic page')
        check(client.chromatic('mode', {'mode': 'auto-sim'})[0], 'simulated console on USB and Wi-Fi')
        t = lambda: client.status()['transport']
        check(wait(lambda: t()['session'] and t()['route'] == 'usb', 20), 'CHR1 session over (simulated) USB')

        rig = Rig(stock, release)
        w = rig.window
        w.pilot, w.service.port_handoff = pilot, pilot.handoff
        seen = {}
        real_factory = w.service.manager_factory

        def watching(say):
            if 'transport' in seen or not threading.current_thread().name.startswith('chromatic-job'):
                return real_factory(say)
            seen['transport'] = client.status()['transport']
            seen['hold'] = client.status()['usbHold']
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not client.status()['transport']['session']:
                time.sleep(0.2)
            seen['wifi'] = client.status()['transport']
            return real_factory(say)
        w.service.manager_factory = watching
        check(rig.run('status') == cm.EXIT_OK, 'a Manager job on the simulated console')
        w.service.manager_factory = real_factory
        check(seen['transport']['portOpen'] is False and seen['hold']['held']
              and seen['hold']['holder'] == 'Chromatic Manager', 'during the job: USB port closed, hold shown')
        check(seen['wifi']['session'] and seen['wifi']['route'] == 'lan', 'during the job: Wi-Fi keeps the session')
        check(client.status()['usbHold'] is None and wait(lambda: t()['portOpen'], 10), 'after the job: USB back')
        check(not link.hold_path(home).exists(), 'no hold left behind')

        # a Manager that dies mid-job: the hold survives a receiver restart, USB stays shut
        crashed = link.UsbHandoff(lambda: home, lambda: client, pid=dead_pid())
        check(crashed.pause('Install the start-up menu').paused and not t()['portOpen'], 'hold taken, port closed')
        pilot.stop()
        check(pilot.start() == '' and wait(lambda: client.status() is not None, 40), 'receiver restarted')
        client.show_chromatic()
        client.chromatic('mode', {'mode': 'auto-sim'})
        check(wait(lambda: t()['session'], 20) and t()['route'] == 'lan', 'after the restart: Wi-Fi only')
        settle(3500)
        check(t()['portOpen'] is False and client.status()['usbHold']['holderRunning'] is False,
              'USB stays shut while the crashed hold is on disk')
        fresh = link.UsbHandoff(lambda: home, lambda: client)
        check('a job did not finish' in fresh.clear_stale(True) and t()['portOpen'] is False,
              'next start with a job waiting: still shut')
        check('has it back' in fresh.clear_stale(False) and wait(lambda: t()['portOpen'], 10),
              'next start, nothing waiting: USB back')

        # closing the window stops the receiver it started, and only that one
        process = pilot.launcher.process
        w.close()
        check(process.poll() is not None or wait(lambda: process.poll() is not None, 10),
              'closing the window stops the receiver it started')
        other = link.Launcher()
        other.start(home, port)
        check(wait(lambda: client.status() is not None, 40), 'a receiver started by someone else')
        rig2 = Rig(stock, release)
        visitor = link.PilotLink(lambda: home, client=client)
        rig2.window.pilot = visitor
        check(visitor.start() == '' and not visitor.launcher.started(), 'already running: not started again')
        rig2.window.close()
        check(other.running() and client.status() is not None, 'and closing the window leaves it running')
        other.stop()
    finally:
        pilot.stop()
    return True


def main():
    app = QApplication.instance() or QApplication([])
    theme.apply(app)
    stock = stock_dump(allow_synthetic=True)
    release = cm.Release.load()
    temp = Path(tempfile.mkdtemp(prefix='rp-tab-'))
    try:
        link_checks(temp / 'link')
        window_checks(temp / 'window', stock, release)
        ran = end_to_end(temp / 'e2e', stock, release)
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    print('PASS: Remote Pilot tab and USB hand-off: folder finding, hold file shared with Remote Pilot, every hand-off '
          'answer (not running, released with Wi-Fi kept, unfinished job keeps it, port that will not close, older '
          'receiver with and without USB), stale holds after a crash, tab states and colours, launch flow (starts on '
          'tab open, opens once on the button, browser always), paused and back signs, refused job before any console '
          'access, wrong folder refused, only its own receiver stopped'
          + ('; end to end with the real receiver (copied, simulated USB and Wi-Fi): USB paused during a job while '
             'Wi-Fi kept the session, given back after, crashed hold survives a restart, cleared only with no job '
             'waiting, close stops only its own receiver' if ran else '') + '. No hardware.')


if __name__ == '__main__':
    main()

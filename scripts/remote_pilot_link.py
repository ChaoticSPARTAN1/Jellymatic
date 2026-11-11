# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Chromatic Manager's link to Remote Pilot (its Chromatic part). No Qt here.

Remote Pilot is a separate project with its own folder, Python (local\\venv), config,
pairing key and tests. Chromatic Manager does not copy it: it finds that folder, starts
its receiver (`receiver.py serve`, a local web viewer on 127.0.0.1) as a child process
when it is not already running, and opens the viewer. So one install of Remote Pilot
keeps working on its own (Start-RemotePilot.cmd) and from the Manager, with the same
settings, and an update to either never needs the other rebuilt.

Where the folder comes from, first match wins: the folder the owner chose on the Remote
Pilot tab (kept in the Manager's private settings), REMOTE_PILOT_HOME, then the usual
places next to this project or under the user's folder. A folder counts only if it has
receiver.py, chromatic/runtime.py and presets/chromatic/manifest.json.

The USB hand-off (UsbHandoff): the console has one USB serial port. Before every
Manager job (all of them open it: install, update, restore, console chip jobs, Wi-Fi,
checks) the Manager
  1. writes Remote Pilot's hold file local/chromatic/usb-hold.json, flushed to disk;
  2. asks a running receiver to close USB (POST /api/chromatic/usb-release), which it
     does at once and confirms; its Wi-Fi link keeps working;
  3. runs the job; then gives USB back (POST usb-return and the file removed), but keeps
     the hold while a job is left unfinished, so a half-written console is never
     touched by Remote Pilot. Remote Pilot checks the file itself before every port
     open, so the hold also covers a receiver that restarts or starts during the job,
     and outlives a Manager that crashes. The next Manager start ends a hold its
     earlier run left behind, only when that run's process has gone and no job waits.
An older receiver without the hand-off that may be using USB is not overridden: the job
is refused with what to do (close Remote Pilot or switch it to Wi-Fi). See
chromatic/usb_hold.py in Remote Pilot for its side.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_PORT = 38764
HOLDER = 'Chromatic Manager'
SETTING = 'remote_pilot_folder'
HOLD_NAME = 'usb-hold.json'
HOLD_FORMAT = 1
LOG_NAME = 'receiver-chromatic-manager.log'  # in Remote Pilot's git-ignored local/ folder
_REASON_DROP = re.compile(r"[^A-Za-z0-9 ,.()'/:+&-]")


# ---- finding Remote Pilot ---------------------------------------------------------------
@dataclass(frozen=True)
class Home:
    folder: Path

    @property
    def receiver(self):
        return self.folder / 'receiver.py'

    @property
    def hold_dir(self):
        return self.folder / 'local' / 'chromatic'

    @property
    def python(self):
        """Remote Pilot's own Python (local\\venv, with pyserial for USB), or None."""
        for path in (self.folder / 'local/venv/Scripts/python.exe', self.folder / 'local/venv/bin/python'):
            if path.is_file():
                return path
        return None


def is_home(folder):
    try:
        folder = Path(folder)
        return ((folder / 'receiver.py').is_file() and (folder / 'chromatic' / 'runtime.py').is_file()
                and (folder / 'presets' / 'chromatic' / 'manifest.json').is_file())
    except (OSError, TypeError, ValueError):
        return False


def candidates(root, chosen=None, env=None):
    env = os.environ if env is None else env
    home = Path.home()
    out = [chosen, env.get('REMOTE_PILOT_HOME'), Path(root) / 'remote-pilot', Path(root).parent / 'RemotePilot',
           home / 'Desktop' / 'RemotePilot', home / 'RemotePilot']
    return [Path(item) for item in out if item]


def locate(root, chosen=None, env=None):
    for folder in candidates(root, chosen, env):
        if is_home(folder):
            return Home(folder.resolve())
    return None


# ---- the hold file (same format as Remote Pilot's chromatic/usb_hold.py) -------------------
def hold_path(home):
    return home.hold_dir / HOLD_NAME


def read_hold(home):
    """None when there is no hold; else a dict ('unreadable' True when it makes no sense)."""
    path = hold_path(home)
    try:
        if path.stat().st_size > 2048:
            raise ValueError
        data = json.loads(path.read_text(encoding='utf-8'))
        if not (isinstance(data, dict) and data.get('format') == HOLD_FORMAT and isinstance(data.get('id'), str)
                and re.fullmatch(r'[0-9a-f]{32}', data['id']) and type(data.get('pid')) is int):
            raise ValueError
        return {'id': data['id'], 'pid': data['pid'], 'holder': str(data.get('holder', '')), 'unreadable': False}
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, UnicodeDecodeError):
        return {'id': None, 'pid': None, 'holder': '', 'unreadable': True}


def write_hold(home, hold_id, reason, pid):
    path = hold_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {'format': HOLD_FORMAT, 'holder': HOLDER, 'id': hold_id, 'pid': pid, 'reason': reason,
            'since': time.strftime('%Y-%m-%d %H:%M:%S')}
    temporary = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    with open(temporary, 'w', encoding='utf-8') as handle:
        handle.write(json.dumps(body) + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def clear_hold(home, hold_id):
    """Removes the hold only if it is still this one."""
    hold = read_hold(home)
    if hold is not None and hold['id'] == hold_id:
        try:
            hold_path(home).unlink()
        except FileNotFoundError:
            pass


def reason_text(title):
    return _REASON_DROP.sub('', str(title or ''))[:80].strip()


def process_alive(pid):
    """Is that process running? Only asks; never signals it. Unknown counts as alive."""
    if type(pid) is not int or pid <= 0:
        return True
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


# ---- talking to the receiver -------------------------------------------------------------
class Foreign(Exception):
    """Something else answers on Remote Pilot's port."""


class Client:
    """Remote Pilot's local viewer API on 127.0.0.1 (the same requests its own CLI makes)."""

    def __init__(self, port=DEFAULT_PORT, timeout=2.5, opener=urlopen):
        if not 1 <= int(port) <= 65535:
            raise ValueError('port must be 1..65535')
        self.port, self.timeout, self._open = int(port), timeout, opener

    @property
    def url(self):
        return f'http://127.0.0.1:{self.port}/'

    def status(self):
        """Remote Pilot's Chromatic status, or None when nothing answers. Foreign: another
        program answers on the port."""
        try:
            with self._open(Request(self.url + 'api/chromatic/status'), timeout=self.timeout) as response:
                value = json.loads(response.read(1 << 20))
        except HTTPError as error:
            text = error.read(300).decode('utf-8', 'replace')
            if error.code == 400 and 'Chromatic' in text:
                return {'transport': {}, 'adapterMissing': True}  # Remote Pilot without its Chromatic part
            raise Foreign(f'HTTP {error.code}') from error
        except (URLError, ConnectionError, TimeoutError, OSError):
            return None
        except ValueError as error:
            raise Foreign('not JSON') from error
        if not isinstance(value, dict) or not isinstance(value.get('transport'), dict):
            raise Foreign('not Remote Pilot')
        return value

    def post(self, path, payload):
        """(True, value) or (False, the receiver's own words or why it did not answer)."""
        request = Request(self.url + path, json.dumps(payload).encode('utf-8'),
                          {'Content-Type': 'application/json', 'X-Remote-Pilot-Viewer': '1'}, method='POST')
        try:
            with self._open(request, timeout=self.timeout) as response:
                return True, json.loads(response.read(1 << 20))
        except HTTPError as error:
            return False, error.read(300).decode('utf-8', 'replace') or f'HTTP {error.code}'
        except (URLError, ConnectionError, TimeoutError, OSError, ValueError) as error:
            return False, f'no answer ({type(error).__name__})'

    def chromatic(self, action, payload=None):
        return self.post(f'api/chromatic/{action}', payload or {})

    def show_chromatic(self):
        """The viewer opens on the Chromatic preset (the owner pressed Open Remote Pilot)."""
        return self.post('api/preset', {'id': 'chromatic'})


# ---- the receiver as the Manager's child --------------------------------------------------
class LaunchError(Exception):
    pass


class Launcher:
    """Starts Remote Pilot's receiver and stops only a receiver it started itself."""

    def __init__(self, popen=subprocess.Popen):
        self._popen, self.process, self._log = popen, None, None

    def start(self, home, port):
        if self.running():
            return self.process
        python = home.python
        if python is None:
            if getattr(sys, 'frozen', False):
                raise LaunchError('python')
            python = Path(sys.executable)  # running from source: this Python has pyserial too
        local = home.folder / 'local'
        local.mkdir(parents=True, exist_ok=True)
        self._close_log()
        self._log = open(local / LOG_NAME, 'ab')
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        try:
            self.process = self._popen([str(python), '-B', str(home.receiver), 'serve', '--viewer-port', str(port)],
                                       cwd=str(home.folder), stdin=subprocess.DEVNULL, stdout=self._log,
                                       stderr=subprocess.STDOUT, creationflags=flags)
        except OSError as error:
            self._close_log()
            raise LaunchError(type(error).__name__) from error
        return self.process

    def running(self):
        return self.process is not None and self.process.poll() is None

    def exited(self):
        """The exit code of a receiver this started that has ended, else None."""
        return None if self.process is None else self.process.poll()

    def started(self):
        return self.process is not None

    def stop(self, wait=5.0):
        """Ends the receiver this started (and only that one). True if one was stopped."""
        process, self.process = self.process, None
        stopped = False
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=wait)
            stopped = True
        self._close_log()
        return stopped

    def _close_log(self):
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None


# ---- the USB hand-off -------------------------------------------------------------------
@dataclass
class Outcome:
    ok: bool
    line: str | None = None
    paused: bool = False  # Remote Pilot was running and let go of USB (the window shows the sign)


OLD_RECEIVER = ('Remote Pilot is open and may be using the console\'s USB cable, and this copy of it cannot hand '
                'the cable over by itself (it is an older version). Close Remote Pilot, or set its connection to '
                'Wi-Fi only, then press the button again. Nothing was done.')
NO_HOLD = ('Remote Pilot could not be told to keep off the USB cable (its folder could not be written). Nothing was '
           'done. Close Remote Pilot, then press the button again.')


def usb_capable(status):
    """Could this receiver's current link open the real USB port?"""
    transport = (status or {}).get('transport') or {}
    if transport.get('simulated'):
        return False
    return transport.get('kind') in ('usb', 'serial') or transport.get('portOpen') is not None


class UsbHandoff:
    def __init__(self, home_fn, client_fn, pid=None, clock=time.monotonic, sleep=time.sleep):
        self.home_fn, self.client_fn = home_fn, client_fn
        self.pid = pid or os.getpid()
        self.clock, self.sleep = clock, sleep
        self.hold_id, self.home = None, None

    def pause(self, title):
        """Before a job: Remote Pilot keeps off USB. Outcome.ok False: do not start the job."""
        home = self.home_fn()
        if home is None:
            return Outcome(True)  # no Remote Pilot on this PC: nothing to hand over
        hold_id = secrets.token_hex(16)
        try:
            write_hold(home, hold_id, reason_text(title), self.pid)
            wrote = True
        except OSError:
            wrote = False
        if wrote:
            self.hold_id, self.home = hold_id, home
        client = self.client_fn()
        try:
            status = client.status()
        except Foreign:
            status = None  # not Remote Pilot: only the file matters
        if status is None:
            if not wrote:
                return Outcome(False, NO_HOLD)
            return Outcome(True, 'Remote Pilot is not running; it keeps off the USB cable until this job ends.')
        ok, value = client.chromatic('usb-release', {'id': hold_id, 'holder': HOLDER, 'reason': reason_text(title),
                                                     'pid': self.pid})
        if ok and isinstance(value, dict) and value.get('held'):
            if value.get('portOpen'):
                if not self._wait_closed(client):
                    self.resume(False)
                    return Outcome(False, 'Remote Pilot did not let go of the USB cable in time. Nothing was done; '
                                          'try again in a moment.')
            if not wrote:  # the receiver wrote the hold itself
                self.hold_id, self.home = hold_id, home
            wifi = '; its Wi-Fi link stays connected' if value.get('wifiSession') else '; Wi-Fi keeps working'
            return Outcome(True, f'Remote Pilot paused its USB link for this job{wifi}.', paused=True)
        if usb_capable(status):
            self.resume(False)
            return Outcome(False, OLD_RECEIVER)
        if not wrote:
            return Outcome(False, NO_HOLD)
        return Outcome(True, 'Remote Pilot is not using the USB cable right now.')

    def _wait_closed(self, client, seconds=3.0):
        end = self.clock() + seconds
        while self.clock() < end:
            try:
                status = client.status()
            except Foreign:
                return False
            if status is None or not (status.get('transport') or {}).get('portOpen'):
                return True
            self.sleep(0.2)
        return False

    def resume(self, keep):
        """After a job: USB goes back to Remote Pilot, unless a job is left unfinished (keep)."""
        if self.hold_id is None:
            return None
        if keep:
            return ("Remote Pilot's USB link stays paused until the unfinished job is finished (press Finish it). "
                    'Its Wi-Fi link keeps working.')
        hold_id, home = self.hold_id, self.home
        self.hold_id = self.home = None
        ok, _ = self.client_fn().chromatic('usb-return', {'id': hold_id})
        try:
            clear_hold(home, hold_id)
        except OSError:
            pass
        return 'Remote Pilot has the USB link back.' if ok else None

    def clear_stale(self, pending):
        """At start-up: a hold an earlier Manager run left behind (it closed mid-job)."""
        home = self.home_fn()
        hold = read_hold(home) if home is not None else None
        if hold is None or hold['unreadable'] or hold['holder'] != HOLDER:
            return None
        if hold['pid'] != self.pid and process_alive(hold['pid']):
            return None
        if pending:
            self.hold_id, self.home = hold['id'], home  # given back after Finish it
            return ("Remote Pilot's USB link is still paused: a job did not finish. Press Finish it on the Home tab; "
                    'USB goes back to Remote Pilot afterwards.')
        self.client_fn().chromatic('usb-return', {'id': hold['id']})
        try:
            clear_hold(home, hold['id'])
        except OSError:
            return None
        return "Remote Pilot's USB link was still paused from an earlier run; it has it back now."


# ---- what the Remote Pilot tab shows -------------------------------------------------------
# key: (tone, headline, what to do, step 1..3 or 0 for none, busy)
VIEWS = {
    'not_found': ('update', 'Remote Pilot not found',
                  'Remote Pilot lives in its own folder on this PC. Press Find Remote Pilot and pick that folder '
                  '(the one with receiver.py in it).', 0, False),
    'ready': ('info', 'Remote Pilot is ready',
              'Press Open Remote Pilot. It starts by itself and opens in its own window.', 0, False),
    'starting': ('busy', 'Starting Remote Pilot...', 'Getting it ready on this PC. This takes a few seconds.', 1, True),
    'looking': ('busy', 'Looking for your Chromatic...',
                'Switch your Chromatic on. Remote Pilot finds it over USB, or over Wi-Fi once you open Jellyfin on it.',
                2, True),
    'not_set_up': ('update', 'Connection not set up',
                   'Press Open Remote Pilot, then Connect there. It finds your Chromatic\'s USB port and asks before '
                   'using it.', 2, False),
    'no_console': ('update', 'Console not found',
                   'Remote Pilot is running but cannot see your Chromatic. Plug in its USB cable and switch it on, or '
                   'open Jellyfin on it so it joins Wi-Fi. It keeps looking.', 2, False),
    'usb': ('ok', 'Connected over USB', 'Remote Pilot sees your Chromatic. Press Open Remote Pilot to use it.', 3,
            False),
    'wifi': ('ok', 'Connected over Wi-Fi', 'Remote Pilot sees your Chromatic. Press Open Remote Pilot to use it.', 3,
             False),
    'practice': ('ok', 'Practice console connected',
                 'Remote Pilot is connected to its pretend console on this PC, not your Chromatic. Choose Configured '
                 'in Remote Pilot for the real one.', 3, False),
    'paused': ('update', 'Paused while Chromatic Manager works',
               'Chromatic Manager is using the USB cable. Remote Pilot\'s Wi-Fi link keeps working; USB is back in a '
               'moment.', 2, False),
    'paused_kept': ('update', 'USB paused: a job did not finish',
                    'Press Finish it on the Home tab. Remote Pilot gets the USB cable back afterwards; Wi-Fi keeps '
                    'working.', 2, False),
    'failed': ('error', 'Remote Pilot did not start',
               'Press Try again. If it fails again, open its folder and run Start-RemotePilot.cmd to see why.', 1,
               False),
    'no_python': ('error', 'Remote Pilot needs its Python',
                  'Its folder has no local\\venv yet. Set it up as Remote Pilot\'s README says, then press Try again.',
                  1, False),
    'foreign': ('error', 'Remote Pilot\'s port is busy',
                'Another program on this PC uses Remote Pilot\'s port. Close that program, then press Try again.', 1,
                False),
}
LOOK_PATIENCE_S = 25.0  # "Looking for your Chromatic" turns into "Console not found" after this long


def view_key(found, launch, status, looking_for, hold):
    """Which VIEWS entry the tab shows.
    found: a Remote Pilot folder exists; launch: '', 'starting', 'failed', 'no_python' or
    'foreign'; status: the receiver's status (None when it does not answer); looking_for:
    seconds since it answered without a connection; hold: Remote Pilot's usbHold or None."""
    if not found:
        return 'not_found'
    if launch in ('failed', 'no_python', 'foreign') and status is None:
        return launch
    if status is None:
        return 'starting' if launch == 'starting' else 'ready'
    transport = status.get('transport') or {}
    if transport.get('connected') and transport.get('session'):
        if transport.get('simulated'):
            return 'practice'
        return 'usb' if transport.get('kind') == 'usb' else 'wifi'
    if hold:
        return 'paused' if hold.get('holderRunning', True) else 'paused_kept'
    if status.get('mode') == 'off' or status.get('adapterMissing'):
        return 'not_set_up'
    return 'looking' if looking_for < LOOK_PATIENCE_S else 'no_console'


def webengine_available():
    """Can this Python show the viewer in a window of its own (Qt WebEngine)? Only checked, never installed."""
    try:
        import importlib.util
        return importlib.util.find_spec('PySide6.QtWebEngineWidgets') is not None
    except (ImportError, ValueError):
        return False


# What the dedicated window would need when Qt WebEngine is missing (reported, never installed).
WEBENGINE_NEED = ('PySide6-Addons 6.12.0 (it has Qt WebEngine; same version as PySide6-Essentials 6.12.0), '
                  'LGPL-3.0 (Qt WebEngine also carries Chromium\'s BSD-style notices); roughly 150 MB to download '
                  'and about 500 MB installed, and the portable build would grow by a few hundred MB.')


def app_browser():
    """A browser that can show the viewer as a window of its own (Edge or Chrome --app), or None."""
    if os.name != 'nt':
        return None
    bases = [os.environ.get(name) for name in ('ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA')]
    for base in filter(None, bases):
        for tail in (r'Microsoft\Edge\Application\msedge.exe', r'Google\Chrome\Application\chrome.exe'):
            path = Path(base) / tail
            if path.is_file():
                return path
    return None


class PilotLink:
    """Everything the Remote Pilot tab needs, without Qt: where Remote Pilot is, its client,
    the receiver this Manager started, and the USB hand-off for jobs."""

    def __init__(self, home_fn, client=None, launcher=None, pid=None):
        self.home_fn = home_fn
        self.client = client or Client()
        self.launcher = launcher or Launcher()
        self.handoff = UsbHandoff(self.home, lambda: self.client, pid=pid)

    @classmethod
    def inert(cls):
        """No Remote Pilot (tests, previews): nothing is looked for, started or contacted."""
        link = cls(lambda: None, client=Client(port=1), launcher=Launcher(popen=_never))
        return link

    @classmethod
    def for_app(cls, root, settings, port=DEFAULT_PORT):
        return cls(lambda: locate(root, settings.get(SETTING)), client=Client(port))

    def home(self):
        try:
            return self.home_fn()
        except OSError:
            return None

    def start(self):
        """Starts the receiver unless one answers already. '' (started or running), 'no_python',
        'failed' or 'foreign'."""
        home = self.home()
        if home is None:
            return 'failed'
        try:
            if self.client.status() is not None:
                return ''
        except Foreign:
            return 'foreign'
        try:
            self.launcher.start(home, self.client.port)
        except LaunchError as error:
            return 'no_python' if str(error) == 'python' else 'failed'
        return ''

    def stop(self):
        return self.launcher.stop()


def _never(*args, **kwargs):
    raise OSError('Remote Pilot is not available here')

# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Chromatic Manager against a simulated console built from the real stock backup and
the real launcher build. Power is cut at every write, the tool dies mid-job, boot
checks fail in several ways, official updates are simulated, settings change mid-job,
journals are torn and two tools race. No device access. Prints only pass/fail.
"""
import hashlib
import struct
import tempfile
from pathlib import Path

import chromatic_manager as cm
from chromatic_lock import Busy, DeviceLock
from chromatic_trial_runner import EXIT_MANUAL, EXIT_OK, EXIT_RECOVERED, EXIT_REFUSED, Journal
from compose_chromatic_image import check_image, stock_dump

# Every test that imports this module (manager, window, API, provisioning) runs without the
# owner's private files: no real Wi-Fi file, no Jellyfin settings, no Jellyfin server.
# A test that needs them patches in synthetic ones.
_SANDBOX = tempfile.TemporaryDirectory()
cm.WIFI_FILE = Path(_SANDBOX.name) / 'no-wifi-file.txt'
# The install in the window also sets up Wi-Fi and Jellyfin: never with this PC's real gateway
# identity, pairing copies or address. A synthetic identity and paths, all in the sandbox.
import wifi_gateway as _wifi_gateway  # noqa: E402
_wifi_gateway.IDENTITY = Path(_SANDBOX.name) / 'gateway-identity'
_wifi_gateway.IDENTITY.mkdir()
(_wifi_gateway.IDENTITY / 'server.der').write_bytes(b'synthetic test certificate')
(_wifi_gateway.IDENTITY / 'pairing.bin').write_bytes(bytes(32))
cm.PROVISION_COPY = Path(_SANDBOX.name) / 'provision-copy.json'
cm.REMOTE_PILOT_PAIRING = Path(_SANDBOX.name) / 'remote-pilot-pairing.json'
cm.lan_ipv4 = lambda: '192.0.2.20'
_REAL_PINNED = cm.PINNED
cm.PINNED = (Path(_SANDBOX.name) / 'pin-a', Path(_SANDBOX.name) / 'pin-b')  # tests never re-pin the real copies
for _source in _REAL_PINNED:  # a read-only copy, so tests still run if the build folder is gone
    if (Path(_source) / 'manifest.json').exists():
        import shutil as _shutil
        _shutil.copytree(_source, cm.PINNED[0])
        break
cm.jellyfin_settings_available = lambda: False


def _no_real_jellyfin(*args, **kwargs):
    raise AssertionError('a test tried to reach the real Jellyfin server')


cm.jellyfin_linker = _no_real_jellyfin
import chromatic_jellyfin_link as _jl  # noqa: E402
_jl.LINKER_FILE = Path(_SANDBOX.name) / 'linker.json'  # never the real linker session file
# The simulated console runs the pinned launcher build; for setup tests it stands in for a
# launcher that reads record v2. test_chromatic_provision checks the real version gate.
REAL_V2_GATE = cm.PROVISION_V2_LAUNCHER
cm.PROVISION_V2_LAUNCHER = (0, 0, 0)

_STOCK_BOOT = []


def stock_boot():
    """ModRetro's stock bootloader region, from the real stock backup (read once)."""
    if not _STOCK_BOOT:
        _STOCK_BOOT.append(cm.span(stock_dump(allow_synthetic=True), cm.BOOT))
    return _STOCK_BOOT[0]


IDENTITY = {'chip':'ESP32-U4WDH', 'revision': 3, 'mac': 'aa', 'flash_id': '204016'}
# Deadlines only matter if the simulated device hangs (none of these tests make it hang); they
# are generous so a busy PC (another session compiling) cannot fail a test by timing out.
FAST = cm.Timing(connect=60, read=60, md5=60, erase=60, write_base=60, write_bytes_per_s=1e9, launcher_watch=1,
                 stock_watch=1, quiet_after_handover=0, device_return=1, poll=0, attempts=3)


class PowerCut(Exception):
    pass


class Console:
    """Flash plus the serial output the real chip would produce, from the flash alone."""

    def __init__(self, image, release):
        self.flash = bytearray(image)
        self.release = release
        self.cut_after = None        # region writes allowed before the power fails
        self.writes = 0
        self.launcher_prints = True  # False: a launcher that never starts
        self.stock_loops = False     # True: stock fails under the new bootloader (seen once Games is chosen)
        self.owner_picks_games = False  # True: Games chosen during the boot watch (or a 1.1.0 countdown)
        self.launcher_crashes = False   # True: the launcher panics after announcing itself
        self.menu_opens = True          # False: the launcher starts but its menu never opens
        self.cartridge = True           # False: no cartridge, so the launcher goes on to stock itself
        self.fpga = None                # the FPGA version line the launcher prints at start, e.g. '18.40 debug=1'
                                        # or 'none'; None: an older launcher that does not print it
        self.silent_reset = False    # True: stock resets again by itself (seen as a loop)
        self.settings_change = None  # bytes stock writes to NVS when it runs
        self.identity_value = dict(IDENTITY)
        self.in_stub = False
        self.present = True
        self.full_reads = 0          # whole-flash reads (about 6.5 minutes each on the real console)
        self.part_reads = []         # (offset, size) of each partial read
        self.md5_calls = []          # (offset, size) of each on-chip MD5
        self.on_md5 = None           # callable(console, offset, size) run before an MD5 is answered

    def boots(self):
        image = bytes(self.flash)
        if not cm.boot_valid(image):
            return None
        try:
            cm.parse_table(cm.span(image, cm.TABLE))
        except Exception:
            return None  # every bootloader must read a valid table
        # Decided like the chip, not by the manager's own describe(): any bootloader other than
        # ModRetro's stock one is the launcher bootloader (whatever its build date), and it
        # starts the launcher when the table has the launcher row and the slot holds the app.
        slot = cm.app_description(cm.span(image, cm.SLOT_APP))
        if (cm.span(image, cm.BOOT) != stock_boot() and cm.layout(image, self.release) == 'launcher'
                and slot and slot[0] == cm.LAUNCHER_PROJECT):
            return 'launcher'
        return 'stock' if cm.app_description(cm.span(image, cm.APP)) else None

    def run_stock(self):
        if self.settings_change:
            start = cm.SETTINGS[0]
            self.flash[start:start + len(self.settings_change)] = self.settings_change


class Device:
    def __init__(self, console):
        self.c = console

    def device_present(self):
        return self.c.present

    def connect(self):
        self.c.in_stub = True

    def identity(self):
        return self.c.identity_value

    def read_all(self, say=None):
        self.c.full_reads += 1
        return bytes(self.c.flash)

    def read(self, offset, size):
        self.c.part_reads.append((offset, size))
        return bytes(self.c.flash[offset:offset + size])

    def md5(self, offset, size):
        self.c.md5_calls.append((offset, size))
        if self.c.on_md5:
            self.c.on_md5(self.c, offset, size)
        return hashlib.md5(bytes(self.c.flash[offset:offset + size])).hexdigest()

    def _cut(self, offset, data):
        c = self.c
        if c.cut_after is not None and c.writes >= c.cut_after:
            c.flash[offset:offset + 4096] = b'\xff' * 4096   # torn: erased, half written
            c.flash[offset:offset + 2048] = data[:2048]
            c.cut_after = None
            raise PowerCut()

    def write(self, offset, data):
        assert offset % 4096 == 0 and len(data) % 4096 == 0
        self._cut(offset, data)
        self.c.flash[offset:offset + len(data)] = data
        self.c.writes += 1

    def erase(self, offset, size):
        assert offset % 4096 == 0 and size % 4096 == 0
        self._cut(offset, b'\xff' * size)
        self.c.flash[offset:offset + size] = b'\xff' * size
        self.c.writes += 1

    def watch(self, seconds, handover, quiet):
        c = self.c
        c.in_stub = False
        booted = c.boots()
        log = b'ets Jun  8 2016\r\nrst:0x1 (POWERON_RESET)\r\n'
        if booted == 'launcher':
            if not c.launcher_prints:
                c.run_stock()  # a silent launcher still reaches stock, through the watchdog
                return log, False
            # Launcher 1.1.2: the menu opens (PJ-LAUNCHER-MENU) and waits for the owner's choice;
            # with no cartridge it goes on to stock by itself.
            log += b'PJ-LAUNCHER-READY 1.1.2\r\nPJ-DIAG t=0 boot reason=1\r\n'
            if c.fpga is not None:
                log += b'PJ-DIAG t=262 fpga version=' + c.fpga.encode('ascii') + b'\r\n'
            if c.menu_opens:
                log += b'PJ-LAUNCHER-MENU\nPJ-DIAG t=40 phase=menu\r\n'
            if c.launcher_crashes:
                log += b"Guru Meditation Error: Core  0 panic'ed (LoadProhibited)\r\nrst:0xc (SW_CPU_RESET)\r\n"
            elif c.owner_picks_games or (c.menu_opens and not c.cartridge):  # (or an older launcher's countdown)
                log += b'PJ-LAUNCHER-TO-STOCK\r\nrst:0xc (SW_CPU_RESET)\r\n'
                if c.stock_loops:
                    log += b'PJ-LAUNCHER-READY 1.1.2\r\nPJ-LAUNCHER-STOCK-INVALID\r\n'
                else:
                    c.run_stock()
            return log, handover is not None and handover in log
        if booted == 'stock':
            c.run_stock()
            if c.silent_reset:
                log += b'rst:0x10 (RTCWDT_RTC_RESET)\r\n'
        if booted is None:
            log += b'invalid header: 0xffffffff\r\nrst:0x10 (RTCWDT_RTC_RESET)\r\n'
        return log, False

    def close(self, reset=True):
        if reset:
            self.c.in_stub = False


def fix_checksums(image):
    data = bytearray(image)
    at, checksum = 24, 0xef
    for _ in range(data[1]):
        _, length = struct.unpack_from('<II', data, at)
        at += 8
        for byte in data[at:at + length]:
            checksum ^= byte
        at += length
    end = (at + 16) & ~15
    data[end - 1] = checksum
    if data[23] == 1:
        data[end:end + 32] = hashlib.sha256(bytes(data[:end])).digest()
    return bytes(data)


def with_app(stock, version=None, idf=None, project=None):
    app = bytearray(stock[cm.APP[0]:cm.APP[1]])
    for value, at in ((version, 16), (project, 48), (idf, 112)):
        if value:
            app[0x20 + at:0x20 + at + 32] = value.encode().ljust(32, b'\0')
    app = fix_checksums(bytes(app))
    check_image(app, 'edited app')
    return stock[:cm.APP[0]] + app + stock[cm.APP[1]:]


class Harness:
    def __init__(self, image, release, original=None):
        self.console = Console(image, release)
        self.release = release
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.saved = []
        self.backup_list = [('original', original or image, dict(IDENTITY))]
        self.log = []

    def manager(self):
        def save(image, identity):
            self.saved.append(image)
            self.backup_list.insert(0, (f'b{len(self.saved)}', image, dict(identity)))
        return cm.Manager(folder=self.folder / 'work', programmer_factory=lambda: Device(self.console),
                          release_loader=lambda: self.release, backups=lambda: list(self.backup_list),
                          save_backup=save, timing=FAST, say=self.log.append, sleep=lambda s: None)

    def state(self):
        return cm.describe(bytes(self.console.flash), self.release).name

    def flash(self):
        return bytes(self.console.flash)


def check(condition, message):
    if not condition:
        raise SystemExit(f'FAIL: {message}')


def release_or_skip(what):
    """The built start-up menu (release/launcher, or a pinned copy), or None after printing why "what" is
    skipped: these checks install that exact build on the simulated console."""
    try:
        return cm.Release.load()
    except cm.Refused:
        print(f'SKIP: {what} needs a built start-up menu (scripts/Build-ChromaticLauncher.ps1, or a release in '
              'release/launcher); no launcher release was found.')
        return None


def same_protected(a, b):
    return all(cm.span(a, r) == cm.span(b, r) for r in (cm.SETTINGS, cm.APP, (0, 0x1000), (cm.APP[1], cm.SLOT[0])))


def crash_mid_install(h, cut):
    m = h.manager()
    h.console.cut_after = cut
    m.recover = lambda: (_ for _ in ()).throw(SystemExit('PC died'))
    try:
        m.install()
    except SystemExit:
        pass


def official_update(flash, stock, app_image, damage=False):
    image = cm.put(bytes(flash), cm.BOOT, cm.span(stock, cm.BOOT).rstrip(b'\xff'))
    image = cm.put(image, cm.TABLE, cm.span(stock, cm.TABLE).rstrip(b'\xff'))
    image = image[:cm.APP[0]] + cm.span(app_image, cm.APP) + image[cm.APP[1]:]
    image = image[:cm.SETTINGS[0]] + b'\xff' * (cm.SETTINGS[1] - cm.SETTINGS[0]) + image[cm.SETTINGS[1]:]
    if damage:  # the updater ignores esptool's exit code, so a failed write looks like success
        image = cm.put(image, cm.BOOT, b'\x00' * 64)
    return bytearray(image)


ALWAYS = {(start, end - start) for start, end in cm.ALWAYS_READ}  # settings + console setup: 36 KiB


def fpga_status_checks(stock, release, installed):
    """The normal status check reads the console chip (FPGA) version the launcher prints at start
    (no JTAG, nothing written) and reports it; the core turns it into the Home tab's sound state."""
    import chromatic_manager_core as core
    for fpga, line, value, sound in (
            ('18.40 debug=1', 'Console chip (FPGA): version 18.40 with the debug bit, as the start-up menu reports it.',
             '18.40 debug', 'installed'),
            ('18.39 debug=1', 'Console chip (FPGA): version 18.39 with the debug bit, as the start-up menu reports it.',
             None, None),  # below our first sound minor: not one of ours
            ('18.14 debug=0', 'Console chip (FPGA): version 18.14, as the start-up menu reports it.', '18.14', 'none'),
            ('18.20 debug=0', 'Console chip (FPGA): version 18.20, as the start-up menu reports it.', '18.20', 'unknown'),
            ('none', 'Console chip (FPGA): no version reply at start.', 'no reply', 'unknown'),
            (None, 'Console chip (FPGA): not reported by this start-up menu.', 'not reported', None)):
        h = Harness(installed, release, stock)
        h.console.fpga = fpga
        check(h.manager().status() == EXIT_OK and line in h.log and h.flash() == installed and not h.console.in_stub,
              f'status reports the launcher\'s FPGA line {fpga!r}, writes nothing, leaves the chip running')
        found = core.summarize(h.log)
        if value is not None:
            check(found.get('fpga') == value and core.sound_state(found) == sound, f'FPGA {fpga!r}: summary {value!r}, '
                                                                                  f'sound {sound!r}')
        else:
            check(core.sound_state(found) in ('unknown', None), f'FPGA {fpga!r}: not taken for a sound image')
    # An older sound image of ours when this app builds a newer one: Update sound.
    check(core.sound_state({'fpga': '18.40 debug'}, (18, 41)) == 'outdated'
          and core.sound_state({'fpga': '18.42 debug'}, (18, 41)) == 'unknown'
          and core.sound_state({'fpga': '18.40'}) == 'unknown' and core.sound_state({'fpga': '18.14 debug'}) == 'unknown'
          and core.built_sound_version('sound build 18.41 (image 1a2b3c4d)') == (18, 41)
          and core.built_sound_version(None) is None, 'sound state: outdated only for an older sound image of ours')
    check(cm.launcher_fpga(b'PJ-DIAG t=5 fpga version=18.40 debug=1\r\n') == (18, 40, 1)
          and cm.launcher_fpga(b'PJ-DIAG t=5 fpga version=18.4') is None
          and cm.launcher_fpga(b'PJ-DIAG t=5 fpga version=none\n') == 'none', 'the FPGA line parses only when whole')
    # Stock (no start-up menu): no restart to read it, no line.
    h = Harness(stock, release, stock)
    h.console.fpga = '18.40 debug=1'
    check(h.manager().status() == EXIT_OK and not any(l.startswith('Console chip (FPGA)') for l in h.log),
          'stock console: the FPGA line is not looked for')
    # FPGA jobs keep the ESP32's facts and record what the chip holds from their own words.
    before = {'state': 'installed', 'launcher': '1.2.5', 'fpga': '18.14'}
    for key, code, lines, holds in (
            ('fpga_media', 0, ['sound build 18.40 (image x) installed: all 4 MiB read back and identical'], 'sound'),
            ('fpga_restore', 0, ['... console restarted. Rehearsal recorded for this session.'], 'original'),
            ('fpga_check', 0, ['Console chip: GW5A-25 confirmed. Its flash holds neither your original image nor'], 'unknown'),
            ('fpga_trial', 0, ['sound build is running from the console chip\'s RAM'], '18.14'),
            ('fpga_media', 1, ['Refused, nothing written: x'], '18.14'),
            ('fpga_media', 3, ['STOP'], None)):
        after = core.summary_after_job(key, code, lines, dict(before))
        check(after['state'] == 'installed' and after.get('fpga') == holds,
              f'{key} exit {code}: the ESP32 facts stay, the chip holds {holds!r}')


def owner_action_checks(stock, release, installed):
    """Owner actions: everything a job needs the owner to do while it runs is announced
    through one mechanism (chromatic_manager_owner.OwnerPrompts), which the window shows big and
    bright; the log keeps the same line. Here: the mechanism alone, the plug-in wait every console
    job goes through, and the guided official update's "run the updater now"."""
    import chromatic_manager_owner as oa
    said, seen = [], []

    def broken(event):
        raise RuntimeError('a listener that fails')

    def keep(event):
        seen.append(event)
    prompts = oa.OwnerPrompts(said.append)
    check(prompts.listen(broken) is broken and prompts.listen(keep) is keep and prompts.listen(keep) is keep
          and prompts.listeners == [broken, keep], 'listeners are kept once each')
    check(not prompts.waiting and prompts.done('x') is False and prompts.missed('x') is False
          and prompts.close() is False and seen == [] and said == [],
          'nothing asked: done, missed and close change nothing and say nothing')
    first = prompts.need('Switch it on', 'Do it now.', seconds=90)
    check(first == 1 and prompts.waiting and said == ['Switch it on. Do it now.']
          and seen == [{'id': 1, 'kind': oa.ACTION, 'state': oa.WAITING, 'title': 'Switch it on',
                        'instruction': 'Do it now.', 'seconds': 90.0, 'note': '', 'next': ''}],
          'a request: one event with its time allowed, the same words in the log, a failing listener ignored')
    seen[0]['title'] = 'changed by a listener'
    check(prompts.current['title'] == 'Switch it on' and prompts.history[0]['title'] == 'Switch it on',
          'a listener gets its own copy')
    check(prompts.done('Seen.', log='It was seen.') is True and not prompts.waiting and said[-1] == 'It was seen.'
          and seen[-1]['state'] == oa.DONE and seen[-1]['note'] == 'Seen.' and seen[-1]['id'] == 1
          and prompts.done() is False and prompts.missed('late') is False and len(seen) == 2,
          'done: one event with its note; it cannot be ended twice')
    second = prompts.need('Plug it in', 'Use the cable.', log='the wording the log always had')
    check(second == 2 and said[-1] == 'the wording the log always had' and seen[-1]['seconds'] is None
          and seen[-1]['state'] == oa.WAITING, 'a request with no time limit keeps an existing log wording')
    lines = len(said)
    third = prompts.need('Answer', 'In the small window.', seconds=-5, log='', kind=oa.QUESTION)
    check(third == 3 and len(said) == lines and [(e['id'], e['state']) for e in seen[-2:]] == [(2, oa.CLOSED), (3, oa.WAITING)]
          and seen[-1]['kind'] == oa.QUESTION and seen[-1]['seconds'] == 0.0,
          'a new request closes the one still waiting; an empty log text says nothing; no negative time')
    check(prompts.missed('Start again and do it when asked.') is True and seen[-1]['state'] == oa.TIMEOUT
          and seen[-1]['next'] == 'Start again and do it when asked.' and not prompts.waiting
          and prompts.close() is False and prompts.count == 3,
          'missed: the time ran out, with the plain next step')
    check([(e['id'], e['state']) for e in prompts.history]
          == [(1, oa.WAITING), (1, oa.DONE), (2, oa.WAITING), (2, oa.CLOSED), (3, oa.WAITING), (3, oa.TIMEOUT)]
          and len(seen) == 6 and all(set(e) == {'id', 'kind', 'state', 'title', 'instruction', 'seconds', 'note', 'next'}
                                     for e in prompts.history),
          'every change is in the history, in order')

    # The plug-in wait (every console job connects through it). Present: nothing is asked.
    h = Harness(installed, release, stock)
    m = h.manager()
    check(m.status() == EXIT_OK and m.owner.history == [] and not any('plug it in' in line for line in h.log),
          'console plugged in: no owner action, no waiting line')
    # Absent, then plugged in while the sign is up: asked once with the time allowed, then done.
    h = Harness(installed, release, stock)
    m = h.manager()
    events = []

    def plug(event):
        events.append(event)
        if event['state'] == oa.WAITING:
            check(not h.console.in_stub, 'asked before anything is opened')
            h.console.present = True
    m.owner.listen(plug)
    h.console.present = False
    check(m.status() == EXIT_OK and [(e['state'], e['title']) for e in events]
          == [(oa.WAITING, cm.PLUG_IN_TITLE), (oa.DONE, cm.PLUG_IN_TITLE)]
          and events[0]['instruction'] == cm.PLUG_IN_TEXT and events[0]['seconds'] == float(FAST.device_return)
          and events[0]['kind'] == oa.ACTION and events[1]['note'] == 'Your Chromatic is plugged in.'
          and sum('Waiting for the console: plug it in and switch it on' in line for line in h.log) == 1
          and not m.owner.waiting,
          'console absent: one owner action with its time, the same line in the log, done when it appears')
    # Never plugged in: the time runs out with the next step, and the job stops without touching anything.
    h = Harness(installed, release, stock)
    m = h.manager()
    h.console.present = False
    try:
        m.status()
        check(False, 'a console that never appears stops the job')
    except cm.StepFailed as error:
        check('did not appear on USB' in str(error), 'the refusal says the console did not appear')
    check([(e['state'], e['next']) for e in m.owner.history] == [(oa.WAITING, ''), (oa.TIMEOUT, cm.PLUG_IN_MISSED)]
          and not m.owner.waiting and not h.console.in_stub and h.console.writes == 0 and h.flash() == installed
          and 'Plug it in' in cm.PLUG_IN_MISSED,
          'never plugged in: time ran out with the plain next step; nothing opened, nothing written')

    # Guided official update, step 2: "run the updater now" is an owner action with no time limit,
    # up for as long as the question is asked; done on "done", gone on any other answer.
    updated = with_app(stock, version='9.9.9', idf='v5.4.1')

    def guided(answers):
        h = Harness(installed, release, stock)
        m = h.manager()
        replies, during = list(answers), []

        def person(prompt):
            reply = replies.pop(0)
            if prompt.startswith('Type done'):
                during.append(dict(m.owner.current))
                if reply == 'done':
                    h.console.flash = official_update(h.console.flash, stock, updated)
            return reply
        return h, m, m.guided_update(person), during
    h, m, result, during = guided(['done'])
    updater = [e for e in m.owner.history if e['title'] == cm.UPDATER_TITLE]
    check(result == EXIT_OK and h.state() == 'installed' and len(during) == 1
          and during[0]['state'] == oa.WAITING and during[0]['title'] == cm.UPDATER_TITLE
          and during[0]['instruction'] == cm.UPDATER_TEXT and during[0]['seconds'] is None
          and [(e['state'], e['note']) for e in updater] == [(oa.WAITING, ''), (oa.DONE, 'The updater finished and is closed.')]
          and sum("Step 2 of 4: run ModRetro's updater now" in line for line in h.log) == 1
          and all(e['title'] in (cm.UPDATER_TITLE, cm.PLUG_IN_TITLE) for e in m.owner.history) and not m.owner.waiting,
          'guided update: the updater step is an untimed owner action while its question is asked, then done')
    h, m, result, during = guided(['', 'maybe', 'done'])
    check(result == EXIT_OK and [d['state'] for d in during] == [oa.WAITING] * 3
          and sum(e['title'] == cm.UPDATER_TITLE and e['state'] == oa.WAITING for e in m.owner.history) == 1,
          'unclear answers: the same owner action stays up, never asked twice')
    for answer in ('cancel', 'failed'):
        h, m, result, during = guided([answer])
        updater = [e['state'] for e in m.owner.history if e['title'] == cm.UPDATER_TITLE]
        check(result == EXIT_OK and h.console.boots() == 'stock' and updater == [oa.WAITING, oa.CLOSED]
              and not m.owner.waiting, f'guided update, {answer}: the owner action is taken down, not called done')
    h = Harness(installed, release, stock)
    m = h.manager()

    def walks_away(prompt):
        raise RuntimeError('the question could not be asked')
    try:
        m.guided_update(walks_away)
        check(False, 'a question that fails stops the job')
    except RuntimeError:
        pass
    check(not m.owner.waiting and m.owner.history[-1]['state'] == oa.CLOSED,
          'the job failed at the question: nothing stays asked')


def identify_checks(stock, release, installed, morning_console):
    """Status and install identify the console by the chip's MD5 of each part; only a part
    nothing matches is read, and a stock firmware no verified backup holds is read in full."""
    check(sum(size for _, size in ALWAYS) == 36 * 1024, 'only 36 KiB is always read')
    # A known console: stock, or with the menu installed. No full read; the result is exact.
    for image, name in ((stock, 'stock'), (installed, 'installed')):
        h = Harness(image, release, stock)
        m = h.manager()
        check(m.status() == EXIT_OK and h.console.full_reads == 0 and set(h.console.part_reads) == ALWAYS,
              f'fast status on a known {name} console: only settings and setup read')
        check(m.last_image == h.flash() and not m.last_read_full, f'{name}: identified image is the flash byte for byte')
        check((0, cm.SIZE) in h.console.md5_calls, f'{name}: proven by the whole-flash MD5 on the chip')
        check(f'Console: {name}. Boots: {cm.describe(image, release).boots}.' in h.log and not h.console.in_stub,
              f'{name}: same answer as a full read, chip left running')
    check('Launcher in flash: version %s, the one this tool installs (up to date).' % release.version in h.log,
          'status says the installed launcher is up to date')
    # An earlier bootloader build and an older launcher: just those parts are read; the
    # bootloader is still compared with its date and checksum fields masked.
    h = Harness(morning_console, release, stock)
    check(h.manager().status() == EXIT_OK and h.console.full_reads == 0
          and set(h.console.part_reads) == ALWAYS | {(cm.BOOT[0], cm.BOOT[1] - cm.BOOT[0]),
                                                     (cm.SLOT_APP[0], cm.SLOT_APP[1] - cm.SLOT_APP[0])}
          and 'Console: outdated. Boots: start-up menu on power-on, stock otherwise.' in h.log,
          'unmatched bootloader and launcher: only those parts read; reads as outdated, not partial')
    check(any('this tool installs %s (choose Install to update)' % release.version in line for line in h.log),
          'status says an update is available')
    # This tool's own finished jobs are known images too (e.g. the launcher a later update replaced).
    check(h.manager().install() == EXIT_OK and h.state() == 'installed' and h.console.full_reads == 0,
          'update without a full read')
    h.console.flash = bytearray(morning_console)
    h.console.part_reads.clear()
    check(h.manager().status() == EXIT_OK and set(h.console.part_reads) == ALWAYS,
          'parts from a finished job are recognised')
    # A stock firmware no verified backup holds: the whole console is read.
    newer = with_app(stock, version='9.9.9', idf='v5.4.1')
    h = Harness(newer, release, stock)
    check(h.manager().status() == EXIT_OK and h.console.full_reads == 1
          and any('in no verified backup yet' in line for line in h.log)
          and 'No backup of this stock firmware yet; install and restore take one first.' in h.log,
          'unknown stock firmware: full read')
    # Install skips the full read only when the stock firmware is already backed up.
    h = Harness(stock, release)
    check(h.manager().install() == EXIT_OK and h.state() == 'installed' and h.console.full_reads == 0
          and not h.saved, 'install on backed-up stock: no full read, no new backup')
    h = Harness(newer, release, stock)
    writes_at_backup = []
    m = h.manager()
    inner = m.save_backup
    m.save_backup = lambda image, identity: (writes_at_backup.append(h.console.writes), inner(image, identity))
    check(m.install() == EXIT_OK and h.state() == 'installed' and h.console.full_reads == 1,
          'install on never-seen stock: read in full')
    check(len(h.saved) == 1 and h.saved[0] == newer and writes_at_backup == [0],
          'never-seen stock backed up from that full read, before any write')
    # A console whose parts do not add up to the chip's whole-flash MD5 (it changed under the
    # tool): the whole console is read instead, and that read is what is used.
    h = Harness(installed, release, stock)

    def change_once(console, offset, size):
        if (offset, size) == (0, cm.SIZE) and console.on_md5:
            console.on_md5 = None
            console.flash[cm.APP[1]:cm.APP[1] + 4] = b'\x00\x11\x22\x33'
    h.console.on_md5 = change_once
    m = h.manager()
    check(m.status() == EXIT_OK and h.console.full_reads == 1 and m.last_read_full and m.last_image == h.flash()
          and any('do not add up' in line for line in h.log), 'mismatch with the whole-flash MD5: full read')
    # Writes planned from an identified image keep every guarantee: a power cut at every step of
    # a fast install is undone exactly (recovery itself always reads in full).
    steps = len(cm.ordered_regions(stock, installed, True))
    for cut in range(steps):
        h = Harness(stock, release)
        h.console.cut_after = cut
        check(h.manager().install() == EXIT_RECOVERED and h.flash() == stock and h.console.full_reads == 1,
              f'fast install cut {cut}: undone from a full read')
    # Console setup and switch-off are planned from an identified image too.
    import chromatic_provision as cp
    h = Harness(installed, release, stock)
    check(h.manager().provision(cp.Record((cp.Network('Test', 'password1'),))) == EXIT_OK
          and h.console.full_reads == 0, 'console setup without a full read')
    closed = list((h.folder / 'work' / 'closed').glob('*/image-*.bin'))
    check(closed and all(cm.span(p.read_bytes(), cm.PROVISION) == b'\xff' * (cm.PROVISION[1] - cm.PROVISION[0])
                         for p in closed) and not list((h.folder / 'work').glob('image-*.bin')),
          'closed job images keep no console setup (scrubbed before they are moved)')
    with tempfile.TemporaryDirectory() as folder:
        journal = cm.ManagerJournal(Path(folder))
        journal.append('plan', 'ready')
        with_setup = cm.put(stock, cm.PROVISION_A, b'\x01' * 4096)
        (Path(folder) / 'image-x.bin').write_bytes(with_setup)
        journal.append('close', 'done')
        real_move = cm.shutil.move
        cm.shutil.move = lambda *a: (_ for _ in ()).throw(OSError('the PC died while archiving'))
        try:
            journal.archive()
        except OSError:
            pass
        finally:
            cm.shutil.move = real_move
        check((Path(folder) / 'image-x.bin').read_bytes() == cm.without_setup(with_setup),
              'a crash while archiving leaves no console setup behind')
    check(h.manager().disable() == EXIT_OK and h.console.full_reads == 0 and h.state() == 'partial',
          'switch-off without a full read')
    # Backup and restore still read in full.
    h = Harness(installed, release, stock)
    check(h.manager().backup() == EXIT_OK and h.console.full_reads == 1 and h.saved[-1] == installed,
          'backup is a full read')


def main():
    stock = stock_dump(allow_synthetic=True)
    release = release_or_skip('the Chromatic Manager checks')
    if release is None:
        return
    check(cm.describe(stock, release).name == 'stock', 'backup reads as stock')
    check(release.stock_table == cm.span(stock, cm.TABLE), 'derived stock table equals the real one byte for byte')
    t = cm.Timing()
    check(t.read >= 2 * 384 and t.write(0x1a0000) > 0x1a0000 / 11500 * 2, 'deadlines cover measured link speed')
    counts = {}

    # Install, idempotent re-install, removal back to the exact original. Never left in the stub.
    h = Harness(stock, release)
    check(h.manager().install() == EXIT_OK and h.state() == 'installed', 'install')
    check(not h.console.in_stub, 'chip left running firmware after install')
    installed = h.flash()
    check(same_protected(installed, stock), 'install kept settings, stock app and other data')
    check(h.manager().install() == EXIT_OK and h.flash() == installed and not h.console.in_stub,
          'second install writes nothing and resets the chip')
    check(h.manager().status() == EXIT_OK and not h.console.in_stub, 'status leaves the chip running')
    check(h.manager().remove() == EXIT_OK and h.flash() == stock, 'remove restores the exact original')

    # Write order: every prefix of an install or removal still boots something.
    for before, after, last in ((stock, installed, True), (installed, stock, False)):
        image = bytearray(before)
        for offset, data in cm.ordered_regions(before, after, last):
            image[offset:offset + len(data)] = data
            check(Console(bytes(image), release).boots() is not None, 'bootable between steps')
    order = cm.ordered_regions(stock, installed, True)
    check(order[-1][0] == cm.BOOT[0] and order[0][0] >= cm.SLOT[0], 'install: slot first, bootloader last')
    check(cm.ordered_regions(installed, stock, False)[0][0] == cm.BOOT[0], 'removal: bootloader first')

    # Power cut at every install write: undone at once, exactly, and seen starting.
    steps = len(order)
    for cut in range(steps):
        h = Harness(stock, release)
        h.console.cut_after = cut
        check(h.manager().install() == EXIT_RECOVERED and h.flash() == stock, f'install cut {cut}: undone')
        check(not h.manager().pending() and not h.console.in_stub, f'install cut {cut}: closed, chip running')
    counts['install_cuts'] = steps

    # The tool dies with the power: journal open, other jobs refused, status writes nothing, resume undoes.
    for cut in range(steps):
        h = Harness(stock, release)
        crash_mid_install(h, cut)
        check(h.manager().pending(), f'crash {cut}: job pending')
        check(h.manager().install() == EXIT_REFUSED, f'crash {cut}: other jobs refused until resumed')
        before = h.flash()
        check(h.manager().status() == EXIT_OK and h.flash() == before, f'crash {cut}: status writes nothing')
        check(h.manager().resume() == EXIT_RECOVERED and h.flash() == stock, f'crash {cut}: resume undoes')
    counts['crash_resumes'] = steps

    # Crash, then an official update before anyone resumes: never overwritten, job closed, user not locked out.
    h = Harness(stock, release)
    crash_mid_install(h, 1)
    newer = with_app(stock, version='9.9.9', idf='v5.4.1')
    h.console.flash = bytearray(newer)
    check(h.manager().resume() == EXIT_OK and h.flash() == newer, 'changed console never overwritten by resume')
    check(not h.manager().pending(), 'superseded job closed, so the tool is usable again')
    check(h.manager().install() == EXIT_OK and h.state() == 'installed', 'install works after a superseded job')
    # Missing job images: manual, and Abandon unlocks the tool.
    h = Harness(stock, release)
    crash_mid_install(h, 1)
    for path in (h.folder / 'work').glob('image-*.bin'):
        path.unlink()
    check(h.manager().resume() == EXIT_MANUAL and h.manager().pending(), 'missing images: manual')
    check(h.manager().abandon() == EXIT_OK and not h.manager().pending(), 'abandon closes the job')
    # A journal opened before any write (disk full, Ctrl+C) is closed, not left locking the tool.
    h = Harness(stock, release)
    m = h.manager()
    m.journal.append('plan', 'ready', kind='install', before='x', target='y')
    check(h.manager().install() == EXIT_OK and h.state() == 'installed', 'unwritten journal does not lock out')

    # Power cut during removal: finished, never left half-way.
    rsteps = len(cm.ordered_regions(installed, stock, False))
    for cut in range(rsteps):
        h = Harness(installed, release, stock)
        h.console.cut_after = cut
        check(h.manager().remove() == EXIT_RECOVERED and h.flash() == stock, f'remove cut {cut}')
    counts['remove_cuts'] = rsteps

    # Power-safe switch-off (guided update step 1): erases only the launcher area.
    h = Harness(installed, release, stock)
    check(h.manager().disable() == EXIT_OK and h.state() == 'partial' and h.console.boots() == 'stock',
          'switch-off erases only the launcher area and boots stock')
    off = h.flash()
    check(off[:cm.SLOT[0]] == installed[:cm.SLOT[0]], 'switch-off touched nothing else')
    h = Harness(installed, release, stock)
    h.console.cut_after = 0
    check(h.manager().disable() == EXIT_RECOVERED and h.flash() == off, 'switch-off cut: finished')

    # A console installed from an earlier build of the same bootloader: only its build date,
    # checksum byte and SHA-256 differ (as 1.1.0's does from 1.1.2's). It reads as installed or
    # outdated, never "partial", and an update writes only the launcher slot: the bootloader,
    # whose writes are not power-safe, is kept.
    def rebuilt(bootloader, at=cm.BOOT_DATE_TIME[0] + 12, value=b'9'):
        data = bytearray(bootloader)
        data[at:at + len(value)] = value
        data = fix_checksums(bytes(data))
        check_image(data, 'rebuilt bootloader')
        return data
    morning = rebuilt(release.bootloader)
    check(morning != release.bootloader and morning[-33:] != release.bootloader[-33:]
          and cm.same_bootloader(cm.span(cm.put(installed, cm.BOOT, morning), cm.BOOT), release.bootloader),
          'a rebuild that differs only by date, checksum and SHA is the same bootloader')
    other = rebuilt(release.bootloader, at=0x200, value=b'\x00\x01')
    check(not cm.same_bootloader(cm.span(cm.put(installed, cm.BOOT, other), cm.BOOT), release.bootloader),
          'any other difference is a different bootloader')
    # The release's own launcher, relabelled 1.1.0 (edited in place: a launcher can be larger than
    # stock's 1 MiB app region, so it is not staged there).
    older_launcher = bytearray(release.launcher)
    older_launcher[0x20 + 16:0x20 + 48] = b'1.1.0'.ljust(32, b'\0')
    older_launcher = fix_checksums(bytes(older_launcher))
    check_image(older_launcher, 'relabelled launcher')
    morning_console = cm.put(cm.put(installed, cm.BOOT, morning), cm.SLOT_APP, older_launcher)
    check(cm.describe(cm.put(installed, cm.BOOT, morning), release).name == 'installed'
          and cm.describe(morning_console, release).name == 'outdated', 'read as installed / outdated, not partial')
    plan = cm.install_target(morning_console, release)
    regions = cm.ordered_regions(morning_console, plan, True)
    check(regions and all(cm.SLOT[0] <= offset and offset + len(data) <= cm.SLOT_APP[1] for offset, data in regions)
          and cm.span(plan, cm.BOOT) == cm.span(morning_console, cm.BOOT), 'update plan: only the launcher slot, bootloader kept')
    h = Harness(morning_console, release, stock)
    check(h.manager().status() == EXIT_OK and 'Console: outdated. Boots: start-up menu on power-on, stock otherwise.'
          in h.log, 'status says outdated')
    check(h.manager().install() == EXIT_OK and h.state() == 'installed' and cm.span(h.flash(), cm.BOOT) == cm.span(morning_console, cm.BOOT),
          'update installs the new launcher and keeps the bootloader')
    # A power cut during that update is undone back to the 1.1.0 console, which starts its
    # menu: the undo must be reported as seen starting (the menu), not "not seen" (exit 5).
    h = Harness(morning_console, release, stock)
    h.console.cut_after = 0
    check(h.manager().install() == EXIT_RECOVERED and h.flash() == morning_console,
          'cut update undone; the menu seen starting')
    check(any('the start-up menu was seen starting' in line for line in h.log), 'undo reports the menu starting')
    identify_checks(stock, release, installed, morning_console)
    fpga_status_checks(stock, release, installed)
    owner_action_checks(stock, release, installed)
    counts['identify'] = True
    # A finished console setup on an installed console is also checked with the launcher's check.
    import chromatic_provision as cp
    h = Harness(installed, release, stock)
    h.console.cut_after = 0
    check(h.manager().provision(cp.Record((cp.Network('Test', 'password1'),))) == EXIT_RECOVERED,
          'cut setup finished, and the menu seen starting')

    # Launcher 1.1.2 waits on its menu: the install succeeds without anyone pressing a button,
    # and nothing is rewritten.
    h = Harness(stock, release)
    check(h.manager().install() == EXIT_OK and h.state() == 'installed'
          and not any('putting the console right' in line for line in h.log),
          'waiting menu: install succeeds with no button press and no recovery')
    check(any('choose Games' in line for line in h.log) and not any('do not press' in line for line in h.log),
          'install tells the owner to choose Games, not to keep off the buttons')
    h = Harness(stock, release)
    h.console.owner_picks_games = True
    check(h.manager().install() == EXIT_OK and h.state() == 'installed', 'Games chosen during the watch: fine')
    h = Harness(stock, release)
    h.console.cartridge = False
    check(h.manager().install() == EXIT_OK and h.state() == 'installed', 'no cartridge: menu goes on to stock, fine')
    check(cm.launcher_boot_ok(b'PJ-LAUNCHER-READY 1.1.2\r\nPJ-LAUNCHER-MENU\nPJ-DIAG x\r\n', True),
          'READY, MENU, quiet: menu waiting')
    check(not cm.launcher_boot_ok(b'PJ-LAUNCHER-READY 1.1.2\r\nPJ-DIAG x\r\n', True), 'READY without MENU: fails')
    check(cm.launcher_boot_ok(b'PJ-LAUNCHER-READY 1.1.0\r\nPJ-LAUNCHER-TO-STOCK\r\nrst:0xc (SW_CPU_RESET)\r\n', True),
          'older launcher countdown to stock: fine')
    check(not cm.launcher_boot_ok(b'PJ-LAUNCHER-READY 1.1.2\r\nPJ-LAUNCHER-MENU\n', False), 'no quiet window: not proven')
    check(not cm.launcher_boot_ok(b'PJ-LAUNCHER-READY 1.1.2\r\nrst:0x10 (RTCWDT_RTC_RESET)\r\n'
                                  b'PJ-LAUNCHER-READY 1.1.2\r\n', True), 'reset loop: fails')
    # Boot checks that must fail: silent launcher; a crashing launcher; stock that will not run
    # under the new bootloader once Games is chosen.
    for flaw in ('launcher_prints', 'menu_opens', 'launcher_crashes', 'stock_loops'):
        h = Harness(stock, release)
        setattr(h.console, flaw, flaw not in ('launcher_prints', 'menu_opens'))
        h.console.owner_picks_games = flaw == 'stock_loops'
        check(h.manager().install() == EXIT_RECOVERED and h.flash() == stock, f'{flaw}: install undone')
    # Removal whose boot check fails is not reported as plain success.
    h = Harness(installed, release, stock)
    h.console.silent_reset = True
    check(h.manager().remove() == cm.EXIT_NOT_SEEN and h.flash() == stock, 'failed stock check reported honestly')

    # Stock saves a setting during the check, then the check fails: that setting is kept.
    h = Harness(stock, release)
    h.console.launcher_prints = False
    h.console.settings_change = b'\x5a' * 16
    check(h.manager().install() == EXIT_RECOVERED, 'undo with changed settings')
    after = h.flash()
    check(after[cm.SETTINGS[0]:cm.SETTINGS[0] + 16] == b'\x5a' * 16, 'settings saved by stock are kept')
    check(after[:cm.SETTINGS[0]] == stock[:cm.SETTINGS[0]] and after[cm.SETTINGS[1]:] == stock[cm.SETTINGS[1]:],
          'everything else back to the original')

    # Refusals before any write, chip left running.
    def refused(image, label, setup=None, original=None):
        h = Harness(image, release, original)
        if setup:
            setup(h)
        check(h.manager().install() == EXIT_REFUSED and h.flash() == image and not h.console.in_stub, label)
    refused(stock, 'other console refused', lambda h: h.console.identity_value.update(mac='bb'))
    junk = bytearray(stock)
    junk[cm.SLOT[0]:cm.SLOT[0] + 16] = b'\x00' * 16
    refused(bytes(junk), 'foreign data refused')
    refused(with_app(stock, idf='v6.0'), 'IDF v6 stock refused')
    refused(with_app(stock, project='shiny_relay'), 'non-ModRetro firmware refused')
    refused(stock, 'no backup refused', lambda h: h.backup_list.clear())
    refused(stock, 'console never appears: refused', lambda h: setattr(h.console, 'present', False))
    rows, raw = cm.parse_table(cm.span(stock, cm.TABLE))
    extra = bytearray(raw[2])
    extra[3], extra[4:12] = 0x82, struct.pack('<II', 0x200000, 0x10000)
    extra[12:28] = b'storage'.ljust(16, b'\0')
    future = cm.put(stock, cm.TABLE, cm.table_bytes(list(raw) + [bytes(extra)]))
    check(cm.describe(future, release).name == 'unknown', 'future table reads as unknown')
    refused(future, 'future table refused')

    # Official update over an installed launcher: bootloader, table, app and settings replaced.
    updated = with_app(stock, version='9.9.9', idf='v5.4.1')
    official = bytes(official_update(installed, stock, updated))
    h = Harness(official, release, stock)
    check(h.state() == 'dormant' and h.console.boots() == 'stock', 'after an official update: dormant, boots stock')
    check(h.manager().install() == EXIT_OK and h.state() == 'installed', 'install after the update')
    check(len(h.saved) == 1 and cm.span(h.saved[0], cm.APP) == cm.span(updated, cm.APP), 'new stock backed up first')
    check(h.manager().remove() == EXIT_OK and h.state() == 'stock', 'remove after the update')
    check(cm.span(h.flash(), cm.APP) == cm.span(updated, cm.APP), 'still the updated firmware')

    # App-only update with the launcher installed: still installed, and removable.
    only_app = installed[:cm.APP[0]] + cm.span(updated, cm.APP) + installed[cm.APP[1]:]
    h = Harness(only_app, release, stock)
    check(h.state() == 'installed' and h.console.boots() == 'launcher', 'app-only update keeps the launcher')
    check(h.manager().remove() == EXIT_OK and h.state() == 'stock', 'removable after an app-only update')
    check(cm.span(h.flash(), cm.APP) == cm.span(updated, cm.APP), 'removal kept the newer app')

    # Restore: a version change needs explicit agreement, and the current image is backed up first.
    h = Harness(installed, release, stock)
    h.backup_list.insert(0, ('newer', updated, dict(IDENTITY)))
    nvs = bytearray(installed)
    nvs[cm.SETTINGS[0]:cm.SETTINGS[0] + 8] = b'\x11' * 8
    h.console.flash = nvs
    snapshot = bytes(nvs)
    check(h.manager().restore_stock() == EXIT_REFUSED and h.flash() == snapshot, 'version change needs agreement')
    check(h.manager().restore_stock(confirm=lambda p: 'nope') == EXIT_REFUSED, 'wrong confirmation refused')
    check(h.manager().restore_stock(confirm=lambda p: 'change') == EXIT_OK, 'restore with typed agreement')
    out = h.flash()
    check(len(h.saved) == 1 and h.saved[0] == snapshot, 'console backed up before a version change')
    check(cm.span(out, cm.APP) == cm.span(updated, cm.APP), 'restore picks the newest backup')
    check(out[cm.SETTINGS[0]:cm.SETTINGS[0] + 8] == b'\x11' * 8, 'restore keeps current settings')
    check(h.state() == 'stock', 'restored console is plain stock')
    goal = cm.restore_target(snapshot, updated, release, stock)
    restore_steps = len(cm.ordered_regions(snapshot, goal, False))
    for cut in range(restore_steps):
        h = Harness(snapshot, release, stock)
        h.backup_list.insert(0, ('newer', updated, dict(IDENTITY)))
        h.console.cut_after = cut
        check(h.manager().restore_stock(allow_version_change=True) == EXIT_RECOVERED and h.flash() == goal,
              f'restore cut {cut}: finished')
    counts['restore_cuts'] = restore_steps
    h = Harness(installed, release, stock)
    check(h.manager().restore_stock() == EXIT_OK and h.flash() == stock, 'same-version restore is plain stock')

    # Guided official update: every answer a person can give.
    def guided(start, answers, app_image=None, damage=False):
        h = Harness(start, release, stock)
        replies = list(answers)

        def person(prompt):
            reply = replies.pop(0)
            if prompt.startswith('Type done'):
                check(h.console.boots() == 'stock', 'menu is off while the person runs the updater')
                if reply == 'done' and app_image is not None:
                    h.console.flash = official_update(h.console.flash, stock, app_image, damage)
            return reply
        return h, h.manager().guided_update(person)
    h, result = guided(installed, ['done'], updated)
    check(result == EXIT_OK and h.state() == 'installed', 'guided update ends with the menu back on')
    check(cm.span(h.flash(), cm.APP) == cm.span(updated, cm.APP), 'guided update keeps the new firmware')
    check(any(cm.span(s, cm.APP) == cm.span(updated, cm.APP) for s in h.saved), 'new firmware backed up')
    check(h.console.full_reads == 1, 'guided update: only the new firmware is read in full')
    check(any(m.startswith('Stock firmware updated from') for m in h.log), 'version change reported')
    h, result = guided(installed, ['', 'maybe', 'cancel'])
    check(result == EXIT_OK and h.console.boots() == 'stock' and h.state() == 'partial',
          'unclear answers re-asked; cancel leaves the menu off, stock running')
    h, result = guided(installed, ['failed'])
    check(result == EXIT_OK and h.console.boots() == 'stock', 'failed update: menu stays off')
    h, result = guided(installed, ['done', 'no'], stock)
    check(result == EXIT_OK and h.state() == 'installed', 'already up to date: menu back on')
    h, result = guided(installed, ['done', 'yes'], stock)
    check(result == EXIT_REFUSED and h.console.boots() == 'stock' and any('failed silently' in m for m in h.log),
          'updater claimed success but nothing changed: menu stays off, told to rerun')
    h, result = guided(installed, ['done'], updated, damage=True)
    check(result == EXIT_REFUSED and any('damaged' in m for m in h.log), 'failed update spotted, no restore advised')
    h, result = guided(stock, ['done'], updated)
    check(result == EXIT_OK and h.state() == 'installed', 'guided update from plain stock also installs')
    h, result = guided(installed, ['done'], with_app(stock, idf='v6.0'))
    check(result == EXIT_REFUSED and h.console.boots() == 'stock', 'incompatible new stock: menu stays off')
    future_update = bytearray(official_update(installed, stock, updated))
    future_update[cm.TABLE[0]:cm.TABLE[1]] = cm.table_bytes(list(raw) + [bytes(extra)])
    h = Harness(installed, release, stock)
    h_replies = ['done']

    def person_future(prompt):
        h.console.flash = bytearray(future_update)
        return h_replies.pop(0)
    check(h.manager().guided_update(person_future) == EXIT_OK and any('does not support yet' in m for m in h.log),
          'new official layout: told it worked and not to restore')

    # Torn journal line: dropped on the next append, so the journal can close again.
    with tempfile.TemporaryDirectory() as folder:
        j = Journal(folder)
        j.append('plan', 'ready')
        with open(j.path, 'ab') as file:
            file.write(b'{"phase":"wri')
        j.load()
        check(not j.damaged, 'a torn last line is not damage')
        j.append('close', 'done')
        check(Journal(folder).load().closed, 'journal closes after a torn line')

    # Operating-system lock: a second holder is refused; it is free again afterwards.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / 'lock'
        with DeviceLock(path):
            try:
                with DeviceLock(path):
                    check(False, 'second lock taken')
            except Busy:
                pass
        with DeviceLock(path):
            pass

    # The release is pinned to two places, so switch-off and restore work without the build folder.
    with tempfile.TemporaryDirectory() as folder:
        pins = (Path(folder) / 'a', Path(folder) / 'b')
        for pin in pins:  # pinned directly: the build folder may hold no image (never an unreviewed one)
            cm.Release.pin(release, pin)
        loaded = cm.Release.load(folder=Path(folder) / 'no-build', pinned=pins)
        check(loaded == release, 'pinned release loads without the build folder')
        (pins[0] / 'chromatic_launcher.bin').write_bytes(b'x')
        check(cm.Release.load(folder=Path(folder) / 'no-build', pinned=pins) == release, 'damaged pin skipped')

    # Command line: writes need --yes; a pending job blocks other writes.
    class NoLock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass
    import re

    def typed(prompt):  # a person reading the one-time word off the screen
        return re.search(r'Type (\S+) to continue', prompt).group(1)
    h = Harness(stock, release)
    for argv in (['install'], ['install', '--yes'], ['remove', '--yes'], ['jellyfin-forget', '--yes'], ['update'],
                 ['abandon', '--yes'], ['menu'], []):
        check(cm.main(argv, h.manager(), NoLock(), ask=typed, at_console=False) == EXIT_REFUSED
              and h.flash() == stock, f'{argv}: no console window, refused even with --yes')
    words = []
    check(cm.main(['install', '--yes'], h.manager(), NoLock(), ask=lambda p: words.append(typed(p)) or 'yes',
                  at_console=True) == EXIT_REFUSED and h.flash() == stock, 'plain "yes" does not confirm')
    check(cm.main(['install'], h.manager(), NoLock(), ask=lambda p: words.append(typed(p)) or 'yes123',
                  at_console=True) == EXIT_REFUSED, 'a guessed word does not confirm')
    check(cm.main(['install'], h.manager(), NoLock(), ask=typed, at_console=True) == EXIT_OK
          and h.state() == 'installed', 'cli install with the typed one-time word')
    check(all(re.fullmatch(r'yes\d{3}', w) for w in words) and len(set(words)) == len(words),
          'the confirmation word is new each time')
    check(cm.main(['status'], h.manager(), NoLock(), at_console=False) == EXIT_OK, 'status needs no console window')
    # The menu with a pending job offers finish, status and abandon.
    h = Harness(stock, release)
    crash_mid_install(h, 1)
    answers = iter(['2'])
    check(cm.menu(h.manager(), lambda p: next(answers)) == EXIT_OK and h.manager().pending(), 'pending menu: status')
    answers = iter(['3', 'abandon'])
    check(cm.menu(h.manager(), lambda p: next(answers)) == EXIT_OK and h.manager().pending(),
          'pending menu: plain "abandon" is not enough')
    answers = iter(['3'])
    check(cm.menu(h.manager(), lambda p: next(answers, None) or typed(p)) == EXIT_OK and not h.manager().pending(),
          'pending menu: abandon with the typed word')

    # The console's port at any player number (0x0101 "Player 01", 0x0100 "Player XX" at the
    # start-up menu); other devices and other vendors are ignored.
    from types import SimpleNamespace
    from serial.tools import list_ports
    real = list_ports.comports
    try:
        list_ports.comports = lambda: [SimpleNamespace(device=d, vid=v, pid=p) for d, v, p in (
            ('A', 0x374e, 0x0100), ('B', 0x374e, 0x0101), ('C', 0x374e, 0x0105), ('D', 0x374e, 0x0200),
            ('E', 0x303a, 0x0101), ('F', None, None))]
        check(cm.ManagerProgrammer.ports() == ['A', 'B', 'C'], 'console port found at any player number')
        list_ports.comports = lambda: [SimpleNamespace(device='A', vid=0x374e, pid=0x0100)]
        check(cm.ManagerProgrammer().device_present(), 'console at the start-up menu is present')
    finally:
        list_ports.comports = real

    check(counts.get('identify'), 'identification checks ran')
    print('PASS: fast identification (known console without a full read, unmatched parts read alone, never-seen '
          'stock read in full and backed up before any write, whole-flash MD5 mismatch falls back to a full read, '
          'fast-install power cuts undone).')
    print(f"PASS: manager install/remove/restore, {counts['install_cuts']} install, {counts['remove_cuts']} removal and "
          f"{counts['restore_cuts']} restore power cuts, {counts['crash_resumes']} tool crashes, power-safe switch-off, "
          'superseded and abandoned jobs never lock the tool, honest boot-check results, settings kept, wrong '
          'console/foreign data/IDF v6/non-ModRetro/no backup/absent console/future table refused, official and '
          'app-only updates, version-change restore needs typed agreement and backs up first, guided update for '
          'every answer, torn journal, OS lock, pinned release, CLI and menu guards. Owner actions: one mechanism '
          '(a request with its time allowed, done, time ran out with the next step, closed; the same line logged; '
          'a failing listener ignored), the plug-in wait (not asked when the console is there; asked once then done; '
          'timed out with nothing opened) and the guided update\'s run-the-updater step (untimed, done only on '
          '"done"). Simulated console, no hardware.')


if __name__ == '__main__':
    main()

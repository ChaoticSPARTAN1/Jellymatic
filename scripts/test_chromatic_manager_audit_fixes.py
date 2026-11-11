# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""The Chromatic Manager fixes from a feature-completeness audit: gap 1 (Wi-Fi setup merges with the console's networks instead of replacing them), gap 8
(Home's Wi-Fi and Jellyfin line, the console's own sign-in tested at status), gap 11 (the erase is
said truthfully), gap 7 (the Jellyfin page on this PC) and gap 9 (the sound setup offered after the
guided official update).

Simulated console from test_chromatic_manager.py, fake Jellyfin answers, temporary files only: no
device, no COM port, no real Wi-Fi file, no private Jellyfin file, no real server. Prints only
pass/fail.

Run: private/chromatic-manager-gui/venv/Scripts/python.exe -B scripts/test_chromatic_manager_audit_fixes.py
"""
import io
import json
import os
from pathlib import Path
import tempfile
import urllib.error

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_QPA_FONTDIR', os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts'))

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QLineEdit

import chromatic_manager as cm
import chromatic_manager_core as core
import chromatic_manager_gui as gui
import chromatic_provision as cp
import chromatic_theme as theme
import connection_config as cc
from compose_chromatic_image import stock_dump

from test_chromatic_manager import EXIT_OK, EXIT_REFUSED, Harness, check, official_update, with_app
from test_chromatic_manager_gui import Rig

ROOT = Path(__file__).resolve().parents[1]
SOUND = 'sound build 18.40 (image 2fb37494)'
CONSOLE_NETS = (cp.Network('Cafe On Console', 'console-pass-1'), cp.Network('Phone Hotspot', 'console-pass-2'))
JELLYFIN = cp.Jellyfin('http://192.0.2.10:8096', '0123456789abcdef0123456789abcdef', 'a1b2c3d4e5f6a7b8c9d0e1f2',
                       'chromatic-001122334455667788990011')
PASSWORDS = ('console-pass-1', 'console-pass-2', 'home-password-1', 'new-phone-pass', 'fourth-pass-1', 'fifth-pass-1',
             'gui-password-1')


def settle(ms=150):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def record(flash):
    return cp.current(cm.span(flash, cm.PROVISION_A), cm.span(flash, cm.PROVISION_B))[0]


def names(rec):
    return [n.ssid for n in rec.networks]


def asker(name, answers, secret_value='home-password-1'):
    """The manager's questions answered by prompt; the list question from `answers` in order."""
    asked, secrets = [], []

    def ask(prompt):
        asked.append(prompt)
        if prompt.startswith('Use this PC at'):
            return 'yes'
        if prompt.startswith('Wi-Fi network name'):
            return name
        if prompt.startswith(cm.WIFI_LIST_PROMPT):
            return answers.pop(0) if answers else 'cancel'
        raise AssertionError(f'unexpected question: {prompt[:40]}')

    def secret(prompt):
        secrets.append(prompt)
        return secret_value
    return ask, secret, asked, secrets


def console_with(stock, release, networks=CONSOLE_NETS, jellyfin=None):
    """An installed console whose owner added `networks` on the console itself."""
    h = Harness(stock, release)
    check(h.manager().install() == EXIT_OK, 'install for the setup tests')
    if networks or jellyfin:
        check(h.manager().provision(cp.Record(tuple(networks), None, jellyfin)) == EXIT_OK, 'networks "added on the console"')
    return h


# ---- gap 1: Wi-Fi setup merges ---------------------------------------------------------------
def wifi_merge_checks(stock, release):
    entries = cm.merge_networks(CONSOLE_NETS, [('Home', 'home-password-1'), ('Phone Hotspot', 'new-phone-pass'),
                                               ('Home', 'ignored-dup-1')])
    check([(n.ssid, note) for n, note in entries] == [('Home', cm.NOTE_NEW), ('Phone Hotspot', cm.NOTE_PASSWORD),
                                                      ('Cafe On Console', cm.NOTE_KEPT)],
          'merge: typed first (a match updates the console network), the console\'s others kept, a repeat counts once')
    check(entries[2][0].psk == 'console-pass-1' and entries[1][0].psk == 'new-phone-pass', 'merge: passwords as intended')
    same = cm.merge_networks(CONSOLE_NETS, [('Cafe On Console', 'console-pass-1')])
    check(same[0][1] == cm.NOTE_KEPT, 'the same name and password counts as kept')
    for answer, want in (('ok', [0, 1, 2]), ('yes', [0, 1, 2]), ('2,1,3', [1, 0, 2]), ('3 1', [2, 0]), (' 2 , 3 ', [1, 2])):
        check([entries.index(e) for e in cm.chosen_networks(entries, answer)] == want, f'list answer {answer!r}')
    for answer in ('', 'cancel', 'no'):
        check(cm.chosen_networks(entries, answer) is None, f'list answer {answer!r} cancels')
    for answer, needle in (('banana', 'Type ok'), ('1,1', 'at most once'), ('4', 'at most once'), ('0', 'at most once')):
        try:
            cm.chosen_networks(entries, answer)
            check(False, f'{answer!r} must be refused')
        except ValueError as error:
            check(needle in str(error), f'list answer {answer!r}: plain reason')
    five = [(cp.Network(f'Net {n}', f'password-{n}'), cm.NOTE_KEPT) for n in range(5)]
    try:
        cm.chosen_networks(five, 'ok')
        check(False, 'five networks must be refused')
    except ValueError as error:
        check('at most 4' in str(error) and 'Remove 1' in str(error), 'the 4-network limit, in plain words')
    check(len(cm.chosen_networks(five, '5,1,2,3')) == 4, 'leaving one out fits the limit')
    prompt = cm.network_list_prompt(entries)
    check(prompt.startswith(cm.WIFI_LIST_PROMPT) and gui.WIFI_LIST_PROMPT == cm.WIFI_LIST_PROMPT,
          'the window recognises the manager\'s list question')
    check(not any(p in prompt for p in PASSWORDS + ('ignored-dup-1',)), 'the list question carries names, never a password')
    parsed, problem = gui.parse_network_prompt(cm.network_list_prompt(entries, 'A problem line.'))
    check(parsed == [(n.ssid, note) for n, note in entries] and problem == 'A problem line.', 'the window reads the list back')
    odd = cm.merge_networks((), [('Say "hi" (5G)', 'password-x')])
    check(gui.parse_network_prompt(cm.network_list_prompt(odd))[0] == [('Say "hi" (5G)', cm.NOTE_NEW)],
          'a name with quotes and brackets still reads back')

    # A console with two networks the owner added on it: Wi-Fi setup adds a third and keeps both.
    h = console_with(stock, release)
    before_tokens = record(h.flash())
    ask, secret, asked, secrets = asker('Home', ['ok'])
    check(cm.wifi_setup(h.manager(), ask, secret) == EXIT_OK, 'Wi-Fi setup on a console with networks')
    rec = record(h.flash())
    check(names(rec) == ['Home', 'Cafe On Console', 'Phone Hotspot']
          and [n.psk for n in rec.networks] == ['home-password-1', 'console-pass-1', 'console-pass-2'],
          'gap 1: the console\'s networks are kept, passwords untouched, the new one added first')
    lists = [p for p in asked if p.startswith(cm.WIFI_LIST_PROMPT)]
    check(len(lists) == 1 and 'Cafe On Console' in lists[0] and cm.NOTE_KEPT in lists[0] and '(new)' in lists[0],
          'the person saw the whole list before the write')
    check(rec.sequence == before_tokens.sequence + 1 and cm.span(h.flash(), cm.PROVISION_A) == cm.span(h.flash(), cm.PROVISION_B),
          'written through the audited provisioning write (both sectors, next sequence)')
    check(any('1 added' in line and '2 kept' in line for line in h.log), 'the result in counts only')

    # The same name with a new password updates that network; the order is chosen.
    ask, secret, asked, _ = asker('Phone Hotspot', ['2,1,3'], 'new-phone-pass')
    check(cm.wifi_setup(h.manager(), ask, secret) == EXIT_OK, 'new password for a console network')
    rec = record(h.flash())
    check(names(rec) == ['Home', 'Phone Hotspot', 'Cafe On Console'] and rec.networks[1].psk == 'new-phone-pass'
          and rec.networks[0].psk == 'home-password-1' and rec.networks[2].psk == 'console-pass-1',
          'matching name: its password changed, the order as chosen, the others kept')
    check(cm.NOTE_PASSWORD in [p for p in asked if p.startswith(cm.WIFI_LIST_PROMPT)][0], 'shown as "new password"')

    # Up to 4; a fifth needs one removed first, with a plain message and the question again.
    ask, secret, _, _ = asker('Fourth Net', ['ok'], 'fourth-pass-1')
    check(cm.wifi_setup(h.manager(), ask, secret) == EXIT_OK and len(record(h.flash()).networks) == 4, 'four fit')
    ask, secret, asked, _ = asker('Fifth Net', ['ok', '1,2,3,4'], 'fifth-pass-1')
    check(cm.wifi_setup(h.manager(), ask, secret) == EXIT_OK, 'a fifth network after removing one')
    lists = [p for p in asked if p.startswith(cm.WIFI_LIST_PROMPT)]
    rec = record(h.flash())
    check(len(lists) == 2 and 'keeps at most 4 networks. Remove 1' in lists[1], 'too many: the reason, then asked again')
    check(names(rec) == ['Fifth Net', 'Fourth Net', 'Home', 'Phone Hotspot'], 'the one left out is removed, the rest kept')
    check(any('1 removed' in line for line in h.log), 'the removal is said, in counts')
    before = h.flash()
    for answers, why in ((['cancel'], 'cancel'), (['banana'] * cm.WIFI_LIST_TRIES, 'no usable answer')):
        ask, secret, _, _ = asker('Sixth Net', answers, 'sixth-pass-1')
        check(cm.wifi_setup(h.manager(), ask, secret) == EXIT_REFUSED and h.flash() == before,
              f'{why}: refused, nothing written')
    check(any('the Wi-Fi list was not confirmed' in line for line in h.log), 'and it says why')

    # No name: only reorder or remove; no password is asked.
    ask, secret, asked, secrets = asker('', ['2,1,3,4'])
    check(cm.wifi_setup(h.manager(), ask, secret) == EXIT_OK and not secrets, 'empty name: no password question')
    check(names(record(h.flash())) == ['Fourth Net', 'Fifth Net', 'Home', 'Phone Hotspot'], 'reordered only')
    ask, secret, _, _ = asker('', ['4,3,2'])
    check(cm.wifi_setup(h.manager(), ask, secret) == EXIT_OK and names(record(h.flash())) == [
        'Phone Hotspot', 'Home', 'Fifth Net'], 'removal without adding anything')

    # A console with no networks yet: no list question; nothing typed: refused before any write.
    fresh = console_with(stock, release, networks=())
    ask, secret, asked, _ = asker('Home', [])
    check(cm.wifi_setup(fresh.manager(), ask, secret) == EXIT_OK and names(record(fresh.flash())) == ['Home']
          and not any(p.startswith(cm.WIFI_LIST_PROMPT) for p in asked), 'first setup: no list question')
    empty = console_with(stock, release, networks=())
    flash = empty.flash()
    ask, secret, asked, _ = asker('', [])
    check(cm.wifi_setup(empty.manager(), ask, secret) == EXIT_REFUSED and empty.flash() == flash,
          'nothing typed and nothing on the console: refused, nothing written')

    # Networks from the private Wi-Fi file (a synthetic one here) merge the same way; more than 4 must be cut.
    with tempfile.TemporaryDirectory() as folder:
        wifi_file = Path(folder) / 'wifi.txt'
        wifi_file.write_text(''.join(f'ssid=File Net {n}\npassword=file-pass-{n}\n' for n in range(5)), encoding='utf-8')
        saved = cm.WIFI_FILE
        try:
            cm.WIFI_FILE = wifi_file
            check(len(cm.read_wifi_networks(wifi_file)) == 4 and len(cm.read_wifi_networks(wifi_file, limit=None)) == 5,
                  'the file reader keeps its old limit unless asked for all')
            h2 = console_with(stock, release)
            ask, secret, asked, _ = asker('unused', ['ok', '6,1,2,3'])  # 5 from the file + the console's 2 = 7
            check(cm.wifi_setup(h2.manager(), ask, secret) == EXIT_OK, 'file with five networks')
            check(asked[-1].count(' network "') == 7, 'all seven listed')
            check(names(record(h2.flash())) == ['Cafe On Console', 'File Net 0', 'File Net 1', 'File Net 2'],
                  'the person chose which four stay (the console\'s own network first)')
        finally:
            cm.WIFI_FILE = saved
    # Two jobs closed in the same second with the same clock digits (the cause of the flaky "four fit"
    # above,: the second closed-job folder gets the next free name; the verified
    # write is not reported as unfinished and nothing is recovered.
    import time as clock
    saved_ns, saved_strftime = clock.monotonic_ns, clock.strftime
    try:
        clock.monotonic_ns = lambda: 123456789
        clock.strftime = lambda form, *rest: saved_strftime(form, clock.localtime(1_800_000_000))
        same = console_with(stock, release)
        for name, password in (('Same Second 1', 'same-pass-1'), ('Same Second 2', 'same-pass-2')):
            ask, secret, _, _ = asker(name, ['ok'], password)
            check(cm.wifi_setup(same.manager(), ask, secret) == EXIT_OK, f'{name}: closed in the same second, still OK')
    finally:
        clock.monotonic_ns, clock.strftime = saved_ns, saved_strftime
    check(len(record(same.flash()).networks) == 4 and not any('did not finish' in line for line in same.log)
          and len(list((same.folder / 'work' / 'closed').iterdir())) >= 2, 'same-second jobs: both closed, no recovery')
    every_log = '\n'.join(h.log + fresh.log + empty.log)
    check(not any(p in every_log for p in PASSWORDS), 'no password in any message')
    check(not any(n in every_log for n in ('Cafe On Console', 'Phone Hotspot', 'Fifth Net')),
          'no network name in the manager\'s messages (only in the list question)')

    # The text menu masks names by default; "show" shows the list once.
    seen = []

    def typed(prompt):
        seen.append(prompt)
        return 'show' if len(seen) == 1 else 'ok'
    masked = cm.private_prompts(typed)
    check(masked(cm.network_list_prompt(entries)) == 'ok', 'text menu: answered after show')
    check('Home' not in seen[0] and 'Cafe On Console' not in seen[0] and 'type show' in seen[0]
          and 'Cafe On Console' in seen[1], 'text menu: names hidden first, shown only when asked')
    plain = []
    cm.private_prompts(lambda p: plain.append(p) or 'x')('Type done once the updater reported success: ')
    check(plain == ['Type done once the updater reported success: '], 'questions without private details are unchanged')


# ---- gap 8: the console's own sign-in tested at status; Home's second line ----------------------
class FakeLinker:
    def __init__(self, state):
        self.state, self.calls = state, []

    def token_state(self, linked):
        self.calls.append(linked.device_id)
        if self.state == 'raise':
            raise OSError('no network')
        return self.state


class FakeResponse(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(b'' if body is None else json.dumps(body).encode('utf-8'))
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeServer:
    """urllib opener stand-in: a Jellyfin server's answers by path."""

    def __init__(self, password='right-password', quick_connect=True, token_ok=True, up=True):
        self.password, self.quick_connect, self.token_ok, self.up, self.calls = password, quick_connect, token_ok, up, []

    def open(self, request, timeout=None):
        from urllib.parse import urlsplit
        path = urlsplit(request.full_url).path
        self.calls.append((request.get_method(), path, dict(request.header_items())))
        if not self.up:
            raise urllib.error.URLError('down')
        if path == '/System/Info/Public':
            return FakeResponse(200, {'ServerName': 'Test'})
        if path == '/QuickConnect/Enabled':
            return FakeResponse(200, self.quick_connect)
        if path == '/Users/AuthenticateByName':
            body = json.loads(request.data)
            if body.get('Pw') == self.password:
                return FakeResponse(200, {'AccessToken': 'linker-token-0123456789', 'User': {'Id': 'user-1'}})
            raise urllib.error.HTTPError(request.full_url, 401, 'no', {}, io.BytesIO())
        if path == '/Users/Me':
            if self.token_ok:
                return FakeResponse(200, {'Id': 'user-1'})
            raise urllib.error.HTTPError(request.full_url, 401, 'no', {}, io.BytesIO())
        raise urllib.error.HTTPError(request.full_url, 404, 'no', {}, io.BytesIO())


def status_checks(stock, release):
    import chromatic_jellyfin_link as jl
    linker = jl.Linker(cc.Connection('http://192.0.2.10:8096'), opener=FakeServer())
    check(linker.token_state(JELLYFIN) == 'works' and linker.works(JELLYFIN), 'token_state: works')
    linker.opener = FakeServer(token_ok=False)
    check(linker.token_state(JELLYFIN) == 'refused' and not linker.works(JELLYFIN), 'token_state: refused (401)')
    linker.opener = FakeServer(up=False)
    check(linker.token_state(JELLYFIN) == 'unreachable', 'token_state: unreachable')
    calls = FakeServer()
    linker.opener = calls
    linker.token_state(JELLYFIN)
    check([(m, p) for m, p, _ in calls.calls] == [('GET', '/Users/Me')]
          and f'DeviceId="{JELLYFIN.device_id}"' in calls.calls[0][2].get('Authorization', ''),
          'one GET /Users/Me with the console\'s own token, as the console device; nothing else')

    saved = cm.jellyfin_linker
    try:
        for state, words, tone in (('works', 'Jellyfin linked.', 'ok'),
                                   ('refused', 'Jellyfin sign-in no longer works.', 'error'),
                                   ('unreachable', 'Jellyfin linked, not checked (server not reachable from this PC).',
                                    'update'),
                                   ('raise', 'Jellyfin linked, not checked.', 'update')):
            fake = FakeLinker(state)
            cm.jellyfin_linker = lambda server_url=None, f=fake: f
            h = console_with(stock, release, jellyfin=JELLYFIN)
            h.log.clear()
            check(h.manager().status() == EXIT_OK, f'status ({state})')
            line = next((l for l in h.log if l.startswith('Console setup:')), '')
            check(line == f'Console setup: 2 Wi-Fi network(s), PC pairing no, {words}' and fake.calls,
                  f'status says the sign-in state ({state}): {line}')
            check(JELLYFIN.access_token not in '\n'.join(h.log) and JELLYFIN.url not in '\n'.join(h.log),
                  'no token or address said')
            sign = core.setup_sign(core.summarize(h.log), pc_jellyfin=True)
            check(sign is not None and sign.tone == tone, f'Home line tone for {state}: {sign}')
    finally:
        cm.jellyfin_linker = saved

    cases = (
        ({'state': 'installed', 'setup': 'none (no Wi-Fi, PC pairing or Jellyfin on the console)'}, True,
         ('Wi-Fi and Jellyfin: not set up yet', 'update', 'Wi-Fi', 'wifi')),
        ({'state': 'installed', 'setup': '0 Wi-Fi network(s), PC pairing yes, Jellyfin linked'}, True,
         ('Wi-Fi: no network saved', 'update', 'Wi-Fi', 'wifi')),
        ({'state': 'installed', 'setup': '2 Wi-Fi network(s), PC pairing yes, Jellyfin not set'}, True,
         ('Wi-Fi: 2 networks. Jellyfin: not signed in', 'update', 'New Jellyfin sign-in', 'jellyfin')),
        ({'state': 'installed', 'setup': '2 Wi-Fi network(s), PC pairing yes, Jellyfin not set'}, False,
         ('Wi-Fi: 2 networks. Jellyfin: not signed in', 'update', 'Jellyfin server', core.JELLYFIN_PAGE)),
        ({'state': 'outdated', 'setup': '1 Wi-Fi network(s), PC pairing no, Jellyfin sign-in no longer works'}, True,
         ('Wi-Fi: 1 network. Jellyfin: sign-in no longer works', 'error', 'New Jellyfin sign-in', 'jellyfin')),
        ({'state': 'installed', 'setup': '3 Wi-Fi network(s), PC pairing no, Jellyfin linked'}, True,
         ('Wi-Fi: 3 networks. Jellyfin: signed in', 'ok', '', None)),
        ({'state': 'installed', 'setup': 'written by this install (Wi-Fi, PC pairing and Jellyfin when linked)'}, True,
         ('Wi-Fi and Jellyfin: press Check again to see them', 'info', '', None)),
    )
    for summary, pc, want in cases:
        sign = core.setup_sign(summary, pc)
        check((sign.text, sign.tone, sign.button, sign.key) == want and sign.detail, f'setup_sign {summary["setup"][:30]}')
    for summary in ({}, {'state': 'stock', 'setup': 'none'}, {'state': 'installed'}):
        check(core.setup_sign(summary) is None, f'no line for {summary}')


# ---- gap 11: the words tell the truth about erasing -------------------------------------------
def wording_checks():
    restore, remove, disable = core.BY_KEY['restore'], core.BY_KEY['remove'], core.BY_KEY['disable']
    check('Your settings stay' not in restore.help and 'erased' in restore.help and 'added on the console' in restore.help
          and "ModRetro's own settings stay" in restore.help, 'Restore original says what stays and what is erased')
    check('erases' in remove.help and 'added on the console' in remove.help, 'Remove menu says it erases the setup')
    check('Wi-Fi and Jellyfin stay' in disable.help, 'Switch menu off says the setup stays')
    check('Erases Wi-Fi and Jellyfin' in core.ROW_TEXT['remove'] and 'Erases Wi-Fi and Jellyfin' in core.ROW_TEXT['restore'],
          'the Fix-a-problem tooltips say it too')
    approve = gui.approve_prompt(restore)
    check('erased' in approve and 'Your settings stay' not in approve, 'the Approve dialog says it')
    check('erased' in gui.version_change_prompt([]) and 'its settings are kept' not in gui.version_change_prompt([]),
          'the version-change approval says it')
    source = Path(cm.__file__).read_text(encoding='utf-8')
    check("settings kept; stock seen starting" not in source and "erases the console\\'s Wi-Fi networks" in source,
          'the text menu and the manager\'s own messages say it')
    guide = (ROOT / 'docs/Recovery.md').read_text(encoding='utf-8')
    rows = {key: next((l for l in guide.splitlines() if l.startswith(f'| **{key}**')), '') for key in
            ('Remove menu', 'Restore original', 'Switch menu off')}
    check("keeping today's settings." not in guide and 'Erased' in rows['Remove menu']
          and 'Erased' in rows['Restore original'] and 'Kept' in rows['Switch menu off'],
          'docs/Recovery.md says it (Remove menu and Restore original erase the console set-up; Switch menu off keeps it)')
    install_doc = (ROOT / 'docs/Install-with-Chromatic-Manager.md').read_text(encoding='utf-8')
    check('This replaces the networks saved on the console' not in install_doc and 'merged' in install_doc,
          'the guide says Wi-Fi setup merges with the networks on the console')


# ---- the window -------------------------------------------------------------------------------
def visible_text(widget):
    out = [w.text() for w in widget.findChildren(QLabel) if w.isVisibleTo(widget)]
    out += [w.text() for w in widget.findChildren(gui.FitButton) if w.isVisibleTo(widget)]
    return '\n'.join(out)


def wifi_dialog_checks():
    nets = [(cp.Network(f'Owner Net {n}', f'password-{n}x'), cm.NOTE_NEW if n == 0 else cm.NOTE_KEPT) for n in range(5)]
    gui.Privacy.on = True
    d = gui.WifiListDialog(None, cm.network_list_prompt(nets))
    d.show()
    settle(gui.WifiListDialog.SETTLE_MS + 200)
    text = visible_text(d)
    check(not any(f'Owner Net {n}' in text for n in range(5)) and '***** *** *' in text,
          'names show as **** while private details are hidden')
    check(not any(f'password-{n}x' in text for n in range(5)), 'no password anywhere in the dialog')
    check(not d.write.isEnabled() and 'at most 4' in d.status.text() and d.sign.meaning() == 'error',
          'five networks: Write off, red sign, the plain reason')
    d.remove(4)
    check(d.write.isEnabled() and d.sign.meaning() == 'ok' and '1 removed' in d.status.text(), 'one removed: ready')
    d.move_up(2)
    check(d.order == [0, 2, 1, 3], 'Up moves one higher')
    d.put_back(4)
    check(not d.write.isEnabled(), 'Put back: too many again')
    d.remove(0)
    d._toggle_privacy()
    settle()
    check('Owner Net 1' in visible_text(d) and not gui.Privacy.on, 'the eye shows the names')
    d._toggle_privacy()
    settle()
    check(gui.Privacy.on and 'Owner Net 1' not in visible_text(d), 'and hides them again')
    check(all(b.text_fits() for b in d.findChildren(gui.FitButton) if not b.isHidden()), 'every button text fits')
    d.accept()
    check(d.answer() == '3,2,4,5', f'the answer is the new order ({d.answer()})')
    d2 = gui.WifiListDialog(None, cm.network_list_prompt(nets[:3]))
    d2.show()
    check(not d2.write.isEnabled(), 'Write ignores input for its first second')
    settle(gui.WifiListDialog.SETTLE_MS + 200)
    d2.accept()
    check(d2.answer() == 'ok', 'unchanged list: ok')
    d3 = gui.WifiListDialog(None, cm.network_list_prompt(nets[:3]))
    d3.reject()
    check(d3.answer() == '', 'Cancel: empty answer (the manager writes nothing)')
    # The real Dialogs route the list question to this dialog.
    real_exec = gui.WifiListDialog.exec
    try:
        def press(self):
            self._settle()
            self.remove(1)
            self.accept()
            return QDialog.DialogCode.Accepted
        gui.WifiListDialog.exec = press
        check(gui.Dialogs(None).ask(cm.network_list_prompt(nets[:3]), False) == '1,3', 'Dialogs.ask: the list dialog')
    finally:
        gui.WifiListDialog.exec = real_exec


def wifi_window_checks(stock, release):
    h = console_with(stock, release)
    rig = Rig(h.flash(), release, stock, confirms=['yes'], answers=['yes', 'GuiNet', 'gui-password-1', 'ok'])
    check(rig.run('wifi') == EXIT_OK, 'Wi-Fi setup from the window on a console with networks')
    rec = record(rig.h.flash())
    check(names(rec) == ['GuiNet', 'Cafe On Console', 'Phone Hotspot'] and rec.networks[1].psk == 'console-pass-1',
          'the window keeps the console\'s networks too')
    check(any(p.startswith(cm.WIFI_LIST_PROMPT) for p in rig.dialogs.asked), 'the list question reached the window')
    log = rig.log_text()
    check(log and not any(s in log for s in ('GuiNet', 'Cafe On Console', 'Phone Hotspot', 'gui-password-1',
                                             'console-pass-1')), 'the log file holds no name or password')
    api = json.dumps(rig.window.service.result())
    check('Cafe On Console' not in api and 'GuiNet' not in api, 'nor does the API\'s result')


def home_line_checks(stock, release):
    rig = Rig(stock, release)
    w = rig.window
    w.show()
    w.consoles = 1
    started = []
    w.start = lambda key, confirmed=False: started.append(key) or False
    opened = []
    w.open_jellyfin_page = lambda: opened.append(True)
    base = {'state': 'installed', 'launcher': release.version, 'release': release.version, 'boots': 'x'}
    for setup, text, tone, label in (
            ('3 Wi-Fi network(s), PC pairing yes, Jellyfin linked', 'Jellyfin: signed in', 'ok', None),
            ('2 Wi-Fi network(s), PC pairing yes, Jellyfin not set', 'Jellyfin: not signed in', 'warn', 'Jellyfin server'),
            ('2 Wi-Fi network(s), PC pairing yes, Jellyfin sign-in no longer works', 'sign-in no longer works', 'error',
             'Jellyfin server'),
            ('none (no Wi-Fi, PC pairing or Jellyfin on the console)', 'not set up yet', 'warn', 'Wi-Fi')):
        w.service.summary = dict(base, setup=setup)
        w._stale = False
        w.refresh_guide()
        check(w.status_card.head.text() == 'Up to date' and not w.setup_row.isHidden() and text in w.setup_text.text()
              and w.setup_mark.meaning() == tone, f'Home: "Up to date" plus the second line: {w.setup_text.text()}')
        check((w.setup_button.isHidden() if label is None else w.setup_button.text() == label), f'its button ({label})')
        if label:
            check(w.setup_button.text_fits(), 'the button text fits')
            w.setup_button.click()
    check(started == ['wifi'] and len(opened) == 2, 'its buttons start Wi-Fi or open the Jellyfin page')
    saved = cm.jellyfin_settings_available
    try:
        cm.jellyfin_settings_available = lambda: True
        w.service.summary = dict(base, setup='2 Wi-Fi network(s), PC pairing yes, Jellyfin sign-in no longer works')
        w.refresh_guide()
        check(w.setup_button.text() == 'New Jellyfin sign-in' and w.setup_button.kind() == 'error', 'with PC settings: '
              'New Jellyfin sign-in, red')
        w.setup_button.click()
        check(started[-1] == 'jellyfin', 'it starts New Jellyfin sign-in (with its own Approve)')
        state = w.service.state()['console_setup']
        check(state['tone'] == 'error' and state['action'] == 'jellyfin', 'the API reports the same line')
    finally:
        cm.jellyfin_settings_available = saved
    w.consoles = 0
    w.refresh_guide()
    check(w.setup_row.isHidden(), 'no line while no console is plugged in')
    w.close()

    # End to end: a status of a console whose sign-in was removed in the Dashboard turns the line red.
    h = console_with(stock, release, jellyfin=JELLYFIN)
    saved = cm.jellyfin_linker
    try:
        cm.jellyfin_linker = lambda server_url=None: FakeLinker('refused')
        rig = Rig(h.flash(), release, stock)
        rig.window.consoles = 1
        check(rig.run('status') == EXIT_OK, 'status from the window')
        rig.window.show_tab('console')
        check('sign-in no longer works' in rig.window.setup_text.text() and rig.window.setup_mark.meaning() == 'error'
              and rig.window.status_card.head.text() == 'Up to date', 'a revoked sign-in shows red under Up to date')
    finally:
        cm.jellyfin_linker = saved


def wait_dialog(d, seconds=10):
    end = 0
    while d.busy and end < seconds * 20:
        settle(50)
        end += 1
    check(not d.busy, 'the page finished its work')


def jellyfin_page_checks(stock, release):
    gui.Privacy.on = True
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / 'jellyfin-connection.txt'
        tested = []

        def tester(connection):
            tested.append(connection)
            ok = connection.password == 'right-password'
            return {'reachable': True, 'signed_in': ok, 'quick_connect': True}
        finder = lambda: [{'name': 'Living Room', 'address': 'http://192.0.2.10:8096'}]
        d = gui.JellyfinDialog(None, path=path, tester=tester, finder=finder)
        d.show()
        settle()
        check(d.server.text() == '' and d.password.echoMode() == QLineEdit.EchoMode.Password
              and d.server.echoMode() == QLineEdit.EchoMode.PasswordEchoOnEdit, 'empty page; private fields masked')
        d.find_servers()
        wait_dialog(d)
        picks = [b for b in d.findChildren(gui.FitButton) if hasattr(b, 'server_name')]
        check(len(picks) == 1 and 'Living Room' not in picks[0].text(), 'Find servers: one found, its name masked')
        picks[0].click()
        check(d.server.text() == 'http://192.0.2.10:8096', 'pressing it fills the address')
        d.user.setText('owner')
        d.password.setText('wrong-password')
        d.save_now()
        wait_dialog(d)
        check(not path.exists() and d.save.text() == 'Save anyway' and 'Sign-in works: no' in visible_text(d)
              and 'Not saved' in d.note.text(), 'a wrong password: not saved, red, what to do')
        d.password.setText('right-password')
        d._edited()
        d.save_now()
        wait_dialog(d)
        check(path.exists() and cc.load_connection(path) == cc.Connection('http://192.0.2.10:8096', 'owner',
                                                                          'right-password'),
              'saved through connection_config, read back exactly')
        check(not d.relink.isHidden() and d.password.text() == '' and 'Saved' in d.note.text(), 'then New Jellyfin '
              'sign-in is offered')
        everything = visible_text(d) + ''.join(w.toolTip() for w in d.findChildren(QLabel))
        check('right-password' not in everything and 'wrong-password' not in everything, 'no password on screen')
        d._relink()
        check(d.choice == 'relink', 'New Jellyfin sign-in chosen')
        # Opened again: the server and user are there, the password is kept when left empty.
        d2 = gui.JellyfinDialog(None, path=path, tester=tester, finder=finder)
        check(d2.server.text() == 'http://192.0.2.10:8096' and d2.user.text() == 'owner' and d2.password.text() == ''
              and 'leave empty' in d2.password.placeholderText(), 'reopened: filled in, password field empty')
        d2.server.setText('192.0.2.11')
        d2.save_now()
        wait_dialog(d2)
        check(tested[-1] == cc.Connection('http://192.0.2.11:8096', 'owner', 'right-password')
              and cc.load_connection(path).server_url == 'http://192.0.2.11:8096', 'a new address, the saved password kept')
        d3 = gui.JellyfinDialog(None, path=path, tester=lambda c: {'reachable': False}, finder=lambda: [])
        d3.find_servers()
        wait_dialog(d3)
        check('No server answered' in d3.note.text(), 'nothing found: what to do')
        d3.test_now()
        wait_dialog(d3)
        check('Server found: no' in visible_text(d3) and 'did not answer' in d3.note.text(), 'unreachable: red, what to do')
        d3.server.setText('http://user:pw@host')
        d3.test_now()
        check('not a server address' in d3.note.text() or 'server address' in d3.note.text(), 'a bad address refused')
        try:
            cc.save_connection(cc.Connection('http://h:8096', 'owner', 'two\nlines'), Path(folder) / 'x.txt')
            check(False, 'a line break must be refused')
        except ValueError:
            check(not (Path(folder) / 'x.txt').exists(), 'a line break: refused, nothing written')
        check(all(b.text_fits() for b in d.findChildren(gui.FitButton) if not b.isHidden()), 'every button text fits')

        # The window opens the page from Settings and starts New Jellyfin sign-in after it.
        rig = Rig(stock, release)
        w = rig.window
        w.jellyfin_options = {'path': path, 'tester': tester, 'finder': finder}
        started = []
        w.start = lambda key, confirmed=False: started.append(key) or False
        real_exec = gui.JellyfinDialog.exec
        try:
            def press(self):
                self._relink()
                return QDialog.DialogCode.Accepted
            gui.JellyfinDialog.exec = press
            w.show_tab('settings')
            w.jellyfin_open.click()
        finally:
            gui.JellyfinDialog.exec = real_exec
        check(started == ['jellyfin'], 'Settings > Jellyfin server, then New Jellyfin sign-in')
        check(not w.jellyfin_open.isHidden() and w.jellyfin_open.text_fits(), 'the Settings button is there and fits')

    # check_pc_connection: yes/no only, the linker's own device, nothing signed out.
    import chromatic_jellyfin_link as jl
    with tempfile.TemporaryDirectory() as folder:
        state = Path(folder) / 'linker.json'
        server = FakeServer()
        right = cc.Connection('http://192.0.2.10:8096', 'owner', 'right-password')
        check(jl.check_pc_connection(right, server, state) == {'reachable': True, 'signed_in': True, 'quick_connect': True},
              'the right password: yes, yes, yes')
        check(jl.check_pc_connection(cc.Connection(right.server_url, 'owner', 'bad'), FakeServer(), state)['signed_in']
              is False, 'a wrong password: sign-in no')
        check(jl.check_pc_connection(right, FakeServer(up=False), state) == {'reachable': False, 'signed_in': False,
                                                                            'quick_connect': False}, 'server down: no')
        check(jl.check_pc_connection(right, FakeServer(quick_connect=False), state)['quick_connect'] is False,
              'Quick Connect off: said')
        paths = {p for _, p, _ in server.calls}
        check(paths <= {'/System/Info/Public', '/Users/AuthenticateByName', '/QuickConnect/Enabled'}
              and all(jl.APPROVER_PREFIX in h.get('Authorization', '') for m, p, h in server.calls
                      if p == '/Users/AuthenticateByName'), 'only the linker\'s own device signs in; nothing signed out')
        kept = json.loads(state.read_text())
        check(kept.get('user') and 'owner' not in json.dumps(kept), 'the kept token is marked by user, never the name')
        other = jl.Linker(cc.Connection(right.server_url, 'someone-else', 'right-password'), FakeServer(), state)
        other._owner_session()
        check(any(p == '/Users/AuthenticateByName' for _, p, _ in other.opener.calls),
              'a different user signs in again instead of reusing the kept token')


def update_sound_checks(stock, release, installed):
    lines = ['Done: official update applied and the start-up menu is back on.']
    stock_line = 'Console chip (FPGA): version 18.14, as the start-up menu reports it.'
    for before, extra, want in (
            ({'fpga': '18.40 debug'}, [stock_line], ('The update removed Jellyfin sound. Set it up again?', True)),
            ({'fpga': 'sound'}, [], ('The update removed Jellyfin sound. Set it up again?', True)),
            ({}, [stock_line], (core.sound_after_update(lines + [stock_line])[0], True)),
            ({}, [], None),
            ({'fpga': '18.40 debug'}, ['Console chip (FPGA): version 19.2, as the start-up menu reports it.'], None)):
        got = core.sound_after_update(lines + extra, before)
        check(got == want if want is not None and want[0] else (got is None or got[1] is False),
              f'sound after update: before {before}, said {extra}: {got}')
    check(core.sound_after_update(['Stopped at step 1. Nothing else was done.'], {'fpga': 'sound'}) is None,
          'only after an update that finished')

    updated = with_app(stock, version='9.9.9', idf='v5.4.1')

    def person(prompt):
        rig.h.console.flash = official_update(rig.h.console.flash, stock, updated)
        rig.h.console.fpga = '18.14 debug=0'  # ModRetro's updater wrote its own console chip image
        return 'done'
    rig = Rig(installed, release, stock, confirms=['yes'], answers=[person])
    w = rig.window
    w.sound_image, w._sound_image_text = (lambda: SOUND), ...
    w.service.summary = {'state': 'installed', 'launcher': release.version, 'release': release.version,
                         'fpga': '18.40 debug'}
    check(rig.run('update') == EXIT_OK and rig.state() == 'installed', 'guided update')
    check(any(l.startswith('Console chip (FPGA): version 18.14') for l in w.run_lines), 'the update says the chip version')
    check(not w.result_sound.isHidden() and 'removed Jellyfin sound' in w.result_todo.text()
          and w.result_ok.text() == 'Not now' and w.result_sound.text_fits(), 'last step: set up Jellyfin sound again?')
    w.result_sound.click()
    check(w.sound.active and w.stack.currentWidget() is w.pages['guide'], 'its button opens the guided sound setup')
    w.guide_choose('stop')
    w.leave_guide()
    # Without a sound image before and no chip line: nothing offered.
    rig = Rig(installed, release, stock, confirms=['yes'],
              answers=[lambda p: (setattr(rig.h.console, 'flash', official_update(rig.h.console.flash, stock, updated)),
                                  'done')[1]])
    rig.window.sound_image, rig.window._sound_image_text = (lambda: SOUND), ...
    check(rig.run('update') == EXIT_OK and rig.window.result_sound.isHidden() and rig.window.result_ok.text() == 'OK',
          'no sound before and none reported: no offer')


def main():
    app = QApplication.instance() or QApplication([])
    theme.apply(app)
    stock = stock_dump(allow_synthetic=True)
    release = cm.Release.load()
    wifi_merge_checks(stock, release)
    status_checks(stock, release)
    wording_checks()
    wifi_dialog_checks()
    wifi_window_checks(stock, release)
    home_line_checks(stock, release)
    jellyfin_page_checks(stock, release)
    h = Harness(stock, release)
    h.manager().install()
    update_sound_checks(stock, release, h.flash())
    print('PASS: Chromatic Manager audit fixes: Wi-Fi setup merges with the console\'s networks (kept with their '
          'passwords, same name updates, order and removals chosen, at most 4 with a plain message, cancel writes '
          'nothing, text menu masks names), Home\'s Wi-Fi and Jellyfin line with green/yellow/red signs (the console\'s '
          'own sign-in tested at status: works, refused, unreachable), truthful erase wording (help, Approve, text menu, '
          'docs, site), the Jellyfin page (Find servers, hidden password, yes/no test, private save, New Jellyfin '
          'sign-in), the sound setup offered after the guided official update. Simulated console, no hardware.')


if __name__ == '__main__':
    main()

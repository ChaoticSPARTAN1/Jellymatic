# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Chromatic Manager local API and MCP server against the simulated console of
test_chromatic_manager.py. Checks the write gate (a model's request writes nothing until
the person types the confirmation in the window), redaction, the device and window locks,
the 127.0.0.1 HTTP endpoint and the MCP stdio protocol. No device access, no real Wi-Fi
file, no real gateway identity; everything is written under temporary folders.
Prints only pass/fail.

Run: private/chromatic-manager-gui/venv/Scripts/python.exe -B scripts/test_chromatic_manager_api.py
"""
import http.client
import io
import json
import os
from pathlib import Path
from user_paths import TOOLS  # noqa: E402  (per-user folder, see user_paths.py)
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
import chromatic_manager_api as api
import chromatic_manager_core as core
import chromatic_manager_mcp as mcp
import chromatic_theme as theme
from chromatic_lock import Busy, DeviceLock
from compose_chromatic_image import stock_dump
from test_chromatic_manager import Device, IDENTITY, Harness, check, with_app
from test_chromatic_manager_gui import USAGE, Rig, Scripted, factory, slow_factory
from test_chromatic_manager import crash_mid_install

ROOT = Path(__file__).resolve().parents[1]
CLAUDE = 'Claude Code via MCP'
WHO = core.api_requester(CLAUDE)  # how the API names that caller


class GateDialogs(Scripted):
    """Scripted approvals and answers that also record which prompts named a requester."""

    def __init__(self, confirms=(), answers=(), ai=(), keep=()):
        super().__init__(confirms, answers, ai, keep)
        self.requests = []

    def confirm(self, action, requested_by=None, reason=''):
        if requested_by:
            self.requests.append((action.key, requested_by, reason))
        return super().confirm(action)


def pump(condition, seconds=60):
    """Let the window's event loop run until `condition()` holds."""
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        loop = QEventLoop()
        QTimer.singleShot(20, loop.quit)
        loop.exec()
    return condition()


def request_state(service):
    return service.request and service.request['state']


def raw_http(port, method, path, headers, body=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    status = response.status
    response.read()
    connection.close()
    return status


def headless(h, folder, host='headless'):
    return core.ManagerService(cm, factory(h), lambda: DeviceLock(folder / 'device.lock'), folder / 'logs', ROOT,
                               host=host)


class SlowWrite(Device):
    """The simulated console, with writes that take a while (to press Stop mid-write)."""

    def write(self, offset, data):
        time.sleep(0.4)
        super().write(offset, data)


def slow_write_factory(h):
    def make(say):
        manager = h.manager()
        manager.say = lambda message: (h.log.append(message), say(message))
        manager.factory = lambda: SlowWrite(h.console)
        return manager
    return make


def served(rig, folder, name):
    server = api.LocalApiServer(rig.window.service, folder / f'{name}.json').start()
    return server, api.RemoteApp.find(folder / f'{name}.json')


def link_update_api_checks(stock, release, folder):
    """The headless entry of the one-step Pocket link update: a program can ask for
    it through chromatic_update_pocket_link; the window opens that screen and shows the Approve
    prompt naming the program; only the owner's click on Approve starts it, never the AI
    permission, and nothing is read from or written to the console chip before that click. That
    click starts the try from RAM and writes nothing: the job then asks the owner once more, with
    the new image running, and only that second Approve (which no program can give) lets it write."""
    import chromatic_fpga as cf
    import test_chromatic_fpga as tf
    from test_chromatic_manager_gui import link_window, press_guide, quiet
    key = core.LINK_UPDATE_KEY
    tool = next(t for t in api.TOOLS if t['name'] == 'chromatic_update_pocket_link')
    general = next(t for t in api.TOOLS if t['name'] == 'chromatic_request_action')
    check(set(tool['inputSchema']['properties']) == {'reason'} and tool['inputSchema']['additionalProperties'] is False
          and 'required' not in tool['inputSchema'] and tool['annotations']['destructiveHint'] is True
          and 'ONLY when the owner presses Approve' in tool['description']
          and 'AI control never starts it' in tool['description'] and 'never answers for the owner' in tool['description']
          and 'Only after that second Approve is it written' in tool['description']
          and key not in general['inputSchema']['properties']['action']['enum'],
          'the one-step tool takes a reason and nothing else, and says that only Approve starts it and only a '
          'second Approve, with the new image running, writes')

    # No window: refused; no argument or other entry starts it.
    h = Harness(stock, release)
    lone = headless(h, folder / 'link-lone')
    answer = api.call_tool(lone, 'chromatic_update_pocket_link', {'reason': 'try the new bridge'}, CLAUDE)
    check(answer['state'] == 'refused' and 'window' in answer['why'] and h.flash() == stock,
          'headless: the one-step update is refused (no window, no Approve button)')
    for name, arguments in (('chromatic_update_pocket_link', {'confirm': 'yes'}),
                            ('chromatic_update_pocket_link', {'approved': True}),
                            ('chromatic_update_pocket_link', {'action': key}),
                            ('chromatic_request_action', {'action': key}),
                            ('chromatic_request_action', {'action': key, 'reason': 'x'})):
        try:
            api.call_tool(lone, name, arguments, CLAUDE)
            check(False, f'{name} {sorted(arguments)}: no argument may approve or start the one-step update')
        except api.ToolError:
            pass
    answer = lone.request_action(key, CLAUDE)
    check(answer['state'] == 'refused' and core.OWNER_APPROVE_WHY in answer['why'],
          'the general request entry refuses the one-step job and names its own entry')

    # With the window, and AI control allowed: still only the owner's Approve click.
    sound = cf.full_flash_image(tf.MEDIA_BITSTREAM)
    fr = tf.Rig(flash=sound)
    rig = Rig(stock, release)
    at_keep = []

    def keep(prompt):  # the owner at the job's own Approve, the new image running: what a program sees then
        at_keep.append({'progress': remote.call('chromatic_get_progress', {}, CLAUDE),
                        'state': remote.call('chromatic_get_state', {}, CLAUDE),
                        'again': remote.call('chromatic_update_pocket_link', {}, CLAUDE),
                        'ram': fr.chip.sram is not None, 'writes': fr.chip.writes, 'flash': fr.flash()})
        return cf.KEEP_ANSWER
    dialogs = rig.dialogs = GateDialogs(confirms=['no', 'yes'], answers=[keep])
    w = link_window(rig, fr)
    w.show()
    w.service.summary = {'state': 'installed', 'launcher': release.version, 'release': release.version,
                         'fpga': '18.40 debug'}
    w.refresh_guide()
    check(w.service.set_ai_control(True) is not False and w.service.ai_decision() == 'allow', 'AI control allowed for this run')
    server = api.LocalApiServer(w.service, folder / 'link.json').start()
    remote = api.RemoteApp.find(folder / 'link.json')
    try:
        check(remote.call('chromatic_get_progress', {}, CLAUDE)['owner_action_needed'] is None
              and remote.call('chromatic_get_state', {}, CLAUDE)['guided_setup'] is None,
              'nothing asked of the owner, no guided screen open')
        asked = remote.call('chromatic_update_pocket_link', {'reason': 'try the new bridge'}, CLAUDE)
        check(asked['state'] == 'waiting' and asked['action'] == key
              and 'AI control never starts this' in asked['confirmation'] and 'Approve' in asked['confirmation'],
              'the request waits for the owner; the answer says AI control never starts it')
        check(w.service.resolve_request(asked['id'], True, by_grant=True) is False
              and request_state(w.service) == 'waiting' and not w.service.busy(),
              'the AI permission cannot start it, even from the window thread')
        check(pump(lambda: request_state(w.service) == 'declined', 30)
              and dialogs.requests == [(key, WHO, 'try the new bridge')] and dialogs.ai_asked == []
              and dialogs.keep_asked == [] and fr.chip.reads == 0 and fr.chip.sram_loads == 0 and fr.flash() == sound
              and w.sound is w.link_update and w.sound.screen == 'start'
              and 'Asked for by' in w.guide_card.sub.text(),
              'the window opened the one-step screen and showed Approve naming the program; Cancel: nothing touched')
        state = remote.call('chromatic_get_state', {}, CLAUDE)
        check(state['guided_setup']['screen'] == 'start' and state['guided_setup']['buttons'][0] == 'Update now'
              and state['request']['state'] == 'declined' and 'did not approve' in state['request']['outcome']
              and CLAUDE not in json.dumps(state['guided_setup']),
              'state: the request declined, and the screen the owner is left on')
        asked = remote.call('chromatic_update_pocket_link', {}, CLAUDE)
        check(asked['state'] == 'waiting', 'asked again (the one-step screen is still open)')
        check(pump(lambda: request_state(w.service) == 'finished', 240) and quiet(w, 240)
              and fr.flash() == fr.expected_link() and fr.chip.sram_loads == 1 and fr.chip.writes == 1
              and w.sound.screen == 'ready' and dialogs.confirms == [] and dialogs.ai_asked == []
              and dialogs.asked == [cf.LINK_KEEP_PROMPT + cf.keep_text('media')] and dialogs.answers == [],
              'Approve: the job ran in the window (tried, checked, approved while it ran, written, read back), '
              'then Ready')
        check(len(at_keep) == 1 and at_keep[0]['ram'] and at_keep[0]['writes'] == 0 and at_keep[0]['flash'] == sound,
              'the second Approve was asked with the new image running from RAM and the flash not yet written')
        needed = at_keep[0]['progress']['owner_action_needed']
        check(needed and needed['state'] == 'waiting' and needed['kind'] == 'action' and needed['title'] == cf.KEEP_TITLE
              and needed['instruction'] == cf.keep_text('media') and needed['seconds_allowed'] is None
              and at_keep[0]['state']['busy'] and at_keep[0]['again']['state'] == 'refused'
              and at_keep[0]['writes'] == 0,
              'meanwhile the API says what the owner is asked (look, then Approve), and a program asking again is '
              'refused: it cannot answer for the owner')
        result = remote.call('chromatic_get_last_result', {}, CLAUDE)
        check(result['confirmed_by'] == core.APPROVED_IN_WINDOW and result['requested_by'] == WHO
              and result['headline'] == 'Pocket link updated' and result['exit_code'] == cf.EXIT_OK,
              'result: requested by the program, approved in the window by the owner')
        state = remote.call('chromatic_get_state', {}, CLAUDE)
        check(state['guided_setup']['screen'] == 'ready' and state['guided_setup']['starts_next'] is None
              and remote.call('chromatic_get_progress', {}, CLAUDE)['owner_action_needed'] is None,
              'state: Ready, nothing more starts by itself, nothing asked of the owner')
        log = (rig.logs / 'api-requests.log').read_text(encoding='utf-8')
        check(f'{key} requested by {WHO}: declined (The owner did not approve it.)' in log
              and f'{key} requested by {WHO}: confirmed by the owner, started' in log
              and 'AI permission' not in log,
              'request log: declined, then confirmed by the owner, never started under the AI permission')
        run = rig.log_text()
        check(f'[owner] asked on screen: {cf.KEEP_TITLE}' in run and f'[owner] done: {cf.KEEP_TITLE}' in run
              and core.SAID_KEPT in run, 'the job log: the Approve with the image running, asked and given')
        # While the owner is in another guided setup, the request is not started.
        check(press_guide(w, 0) is None and not w.link_update.active, 'OK closes the one-step screen')
        w.open_link_guide()
        check(w.sound is w.link_guide and w.sound.active, 'the owner opens the question-based guide')
        reads = fr.chip.reads
        asked = remote.call('chromatic_update_pocket_link', {}, CLAUDE)
        check(asked['state'] == 'waiting' and pump(lambda: request_state(w.service) == 'declined', 30)
              and 'guided' in w.service.request['outcome'] and fr.chip.reads == reads
              and len(dialogs.requests) == 2 and w.sound is w.link_guide,
              'another guided setup is open: declined without a prompt, the guide left alone')
    finally:
        server.stop()
        w.close()


def ai_control_checks(stock, release, folder):
    """AI control by owner opt-in."""
    settings = folder / 'ai-settings.json'

    # No grant, no window: refused, even with "always allow" remembered.
    core.AppSettings(settings).set('ai_control', 'allow')
    h = Harness(stock, release)
    lone = core.ManagerService(cm, factory(h), lambda: DeviceLock(folder / 'lone.lock'), folder / 'lone', ROOT,
                               settings_path=settings, hardware_gate=lambda: None)
    answer = api.call_tool(lone, 'chromatic_request_action', {'action': 'install'}, CLAUDE)
    check(answer['state'] == 'refused' and lone.ai_decision() == 'deny' and h.flash() == stock,
          'headless: a remembered Allow grants nothing (no window, no one watching)')
    check(api.call_tool(lone, 'chromatic_set_log_panel', {'open': True}, CLAUDE)['state'] == 'refused',
          'headless: no log panel to open')
    core.AppSettings(settings).set('ai_control', None)
    for arguments in ({'action': 'install', 'allow_ai': True}, {'action': 'install', 'grant': 'always'}):
        try:
            api.call_tool(lone, 'chromatic_request_action', arguments, CLAUDE)
            check(False, 'a program must not be able to grant itself control')
        except api.ToolError:
            pass

    # The first request of a run shows the pop-up. Allow + Always remember: it runs at once,
    # with every check, and the choice is kept in the private settings file.
    rig = Rig(stock, release, ai=[(True, True)])
    rig.window.service.settings = core.AppSettings(settings)
    dialogs = rig.window.dialogs = GateDialogs(ai=[(True, True)])
    server, remote = served(rig, folder, 'ai1')
    try:
        check(remote.call('chromatic_get_state', {}, CLAUDE)['ai_control']['this_run'] == 'not asked yet',
              'state: AI not asked yet')
        worker = threading.Thread(target=lambda: check(rig.window.service.set_ai_control(True, True) is False,
                                                       'only the window thread can allow AI control'))
        worker.start()
        worker.join()
        asked = remote.call('chromatic_request_action', {'action': 'install', 'reason': 'update the menu'}, CLAUDE)
        check(asked['state'] == 'waiting', 'request waits for the window')
        check(pump(lambda: request_state(rig.window.service) == 'finished', 90), 'allowed request ran')
        check(dialogs.ai_asked == [(WHO, 'install')] and dialogs.requests == [] and rig.state() == 'installed'
              and not rig.h.console.in_stub, 'Allow: installed without a per-change Approve')
        check(dialogs.image == core.short_release(release)
              and rig.window.service.ai_release == release.fingerprint(),
              'the pop-up named the exact image (version and SHA-256) the AI may install')
        check(USAGE in rig.log_text() and USAGE in rig.window.work_usage.text()
              and any(USAGE in line for line in remote.call('chromatic_read_log', {'lines': 400}, CLAUDE)['lines']),
              'the AI\'s usage reading is shown on the working screen and in the job log')
        result = remote.call('chromatic_get_last_result', {}, CLAUDE)
        check(result['confirmed_by'] == core.AI_GRANT and result['requested_by'] == WHO,
              'result: started under the owner\'s AI permission')
        check(json.loads(settings.read_text(encoding='utf-8')).get('ai_control') == 'allow'
              and settings.is_relative_to(folder), 'Always remember: kept in the (temporary) private settings file')
        log = (rig.logs / 'api-requests.log').read_text(encoding='utf-8')
        check('AI control: the owner chose allow (remembered)' in log
              and f'install requested by {WHO}: allowed by the owner\'s AI permission, started' in log,
              'request log: the choice and the grant')
        check(not rig.window.ai_chip.isHidden() and not rig.window.ai_stop.isHidden(),
              'the window shows that the AI may use the app, with a Stop button')
        # The AI can show the owner the log, and read it (redacted).
        check(remote.call('chromatic_set_log_panel', {'open': True}, CLAUDE) == {'log_panel_open': True}
              and pump(lambda: not rig.window.log.isHidden(), 5), 'the AI expands the log panel')
        read = remote.call('chromatic_read_log', {'lines': 400}, CLAUDE)
        check(read['log_panel_open'] is True and any('installed' in line for line in read['lines'])
              and IDENTITY['chip'] not in json.dumps(read), 'the AI reads the log (redacted)')
        check(remote.call('chromatic_set_log_panel', {'open': False}, CLAUDE)['log_panel_open'] is False
              and pump(lambda: rig.window.log.isHidden(), 5) and rig.window.log_toggle.text() == 'Show log',
              'and collapses it again')
        try:
            remote.call('chromatic_set_log_panel', {'open': 'yes'}, CLAUDE)
            check(False, 'open must be true or false')
        except api.ToolError:
            pass
    finally:
        server.stop()

    # A new run: a remembered Allow is only a preference. The owner confirms it with Keep
    # (once per run); the hardware usage rule still refuses a write.
    rig = Rig(stock, release, gate=lambda: 'Recovery reserve reached; do not start a hardware experiment.')
    rig.window.service.settings = core.AppSettings(settings)
    dialogs = rig.window.dialogs = GateDialogs(keep=[True])
    server, remote = served(rig, folder, 'ai2')
    try:
        state = remote.call('chromatic_get_state', {}, CLAUDE)['ai_control']
        check(state['this_run'] == 'not asked yet' and state['remembered'] == 'allow',
              'a new run: Allow remembered, but nothing granted yet')
        remote.call('chromatic_request_action', {'action': 'install'}, CLAUDE)
        check(pump(lambda: request_state(rig.window.service) == 'declined', 10)
              and 'Recovery reserve' in rig.window.service.request['outcome'] and rig.h.flash() == stock
              and dialogs.ai_asked == [] and len(dialogs.keep_asked) == 1 and dialogs.requests == [],
              'Keep confirmed the grant; the usage rule refused the write, nothing written')
        remote.call('chromatic_start_status', {}, CLAUDE)
        check(pump(lambda: request_state(rig.window.service) == 'finished', 60) and rig.h.flash() == stock
              and len(dialogs.keep_asked) == 1, 'the read-only status runs under the grant, no second prompt')
        # Clear the remembered choice on the Settings tab: the next request asks again.
        rig.window.show_tab('settings')
        check('allow (you confirm it once' in rig.window.ai_remembered.text() and rig.window.ai_forget.isEnabled(),
              'Settings shows the remembered choice')
        rig.window.ai_forget.click()
        check('ai_control' not in json.loads(settings.read_text(encoding='utf-8'))
              and rig.window.ai_remembered.text().endswith('it asks you the first time)')
              and not rig.window.ai_forget.isEnabled(), 'Forget my choice clears it')
        dialogs.ai.append((False, False))
        dialogs.confirms.append('no')
        remote.call('chromatic_request_action', {'action': 'remove'}, CLAUDE)
        check(pump(lambda: request_state(rig.window.service) == 'declined', 10) and len(dialogs.ai_asked) == 1
              and dialogs.requests[-1][0] == 'remove', "asked again; Don't allow: the Approve dialog decides")
    finally:
        server.stop()

    # A forged settings file grants nothing: without the owner's Keep, it goes to Approve.
    forged = folder / 'forged.json'
    forged.write_text(json.dumps({'ai_control': 'allow'}), encoding='utf-8')
    rig = Rig(stock, release)
    rig.window.service.settings = core.AppSettings(forged)
    dialogs = rig.window.dialogs = GateDialogs(confirms=['no'])
    server, remote = served(rig, folder, 'ai-forged')
    try:
        remote.call('chromatic_request_action', {'action': 'install'}, CLAUDE)
        check(pump(lambda: request_state(rig.window.service) == 'declined', 10) and len(dialogs.keep_asked) == 1
              and dialogs.requests and rig.h.flash() == stock and rig.window.service.ai_decision() == 'deny',
              'a file saying allow: the owner is asked; Turn off, then Approve declined: nothing written')
    finally:
        server.stop()
    denied = folder / 'denied.json'
    denied.write_text(json.dumps({'ai_control': 'deny'}), encoding='utf-8')
    rig = Rig(stock, release)
    rig.window.service.settings = core.AppSettings(denied)
    dialogs = rig.window.dialogs = GateDialogs(confirms=['no'])
    server, remote = served(rig, folder, 'ai-denied')
    try:
        remote.call('chromatic_request_action', {'action': 'install'}, CLAUDE)
        check(pump(lambda: request_state(rig.window.service) == 'declined', 10) and not dialogs.keep_asked
              and not dialogs.ai_asked and dialogs.requests, "a remembered Don't allow skips the prompt: Approve")
    finally:
        server.stop()

    # The install image is pinned: a different image needs the owner's click; one that changes
    # between that click and the job is refused before anything is written.
    import dataclasses  # a different image (never written: every path below refuses it first)
    other = dataclasses.replace(release, version='9.9.9', launcher=bytes(release.launcher[:-1]) + bytes([
        release.launcher[-1] ^ 1]))
    rig = Rig(stock, release)
    rig.window.service.settings = core.AppSettings(folder / 'pin.json')
    dialogs = rig.window.dialogs = GateDialogs(ai=[(True, False)], keep=[False], confirms=['no'])
    server, remote = served(rig, folder, 'ai-pin')
    try:
        remote.call('chromatic_start_status', {}, CLAUDE)
        check(pump(lambda: request_state(rig.window.service) == 'finished', 60), 'grant given (status)')
        rig.window.service.release_peek = lambda: other
        remote.call('chromatic_request_action', {'action': 'install'}, CLAUDE)
        check(pump(lambda: request_state(rig.window.service) == 'declined', 10)
              and dialogs.keep_asked[-1][3] is True and dialogs.keep_asked[-1][2] == core.short_release(other)
              and rig.h.flash() == stock, 'a changed image: asked again (Turn off), then Approve declined')
        rig.window.forget_ai()  # after Turn off the AI is off for this run; start over with a fresh Allow
        dialogs.ai.append((True, False))
        calls = []

        def swapped():  # the window sees the approved image; the job then finds another
            calls.append(1)
            return release if len(calls) == 1 else other
        rig.window.service.release_peek = swapped
        remote.call('chromatic_request_action', {'action': 'install'}, CLAUDE)
        check(pump(lambda: request_state(rig.window.service) == 'finished', 30)
              and rig.window.service.result()['exit_code'] == cm.EXIT_REFUSED and rig.h.flash() == stock
              and any('not the one you approved' in m for m in rig.window.service.result()['messages']),
              'image swapped after the click: refused inside the job, nothing written')
        rig.window.service.release_peek = lambda: release
        rig.window.service.usage_note = lambda: None
        remote.call('chromatic_request_action', {'action': 'install'}, CLAUDE)
        check(pump(lambda: request_state(rig.window.service) == 'declined', 10)
              and 'No usage reading' in rig.window.service.request['outcome'] and rig.h.flash() == stock,
              'no usage reading to show: refused')
    finally:
        server.stop()
    check(core.usage_note(folder / 'nowhere') is None, 'usage note: none without a reading')

    # The real hardware rule: a fresh usage reading under the reserve, then the backup records.
    root = folder / 'gate-root'
    snapshot = root / core.USAGE_SNAPSHOT
    check('found no fresh usage reading' in core.hardware_rule(root), 'no usage reading: refused')
    snapshot.parent.mkdir(parents=True)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    for used, want in ((90, 'Recovery reserve reached'), (89, 'found no fresh usage reading or backup record')):
        snapshot.write_text(json.dumps({'observed_at': now, 'used_percent_by_window': [used], 'reporter': 'claude',
                                        'ordinary_usage_allowed': True}), encoding='utf-8')
        check(want in core.hardware_rule(root), f'usage {used}% used (claude): {want}')
    snapshot.write_text(json.dumps({'observed_at': '2026-01-01T00:00:00+00:00', 'used_percent_by_window': [10],
                                    'reporter': 'claude', 'ordinary_usage_allowed': True}), encoding='utf-8')
    check('stale' in core.hardware_rule(root), 'stale usage reading: refused')

    # Stop before anything is written: a never-seen stock firmware is read in full first.
    newer = with_app(stock, version='9.9.9', idf='v5.4.1')
    rig = Rig(newer, release, stock)
    rig.window.manager_factory = slow_factory(rig.h)
    rig.window.service.settings = core.AppSettings(folder / 'stop-settings.json')
    dialogs = rig.window.dialogs = GateDialogs(ai=[(True, True)])
    server, remote = served(rig, folder, 'ai3')
    try:
        remote.call('chromatic_request_action', {'action': 'install'}, CLAUDE)
        check(pump(lambda: rig.window.tracker.step is not None and rig.window.tracker.step['kind'] == 'read', 30),
              'the AI\'s install is reading the console')
        check(not rig.window.work_stop.isHidden() and CLAUDE in rig.window.work_title.text(),
              'the window shows what the AI is doing, with a Stop button')
        rig.window.work_stop.click()
        check(pump(lambda: not rig.window.busy(), 60), 'stopped job ended')
        result = rig.window.service.result()
        check(result['exit_code'] == cm.EXIT_REFUSED and rig.h.flash() == newer
              and any(core.STOPPED_BY_OWNER in m for m in result['messages'])
              and 'needs your OK' in result['what_to_do'], 'Stop before a write: nothing changed')
        check(rig.window.service.ai_decision() == 'deny' and rig.window.service.remembered_ai_control() is None
              and rig.window.ai_chip.isHidden(), 'Stop takes the permission back, remembered Allow too')
        check('AI control: stopped by the owner (job running)' in
              (rig.logs / 'api-requests.log').read_text(encoding='utf-8'), 'request log: stopped by the owner')
    finally:
        server.stop()

    # Stop during an AI-started Finish job: ends cleanly, nothing written, the job still waits.
    rig = Rig(stock, release, stock)
    crash_mid_install(rig.h, 1)
    torn = rig.h.flash()
    rig.window.manager_factory = slow_factory(rig.h)
    rig.window.refresh_pending()
    rig.window.service.settings = core.AppSettings(folder / 'stop-resume.json')
    dialogs = rig.window.dialogs = GateDialogs(ai=[(True, False)])
    server, remote = served(rig, folder, 'ai-resume')
    try:
        remote.call('chromatic_request_action', {'action': 'resume'}, CLAUDE)
        check(pump(lambda: rig.window.tracker.step is not None and rig.window.tracker.step['kind'] == 'read', 30),
              'the AI\'s Finish job is reading the console')
        rig.window.work_stop.click()
        check(pump(lambda: not rig.window.busy(), 60), 'stopped Finish job ended')
        result = rig.window.service.result()
        check(result['exit_code'] == cm.EXIT_REFUSED and rig.h.flash() == torn and rig.window.pending
              and not any('manual' in m.lower() or 'runbook' in m for m in result['messages'])
              and any(core.STOPPED_BY_OWNER in m for m in result['messages']),
              'Stop during Finish job: clean stop, nothing written, the job still waits (no manual-required)')
    finally:
        server.stop()

    # Stop while a change is being written: never cut off; it finishes with all its checks.
    rig = Rig(stock, release)
    rig.window.manager_factory = slow_write_factory(rig.h)
    rig.window.service.settings = core.AppSettings(folder / 'stop2-settings.json')
    dialogs = rig.window.dialogs = GateDialogs(ai=[(True, False)], confirms=['no'])
    server, remote = served(rig, folder, 'ai4')
    try:
        remote.call('chromatic_request_action', {'action': 'install'}, CLAUDE)
        check(pump(lambda: rig.window.service._wrote, 30), 'the AI\'s install is writing')
        outcome = rig.window.stop_ai()
        check(outcome == {'stopping_job': True, 'writing': True}, 'Stop pressed mid-write')
        check(pump(lambda: not rig.window.busy(), 60) and rig.window.service.result()['exit_code'] == cm.EXIT_OK
              and rig.state() == 'installed' and not rig.h.console.in_stub,
              'mid-write Stop: the install finished safely and was checked')
        check(any('finishes safely first' in line for line in rig.window.lines), 'the window said so')
        remote.call('chromatic_request_action', {'action': 'remove'}, CLAUDE)
        check(pump(lambda: request_state(rig.window.service) == 'declined', 10) and dialogs.requests[-1][0] == 'remove'
              and rig.state() == 'installed', 'after Stop, the next request needs the owner\'s Approve again')
    finally:
        server.stop()


def main():
    app = QApplication.instance() or QApplication([])
    theme.apply(app)
    stock = stock_dump(allow_synthetic=True)
    release = cm.Release.load()
    temp = tempfile.TemporaryDirectory()
    folder = Path(temp.name)

    # ---- the tool set itself: reads, the read-only status, and requests; nothing else ----
    check(set(api.TOOL_NAMES) == {'chromatic_get_state', 'chromatic_get_progress', 'chromatic_get_last_result',
                                  'chromatic_list_backups', 'chromatic_get_update_guide', 'chromatic_start_status',
                                  'chromatic_request_action', 'chromatic_cancel_request', 'chromatic_set_log_panel',
                                  'chromatic_read_log', 'chromatic_update_pocket_link'}, 'the API tool set')
    request_tool = next(t for t in api.TOOLS if t['name'] == 'chromatic_request_action')
    check(set(request_tool['inputSchema']['properties']) == {'action', 'reason'}
          and request_tool['inputSchema']['additionalProperties'] is False
          and set(request_tool['inputSchema']['properties']['action']['enum']) == set(core.PROGRAM_WRITE_KEYS)
          and 'status' not in core.WRITE_KEYS and 'backup' not in core.WRITE_KEYS,
          'a write request takes only an action and a reason: no confirm flag exists')
    check(set(core.PROGRAM_WRITE_KEYS) == set(core.WRITE_KEYS) - set(core.GUIDE_ONLY_KEYS) - set(core.OWNER_APPROVE_KEYS)
          and core.OWNER_APPROVE_KEYS == (core.LINK_UPDATE_KEY,) and core.LINK_UPDATE_KEY in core.WRITE_KEYS
          and not any(k in request_tool['inputSchema']['properties']['action']['enum'] or k in request_tool['description']
                      for k in core.GUIDE_ONLY_KEYS + core.OWNER_APPROVE_KEYS),
          'the Pocket link jobs (guide only) are not offered to programs')
    check(all(t['inputSchema'].get('additionalProperties') is False for t in api.TOOLS), 'no tool takes extra switches')

    # ---- headless (MCP server with no window): reads and status work, writes are refused ----
    h = Harness(stock, release)
    service = headless(h, folder / 'headless')
    state = api.call_tool(service, 'chromatic_get_state', {}, CLAUDE)
    check(state['busy'] is False and 'refused' in state['write_confirmations'], 'headless: state says writes refused')
    for key in core.PROGRAM_WRITE_KEYS:
        answer = api.call_tool(service, 'chromatic_request_action', {'action': key, 'reason': 'test'}, CLAUDE)
        check(answer['state'] == 'refused' and 'window' in answer['why'], f'headless: {key} request refused')
    for key in core.GUIDE_ONLY_KEYS:  # not in the tool's list; asked for directly, refused as guide only
        try:
            api.call_tool(service, 'chromatic_request_action', {'action': key}, CLAUDE)
            check(False, f'{key}: the API took a guide-only job')
        except api.ToolError:
            pass
        answer = service.request_action(key, CLAUDE, 'test')
        check(answer['state'] == 'refused' and 'Update the Pocket link' in answer['why']
              and 'Add the Pocket link' not in answer['why'], f'headless: {key} refused (guide only)')
    check(h.flash() == stock and h.log == [], 'headless: refused requests never reached the manager')
    for name, arguments in (('chromatic_request_action', {'action': 'install', 'confirm': 'yes'}),
                            ('chromatic_request_action', {'action': 'install', 'confirmed': True}),
                            ('chromatic_request_action', {'action': 'status'}),
                            ('chromatic_start_status', {'action': 'install'}),
                            ('start', {'key': 'install'}), ('resolve_request', {}), ('chromatic_get_progress',
                                                                                      {'lines': 'all'})):
        try:
            api.call_tool(service, name, arguments, CLAUDE)
            check(False, f'{name} {arguments} should be refused')
        except api.ToolError:
            pass
    check(h.flash() == stock, 'no argument or tool name gets past the gate')
    check(service.resolve_request('r1', True) is False, 'headless: nothing can confirm a request')
    refused = api.call_tool(service, 'chromatic_start_status', {}, CLAUDE)
    check(refused['state'] == 'refused' and 'window' in refused['why'] and h.log == [],
          'headless: a program cannot start status either (no one to confirm it)')
    check(service.start('status', requested_by=WHO) and not service.start('status', requested_by=WHO),
          'one job at a time')
    service.wait(60)
    result = api.call_tool(service, 'chromatic_get_last_result', {}, CLAUDE)
    check(result['exit_code'] == cm.EXIT_OK and result['headline'] == core.STATUS_DONE
          and result['requested_by'] == WHO and result['confirmed_by'] is None
          and 'stock firmware (games)' in result['next'], 'headless status result names who asked')
    check(h.flash() == stock and not h.console.in_stub, 'headless status wrote nothing and left the chip running')
    check(api.call_tool(service, 'chromatic_get_state', {}, CLAUDE)['last_status']['state'] == 'stock',
          'state carries the status summary')
    log = (folder / 'headless/logs/api-requests.log').read_text(encoding='utf-8')
    check(f'install requested by {WHO}: refused' in log and f'status requested by {WHO}: refused' in log, 'refused requests are logged with who asked')

    # ---- device lock: a headless status while another tool (or the window) holds it ----
    with DeviceLock(folder / 'headless/device.lock'):
        service.start('status', requested_by=WHO)
        service.wait(30)
    check(service.result()['exit_code'] == cm.EXIT_REFUSED
          and any('Another Chromatic tool' in m for m in service.result()['messages']), 'device lock shared: refused')
    # The window's status reads a stock firmware no backup holds, so it is a slow full read.
    rig = Rig(with_app(stock, version='9.9.9', idf='v5.4.1'), release, stock)
    rig.window.manager_factory = slow_factory(rig.h)
    rig.window.service.lock_factory = lambda: DeviceLock(folder / 'shared.lock')
    other = headless(Harness(stock, release), folder / 'other')
    other.lock_factory = lambda: DeviceLock(folder / 'shared.lock')
    loop, outcome = QEventLoop(), {}
    rig.window.finished.connect(lambda code: (outcome.setdefault('code', code), loop.quit()))
    rig.window.start('status')
    check(pump(lambda: rig.window.tracker.step is not None), 'window job holds the device')
    other.start('status', requested_by=WHO)
    other.wait(30)
    check(other.result()['exit_code'] == cm.EXIT_REFUSED, 'headless MCP refused while the window job runs')
    QTimer.singleShot(60000, loop.quit)
    loop.exec()
    check(outcome.get('code') == cm.EXIT_OK, 'the window job finished normally')

    # ---- the window: one open at a time ----
    first = core.acquire_window_lock(folder)
    try:
        core.acquire_window_lock(folder)
        check(False, 'a second window must be refused')
    except Busy:
        pass
    first.__exit__(None, None, None)
    core.acquire_window_lock(folder).__exit__(None, None, None)

    # ---- the gate with the window open, over the 127.0.0.1 endpoint ----
    rig = Rig(stock, release)
    dialogs = rig.window.dialogs = GateDialogs(confirms=['no'])
    discovery = folder / 'api.json'
    server = api.LocalApiServer(rig.window.service, discovery).start()
    try:
        info = json.loads(discovery.read_text(encoding='utf-8'))
        check(info['url'] == f'http://127.0.0.1:{server.port}' and server.httpd.server_address[0] == '127.0.0.1',
              'API bound to 127.0.0.1 only')
        remote = api.RemoteApp.find(discovery)
        check(remote is not None, 'MCP finds the open window')
        good = {'Host': f'127.0.0.1:{server.port}', 'Authorization': f'Bearer {server.token}'}
        check(raw_http(server.port, 'GET', '/v1/health', {'Host': good['Host']}) == 401, 'no token: refused')
        check(raw_http(server.port, 'GET', '/v1/health', dict(good, Authorization='Bearer wrong')) == 401,
              'wrong token: refused')
        check(raw_http(server.port, 'GET', '/v1/health', dict(good, Origin='http://evil.example')) == 403,
              'browser origin: refused')
        check(raw_http(server.port, 'GET', '/v1/health', dict(good, Host='evil.example')) == 403,
              'foreign host header: refused')
        check(raw_http(server.port, 'GET', '/v1/health', good) == 200, 'token: accepted')
        check(raw_http(server.port, 'POST', '/v1/call', dict(good, **{'Content-Length': '-1'})) == 400,
              'negative Content-Length: refused at once, never read to the end of the stream')
        check(raw_http(server.port, 'POST', '/v1/call', dict(good, **{'Content-Type': 'application/json'}),
                       json.dumps({'tool': 'chromatic_request_action', 'arguments': {'action': 'install'},
                                   'confirmed': True, 'confirm': 'yes', 'force': True})) == 200,
              'extra body fields are ignored')
        check(request_state(rig.window.service) == 'waiting' and rig.h.flash() == stock,
              'a request waits; nothing written')
        second = remote.call('chromatic_request_action', {'action': 'remove'}, CLAUDE)
        check(second['state'] == 'refused' and 'already waiting' in second['why'], 'one waiting request at a time')
        check(pump(lambda: request_state(rig.window.service) == 'declined'), 'the owner said no')
        check(rig.h.flash() == stock and dialogs.requests and dialogs.requests[0][0] == 'install',
              'declined: nothing written')
        check(rig.window.service.resolve_request(rig.window.service.request['id'], True) is False,
              'a decided request cannot be confirmed later')

        # Confirmed by typing: it runs, and the log says who asked and who confirmed.
        dialogs.confirms.append('yes')
        asked = remote.call('chromatic_request_action', {'action': 'install', 'reason': 'update to 1.1.1, please'},
                            CLAUDE)
        check(asked['state'] == 'waiting' and asked['requested_by'] == WHO, 'request waits for the owner')
        confirmed_from_api = []
        worker = threading.Thread(target=lambda: confirmed_from_api.append(
            rig.window.service.resolve_request(asked['id'], True)))
        worker.start()
        worker.join()
        check(confirmed_from_api == [False] and request_state(rig.window.service) == 'waiting',
              'only the window thread can confirm')
        check(pump(lambda: request_state(rig.window.service) == 'finished', 90), 'confirmed request ran')
        check(dialogs.requests[-1] == ('install', WHO, 'update to 1.1.1, please'), 'the prompt named the requester')
        check(rig.state() == 'installed' and not rig.h.console.in_stub, 'install ran after the owner approved it')
        result = remote.call('chromatic_get_last_result', {}, CLAUDE)
        check(result['requested_by'] == WHO and result['confirmed_by'] == 'the owner, approved in the window'
              and result['exit_code'] == cm.EXIT_OK, 'result: who asked, who confirmed')
        requests_log = (rig.logs / 'api-requests.log').read_text(encoding='utf-8')
        anonymous = core.api_requester(None)
        for want in (f'r1 install requested by {anonymous}: waiting',
                     f'r1 install requested by {anonymous}: declined',
                     f'remove requested by {WHO}: refused (Another request is already waiting',
                     f'r2 install requested by {WHO}: confirmed by the owner, started',
                     f'r2 install requested by {WHO}: finished, exit 0'):
            check(want in requests_log, f'request log: {want}')
        job_log = rig.log_text()
        check(f'Requested by {WHO}. Confirmed by the owner, approved in the window.' in job_log,
              'job log: who asked and who confirmed')

        # Withdrawn before the owner answers: nothing runs.
        dialogs.confirms.append('yes')
        withdrawn = remote.call('chromatic_request_action', {'action': 'remove'}, CLAUDE)
        check(remote.call('chromatic_cancel_request', {'request_id': withdrawn['id']}, CLAUDE)['cancelled'],
              'request withdrawn')
        prompts = len(dialogs.requests)
        pump(lambda: False, 0.3)
        check(rig.state() == 'installed' and len(dialogs.requests) == prompts and dialogs.confirms == ['yes'],
              'withdrawn request: no prompt shown, nothing written')
        dialogs.confirms.clear()

        # A model may start the read-only status; the window shows it and who asked.
        dialogs.confirms.append('no')
        declined = remote.call('chromatic_start_status', {'reason': 'check the version'}, CLAUDE)
        check(declined['state'] == 'waiting' and pump(lambda: request_state(rig.window.service) == 'declined')
              and not rig.window.busy() and dialogs.requests[-1] == ('status', WHO, 'check the version'),
              'a status request waits for the owner; declined, nothing runs')
        dialogs.confirms.append('yes')
        started = remote.call('chromatic_start_status', {}, CLAUDE)
        check(started['state'] == 'waiting', 'status asked for again')
        check(pump(lambda: rig.window.busy()), 'the window shows the job')
        check(CLAUDE in rig.window.work_title.text(), 'working screen names who asked')
        check(pump(lambda: not rig.window.busy(), 60) and rig.window.result_head.text() == core.STATUS_DONE,
              'API status finished in the window')
        check(len(dialogs.ai_asked) == 1 and dialogs.ai_asked[0] == (core.api_requester(None), 'install'),
              'the AI pop-up was shown once, on the first request of this run (answered Don\'t allow)')
        view = remote.call('chromatic_get_progress', {'lines': 400}, CLAUDE)
        check(view['busy'] is False and view['log_tail'], 'progress and log tail readable')

        # The MCP backend attaches to this window rather than running its own core.
        backend = mcp.Backend(discovery, make_service=lambda: check(False, 'must attach, not go headless'))
        check(backend.call('chromatic_get_state', {}, CLAUDE)['host'] == 'window', 'MCP attaches to the window')

        # Wi-Fi through the window, read through the API: nothing about the network leaves.
        rig2 = Rig(rig.h.flash(), release, stock, confirms=['yes'])
        server2 = api.LocalApiServer(rig2.window.service, folder / 'api2.json').start()
        remote2 = api.RemoteApp.find(folder / 'api2.json')
        seen = []

        def answer(text):
            def reply(prompt):
                seen.append(remote2.call('chromatic_get_progress', {'lines': 400}, CLAUDE))
                return text
            return reply
        rig2.dialogs.answers = [answer('yes'), answer('TestNetwork'), answer('test-passphrase-123')]
        with tempfile.TemporaryDirectory() as wifi_folder:
            wifi_folder = Path(wifi_folder)
            identity = wifi_folder / 'identity'
            identity.mkdir()
            (identity / 'server.der').write_bytes(b'test certificate')
            (identity / 'pairing.bin').write_bytes(bytes(32))
            import wifi_gateway
            saved = {name: getattr(cm, name) for name in ('WIFI_FILE', 'PROVISION_COPY', 'REMOTE_PILOT_PAIRING',
                                                          'lan_ipv4')}
            saved_identity = wifi_gateway.IDENTITY
            try:
                cm.WIFI_FILE = wifi_folder / 'no-wifi-file.txt'
                cm.PROVISION_COPY = wifi_folder / 'provision.json'
                cm.REMOTE_PILOT_PAIRING = wifi_folder / 'pairing.json'
                cm.lan_ipv4 = lambda: '192.0.2.20'
                wifi_gateway.IDENTITY = identity
                check(rig2.run('wifi') == cm.EXIT_OK, 'Wi-Fi setup (window)')
            finally:
                for name, value in saved.items():
                    setattr(cm, name, value)
                wifi_gateway.IDENTITY = saved_identity
        everything = json.dumps([seen, remote2.call('chromatic_get_last_result', {}, CLAUDE),
                                 remote2.call('chromatic_get_state', {}, CLAUDE),
                                 remote2.call('chromatic_get_progress', {'lines': 400}, CLAUDE)])
        check(len(seen) == 3 and all(s['waiting_for_owner_answer'] for s in seen), 'questions visible as waiting')
        check(not any(secret in everything for secret in ('TestNetwork', 'test-passphrase-123', '192.0.2.20')),
              'API never returns the network name, password or address')
        server2.stop()
    finally:
        server.stop()
    check(not discovery.exists(), 'discovery file removed when the window closes')
    check(api.RemoteApp.find(discovery) is None, 'closed window is not found')

    # ---- redaction and backups by path only ----
    check(core.redact('chip aa:bb:cc:dd:ee:ff hash 0123456789abcdef0123456789abcdef') ==
          'chip [withheld] hash [withheld]', 'identifiers and hashes redacted')
    service._say('Identity 24-6f-28-aa-bb-cc, flash 0123456789abcdef01')
    tail = json.dumps(service.progress_view(400))
    check('24-6f-28' not in tail and '0123456789abcdef01' not in tail, 'log tail redacted')
    backups_h, backups_c = folder / 'bk-h', folder / 'bk-c'
    for base in (backups_h, backups_c):
        (base / '20261107T000000000000Z').mkdir(parents=True)
        (base / '20261107T000000000000Z/record.json').write_text(json.dumps(
            {'identity': IDENTITY, 'sha256': 'f' * 64}), encoding='utf-8')
        (base / '20261107T000000000000Z/esp32-flash.bin').write_bytes(b'secret flash bytes')
    saved_backups = cm.BACKUPS, cm.SECONDARY
    try:
        cm.BACKUPS, cm.SECONDARY = backups_h, backups_c
        listing = api.call_tool(service, 'chromatic_list_backups', {}, CLAUDE)
    finally:
        cm.BACKUPS, cm.SECONDARY = saved_backups
    text = json.dumps(listing)
    check(len(listing['backups']) == 2 and listing['backups'][0]['files'] == ['esp32-flash.bin', 'record.json'],
          'backups listed by path and file name')
    check(not any(s in text for s in (IDENTITY['chip'], 'ffffffff', 'secret flash', 'identity"')),
          'backup listing carries no contents, hashes or identities')
    check(IDENTITY['chip'] not in json.dumps([service.state(), service.result(), service.update_guide()]),
          'no identifier anywhere in the API answers')

    # ---- MCP stdio protocol (in process, headless core on the simulated console) ----
    h3 = Harness(stock, release)
    headless3 = headless(h3, folder / 'mcp')
    backend = mcp.Backend(folder / 'no-window.json', make_service=lambda: headless3)
    out = io.BytesIO()
    lines = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'claude-code'}}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'chromatic_get_state', 'arguments': {}}},
        {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {
            'name': 'chromatic_request_action', 'arguments': {'action': 'restore'}}},
        {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'nope', 'arguments': {}}},
        {'jsonrpc': '2.0', 'id': 6, 'method': 'ping'},
        {'jsonrpc': '2.0', 'id': 7, 'method': 'resources/list'},
        {'jsonrpc': '2.0', 'id': 8, 'method': 'tools/call', 'params': {'name': 'chromatic_start_status'}},
    ]
    stream = io.BytesIO(b'\n'.join(json.dumps(m).encode() for m in lines) + b'\n{not json\n')
    mcp.McpServer(backend, out).serve(stream)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    by_id = {r.get('id'): r for r in replies}
    check(by_id[1]['result']['protocolVersion'] == '2025-06-18'
          and by_id[1]['result']['serverInfo']['name'] == 'chromatic-manager', 'MCP initialize')
    check(len(by_id[2]['result']['tools']) == len(api.TOOLS), 'MCP tools/list')
    check(by_id[3]['result']['isError'] is False and by_id[3]['result']['structuredContent']['busy'] is False,
          'MCP tools/call')
    check(by_id[4]['result']['structuredContent']['state'] == 'refused', 'MCP write request refused headless')
    check(by_id[5]['result']['isError'] is True, 'MCP unknown tool is an error')
    check(by_id[6]['result'] == {} and by_id[7]['error']['code'] == -32601, 'MCP ping, unknown method')
    check(by_id[None]['error']['code'] == -32700, 'MCP parse error')
    check(by_id[8]['result']['structuredContent'].get('started', False)
          is False and by_id[8]['result']['structuredContent']['state'] == 'refused', 'MCP status refused headless')
    check(h3.flash() == stock, 'MCP: nothing written')
    check(f'restore requested by {core.api_requester("claude-code via MCP")}: refused' in
          (folder / 'mcp/logs/api-requests.log').read_text(encoding='utf-8'), 'MCP client named in the request log')

    # ---- the real stdio entry point, without Qt (the tools venv has none), protocol only ----
    tools_python = TOOLS / 'chromatic-tools/venv/Scripts/python.exe'
    python = tools_python if tools_python.exists() else Path(sys.executable)
    run = subprocess.run([str(python), '-B', str(ROOT / 'scripts/chromatic_manager_mcp.py'),
                          '--discovery', str(folder / 'no-window.json')],
                         input=b'\n'.join(json.dumps(m).encode() for m in lines[:3]) + b'\n',
                         capture_output=True, timeout=60)
    out_lines = run.stdout.splitlines()
    check(run.returncode == 0 and len(out_lines) == 2 and all(json.loads(line)['jsonrpc'] == '2.0'
                                                              for line in out_lines),
          'stdio server: clean JSON-RPC on stdout, no Qt needed')

    ai_control_checks(stock, release, folder)
    link_update_api_checks(stock, release, folder)

    print('PASS: Chromatic Manager API/MCP: write requests refused headless and on any argument or tool name; with '
          'the window open a request runs only after the owner approves it on the window thread (declined, '
          'withdrawn and confirmed paths, requester named on the prompt and in the logs); status by API; '
          '127.0.0.1 endpoint refuses missing/wrong token, browser origin and foreign host; device and window '
          'locks; redaction of identifiers, hashes, Wi-Fi and backups; MCP stdio protocol; AI control (pop-up on the '
          'first request, refusal without a grant, writes under a grant, remember and clear, hardware usage rule, '
          'Stop before and during a write, log panel by API); the one-step Pocket link update asked for by a '
          'program (refused headless and through any other entry or argument; the window opens its screen and the '
          'Approve prompt naming the program; AI control never starts it; Cancel touches nothing; Approve starts '
          'the try from RAM, then the job asks the owner once more with the new image running and the flash '
          'unwritten (shown to a program as owner_action_needed; a program asking again meanwhile is refused), and '
          'that Approve runs it to Ready; declined while another guided setup is open). No hardware.')
    temp.cleanup()


if __name__ == '__main__':
    main()

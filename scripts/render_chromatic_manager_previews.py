# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Pictures of the Chromatic Manager window in each state, drawn offscreen for the owner.

No console, no manager job, no private file: the window gets a stand-in manager that only
says no job is waiting, a temporary settings file and log folder, and made-up states
(versions and the LAN address are examples). Writes PNGs to
staging/chromatic-manager-app/previews/ (Git-ignored), or --out <folder>. With
--reference <png> [<png> ...] (screenshots of ModRetro's updater, in the order System Update,
Cart Clinic, About) it also saves side-by-side comparisons with our matching tab; the
screenshots are only read and placed next to ours in those comparison images.

Run: private/chromatic-manager-gui/venv/Scripts/python.exe -B scripts/render_chromatic_manager_previews.py
"""
import argparse
import os
from pathlib import Path
import sys
import tempfile

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_QPA_FONTDIR', os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts'))  # offscreen has no fonts of its own
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import chromatic_manager as cm  # noqa: E402
import chromatic_manager_core as core  # noqa: E402
import chromatic_manager_gui as gui  # noqa: E402
import chromatic_theme as theme  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'staging/chromatic-manager-app/previews'
AI = core.api_requester('Claude Code via MCP')


class StandIn:
    """Only what the window asks a manager when nothing runs."""

    def pending(self):
        return False


class Image:
    """A made-up start-up menu image (version and fingerprint are examples)."""
    version = '1.2.0'

    def fingerprint(self):
        return '48b11f9a' + '0' * 56


IMAGE = core.short_release(Image())
USAGE = 'Usage check: 62% used, reported by the AI (claude) at 14:02 (2 min ago).'


class NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def settle(ms=150):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


SOUND_IMAGE = 'sound build 18.40 (image 2fb37494)'  # example short name, as the Approve dialog shows it
LINK_IMAGE = 'sound + Pocket link build 18.41 (image 0123abcd)'  # example short name of the reviewed 18.41 image


def window(folder, size=gui.WINDOW):
    w = gui.MainWindow(cm, lambda say: StandIn(), NoLock, gui.Dialogs(None), root=ROOT,
                       log_folder=Path(folder) / 'logs', settings_path=Path(folder) / 'settings.json',
                       hardware_gate=lambda: None, guide_path=Path(folder) / 'sound-guide.json',
                       guide_session='preview', sound_image=lambda: SOUND_IMAGE, link_image=lambda: None)
    w.service.release_peek = Image
    w.resize(*size)
    w.show()
    w.consoles = 1
    return w


def home(w, summary):
    w.service.summary = summary
    w.show_summary(summary)
    w.refresh_guide()
    w.show_tab('console')


def compare(reference, ours, name, caption):
    """The owner's screenshot of the official updater and ours, side by side at the same height."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage, QPainter
    left, right = QImage(str(reference)), QImage(str(ours))
    height = max(left.height(), right.height())
    left = left.scaledToHeight(height, Qt.TransformationMode.FastTransformation)
    right = right.scaledToHeight(height, Qt.TransformationMode.FastTransformation)
    out = QImage(left.width() + right.width() + 30, height + 28, QImage.Format.Format_RGB32)
    out.fill(QColor('#202020'))
    p = QPainter(out)
    p.drawImage(0, 28, left)
    p.drawImage(left.width() + 30, 28, right)
    p.setPen(QColor('#ffffff'))
    p.drawText(8, 18, "Reference: ModRetro's official updater (owner's screenshot)")
    p.drawText(left.width() + 38, 18, f'Chromatic Manager: {caption}')
    p.end()
    out.save(str(OUT / name))
    return OUT / name


def save(widget, name):
    settle()
    widget.grab().save(str(OUT / name))
    return OUT / name


def guide_screen(w, screen, reason=None, note='', todo='', sound_written=False, trial_loaded=False):
    """Put the guided sound setup on one screen, as the window would show it."""
    g = w.sound
    g.screen, g.reason, g.note, g.todo = screen, reason, note, todo
    g.sound_written, g.trial_loaded = sound_written, trial_loaded
    w.show_guide()


def guide_previews(w):
    """Every screen of the guided Jellyfin sound setup, plus the two places its button lives and
    the result cards that now say what a console chip job wrote."""
    made = []
    installed = {'state': 'installed', 'boots': 'start-up menu on power-on, stock otherwise', 'launcher': '1.2.4',
                 'release': '1.2.4', 'stock': 'v4.6', 'idf': 'v5.1.2', 'fpga': '18.14'}
    home(w, installed)
    made.append(save(w, 'guide-00-home-button.png'))
    w.show_tab('recovery')
    made.append(save(w, 'guide-00b-fix-a-problem-button.png'))
    w.show_tab('console')
    w.sound.start()
    for name, args in (
            ('01-step1-check', ('check',)),
            ('02-step2-rehearse', ('rehearse', None, 'Checked: your original image, byte for byte. Nothing was written.')),
            ('03-step2-play-a-game', ('game_test',)),
            ('04-step3-try', ('trial',)),
            ('05-step3-look', ('trial_look',)),
            ('06-step4-power-cycle-write', ('power_cycle',)),
            ('07-step5-final-checks', ('final',)),
            ('08-done', ('done',)),
            ('09-final-wrong-restore', ('final_wrong',)),
            ('10-restored', ('restored',)),
            ('11-stop-not-original', ('stopped', 'unknown', 'Checked. Nothing was written.')),
            ('12-stop-sound-already', ('stopped', 'already_sound', 'Checked. Nothing was written.')),
            ('13-stop-game-wrong', ('stopped', 'game_wrong')),
            ('14-stop-trial-wrong', ('stopped', 'trial_wrong')),
            ('15-stopped-by-you', ('stopped', 'cancelled', '', 'Switch your Chromatic off and on: it goes back to its '
                                   'original image.')),
            ('16-stop-rehearsal-lost', ('stopped', 'rehearsal_lost', 'Nothing was changed.')),
            ('17-stop-needs-you', ('stopped', 'needs_you', 'Stopped: this needs you.', core.what_to_do(
                'fpga_media', 3, 'none', {}, [])))):
        guide_screen(w, *args)
        made.append(save(w, f'guide-{name}.png'))
    guide_screen(w, 'check', note='Check the console chip (FPGA): cancelled. Nothing was done. Press the button '
                                  'again when you are ready.')
    made.append(save(w, 'guide-18-approve-cancelled.png'))
    guide_screen(w, 'resume', trial_loaded=True)
    made.append(save(w, 'guide-19-reopened-start-again.png'))
    guide_screen(w, 'resume', sound_written=True)
    made.append(save(w, 'guide-20-reopened-final-checks.png'))
    w.sound.close()
    w.leave_guide()
    # The result card after a console chip job, outside the guide (it used to say the console
    # was not touched).
    for key, line, name in (
            ('fpga_restore', 'Original console chip image written back (backup of 2026-10-28), all 4 MiB read back and '
                             'identical, console restarted. Rehearsal recorded for this session.', 'restore'),
            ('fpga_media', f'{SOUND_IMAGE} installed: all 4 MiB read back and identical to the expected flash, '
                           'console restarted.', 'sound')):
        head, next_text = core.fpga_result(key, 0, [line])
        w.running = key
        w.run_lines = [line]
        w.service.last_result = {'action': key, 'title': core.BY_KEY[key].title, 'exit_code': 0, 'headline': head,
                                 'role': 'ok', 'next': next_text, 'what_to_do': 'Nothing else is needed.',
                                 'took': '2:40', 'log_file': None}
        w.on_done(0)
        made.append(save(w, f'guide-21-result-{name}.png'))
    w.show_tab('console')
    return made


def link_screen(w, screen, reason=None, note='', todo='', link_written=False, trial_loaded=False, answered=False):
    """Put the guided Pocket link setup on one screen, as the window would show it."""
    g = w.link_guide
    w.sound = g
    g.screen, g.reason, g.note, g.todo = screen, reason, note, todo
    g.link_written, g.trial_loaded, g.answered = link_written, trial_loaded, answered
    w.show_guide()


def link_previews(w):
    """Every screen of the guided Pocket link setup (the combined image 18.41), the places its
    button lives, Home on 18.41, both Approve dialogs and the write's result card."""
    made = []
    w.link_image, w._link_image_text = (lambda: LINK_IMAGE), ...
    installed = {'state': 'installed', 'boots': 'start-up menu on power-on, stock otherwise', 'launcher': '1.3.2',
                 'release': '1.3.2', 'stock': 'v4.6', 'idf': 'v5.1.2', 'fpga': '18.40 debug'}
    home(w, installed)
    made.append(save(w, 'link-00-home-offer.png'))
    home(w, dict(installed, fpga='18.41 debug'))
    made.append(save(w, 'link-00b-home-pocket-link-ready.png'))
    w.show_tab('recovery')
    made.append(save(w, 'link-00c-fix-a-problem-button.png'))
    w.show_tab('console')
    w.link_guide.start()
    for name, args in (
            ('01-step1-check', ('check',)),
            ('02-step2-try', ('trial', None, 'Checked: the sound image, byte for byte. Nothing was written.')),
            ('03-step3-look-pocket-link-row', ('look_link_row', None, core.SAID_CONFIRMED_NOTE)),
            ('04-step3-look-sound', ('look_sound',)),
            ('05-step3-look-games', ('look_games',)),
            ('06-step3-look-link-game', ('look_link_game',)),
            ('06b-stop-trial-not-running', ('stopped', 'trial_not_running', 'Pocket link NOT running.')),
            ('07-step4-power-cycle-write', ('power_cycle', None, 'Your answers are saved with this try.')),
            ('08-step5-final-checks', ('final',)),
            ('09-done', ('done',)),
            ('10-final-wrong-go-back', ('final_wrong',)),
            ('11-sound-back', ('sound_back',)),
            ('12-restored', ('restored',)),
            ('13-stop-not-cleared', ('stopped', 'no_image')),
            ('14-stop-needs-sound', ('stopped', 'needs_sound', 'Checked. Nothing was written.')),
            ('15-stop-already-link', ('stopped', 'already_link', 'Checked. Nothing was written.')),
            ('16-stop-look-wrong', ('stopped', 'look_wrong')),
            ('17-stop-try-lost', ('stopped', 'try_lost', 'Nothing was changed.')),
            ('18-stop-link-undone', ('stopped', 'link_undone', 'Fixed: the cut-off job was put right.')),
            ('19-stopped-by-you', ('stopped', 'cancelled', '', 'Choose Games, wait a few seconds, then switch your '
                                   'Chromatic off and on: it goes back to the image in its flash.')),
            ('20-stop-unknown', ('stopped', 'unknown', 'Checked. Nothing was written.'))):
        link_screen(w, *args)
        made.append(save(w, f'link-{name}.png'))
    link_screen(w, 'resume', trial_loaded=True, answered=True)
    made.append(save(w, 'link-21-reopened-go-on.png'))
    link_screen(w, 'resume', trial_loaded=True)
    made.append(save(w, 'link-22-reopened-start-again.png'))
    link_screen(w, 'resume', link_written=True)
    made.append(save(w, 'link-23-reopened-final-checks.png'))
    w.link_guide.close()
    w.leave_guide()
    w.sound = w.sound_guide
    for name, key in (('24-approve-try', 'fpga_link_trial'), ('25-approve-write', 'fpga_link')):
        dialog = gui.ApproveDialog(w, gui.approve_prompt(core.BY_KEY[key]).replace(
            'none ready (refused)', LINK_IMAGE))  # the example image, as a cleared build shows it
        dialog.show()
        settle(gui.AskDialog.SETTLE_MS + 150)
        made.append(save(dialog, f'link-{name}.png'))
        dialog.close()
    line = f'{LINK_IMAGE} installed: all 4 MiB read back and identical to the expected flash, console restarted.'
    head, next_text = core.fpga_result('fpga_link', 0, [line])
    w.running = 'fpga_link'
    w.run_lines = [line]
    w.service.last_result = {'action': 'fpga_link', 'title': core.BY_KEY['fpga_link'].title, 'exit_code': 0,
                             'headline': head, 'role': 'ok', 'next': next_text, 'what_to_do': 'Nothing else is needed.',
                             'took': '2:40', 'log_file': None}
    w.on_done(0)
    made.append(save(w, 'link-26-result-written.png'))
    w.show_tab('console')
    w.link_image, w._link_image_text = (lambda: None), ...
    return made


def link_update_previews(w):
    """Update the Pocket link in one step: its one screen, the Approve asked with the new image
    running (the big sign and the small window), Ready, its stops (checks failed, not kept, switched
    off, refused after Approve, a write undone, the original image, Restore FPGA first, an older
    menu) and the sound image that follows Restore FPGA, and the owner-action sign for the watched
    power cycle: waiting with its countdown, nearly out of time, done, timed out."""
    import chromatic_fpga as cf
    made = []
    w.link_image, w._link_image_text = (lambda: LINK_IMAGE), ...
    installed = {'state': 'installed', 'boots': 'start-up menu on power-on, stock otherwise', 'launcher': '1.3.5',
                 'release': '1.3.5', 'stock': 'v4.6', 'idf': 'v5.1.2', 'fpga': '18.40 debug'}
    home(w, installed)
    made.append(save(w, 'update-00-home-button.png'))
    w.open_link_update()
    made.append(save(w, 'update-01-one-step-screen.png'))
    g = w.link_update
    back = core.late_refusal_note(f'{core.SAID_STILL_HOLDS}{core.HELD_WORDS[0]}: {core.SAID_AS_IT_WAS}.')
    for name, screen, reason, note in (
            ('06-ready', 'ready', None, ''), ('07-stop-checks-failed', 'stopped', 'auto_failed', ''),
            ('08-stop-not-kept', 'stopped', 'not_kept', ''), ('09-stop-switched-off', 'stopped', 'switched_off', ''),
            ('10-stop-refused-after-approve', 'stopped', 'late_refused', back),
            ('11-stop-write-undone', 'stopped', 'link_undone', ''),
            ('12-stop-original-image', 'stopped', 'needs_sound', ''),
            ('13-stop-restore-first', 'stopped', 'sound_needs_restore', ''),
            ('14-sound-image-next', 'sound_next', None, ''), ('15-stop-menu-too-old', 'stopped', 'menu_too_old', '')):
        g.screen, g.reason, g.note, g.todo, g.link_written = screen, reason, note, '', screen == 'ready'
        w.show_guide()
        made.append(save(w, f'update-{name}.png'))
    g.close()
    w.leave_guide()
    w.sound = w.sound_guide

    # The sign, as a running job puts it up (no job runs here: the window is told one does).
    now, real_clock = [1000.0], w.clock
    w.busy, w.clock, w.call_owner = (lambda: True), (lambda: now[0]), (lambda sound=True: None)

    def sign(number, title, text, seconds):
        w.on_owner({'id': number, 'kind': 'action', 'state': 'waiting', 'title': title, 'instruction': text,
                    'seconds': seconds, 'note': '', 'next': ''})
        w.owner_timer.stop()  # the pictures set the clock themselves

    def ended(number, state, note='', next_step=''):
        w.on_owner({'id': number, 'kind': 'action', 'state': state, 'title': '', 'instruction': '', 'seconds': None,
                    'note': note, 'next': next_step})
    sign(1, cf.KEEP_TITLE, cf.keep_text('media'), None)
    made.append(save(w, 'update-02-approve-new-image-running.png'))
    dialog = gui.ApproveDialog(w, gui.keep_prompt(cf.keep_text('media')), 'Approve', cf.KEEP_TITLE, 'Not right')
    dialog.show()
    settle(gui.AskDialog.SETTLE_MS + 150)
    made.append(save(dialog, 'update-03-approve-window.png'))
    dialog.close()
    ended(1, 'closed')
    sign(2, cf.POWER_CYCLE_TITLE, cf.POWER_CYCLE_TEXT, 90.0)
    now[0] += 18
    w.owner_banner.set_left(w.owner_left(), 90.0)
    made.append(save(w, 'owner-01-waiting-countdown.png'))
    now[0] += 64
    w.owner_banner.set_left(w.owner_left(), 90.0)
    made.append(save(w, 'owner-02-waiting-last-seconds.png'))
    ended(2, 'done', 'Your Chromatic was switched off and on.')
    made.append(save(w, 'owner-03-done-thank-you.png'))
    w._owner = None
    sign(3, cf.POWER_CYCLE_TITLE, cf.POWER_CYCLE_TEXT, 90.0)
    ended(3, 'timeout', next_step=cf.POWER_CYCLE_MISSED)
    made.append(save(w, 'owner-04-timed-out.png'))
    del w.busy, w.call_owner
    w.clock = real_clock
    w._owner = None
    w.leave_guide('console')
    w.link_image, w._link_image_text = (lambda: None), ...
    return made


def sound_home_previews(w):
    """Home's four sound states, from the FPGA version the start-up menu reports at the normal check
    (versions are examples; the older-image case pretends this PC built a newer 18.41)."""
    made = []
    installed = {'state': 'installed', 'boots': 'start-up menu on power-on, stock otherwise', 'launcher': '1.2.6',
                 'release': '1.2.6', 'stock': 'v4.6', 'idf': 'v5.1.2'}
    for name, fpga, built in (('installed', '18.40 debug', SOUND_IMAGE),
                              ('outdated', '18.40 debug', 'sound build 18.41 (image 5e6f7a8b)'),
                              ('none', '18.14', SOUND_IMAGE),
                              ('unknown', '18.20', SOUND_IMAGE)):
        w.sound_image, w._sound_image_text = (lambda b=built: b), ...
        home(w, dict(installed, fpga=fpga))
        made.append(save(w, f'home-sound-{name}.png'))
    w.sound_image, w._sound_image_text = (lambda: SOUND_IMAGE), ...
    return made


def main(argv=None):
    global OUT
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, default=OUT)
    parser.add_argument('--reference', type=Path, nargs='*', default=[])
    parser.add_argument('--guide-only', action='store_true', help='only the guided sound setup screens')
    parser.add_argument('--sound-only', action='store_true', help='only the Home tab\'s four sound states')
    parser.add_argument('--link-only', action='store_true', help='only the guided Pocket link setup screens')
    parser.add_argument('--update-only', action='store_true',
                        help='only Update the Pocket link (one step) and the owner-action sign')
    args = parser.parse_args(argv)
    OUT = args.out.resolve()
    app = QApplication.instance() or QApplication([])
    theme.apply(app)
    OUT.mkdir(parents=True, exist_ok=True)
    made = []
    with tempfile.TemporaryDirectory() as folder:
        installed = {'state': 'installed', 'boots': 'start-up menu on power-on, stock otherwise', 'stock': 'v4.6',
                     'idf': 'v5.1.2', 'launcher': '1.1.3', 'release': '1.1.3',
                     'setup': '2 Wi-Fi network(s), PC pairing yes, Jellyfin linked'}
        w = window(folder)
        if args.guide_only or args.sound_only or args.link_only or args.update_only:
            made += (sound_home_previews(w) if args.sound_only else link_previews(w) if args.link_only
                     else link_update_previews(w) if args.update_only else guide_previews(w))
            w.close()
            for path in made:
                print(path)
            return
        w.sound_image = lambda: None  # the tab pictures below show the window without a sound image
        w._sound_image_text = ...
        home(w, installed)
        made.append(save(w, '01-up-to-date.png'))
        w.details.show()
        w.details_toggle.setText('Hide details')
        made.append(save(w, '01b-up-to-date-details.png'))
        w.details.hide()
        w.details_toggle.setText('Show details')
        home(w, dict(installed, state='outdated', launcher='1.1.2'))
        made.append(save(w, '02-update-available.png'))
        home(w, dict(installed, state='damaged'))
        made.append(save(w, '03-error-cannot-start.png'))
        w.consoles = 0
        home(w, {})
        made.append(save(w, '04-plug-in.png'))
        w.consoles = 1

        # Working: a job the AI started under the owner's permission, writing, log open.
        w.service.ai_session = 'allow'
        w.refresh_ai()
        w.service.job = {'key': 'install', 'title': core.BY_KEY['install'].title, 'requested_by': AI,
                         'confirmed_by': core.AI_GRANT, 'started': '', 'setup': 'auto', 'note': USAGE}
        w.service.tracker = core.Tracker()
        w.on_started('install', AI)
        for line in ('Identifying the console from on-chip checksums (about 20 seconds; nothing is written)...',
                     'Writing step 1 of 3 (884 KiB)...'):
            w.service.progress.feed(line)
            w.add_line(line)
        w.service.tracker.event('mode', {'mode': 'download'})
        w.service.tracker.event('step', {'kind': 'write', 'size': 884 * 1024})
        w.service.tracker.event('bytes', {'done': 400 * 1024, 'total': 884 * 1024})
        w.show_banner()
        w.refresh_work()
        w.clock_timer.stop()
        w.set_log_open(True)
        made.append(save(w, '05-working-ai-with-stop.png'))
        w.set_log_open(False)

        # Done.
        w.service.job = None
        w.service.last_result = {'action': 'install', 'title': core.BY_KEY['install'].title, 'exit_code': 0,
                                 'headline': 'Done', 'role': 'ok',
                                 'next': 'It has restarted into the start-up menu, which waits for you: choose '
                                         'Games to play.',
                                 'what_to_do': 'Insert a cartridge, then choose Games on the console to play.',
                                 'took': '2:05', 'log_file': None}
        w.run_lines.append('Start-up menu 1.1.3 installed. The whole-flash check matches, and the menu was seen '
                           'running.')
        w.on_done(0)
        made.append(save(w, '06-done.png'))

        # A problem after a job: orange-red, straight to the recovery steps.
        w.service.ai_session = None
        w.refresh_ai()
        w.running = 'install'
        w.service.last_result = dict(w.service.last_result, exit_code=3, headline='Stopped: this needs you',
                                     role='error', next='It has restarted and runs its own firmware.',
                                     what_to_do=core.what_to_do('install', 3, 'running', {}, []))
        w.on_done(3)
        made.append(save(w, '07-error-result.png'))

        w.show_tab('recovery')
        made.append(save(w, '08-fix-a-problem.png'))
        w.service.ai_session = 'allow'
        w.service.settings.set('ai_control', 'allow')
        w.refresh_ai()
        w.show_tab('settings')
        made.append(save(w, '09-settings-ai-remembered.png'))
        w.service.ai_session = None
        w.service.settings.set('ai_control', None)
        w.refresh_ai()

        w.show_tab('about')
        made.append(save(w, '10-about.png'))
        w.show_tab('console')
        home(w, installed)
        w.set_log_open(True)
        made.append(save(w, '10b-log-open.png'))
        w.set_log_open(False)
        small = w

        # Dialogs, drawn over the window.
        home(w, installed)
        for name, dialog in (
                ('11-wifi-name.png', gui.AskDialog(w, 'Wi-Fi network name (2.4 GHz): ')),
                ('12-wifi-password.png', gui.AskDialog(w, 'Wi-Fi password (hidden; leave empty for an open '
                                                          'network): ', secret=True)),
                ('13-wifi-use-this-pc.png', gui.AskDialog(w, 'Use this PC at 192.0.2.20 for Jellyfin? Type yes: ')),
                ('14-ai-popup.png', gui.AiControlDialog(w, AI, core.BY_KEY['install'], IMAGE)),
                ('14b-ai-keep-allowing.png', gui.AiKeepDialog(w, AI, core.BY_KEY['install'], IMAGE)),
                ('14c-ai-image-changed.png', gui.AiKeepDialog(w, AI, core.BY_KEY['install'], IMAGE, changed=True)),
                ('15-approve.png', gui.ApproveDialog(w, gui.approve_prompt(core.BY_KEY['install'], release=IMAGE))),
                ('15b-approve-ai-request.png', gui.ApproveDialog(w, gui.approve_prompt(
                    core.BY_KEY['install'], AI, 'You asked me to update the start-up menu to the newest version '
                    'today; please approve this so I can finish the task for you.', IMAGE)))):
            dialog.show()
            settle(gui.AskDialog.SETTLE_MS + 150)
            made.append(save(dialog, name))
            dialog.close()
        w.sound_image = lambda: SOUND_IMAGE
        w._sound_image_text = ...
        made += guide_previews(w)
        made += sound_home_previews(w)
        made += link_previews(w)
        made += link_update_previews(w)
        w.close()
        small.close()
    pairs = (('01-up-to-date.png', 'Home (up to date)'), ('08-fix-a-problem.png', 'Fix a problem'),
             ('10-about.png', 'About'))
    for n, (reference, (ours, caption)) in enumerate(zip(args.reference, pairs), 1):
        made.append(compare(reference, OUT / ours, f'compare-{n}-{ours}', caption))
    for path in made:
        print(path.relative_to(ROOT))


if __name__ == '__main__':
    main()

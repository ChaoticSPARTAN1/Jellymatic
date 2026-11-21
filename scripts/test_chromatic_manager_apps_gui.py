# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Chromatic Manager window, launcher 1.6.4: Settings > Apps against the simulated console of test_chromatic_manager.py,
and the optional server setups (Live TV channels, ChromaTube server) against the fake Jellyfin of
test_chromatic_server_apps.py (the owner's tuner, libraries and other sessions present). No hardware, no real server,
no network. Writes the dialogs' previews to staging/chromatic-manager-app/previews-apps/. Prints pass/fail only.

Run: private/chromatic-manager-gui/venv/Scripts/python.exe -B scripts/test_chromatic_manager_apps_gui.py
"""
import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_QPA_FONTDIR', os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts'))

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

import chromatic_apps as ap
import chromatic_manager as cm
import chromatic_server_apps as sa
from compose_chromatic_image import stock_dump
from test_chromatic_manager import Harness, check
from test_chromatic_manager_apps import relabel
from test_chromatic_manager_gui import Rig
import test_chromatic_server_apps as fake_server

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'staging' / 'chromatic-manager-app' / 'previews-apps'


def wait(condition, seconds=20):
    loop, timer = QEventLoop(), QTimer()
    timer.timeout.connect(lambda: condition() and loop.quit())
    timer.start(20)
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec()
    timer.stop()
    return condition()


def shot(app, dialog, name):
    dialog.adjustSize()
    app.processEvents()
    dialog.grab().save(str(OUT / f'{name}.png'))


def run_apps(rig, drive, job_seconds=120):
    window, result, loop = rig.window, {}, QEventLoop()

    def done(code):
        result['code'] = code
        loop.quit()
    window.finished.connect(done)
    window.on_apps_dialog = drive
    try:
        choice = window.open_apps_page()
        if window.busy():
            QTimer.singleShot(job_seconds * 1000, loop.quit)
            loop.exec()
    finally:
        window.finished.disconnect(done)
    return choice, result.get('code')


def main():
    app = QApplication.instance() or QApplication([])
    OUT.mkdir(parents=True, exist_ok=True)
    stock, r164 = stock_dump(allow_synthetic=True), relabel(cm.Release.load(), '1.6.4')
    h = Harness(stock, r164)
    check(h.manager().install() == 0, 'install 1.6.4 for the start')
    installed = h.flash()
    with tempfile.TemporaryDirectory() as folder:
        cm.APPS_FILE, cm.APPS_PENDING = Path(folder) / 'apps.json', Path(folder) / 'apps.pending.json'
        rig = Rig(installed, r164, stock, confirms=['yes', 'no', 'yes'])
        w = rig.window
        w.show_tab('settings')
        check(w.apps_state.text() == 'TV shown (Local Weather only), ChromaTube hidden (the defaults).'
              and w.apps_open.text() == 'Apps' and w.livetv_open.text() == 'Live TV channels', 'Settings: the apps line and buttons')
        w.resize(980, 760)
        app.processEvents()
        w.grab().save(str(OUT / 'settings-apps-section.png'))

        def choose(dialog):
            check(dialog.tv.isChecked() and dialog.weather_only.isChecked() and not dialog.tube.isChecked(),
                  'the page opens on the defaults')
            shot(app, dialog, 'apps-1-defaults')
            dialog.with_livetv.setChecked(True)
            dialog.tube.setChecked(True)
            shot(app, dialog, 'apps-2-tv-live-tv-and-chromatube')
            dialog.tv.setChecked(False)
            check(not dialog.with_livetv.isEnabled(), 'TV off: its two choices are greyed')
            dialog.tv.setChecked(True)
            dialog._write()
        choice, code = run_apps(rig, choose)
        on_console = ap.current(cm.span(rig.h.flash(), cm.APPS_A), cm.span(rig.h.flash(), cm.APPS_B))[0]
        check(choice == 'write' and code == 0 and on_console == ap.KNOWN and ap.load_saved(cm.APPS_FILE) == ap.KNOWN,
              f'Save and write: Approve, the job writes the console, the PC keeps a copy (exit {code})')
        check(w.apps_state.text().startswith('TV shown (Local Weather and Live TV channels), ChromaTube shown'),
              'Settings follows what was written')
        before = rig.h.flash()
        choice, code = run_apps(rig, lambda d: d._use_default())
        check(choice == 'default' and code is None and rig.h.flash() == before and not cm.APPS_PENDING.exists(),
              'Approve cancelled: nothing written')
        choice, code = run_apps(rig, lambda d: d._use_default())
        check(code == 0 and cm.span(rig.h.flash(), cm.APPS) == b'\xff' * 8192 and ap.load_saved(cm.APPS_FILE) is None,
              'Use defaults: erased on the console and forgotten on this PC')
        flash = rig.h.flash()
        check(run_apps(rig, lambda d: d.reject()) == (None, None) and rig.h.flash() == flash, 'Close: nothing')

        # ---- Live TV channels against the fake server, typed administrator ----
        approvals, approved_texts = [], []
        rig.dialogs.approve = lambda text, approve_text='Approve', headline=None, cancel_text='Cancel': (
            approved_texts.append((headline, text)), approvals.pop(0) if approvals else False)[1]
        with tempfile.TemporaryDirectory() as root:
            share = fake_server.make_share(root)
            fake = fake_server.Fake(share).start()
            changes = lambda: [c for c in fake.changing() if c[1] != '/Sessions/Logout']  # each job signs its own session out
            try:
                w.server_options = {'saved_available': False, 'fetch': fake_server.fetch, 'choose_folder': lambda: str(share),
                                    'sleep': lambda s: None, 'wait': 1}

                def livetv(dialog):
                    check(dialog.use_typed.isChecked() and not dialog.use_saved.isEnabled(), 'no saved sign-in: typed for this job')
                    dialog.address.setText(fake.url)
                    dialog.user.setText('admin-name')
                    dialog.password.setText('hunter2')
                    dialog._choose()
                    shot(app, dialog, 'livetv-1-start')
                    dialog.dry_run()
                    check(wait(lambda: not dialog.busy) and dialog.plan and dialog.plan.ready and dialog.add.isEnabled()
                          and not changes(), 'dry run: ready, nothing changed on the server')
                    # (the word "movies" now names the library type the file goes into, review M2; the path must not show)
                    check(fake.url not in dialog.lines.toPlainText() and '/media/movies' not in dialog.lines.toPlainText().lower()
                          and 'media/movies' not in dialog.lines.toPlainText().lower().replace('\\', '/'),
                          'the dry run shows no address or server path')
                    shot(app, dialog, 'livetv-2-dry-run')
                    dialog.add_now()  # Approve refused
                    check(not dialog.busy and len(fake.tuners) == 1 and 'Not approved' in dialog.note.text(), 'not approved: nothing')
                    approvals.append(True)
                    dialog.add_now()
                    check(wait(lambda: not dialog.busy) and dialog.results[-1] == ('add', True) and len(fake.tuners) == 2
                          and fake.tuners[1]['FriendlyName'] == sa.TUNER_NAME and 'open Apps' in dialog.note.text(),
                          'Add: one tuner, then told to turn Live TV on in Apps')
                    shot(app, dialog, 'livetv-3-added')
                    approvals.append(True)
                    dialog.remove_now()
                    check(wait(lambda: not dialog.busy) and dialog.results[-1] == ('remove', True) and len(fake.tuners) == 1
                          and fake.tuners[0]['Id'] == 'owner1', 'Remove: exactly that tuner; the owner\'s untouched')
                    dialog.reject()
                w.on_apps_dialog = livetv
                check(w.open_livetv_page() is not None, 'the Live TV page opened')
                temp = [t for t in fake.logouts if t.startswith('TOKEN-temp-chromatic-manager-setup-')]
                check(len(fake.logouts) == 3 == len(temp) and 'TOKEN-den-tv' in fake.tokens and 'TOKEN-companion' in fake.tokens,
                      'each job signed its own temporary session out; no other session ever')
                check(any('Add Live TV channels' in (head or '') and 'changes your Jellyfin server' in text
                          for head, text in approved_texts), 'the Approve says it changes the server')

                # ---- ChromaTube server ----
                files = Path(root) / 'files'
                files.mkdir()
                checks = ['open', 'ok']
                w.server_options = {'saved_available': False, 'choose_folder': lambda: str(files),
                                    'check': lambda host: checks.pop(0)}

                def tube(dialog):
                    from PySide6.QtWidgets import QLabel
                    check(not dialog.host.isEnabled() and not dialog.dry.isEnabled()
                          and any(sa.TERMS == x.text() for x in dialog.findChildren(QLabel)), 'nothing before the terms notice is ticked')
                    shot(app, dialog, 'chromatube-1-terms')
                    dialog.agree.setChecked(True)
                    dialog.host.setText('192.168.1.20')
                    dialog.media.setText('/volume2/docker/chromatube/media')
                    dialog.settings.setText('/volume2/docker/chromatube')
                    dialog.channels.setPlainText('https://www.youtube.com/@SomeChannel\n@Other')
                    dialog.save_files()
                    config = json.loads((files / 'config.json').read_text())
                    kept = sa.saved_signin()
                    check(sorted(p.name for p in files.iterdir()) == ['channel_list.json', 'config.json', 'crons.json',
                                                                     'docker-compose.yml', 'yt_config.json']
                          and json.loads((files / 'yt_config.json').read_text())['cookie_value'] == ''
                          and ':ro' in (files / 'docker-compose.yml').read_text(), 'Save files: the five files')
                    check(kept is not None and config['ytdlp2strm_web_password'] == kept['password']
                          and 'chromatube-server-signin.json' in dialog.note.text() and kept['password'] not in dialog.note.text()
                          and 'firewall step is required' in dialog.note.text(),
                          'Save files: a random page sign-in in config.json and kept on this PC; where it is said (readable '
                          'while private details are hidden), never the sign-in')
                    shot(app, dialog, 'chromatube-2-saved')
                    dialog.check_answers()
                    check(not dialog.check_tool.isEnabled() and 'Asking' in dialog.note.text(),
                          'Check it answers runs off the window\'s thread (the button waits, the window does not; review L8)')
                    check(wait(lambda: 'WITHOUT a sign-in' in dialog.note.text()) and dialog.check_tool.isEnabled(),
                          'the tool open to anyone: said plainly, not "answers"')
                    dialog.check_answers()
                    check(wait(lambda: 'with its sign-in; its settings page asks for it' in dialog.note.text()
                               and 'remove the firewall rule for this PC' in dialog.note.text()),
                          'the tool answers with its sign-in, and the PC\'s temporary firewall rule is to be removed')
                    dialog.address.setText(fake.url)
                    dialog.user.setText('admin-name')
                    dialog.password.setText('hunter2')
                    dialog.find_folder()
                    check(wait(lambda: not dialog.busy) and dialog.server_folder.text() == '/config/ChromaTube', 'Find it')
                    shot(app, dialog, 'chromatube-2-filled')
                    seen = len(changes())
                    dialog.dry_run()
                    check(wait(lambda: not dialog.busy) and dialog.add.isEnabled() and len(changes()) == seen, 'dry run: ready')
                    approvals.append(True)
                    dialog.add_now()
                    check(wait(lambda: not dialog.busy) and dialog.results[-1] == ('add', True)
                          and fake.libraries[-1]['Name'] == 'ChromaTube' and len(fake.libraries) == 3, 'Add library')
                    shot(app, dialog, 'chromatube-3-added')
                    approvals.append(True)
                    dialog.remove_now()
                    check(wait(lambda: not dialog.busy) and dialog.results[-1] == ('remove', True) and len(fake.libraries) == 2,
                          'Remove library')
                    # review L1 of 1.6.4: a library this PC has no record of: looked at first, then its own Approve
                    approvals.append(True)
                    dialog.add_now()
                    check(wait(lambda: not dialog.busy) and len(fake.libraries) == 3, 'added again')
                    sa.CHROMATUBE_STATE.unlink()
                    approvals.append(True)
                    dialog.remove_now()
                    check(wait(lambda: not dialog.busy) and dialog.results[-1] == ('remove', True) and len(fake.libraries) == 2,
                          'no record on this PC, a ChromaTube folder, approved: removed')
                    fake.libraries.append({'Name': 'ChromaTube', 'ItemId': 'lib-x', 'CollectionType': 'tvshows',
                                           'Locations': ['/media/shows'], 'LibraryOptions': {}})
                    results = len(dialog.results)
                    dialog.remove_now()
                    check(wait(lambda: not dialog.busy) and len(dialog.results) == results and len(fake.libraries) == 3
                          and 'does not look like a ChromaTube folder' in dialog.note.text(),
                          'no record and not a ChromaTube folder: refused before any Approve, nothing changed')
                    fake.libraries.pop()
                    dialog.reject()
                w.on_apps_dialog = tube
                check(w.open_chromatube_page() is not None, 'the ChromaTube page opened')
                check(any('Terms of Service' in text for _, text in approved_texts), 'the library\'s Approve repeats the terms notice')
            finally:
                fake.stop()
        w.close()
    print(f'PASS: Settings > Apps in the window: the defaults shown, TV with Live TV and ChromaTube chosen, Approve, written '
          f'and kept on this PC; Approve cancelled; Use defaults; Close. Live TV channels and ChromaTube server pages against a '
          f'fake Jellyfin: typed administrator for each job, dry runs change nothing and show no address or path, Add and '
          f'Remove each after their own Approve, each job\'s temporary session signed out and no other; the terms notice gates '
          f'ChromaTube; its five files saved. Previews in {OUT.relative_to(ROOT)}. Simulated console, no network, no hardware.')


if __name__ == '__main__':
    main()

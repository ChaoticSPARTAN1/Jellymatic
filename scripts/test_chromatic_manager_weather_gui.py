# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Chromatic Manager window: Settings > Weather location (launcher 1.6.3+) against the simulated
console of test_chromatic_manager.py. The place is looked up in the bundled Census table and
completed from the RECORDED api.weather.gov replies of chromatic/weather/fixtures/places/ (no
network); the write runs the real job through the window with a scripted Approve. Also writes
the dialog's previews (staging/chromatic-manager-app/previews-weather/). Prints pass/fail only.

Run: private/chromatic-manager-gui/venv/Scripts/python.exe -B scripts/test_chromatic_manager_weather_gui.py
"""
import json
import os
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_QPA_FONTDIR', os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts'))

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

import chromatic_manager as cm
import chromatic_manager_gui as gui
import chromatic_weather_location as wl
from compose_chromatic_image import stock_dump
from test_chromatic_manager import check
from test_chromatic_manager_gui import Rig

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'chromatic' / 'weather' / 'fixtures' / 'places' / 'denver'
OUT = ROOT / 'staging' / 'chromatic-manager-app' / 'previews-weather'
PRIVATE = ('DENVER', 'BROOMFIELD', 'KBJC', '39.7', '104.9')


def recorded(path):
    index = json.loads((FIX / 'index.json').read_text('utf-8'))
    if path.startswith('/points/'):
        path = next(p for p in index if p.startswith('/points/'))
    return json.loads((FIX / index[path]).read_text('utf-8'))


def resolver(found):
    return wl.resolve(found, recorded)


def wait(condition, seconds=10):
    loop = QEventLoop()
    timer = QTimer()
    timer.timeout.connect(lambda: condition() and loop.quit())
    timer.start(20)
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec()
    timer.stop()
    return condition()


def run_page(rig, drive, job_seconds=120):
    """Open Settings > Weather location; `drive(dialog)` acts in it while it is open; then wait for
    the job it may start. Returns (choice, exit code or None)."""
    window, result, loop = rig.window, {}, QEventLoop()

    def done(code):
        result['code'] = code
        loop.quit()
    window.finished.connect(done)
    QTimer.singleShot(0, lambda: drive(window.weather_dialog))
    try:
        choice = window.open_weather_page()
        if window.busy():
            QTimer.singleShot(job_seconds * 1000, loop.quit)
            loop.exec()
    finally:
        window.finished.disconnect(done)
    return choice, result.get('code')


def main():
    app = QApplication.instance() or QApplication([])
    OUT.mkdir(parents=True, exist_ok=True)
    stock, release = stock_dump(allow_synthetic=True), cm.Release.load()
    from test_chromatic_manager import Harness
    h = Harness(stock, release)
    check(h.manager().install() == 0, 'install for the start')
    installed = h.flash()
    saved_path = cm.LOCATION_FILE
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        cm.LOCATION_FILE = Path(folder) / 'weather-location.json'
        try:
            rig = Rig(installed, release, stock, confirms=['yes', 'no', 'yes'])
            w = rig.window
            w.weather_options = {'resolver': resolver}
            w.show_tab('settings')
            default = lambda: wl.default_name(w.console_launcher())  # 1.6.4+ (or not checked yet): Orlando, FL
            check(w.weather_state.text() == default() + ' (default).' and w.weather_open.text() == 'Weather location',
                  f'Settings: {default()} (default) and the Weather location button')
            w.service.summary['launcher'] = '1.6.3'
            w.refresh_weather_state()
            check(w.weather_state.text() == 'its built-in place (default).',
                  'Settings under launcher 1.6.3 (no weather migration on this PC): its built-in place (default)')
            w.service.summary['launcher'] = '1.6.4'
            w.refresh_weather_state()
            check(w.weather_state.text() == 'Orlando, FL (default).', 'Settings under launcher 1.6.4: Orlando, FL (default)')

            seen = {}

            def choose(dialog, approve=True):
                check(dialog is not None and dialog.now.text() == f'Now: {default()} (default)' and not dialog.write.isEnabled()
                      and dialog.default.text() == 'Use default (Orlando, FL)',
                      'the page opens on the default, Save and write greyed until a place is checked')
                dialog.query.setText('Denver, CO')
                dialog.find_now()
                check(len(dialog.found) >= 1 and dialog.found[0].name == 'DENVER, CO', 'Find: the table\'s match')
                app.processEvents()
                dialog.grab().save(str(OUT / 'weather-location-1-found-hidden.png'))
                dialog.pick(dialog.found[0])
                check(wait(lambda: dialog.place is not None and not dialog.busy), 'the place is completed from the replies')
                details = dialog.details.text()
                seen['hidden'] = details
                check('DENVER' not in details and 'BROOMFIELD' not in details and 'US Mountain' in details
                      and 'Regional Forecast' in details and dialog.write.isEnabled(),
                      'hidden private details: the place and stations masked, zone and pages shown')
                dialog.adjustSize()
                app.processEvents()
                dialog.grab().save(str(OUT / 'weather-location-2-checked-hidden.png'))
                dialog._toggle_privacy()
                seen['shown'] = dialog.details.text()
                check('DENVER, CO' in seen['shown'] and 'BROOMFIELD' in seen['shown'], 'shown on request: the place to confirm')
                app.processEvents()
                dialog.grab().save(str(OUT / 'weather-location-3-checked-shown.png'))
                dialog._toggle_privacy()
                dialog._write()

            choice, code = run_page(rig, choose)
            check(choice == 'write' and code == 0, f'Save and write: Approve, the job runs (exit {code})')
            on_console = wl.current(cm.span(rig.h.flash(), cm.LOCATION_A), cm.span(rig.h.flash(), cm.LOCATION_B))[0]
            check(on_console is not None and on_console.name == 'DENVER, CO' and wl.load_saved(cm.LOCATION_FILE) == on_console,
                  'written to the console and saved on this PC')
            check(w.weather_state.text().startswith('Saved on this PC: ') and 'DENVER' not in w.weather_state.text(),
                  'Settings shows a saved place, masked')
            log = '\n'.join(rig.h.log) + rig.log_text() + '\n'.join(w.log_lines)
            check(not any(p in log.upper() for p in PRIVATE) and 'your location' in log,
                  'the log says "your location", never the place')
            # Approve cancelled: nothing written and the PC keeps its earlier choice
            before = rig.h.flash()

            def cancel(dialog):
                dialog._use_default()
            choice, code = run_page(rig, cancel)
            check(choice == 'default' and code is None and rig.h.flash() == before and not cm.LOCATION_PENDING.exists()
                  and wl.load_saved(cm.LOCATION_FILE) == on_console, 'Approve cancelled: nothing written, the saved place kept')
            # Use default, approved
            choice, code = run_page(rig, cancel)
            check(choice == 'default' and code == 0 and cm.span(rig.h.flash(), cm.LOCATION) == b'\xff' * 8192
                  and wl.load_saved(cm.LOCATION_FILE) is None and w.weather_state.text() == default() + ' (default).',
                  'Use default: erased on the console and forgotten on this PC')
            # Close: nothing
            def close(dialog):
                dialog.reject()
            flash = rig.h.flash()
            check(run_page(rig, close) == (None, None) and rig.h.flash() == flash, 'Close: nothing saved or written')
            # a lookup that finds nothing, and a weather service that does not answer
            def nothing(dialog):
                dialog.query.setText('Springfield')
                dialog.find_now()
                check(len(dialog.found) == 6 and 'More places match: add the state, for example "Springfield, ' in dialog.note.text(),
                      'an ambiguous town: six shown and the add-the-state hint')
                dialog.grab().save(str(OUT / 'weather-location-0-more-places.png'))
                dialog.query.setText('Nowhere Special, ZZ')
                dialog.find_now()
                check(not dialog.found and dialog.note.isVisible() and not dialog.write.isEnabled(), 'no match: said, nothing to write')
                dialog.weather_failed = True
                dialog.resolver = lambda found: (_ for _ in ()).throw(wl.LocationError('The weather service did not answer.'))
                dialog.query.setText('80202')
                dialog.find_now()
                dialog.pick(dialog.found[0])
                check(wait(lambda: not dialog.busy) and dialog.place is None and not dialog.write.isEnabled()
                      and 'did not answer' in dialog.note.text(), 'weather service down: said, nothing to write')
                dialog.grab().save(str(OUT / 'weather-location-4-no-answer.png'))
                dialog.reject()
            check(run_page(rig, nothing) == (None, None), 'failed look-ups: nothing saved or written')
            # ---- 1.6.5: the area pages: made, previewed (the console's own drawing), exported, imported, written ----
            import chromatic_location_pack as lp
            import chromatic_weather_preview as wp
            from test_chromatic_manager_weather import denver_pack
            _, pack = denver_pack()
            export = Path(folder) / 'pages.pjlp'
            saved_pack = cm.PACK_FILE
            cm.PACK_FILE = Path(folder) / 'weather-pack.pjlp'
            lp_saved = lp.SAVED
            lp.SAVED = cm.PACK_FILE
            from dataclasses import replace as _replace
            made = {}

            def maker(place):  # the sample pack, bound to the place the page completed (its grid may differ by a cell)
                made['pack'] = _replace(pack, state=place.state, lat_e4=place.lat_e4, lon_e4=place.lon_e4, office=place.office,
                                        grid_x=place.grid_x, grid_y=place.grid_y)
                return made['pack']
            w.weather_options = {'resolver': resolver, 'maker': maker,
                                 'pick_file': lambda: str(export), 'save_file': lambda: str(export)}

            def pages(dialog):
                dialog.query.setText('Denver, CO')
                dialog.find_now()
                check(not dialog.make_pages.isEnabled(), 'Make the area pages waits for a checked place')
                dialog.pick(dialog.found[0])
                check(wait(lambda: dialog.place is not None and not dialog.busy) and dialog.make_pages.isEnabled()
                      and not dialog.export_pages.isEnabled(), 'a checked place: Make the area pages offered')
                dialog.make_now()
                check(wait(lambda: dialog.pack is not None and not dialog.busy, 60) and dialog.pack.binds(dialog.place),
                      'the area pages are made for that place')
                shots = getattr(dialog, 'preview_shots', [])
                kinds = {name.split('-', 1)[1].rsplit('-', 1)[0] for name, _ in shots}
                check(len(shots) >= 10 and {'regional-state', 'regional-close', 'current-radar'} <= kinds
                      and not dialog.preview.isVisible(), f'every page previewed by the console\'s own drawing, the maps '
                                                          f'and radar among them; hidden while private details are ({len(shots)})')
                for name, data in shots:
                    if any(k in name for k in ('ident', 'regional', 'radar')):
                        (OUT / f'weather-pages-preview-{name}.png').write_bytes(wp.png(data))
                dialog._toggle_privacy()
                app.processEvents()
                check(dialog.preview.isVisible(), 'the preview shows with private details shown')
                dialog.adjustSize()
                app.processEvents()
                dialog.grab().save(str(OUT / 'weather-location-5-area-pages.png'))
                dialog._toggle_privacy()
                dialog.export_now()
                pack_made = made['pack']
                check(lp.load_file(export) == pack_made, 'Export pages: the file holds the pack, checked')
                dialog.pack = None
                dialog.import_now()
                check(dialog.pack == pack_made, 'Import pages: the file read back and checked for this place')
                dialog._write()
            w.dialogs.confirms[:] = ['yes']
            choice, code = run_page(rig, pages)
            flash = rig.h.flash()
            on_console = wl.current(cm.span(flash, cm.LOCATION_A), cm.span(flash, cm.LOCATION_B))[0]
            check(choice == 'write' and code == 0 and on_console is not None and on_console.name == 'DENVER, CO'
                  and lp.current(cm.span(flash, cm.PACK_A), cm.span(flash, cm.PACK_B))[0] == made['pack']
                  and lp.load_saved(cm.PACK_FILE) == made['pack'] and not cm.PACK_PENDING.exists(),
                  'Save and write: the place and its pages written in one job, then kept on this PC')
            # ---- the first install's question ----
            w.service.summary['state'] = 'stock'
            wl.save(None, cm.LOCATION_FILE)
            w.dialogs.first_weather = lambda window: ('skip',)
            check(w.first_weather_choice() == ('skip',), 'a console without the menu: "Add Local Weather?" is asked')
            w.dialogs.first_weather = lambda window: None
            check(w.first_weather_choice() is False and not w.start('install') and 'cancelled' in w.log_lines[-1],
                  'the question closed: the install is not started')
            w.service.summary['state'] = 'installed'
            check(w.first_weather_choice() is None, 'an installed console is not asked')
            del w.dialogs.first_weather
            first = gui.FirstWeatherDialog(w)
            first.show()
            app.processEvents()
            first.grab().save(str(OUT / 'first-install-add-local-weather.png'))
            first._pick('default')
            check(first.answer == 'default', 'the question\'s buttons: place, Orlando, skip')
            cm.PACK_FILE, lp.SAVED = saved_pack, lp_saved
            w.close()
        finally:
            cm.LOCATION_FILE = saved_path
    print(f'PASS: Settings > Weather location in the window: find, check (masked until shown), Approve, written and '
          f'saved; Approve cancelled keeps the earlier choice; Use default; Close; no match; weather service down; the '
          f'log never names the place; 1.6.5: Make the area pages (every page previewed by the console\'s drawing, '
          f'hidden with private details), export, import, written with the place in one job; the first install\'s '
          f'"Add Local Weather?". Previews in {OUT.relative_to(ROOT)}. Simulated console, no network, no hardware.')


if __name__ == '__main__':
    main()

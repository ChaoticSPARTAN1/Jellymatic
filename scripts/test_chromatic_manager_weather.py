# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Chromatic Manager's weather location (launcher 1.6.3+) and its area pages (location packs, launcher 1.6.5+) on the
simulated console of test_chromatic_manager.py (the real stock backup and the pinned launcher build; no hardware).

Write, verify on the chip, write again, use default; a power cut at every write and the tool dying
mid-job (finished by Finish job, the reader always sees the old place, the new one or the default);
install, update and downgrade keep the location; an install after the location was wiped (by an
older Manager or a removal) writes the saved copy back; switch-off keeps it, remove and restore erase
it; refusals on stock; status says whether, never which; nothing personal in the log, closed journals
or backups; the job through the window's core (run_action) and the text menu.

1.6.5: a place's area pages (a location pack made by make_location_pack.py from recorded public replies for Denver,
CO) are written with the location in one job, erased by Use default, refused for another place, written back at an
install from this PC's saved copy, imported and exported as a file. A PC's private weather migration (here a sample
file in the sandbox) gives a console whose older start-up menu showed a place of its own that place and its pages in
the same install job and Approve, announced before any write, never over anything already there. A first install asks
"Add Local Weather?" before its Approve: a place (with its pages), the default, or skip (the TV row hidden). Prints
pass/fail only.
"""
import contextlib
import json
import re
import tempfile
import types
from pathlib import Path

import test_chromatic_manager as t  # the sandbox: no private files are read or written
import chromatic_location_pack as lp
import chromatic_manager as cm
import chromatic_manager_core as core
import chromatic_weather_location as wl
import make_location_pack as mk
from chromatic_trial_runner import EXIT_OK, EXIT_RECOVERED, EXIT_REFUSED
from compose_chromatic_image import stock_dump

check, Harness = t.check, t.Harness
ROOT = Path(__file__).resolve().parents[1]
DENVER = wl.Place('DENVER, CO', 'CO', 397392, -1049903, -420, 1, 'BOU', 63, 62,
                  (wl.Station('KBJC', 'BROOMFIELD'), wl.Station('KDEN', 'DENVER')), 'America/Denver')
SECOND = wl.Place('WINTER PARK, FL', 'FL', 286000, -813392, -300, 1, 'MLB', 27, 71, (wl.Station('KORL', 'ORLANDO EXEC'),))
PRIVATE = ('DENVER', 'WINTER PARK', 'BROOMFIELD', 'KBJC', '39.7', '104.9', '28.60', '81.33')


def location(image):
    return wl.current(cm.span(image, cm.LOCATION_A), cm.span(image, cm.LOCATION_B))


def pack_of(image):
    return lp.current(cm.span(image, cm.PACK_A), cm.span(image, cm.PACK_B))


def outside_location_same(a, b):
    lo = cm.PACK[0]
    return a[:lo] == b[:lo] and a[cm.LOCATION[1]:] == b[cm.LOCATION[1]:] and cm.span(a, cm.APPS) == cm.span(b, cm.APPS)


def clean(lines, name):
    bad = [l for l in lines if any(p in l.upper() for p in PRIVATE)]
    check(not bad, f'{name}: the place never appears in the log ({bad[:1]})')


def journals_clean(h, name):
    for path in (h.folder / 'work' / 'closed').glob('*/image-*.bin'):
        data = path.read_bytes()
        check(len(data) != cm.SIZE or (cm.span(data, cm.LOCATION) == b'\xff' * (cm.LOCATION[1] - cm.LOCATION[0])
                                      and cm.span(data, cm.PACK) == b'\xff' * (cm.PACK[1] - cm.PACK[0])),
              f'{name}: a closed journal image keeps the weather location or its pages')


def denver_pack():
    """Denver's area pages, made from the recorded public replies (chromatic/weather/fixtures/packs/denver)."""
    fix = ROOT / 'chromatic' / 'weather' / 'fixtures' / 'packs' / 'denver'
    index = json.loads((fix / 'index.json').read_text('utf-8'))
    radar = json.loads((fix / 'radar.json').read_text('utf-8'))
    fetch = lambda path: json.loads((fix / index[path]).read_text('utf-8'))
    place = wl.resolve(wl.find('Denver, CO')[0], fetch)
    return place, mk.make(place, fetch, lambda p: radar.get(p, False))


def migration_and_packs(stock, release):
    """The location pack jobs, this PC's weather migration and the first-install question."""
    from test_chromatic_manager_apps import relabel
    r163, r164, r165, r151 = (relabel(release, v) for v in ('1.6.3', '1.6.4', '1.6.5', '1.5.1'))
    place, pack = denver_pack()
    check(pack.binds(place) and not pack.binds(SECOND) and pack.size() <= lp.COPY, 'the sample pack belongs to its place')
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        keep = (cm.LOCATION_FILE, cm.PACK_FILE, cm.MIGRATION_FILE, lp.SAVED, wl.MIGRATION)
        cm.LOCATION_FILE, cm.PACK_FILE = folder / 'weather-location.json', folder / 'weather-pack.pjlp'
        cm.MIGRATION_FILE = wl.MIGRATION = folder / 'migration.json'
        lp.SAVED = cm.PACK_FILE
        try:
            # ---- the weather location job with its pages ----
            h = Harness(stock, r165)
            check(h.manager().install() == EXIT_OK and location(h.flash()) == (None, 0) and pack_of(h.flash()) == (None, 0),
                  'a new console: no location, no pages')
            installed = h.flash()
            h = Harness(installed, r165, stock)
            m = h.manager()
            check(m.weather_location(place, pack) == EXIT_OK and location(h.flash())[0] == place and pack_of(h.flash())[0] == pack
                  and cm.span(h.flash(), cm.PACK_A) == cm.span(h.flash(), cm.PACK_B) == pack.sector(1)
                  and outside_location_same(h.flash(), installed) and any('with its area pages' in l for l in h.log),
                  'a place with its pages: both written in one job (both copies each), nothing else changed')
            writes = h.console.writes
            check(m.weather_location(place, pack) == EXIT_OK and h.console.writes == writes, 'the same again: nothing written')
            check(m.status() == EXIT_OK and any('your location with its area pages' in l for l in h.log),
                  'status: a location with its pages (never which)')
            check(m.weather_location(place, None) == EXIT_OK and location(h.flash())[0] == place and pack_of(h.flash()) == (None, 0)
                  and cm.span(h.flash(), cm.PACK) == b'\xff' * 16384, 'the place without pages: the pages erased')
            check(m.weather_location(SECOND, pack) == EXIT_REFUSED and location(h.flash())[0] == place,
                  'pages made for another place: refused, nothing written')
            m.weather_location(place, pack)
            check(m.weather_location(None) == EXIT_OK and h.flash() == installed, 'Use default: the location and its pages erased')
            journals_clean(h, 'location with pages')
            # remove and restore erase the pages; backups never keep them
            h.manager().weather_location(place, pack)
            check(cm.span(cm.without_setup(h.flash()), cm.PACK) == b'\xff' * 16384, 'backups and closed journals leave the pages out')
            check(h.manager().remove() == EXIT_OK and cm.span(h.flash(), cm.PACK) == b'\xff' * 16384, 'remove erases the pages')
            # an install writes this PC's saved place and pages back when the console lost them
            wl.save(place, cm.LOCATION_FILE)
            lp.save(pack, cm.PACK_FILE)
            h = Harness(stock, r165, stock)
            check(h.manager().install() == EXIT_OK and location(h.flash())[0] == place and pack_of(h.flash())[0] == pack
                  and any('with its area pages' in l for l in h.log), 'an install writes the saved place and its pages back')
            wl.save(None, cm.LOCATION_FILE)
            lp.save(None, cm.PACK_FILE)
            # ---- this PC's weather migration ----
            pack_file = folder / 'owner.pjlp'
            lp.save_file(pack, pack_file)
            cm.MIGRATION_FILE.write_text(json.dumps({'showed': ['1.6.3'], 'showed_with_record': ['1.6.4'],
                                                     'place': place.to_json(), 'pack': pack_file.name}), 'utf-8')
            h = Harness(stock, r163)
            h.manager().install()
            owner_163 = h.flash()
            check(location(owner_163) == (None, 0), 'migration: a 1.6.3 console with no location')
            h = Harness(owner_163, r165, stock)
            m = h.manager()
            said, jobs = [], []
            say = m.say
            m.say = lambda line: (said.append((line, h.console.writes)), say(line))
            run_job = m.run_job
            m.run_job = lambda kind, *a, **k: (jobs.append(kind), run_job(kind, *a, **k))[1]
            check(m.install() == EXIT_OK and jobs == ['install'] and location(h.flash())[0] == place and pack_of(h.flash())[0] == pack,
                  'migration 1.6.3 -> 1.6.5: the place and its pages in the one install job')
            after = h.flash()
            announce = [n for n, (line, w) in enumerate(said) if 'its weather location and its area pages' in line and w == 0]
            check(announce and 'shows Orlando, FL by default' in said[announce[0]][0] and 'choose Games' in said[-1][0]
                  and said[-1][1] == h.console.writes, 'migration: announced before any write; "choose Games" said last')
            check(wl.load_saved(cm.LOCATION_FILE) == place and lp.load_saved(cm.PACK_FILE) == pack,
                  'migration: this PC keeps the place and its pages as its saved copies')
            h2 = Harness(after, r163, stock)
            check(h2.manager().install() == EXIT_OK and location(h2.flash())[0] == place and pack_of(h2.flash())[0] == pack,
                  'migration: downgrade to 1.6.3 keeps both (it ignores the pages)')
            h2 = Harness(h2.flash(), r165, stock)
            check(h2.manager().install() == EXIT_OK and h2.flash() == after, 'migration: back to 1.6.5, byte for byte')
            wl.save(None, cm.LOCATION_FILE)
            lp.save(None, cm.PACK_FILE)
            # 1.6.4 showed the place from its own tables (its record there): 1.6.5 adds the pages only
            with164 = wl.with_place(cm.put(owner_163, cm.SLOT_APP, cm.span(cm.install_target(owner_163, r164), cm.SLOT_APP)),
                                    place, cm.LOCATION_A, cm.LOCATION_B)
            h2 = Harness(with164, r165, stock)
            check(h2.manager().install() == EXIT_OK and location(h2.flash()) == location(with164) and pack_of(h2.flash())[0] == pack,
                  'migration 1.6.4 -> 1.6.5: the record kept as it is, its pages added')
            wl.save(None, cm.LOCATION_FILE)
            lp.save(None, cm.PACK_FILE)
            # a release that does not read pages: the record only
            h2 = Harness(owner_163, r164, stock)
            check(h2.manager().install() == EXIT_OK and location(h2.flash())[0] == place and pack_of(h2.flash()) == (None, 0),
                  'migration to 1.6.4: the record only (it reads no pages)')
            wl.save(None, cm.LOCATION_FILE)
            # never over anything there; a saved place wins; nothing for consoles that did not show the place
            damaged_pack = cm.put(owner_163, cm.PACK_A, b'PJLP' + bytes(60))
            for name, image in (('another place on the console', wl.with_place(owner_163, SECOND, cm.LOCATION_A, cm.LOCATION_B)),
                                ('a damaged record', cm.put(owner_163, cm.LOCATION_A, b'PJWX' + b'\x00' * 252))):
                h2 = Harness(image, r165, stock)
                check(h2.manager().install() == EXIT_OK and cm.span(h2.flash(), cm.LOCATION) == cm.span(image, cm.LOCATION)
                      and pack_of(h2.flash()) == (None, 0) and wl.load_saved(cm.LOCATION_FILE) is None,
                      f'migration: {name}: never overwritten')
            h2 = Harness(damaged_pack, r165, stock)
            check(h2.manager().install() == EXIT_OK and cm.span(h2.flash(), cm.PACK) == cm.span(damaged_pack, cm.PACK)
                  and location(h2.flash())[0] == place, 'migration: damaged pages on the console are never overwritten')
            wl.save(None, cm.LOCATION_FILE)
            lp.save(None, cm.PACK_FILE)
            wl.save(SECOND, cm.LOCATION_FILE)
            h2 = Harness(owner_163, r165, stock)
            check(h2.manager().install() == EXIT_OK and location(h2.flash())[0] == SECOND and pack_of(h2.flash()) == (None, 0),
                  'migration: the place saved on this PC wins')
            wl.save(None, cm.LOCATION_FILE)
            h2 = Harness(stock, r151)
            h2.manager().install()
            for name, start in (('a new console', stock), ('1.5.1', h2.flash())):
                h3 = Harness(start, r165, stock)
                h3.manager().first_weather = lambda: None
                check(h3.manager().install() == EXIT_OK and cm.span(h3.flash(), cm.LOCATION) == b'\xff' * 8192,
                      f'migration: {name}: nothing (it never showed the place)')
            check(wl.default_name('1.6.3', cm.MIGRATION_FILE) == 'Denver, CO' and wl.default_name('1.6.2', cm.MIGRATION_FILE) ==
                  'its built-in place' and wl.default_name('1.6.5', cm.MIGRATION_FILE) == 'Orlando, FL',
                  'the default a console shows: its older launcher\'s place from this PC\'s migration, else Orlando, FL')
            cm.MIGRATION_FILE.unlink()
            check(wl.default_name('1.6.3', cm.MIGRATION_FILE) == 'its built-in place', 'no migration file: no place named')
            # ---- the first install's question ----
            for answer, want_place, want_pack, want_tv in ((('place', place, pack), place, pack, True),
                                                            (('place', place, None), place, None, True),
                                                            (('default',), None, None, True), (('skip',), None, None, False),
                                                            (None, None, None, True)):
                import chromatic_apps as ap
                wl.save(None, cm.LOCATION_FILE)
                lp.save(None, cm.PACK_FILE)
                ap.save(None, cm.APPS_FILE)
                cm.pending_first_weather(answer)
                h = Harness(stock, r165)
                m = h.manager()
                said = []
                say = m.say
                m.say = lambda line, said=said, h=h: (said.append((line, h.console.writes)), say(line))
                check(m.install() == EXIT_OK and location(h.flash())[0] == want_place and pack_of(h.flash())[0] == want_pack
                      and not cm.FIRST_PENDING.exists(), f'first install, {answer[0] if answer else "nothing asked"}: written as chosen')
                import chromatic_apps as ap
                flags, _ = ap.current(cm.span(h.flash(), cm.APPS_A), cm.span(h.flash(), cm.APPS_B))
                check(((flags is None) or bool(flags & ap.TV)) == want_tv, f'first install, {answer[0] if answer else "-"}: TV row')
                if answer and answer[0] != 'default':
                    words = 'your weather location' if answer[0] == 'place' else 'Local Weather skipped'
                    check(any(words in l and w == 0 for l, w in said), f'first install, {answer[0]}: said before any write')
            # not a first install: never asked
            asked = []
            h = Harness(installed, r165, stock)
            m = h.manager()
            m.first_weather = lambda: asked.append(1) or ('skip',)
            check(m.install() == EXIT_OK and not asked, 'an installed console is never asked again')
            # review L3 of 1.6.5: an answer never waits for a later install: removed when an install stops early, and
            # an old one (over FIRST_PENDING_SECONDS) is never used
            import time as _time
            cm.pending_first_weather(('skip',))
            h = Harness(stock, r165)
            m = h.manager()
            m.release_loader = lambda: (_ for _ in ()).throw(cm.Refused('no release (test)'))
            check(m.install() != EXIT_OK and not cm.FIRST_PENDING.exists() and h.console.writes == 0,
                  'an install that stops before the question removes the window\'s answer (L3)')
            cm.pending_first_weather(('skip',))
            body = json.loads(cm.FIRST_PENDING.read_text(encoding='utf-8'))
            body['made'] = _time.time() - cm.FIRST_PENDING_SECONDS - 5
            cm.FIRST_PENDING.write_text(json.dumps(body), encoding='utf-8')
            ap.save(None, cm.APPS_FILE)
            h = Harness(stock, r165)
            check(h.manager().install() == EXIT_OK and ap.current(cm.span(h.flash(), cm.APPS_A), cm.span(h.flash(), cm.APPS_B))[0] is None
                  and not cm.FIRST_PENDING.exists(), 'an answer older than ten minutes is not used (the defaults) and is removed (L3)')
            # review L5 of 1.6.5: the page renderer is used only when verified (its own sha256 and the sources it was
            # built from); the app uses only the one beside it
            import sys as _sys
            import chromatic_weather_preview as wp
            if Path(wp.GCC).exists():
                app = folder / 'app'
                exe = wp.build(app / 'wx_preview.exe')
                good = exe is not None and wp.verified(exe) == exe
                frozen = getattr(_sys, 'frozen', None), _sys.executable
                try:
                    _sys.frozen, _sys.executable = True, str(app / 'ChromaticManager.exe')
                    as_app = wp.renderer()
                    record = json.loads((app / 'wx_preview.json').read_text(encoding='utf-8'))
                    (app / 'wx_preview.json').write_text(json.dumps(dict(record, sources='0' * 64)), encoding='utf-8')
                    stale = wp.renderer()
                    (app / 'wx_preview.json').write_text(json.dumps(record), encoding='utf-8')
                    data = bytearray(exe.read_bytes())
                    data[-1] ^= 1
                    exe.write_bytes(bytes(data))
                    swapped = wp.renderer()
                    (app / 'wx_preview.json').unlink()
                    unrecorded = wp.renderer()
                finally:
                    if frozen[0] is None:
                        del _sys.frozen
                    else:
                        _sys.frozen = frozen[0]
                    _sys.executable = frozen[1]
                check(good and as_app == exe and stale is None and swapped is None and unrecorded is None,
                      'the page renderer: the app uses only its own, verified by sha256 and by the sources it was made from; '
                      'one from older sources, changed or without its record is refused (L5)')
            else:
                print('INFO no gcc on this PC: the renderer check is skipped')
            # review L2 of 1.6.5: the text menu asks "Add Local Weather?" before the typed confirmation, then installs
            asked_order = []

            def menu_person(prompt):
                asked_order.append('confirm' if re.search(r'Type (yes\d{3})', prompt) else 'weather' if 'Add Local Weather' in prompt
                                   else 'choose' if prompt.startswith('Choose') else prompt[:20])
                word = re.search(r'Type (yes\d{3})', prompt)
                return word.group(1) if word else '3' if prompt.startswith('Choose') else 'skip'
            ap.save(None, cm.APPS_FILE)
            h = Harness(stock, r165)
            check(cm.menu(h.manager(), menu_person) == EXIT_OK and asked_order == ['choose', 'weather', 'confirm']
                  and ap.current(cm.span(h.flash(), cm.APPS_A), cm.span(h.flash(), cm.APPS_B))[0] is not None
                  and not ap.current(cm.span(h.flash(), cm.APPS_A), cm.span(h.flash(), cm.APPS_B))[0] & ap.TV,
                  f'text menu 3: "Add Local Weather?" first, then the typed confirmation, then the install writes the '
                  f'answer (skip: TV hidden) (L2; asked {asked_order})')
            ap.save(None, cm.APPS_FILE)
            wl.save(None, cm.LOCATION_FILE)
            lp.save(None, cm.PACK_FILE)
            # ---- the text menu: import and export a pack, make the pages ----
            h = Harness(installed, r165, stock)
            m = h.manager()

            def person(answers):
                answers = list(answers)

                def ask(prompt):
                    word = re.search(r'Type (yes\d{3})', prompt)
                    return word.group(1) if word else answers.pop(0)
                return ask
            real = wl.resolve
            wl.resolve = lambda found, fetch=None: place
            try:
                check(cm.weather_location_menu(m, person(['Denver, CO', '1', 'yes', 'yes']), maker=lambda p: pack) == EXIT_OK
                      and location(h.flash())[0] == place and pack_of(h.flash())[0] == pack and lp.load_saved(cm.PACK_FILE) == pack,
                      'text menu: the place and its pages made, written, saved')
                out = folder / 'export.pjlp'
                check(cm.weather_location_menu(m, person([f'export {out}'])) == EXIT_OK and lp.load_file(out) == pack,
                      'text menu: export the pages as a file')
                bad = folder / 'bad.pjlp'
                bad.write_bytes(out.read_bytes()[:-1] + b'\x00')
                check(cm.weather_location_menu(m, person([f'import {bad}'])) == EXIT_REFUSED, 'text menu: a damaged pack file is refused')
                m.weather_location(place, None)
                check(cm.weather_location_menu(m, person([f'import {out}'])) == EXIT_OK and pack_of(h.flash())[0] == pack,
                      'text menu: import the pages for the saved place')
                wl.save(SECOND, cm.LOCATION_FILE)
                check(cm.weather_location_menu(m, person([f'import {out}'])) == EXIT_REFUSED, 'text menu: pages for another place refused')
                check(cm.first_weather_menu(person(['skip'])) == ('skip',) and cm.first_weather_menu(person(['default'])) == ('default',)
                      and cm.first_weather_menu(person(['Denver, CO', 'yes']), maker=lambda p: pack) == ('place', place, pack),
                      'text menu: the first-install question')
            finally:
                wl.resolve = real
            check('area pages' in core.BY_KEY['install'].help and 'Add Local Weather' in core.BY_KEY['install'].help,
                  'the install\'s Approve names the weather it may write')
            clean(h.log, 'packs')
        finally:
            cm.LOCATION_FILE, cm.PACK_FILE, cm.MIGRATION_FILE, lp.SAVED, wl.MIGRATION = keep


def main():
    stock = stock_dump(allow_synthetic=True)
    release = cm.Release.load()
    migration_and_packs(stock, release)
    h = Harness(stock, release)
    check(h.manager().install() == EXIT_OK, 'install for the start')
    installed = h.flash()
    # 1.6.5: the location pack (0x3F6000-0x3FA000), then the apps record (0x3FA000-0x3FC000), then the location
    check(cm.SLOT_APP == (0x260000, 0x3F6000) and cm.PACK == (0x3F6000, 0x3FA000) and cm.APPS == (0x3FA000, 0x3FC000)
          and cm.LOCATION == (0x3FC000, 0x3FE000) and cm.LOCATION_A[0] == wl.FLASH_A and cm.LOCATION_B[0] == wl.FLASH_B
          and cm.PACK_A[0] == lp.FLASH_A and cm.PACK_B[0] == lp.FLASH_B and cm.PACK_A[1] - cm.PACK_A[0] == lp.COPY
          and len(release.launcher) <= cm.SLOT_APP[1] - cm.SLOT_APP[0]
          and cm.span(installed, cm.LOCATION) == b'\xff' * 8192, 'layout: the launcher area ends where the location pack '
                                                                'begins, then the apps record, then the location')
    check(location(installed) == (None, 0), 'a console without a location reads as the default')

    # write, verify, the same again, another place, use default
    h = Harness(installed, release, stock)
    m = h.manager()
    check(m.weather_location(DENVER) == EXIT_OK, 'write: done')
    after = h.flash()
    check(location(after) == (DENVER, 1) and cm.span(after, cm.LOCATION_A) == cm.span(after, cm.LOCATION_B) == DENVER.sector(1)
          and outside_location_same(after, installed), 'write: both copies, sequence 1, nothing else changed')
    check((0, cm.SIZE) in h.console.md5_calls and h.console.writes == 2, 'write: two sector writes, checked by the whole-flash MD5')
    check(any('Weather location written' in l and 'your location' in l for l in h.log), 'write: said in plain words')
    writes = h.console.writes
    check(m.weather_location(DENVER) == EXIT_OK and h.console.writes == writes
          and any('already has this weather location' in l for l in h.log), 'the same place again: nothing written')
    check(m.weather_location(SECOND) == EXIT_OK and location(h.flash()) == (SECOND, 2), 'another place: sequence 2')
    check(m.weather_location(None) == EXIT_OK and cm.span(h.flash(), cm.LOCATION) == b'\xff' * 8192
          and h.flash() == installed and any(f'default, {wl.default_name(release.version)}' in l for l in h.log), 'use default: both copies erased (named as the console\'s launcher shows it)')
    check(m.weather_location(None) == EXIT_OK and any('already uses the default' in l for l in h.log), 'default again: nothing')
    clean(h.log, 'write and default')
    journals_clean(h, 'write and default')

    # status: whether, never which; the location sectors are read, not compared
    h = Harness(cm.put(cm.put(installed, cm.LOCATION_A, DENVER.sector(4)), cm.LOCATION_B, DENVER.sector(4)), release, stock)
    m = h.manager()
    check(m.status() == EXIT_OK and 'Weather location: your location (set in Chromatic Manager).' in h.log
          and h.console.full_reads == 0 and (cm.LOCATION[0], 8192) in h.console.part_reads, 'status with a location')
    check(not any('match no image' in l for l in h.log), 'status: the location is not called unknown data')
    h = Harness(installed, release, stock)
    check(h.manager().status() == EXIT_OK and f'Weather location: {wl.default_name(release.version)} (the default).' in h.log
          and set(h.console.part_reads) == t.ALWAYS, 'status without a location: nothing more is read')
    clean(h.log, 'status')

    # power cut at every write, and the tool dying mid-job: finished by Finish job
    for old, new in ((None, DENVER), (DENVER, SECOND), (SECOND, None)):
        for cut in (0, 1):
            start = installed if old is None else cm.put(cm.put(installed, cm.LOCATION_A, old.sector(5)), cm.LOCATION_B, old.sector(5))
            h = Harness(start, release, stock)
            m = h.manager()
            h.console.cut_after = cut
            m.recover = lambda: (_ for _ in ()).throw(SystemExit('PC died'))
            try:
                m.weather_location(new)
            except SystemExit:
                pass
            seen, _ = location(h.flash())
            check(seen in (old, new), f'cut at write {cut + 1} ({old and old.name} -> {new and new.name}): the console reads '
                                      f'the old place, the new one or the default, never anything else')
            check(t.Console(h.flash(), release).boots() == 'launcher', 'a cut location write leaves the menu starting')
            m = h.manager()
            check(m.pending() and m.resume() in (EXIT_OK, EXIT_RECOVERED) and not m.pending(), f'cut at {cut + 1}: finished')
            check(location(h.flash())[0] == new and outside_location_same(h.flash(), start), f'cut at {cut + 1}: the new place, '
                                                                                         'nothing else changed')
            clean(h.log, 'power cut')
            journals_clean(h, 'power cut')

    # install, update and downgrade keep it; switch-off keeps it; remove erases it
    placed = cm.put(cm.put(installed, cm.LOCATION_A, DENVER.sector(3)), cm.LOCATION_B, DENVER.sector(3))
    older = cm.put(placed, cm.SLOT_APP, cm.span(t.with_app(stock, version='1.5.1', project=cm.LAUNCHER_PROJECT), cm.APP).rstrip(b'\xff'))
    check(cm.describe(older, release).name == 'outdated', 'an older launcher with a location reads as outdated')
    h = Harness(older, release, stock)
    check(h.manager().install() == EXIT_OK and h.state() == 'installed' and location(h.flash()) == (DENVER, 3)
          and h.flash() == placed, 'update (or downgrade to the build folder\'s launcher) keeps the location, byte for byte')
    h = Harness(placed, release, stock)
    check(h.manager().disable() == EXIT_OK and location(h.flash()) == (DENVER, 3), 'switch-off keeps the location')
    check(h.manager().install() == EXIT_OK and location(h.flash()) == (DENVER, 3), 'switched on again: still there')
    h = Harness(placed, release, stock)
    check(h.manager().remove() == EXIT_OK and cm.span(h.flash(), cm.LOCATION) == b'\xff' * 8192, 'remove erases it')
    check(h.manager().weather_location(DENVER) == EXIT_REFUSED and any('Install it first' in l for l in h.log),
          'stock console: a place is refused, nothing written')
    # a location left on a plain stock console (an official update, no re-install): Use default clears it
    left = cm.put(cm.put(stock, cm.LOCATION_A, DENVER.sector(9)), cm.LOCATION_B, DENVER.sector(9))
    h = Harness(left, release, stock)
    check(cm.describe(left, release).name == 'stock' and h.manager().weather_location(None) == EXIT_OK
          and h.flash() == stock and h.state() == 'stock', 'stock console: Use default erases the location, exact stock')
    check(h.manager().weather_location(None) == EXIT_OK and any('already uses the default' in l for l in h.log),
          'stock console without a location: nothing written')
    # the next sequence is newer than the copy a cut write would leave (B), even for crafted copies
    odd = cm.put(cm.put(installed, cm.LOCATION_A, DENVER.sector(5)), cm.LOCATION_B, SECOND.sector(0x80000005))
    h = Harness(odd, release, stock)
    check(h.manager().weather_location(DENVER) == EXIT_OK and location(h.flash()) == (DENVER, 0x80000006),
          'two copies 2^31 apart: the new write is newer than B')
    h = Harness(odd, release, stock)
    h.console.cut_after = 1  # A written, B torn
    m = h.manager()
    m.recover = lambda: (_ for _ in ()).throw(SystemExit('PC died'))
    try:
        m.weather_location(DENVER)
    except SystemExit:
        pass
    check(location(h.flash())[0] == DENVER, 'cut after A: the new A wins over the old B')
    h.manager().resume()

    # no two flash records or areas overlap; the console's own constants agree with the Manager's
    records = {'launcher image': cm.SLOT_APP, 'weather location A': cm.LOCATION_A, 'weather location B': cm.LOCATION_B,
               'setup record A': cm.PROVISION_A, 'setup record B': cm.PROVISION_B, 'bootloader': cm.BOOT,
               'partition table': cm.TABLE, 'stock settings': cm.SETTINGS, 'stock firmware': cm.APP,
               'apps record A': cm.APPS_A, 'apps record B': cm.APPS_B,  # 1.6.4
               'location pack A': cm.PACK_A, 'location pack B': cm.PACK_B}  # 1.6.5
    names = sorted(records, key=lambda n: records[n][0])
    clash = [(a, b) for a, b in zip(names, names[1:]) if records[a][1] > records[b][0]]
    check(not clash and all(s % 4096 == 0 and e % 4096 == 0 and s < e for s, e in records.values()),
          f'no two flash records or areas overlap ({clash})')
    check(cm.SLOT_APP[1] == cm.PACK[0] and cm.PACK[1] == cm.APPS[0] and cm.PACK == (cm.PACK_A[0], cm.PACK_B[1])
          and cm.PACK_A[1] == cm.PACK_B[0] and cm.APPS[1] == cm.LOCATION[0] and cm.LOCATION[1] == cm.PROVISION[0]
          and cm.PROVISION[1] == cm.SLOT[1] and cm.APPS == (cm.APPS_A[0], cm.APPS_B[1]) and cm.APPS_A[1] == cm.APPS_B[0]
          and cm.LOCATION == (cm.LOCATION_A[0], cm.LOCATION_B[1]) and cm.LOCATION_A[1] == cm.LOCATION_B[0],
          'the launcher slot is exactly the image area, the pack pair, the apps pair, the location pair and the setup pair')
    apps_h = (Path(cm.__file__).resolve().parents[1] / 'chromatic/launcher/shared/apps_record.h').read_text('utf-8')
    check(int(re.search(r'#define PJ_APPS_FLASH_A (0x[0-9A-Fa-f]+)u', apps_h).group(1), 16) == cm.APPS_A[0]
          and int(re.search(r'#define PJ_APPS_FLASH_B (0x[0-9A-Fa-f]+)u', apps_h).group(1), 16) == cm.APPS_B[0],
          'the console reads the apps record exactly where the Manager writes it')
    root = Path(cm.__file__).resolve().parents[1]
    place_h = (root / 'chromatic/weather/wx_place.h').read_text('utf-8')
    prov_h = (root / 'chromatic/launcher/shared/provision.h').read_text('utf-8')
    slot_h = (root / 'chromatic/launcher/shared/provision_write.h').read_text('utf-8')
    const = lambda text, name: int(re.search(rf'#define {name} (0x[0-9A-Fa-f]+)u', text).group(1), 16)
    lpack_h = (root / 'chromatic/weather/wx_lpack.h').read_text('utf-8')
    check(const(lpack_h, 'WX_LPACK_FLASH_A') == cm.PACK_A[0] == lp.FLASH_A and const(lpack_h, 'WX_LPACK_FLASH_B') == cm.PACK_B[0]
          == lp.FLASH_B and const(lpack_h, 'WX_LPACK_COPY') == lp.COPY == cm.PACK_A[1] - cm.PACK_A[0],
          'the console reads the location pack exactly where the Manager writes it')
    # review L1 of 1.6.5: the two copies (and the location's two copies beside them) are separate write steps
    blank = b'\xff' * cm.SIZE
    both = cm.put(cm.put(cm.put(blank, cm.PACK, b'\x11' * (cm.PACK[1] - cm.PACK[0])), cm.APPS, b'\x22' * 8192),
                  cm.LOCATION, b'\x33' * 8192)
    steps = [(o, o + len(d)) for o, d in cm.ordered_regions(blank, both, True)]
    check(cm.PACK_A in steps and cm.PACK_B in steps and cm.LOCATION_A in steps and cm.LOCATION_B in steps
          and cm.APPS_A in steps and cm.APPS_B in steps and len(steps) == 6,
          'every record copy is its own write step, checked on the chip by itself: pack A, pack B, apps, location (L1)')
    check(const(place_h, 'WX_PLACE_FLASH_A') == cm.LOCATION_A[0] == wl.FLASH_A
          and const(place_h, 'WX_PLACE_FLASH_B') == cm.LOCATION_B[0] == wl.FLASH_B
          and const(slot_h, 'PJ_PROV_FLASH_SLOT') + const(prov_h, 'PJ_PROVISION_OFFSET_A') == cm.PROVISION_A[0]
          and const(slot_h, 'PJ_PROV_FLASH_SLOT') + const(prov_h, 'PJ_PROVISION_OFFSET_B') == cm.PROVISION_B[0],
          'the console reads the location and the setup record exactly where the Manager writes them')
    plans = {p: p.read_text('utf-8') for p in (root / 'docs/Design-notes.md',) if p.exists()}
    claims = [str(p.relative_to(root)) for p, text in plans.items()
              for line in text.splitlines() if re.search(r'0x3F[CD]000', line, re.I)
              and not re.search(r'weather', line, re.I)]
    check(not claims, f'no plan claims the weather location sectors for anything else ({claims})')

    # the PC's saved copy is written back after an install when the console lost it
    with tempfile.TemporaryDirectory() as folder:
        saved_path = cm.LOCATION_FILE
        cm.LOCATION_FILE = Path(folder) / 'weather-location.json'
        try:
            wl.save(DENVER, cm.LOCATION_FILE)

            def install_in_order(image, name):
                """An install with the saved location: ONE job (one journal, one whole-flash check, one boot
                watch), every write before the boot watch, and "choose Games" said last, after every write."""
                h = Harness(image, release, stock)
                m = h.manager()
                said, jobs = [], []
                say = m.say
                m.say = lambda line: (said.append((line, h.console.writes, h.console.in_stub)), say(line))
                run_job = m.run_job
                m.run_job = lambda kind, *a, **k: (jobs.append(kind), run_job(kind, *a, **k))[1]
                check(m.install() == EXIT_OK and h.state() == 'installed' and location(h.flash())[0] == DENVER,
                      f'{name}: installed with the saved location')
                games = [n for n, (line, _, _) in enumerate(said) if 'choose Games' in line]
                check(jobs == ['install'] and len(games) == 1 and games[0] == len(said) - 1
                      and said[games[0]][1] == h.console.writes and 'Your weather location was written with it.' in said[games[0]][0]
                      and any('this install writes your location too' in line for line, _, _ in said[:games[0]]),
                      f'{name}: one install job; "choose Games" is the last line, after every write ({jobs}, {said[-1][0]})')
                check(not any(line.startswith(('Writing step', 'Erasing step')) for line, _, _ in said[games[0]:])
                      and not h.console.in_stub, f'{name}: nothing written or reset after "choose Games"')
                clean(h.log, name)
                return h
            # an older Manager's install padded the launcher area to 0x3FE000: the location went
            install_in_order(cm.put(placed, (cm.SLOT[0], 0x3FE000), release.launcher), 'already installed, location lost')
            h = install_in_order(stock, 'install on stock (after a removal)')
            writes = h.console.writes
            check(h.manager().install() == EXIT_OK and h.console.writes == writes
                  and any('already installed. Nothing written' in l for l in h.log), 'nothing more when it is already there')
            check('weather location' in core.BY_KEY['install'].help, 'the install\'s Approve names the location write')
            # a cut install with the location is undone as a whole (the install's own recovery)
            h = Harness(stock, release, stock)
            t.crash_mid_install(h, 1)
            m = h.manager()
            check(m.pending() and m.resume() in (EXIT_OK, EXIT_RECOVERED) and h.state() == 'stock'
                  and cm.span(h.flash(), cm.LOCATION) == b'\xff' * 8192, 'a cut install with the location is undone whole')
            # the window's job: the saved copy, or the default when none is saved
            h = Harness(installed, release, stock)
            lock, fpga = contextlib.nullcontext(), types.SimpleNamespace(pending=lambda: False)
            check(core.run_action(cm, h.manager(), 'weather', lambda p: '', None, lock, fpga=fpga) == EXIT_OK
                  and location(h.flash())[0] == DENVER, 'the weather job writes the saved place')
            wl.save(None, cm.LOCATION_FILE)
            check(core.run_action(cm, h.manager(), 'weather', lambda p: '', None, lock, fpga=fpga) == EXIT_OK
                  and cm.span(h.flash(), cm.LOCATION) == b'\xff' * 8192, 'with none saved it puts the default back')
            # the window's approved choice: written first, saved on this PC only once the console holds it
            cm.pending_location(SECOND)
            check(core.run_action(cm, h.manager(), 'weather', lambda p: '', None, lock, fpga=fpga) == EXIT_OK
                  and location(h.flash())[0] == SECOND and wl.load_saved(cm.LOCATION_FILE) == SECOND
                  and not cm.LOCATION_PENDING.exists(), 'approved choice: written, then saved, hand-over file gone')
            hs = Harness(stock, release, stock)
            cm.pending_location(DENVER)
            check(core.run_action(cm, hs.manager(), 'weather', lambda p: '', None, lock, fpga=fpga) == EXIT_REFUSED
                  and wl.load_saved(cm.LOCATION_FILE) == SECOND and not cm.LOCATION_PENDING.exists()
                  and hs.flash() == stock, 'a write that does not happen leaves the PC\'s saved choice as it was')
            cm.pending_location(None)
            check(core.run_action(cm, h.manager(), 'weather', lambda p: '', None, lock, fpga=fpga) == EXIT_OK
                  and cm.span(h.flash(), cm.LOCATION) == b'\xff' * 8192 and wl.load_saved(cm.LOCATION_FILE) is None,
                  'approved default: erased, then forgotten on this PC')
            before = {'state': 'installed', 'launcher': release.version}
            check(core.summary_after_job('weather', 0, ['Weather location written'], dict(before)) == before
                  and core.BY_KEY['weather'].confirm == 'yes' and 'weather' in core.PROGRAM_WRITE_KEYS,
                  'the job needs an Approve and leaves the last status as it was')
            # the text menu: look-up, confirmation on the person's screen, the typed word, then the write
            h = Harness(installed, release, stock)
            m = h.manager()
            asked = []

            def person(answers):
                answers = list(answers)

                def ask(prompt):
                    asked.append(prompt)
                    word = re.search(r'Type (yes\d{3})', prompt)
                    return word.group(1) if word else answers.pop(0)
                return ask
            real = wl.resolve
            wl.resolve = lambda found, fetch=None: DENVER
            try:
                check(cm.weather_location_menu(m, person(['Denver, CO', '1', 'yes', 'no'])) == EXIT_OK
                      and location(h.flash())[0] == DENVER and wl.load_saved(cm.LOCATION_FILE) == DENVER,
                      'text menu: found, confirmed, saved on this PC and written')
                check(cm.weather_location_menu(m, person(['default'])) == EXIT_OK and wl.load_saved(cm.LOCATION_FILE) is None
                      and cm.span(h.flash(), cm.LOCATION) == b'\xff' * 8192, 'text menu: default')
                writes = h.console.writes
                check(cm.weather_location_menu(m, person(['Nowhere, ZZ'])) == EXIT_REFUSED
                      and cm.weather_location_menu(m, person(['Denver, CO', '1', 'no'])) == EXIT_OK
                      and h.console.writes == writes and wl.load_saved(cm.LOCATION_FILE) is None,
                      'text menu: no match, or not confirmed: nothing saved or written')
                check(cm.weather_location_menu(Harness(stock, release, stock).manager(), person(['Denver, CO', '1', 'yes', 'no']))
                      == EXIT_REFUSED and wl.load_saved(cm.LOCATION_FILE) is None,
                      'text menu: a write that is refused saves nothing on this PC')
                hint = []
                cm.weather_location_menu(m, lambda prompt: (hint.append(prompt), '')[1] if prompt.startswith('Which')
                                         else 'Springfield')
                check(hint and 'More places match: add the state, for example "Springfield, ' in hint[0],
                      'text menu: an ambiguous town gets the add-the-state hint')
            finally:
                wl.resolve = real
            clean(h.log, 'text menu')
        finally:
            cm.LOCATION_FILE = saved_path
    print('PASS: weather location on the simulated console: write and verify, same place, another place, default; '
          'status; 6 power cuts with the tool dying, each finished; install, update and switch-off keep it, remove '
          'erases it; on stock a place is refused and Use default clears a left-over location; the saved copy written back INSIDE the install job (one job, "choose Games" said last, after every write; a cut install undone whole); sequences newer than B; no two flash areas overlap and the console\'s constants agree; the window\'s job '
          'and the text menu; nothing personal in logs or closed journals; 1.6.5: a place\'s area pages written with it, erased by Use default, refused for another place, written back at an install, imported and exported; a PC\'s weather migration (announced first, never over anything there); the first install\'s "Add Local Weather?" (a place with its pages, the default, skip). No hardware.')


if __name__ == '__main__':
    main()

# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Chromatic Manager's apps record (launcher 1.6.4+) on the simulated console of test_chromatic_manager.py (the real
stock backup and the pinned launcher build, re-labelled 1.6.4 where a 1.6.4 release is needed; no hardware).

The owner's console (launcher 1.6.3, no record) keeps both rows and TV's server list after the 1.6.4 install; a new
console gets the defaults (nothing written); a console that lost its record gets this PC's copy back; a record already
on the console (also one its own menu wrote) is never overwritten; the apps job writes, verifies, writes the defaults;
a power cut at every write and the tool dying mid-job (finished by Finish job); switch-off, update and downgrade keep it,
remove and restore erase it; a job journalled by the previous layout (launcher area to 0x3FC000) is finished by this one;
status names the rows; the job through the window's core and the text menu. Prints pass/fail only.

Run: private/chromatic-tools/venv/Scripts/python.exe -B scripts/test_chromatic_manager_apps.py
"""
import contextlib
import re
import tempfile
import types
from pathlib import Path

import test_chromatic_manager as t  # the sandbox: no private files are read or written
import chromatic_apps as ap
import chromatic_manager as cm
import chromatic_manager_core as core
from chromatic_trial_runner import EXIT_OK, EXIT_RECOVERED, EXIT_REFUSED
from compose_chromatic_image import check_image, stock_dump

check, Harness = t.check, t.Harness


def relabel(release, version):
    """The same launcher build with another version in its app description (checksums fixed)."""
    app = bytearray(release.launcher)
    app[0x20 + 16:0x20 + 48] = version.encode().ljust(32, b'\0')
    app = t.fix_checksums(bytes(app))
    check_image(app, 'relabelled launcher')
    return cm.Release.check(release.bootloader, release.table, app)


def apps_of(image):
    return ap.current(cm.span(image, cm.APPS_A), cm.span(image, cm.APPS_B))


def outside_apps_same(a, b):
    return a[:cm.APPS[0]] == b[:cm.APPS[0]] and a[cm.APPS[1]:] == b[cm.APPS[1]:]


def with_record(image, a=None, b=None):
    """`image` with these (flags, sequence) copies in the apps sectors (None: erased)."""
    image = cm.put(image, cm.APPS_A, ap.sector(*a) if a else b'')
    return cm.put(image, cm.APPS_B, ap.sector(*b) if b else b'')


def main():
    stock = stock_dump(allow_synthetic=True)
    built = cm.Release.load()
    r163, r164, r151 = relabel(built, '1.6.3'), relabel(built, '1.6.4'), relabel(built, '1.5.1')
    check(cm.SLOT_APP == (0x260000, 0x3F6000) and cm.PACK == (0x3F6000, 0x3FA000) and cm.APPS == (0x3FA000, 0x3FC000)
          and cm.APPS_A[0] == ap.FLASH_A and cm.APPS_B[0] == ap.FLASH_B and cm.APPS[1] == cm.LOCATION[0]
          and len(r164.launcher) <= cm.SLOT_APP[1] - cm.SLOT_APP[0] and cm.SLOT_APP[1] - cm.SLOT_APP[0] == 1_662_976,
          'layout (1.6.5): launcher area 1,662,976 B, then the location pack, the apps pair, the location')
    with tempfile.TemporaryDirectory() as folder:
        cm.APPS_FILE, cm.APPS_PENDING = Path(folder) / 'apps.json', Path(folder) / 'apps.pending.json'

        # ---- the owner's console: launcher 1.6.3, no record. Installing 1.6.4 keeps both rows and TV's list. ----
        h = Harness(stock, r163)
        check(h.manager().install() == EXIT_OK and h.state() == 'installed', 'start: 1.6.3 installed')
        owner_163 = h.flash()
        check(apps_of(owner_163) == (None, 0) and cm.span(owner_163, cm.APPS) == b'\xff' * 8192, '1.6.3: no apps record')
        h = Harness(owner_163, r164, stock)
        m = h.manager()
        said, jobs = [], []
        say = m.say
        m.say = lambda line: (said.append((line, h.console.writes)), say(line))
        run_job = m.run_job
        m.run_job = lambda kind, *a, **k: (jobs.append(kind), run_job(kind, *a, **k))[1]
        check(m.install() == EXIT_OK and h.state() == 'installed' and jobs == ['install'], '1.6.3 -> 1.6.4: one install job')
        after = h.flash()
        check(apps_of(after) == (ap.AS_1_6, 1) and cm.span(after, cm.APPS_A) == cm.span(after, cm.APPS_B) == ap.sector(ap.AS_1_6, 1),
              '1.6.3 -> 1.6.4: TV (with the Live TV list) and ChromaTube kept, both copies, in the same job')
        plain = cm.install_target(owner_163, r164)
        # no weather migration on this (sandbox) PC: the weather location stays as it was (none)
        import chromatic_weather_location as wl
        check(cm.span(after, cm.LOCATION) == b'\xff' * 8192 and outside_apps_same(after, plain),
              'nothing else differs from a plain install')
        check(any('keeps them exactly so: TV shown (Local Weather and Live TV channels), ChromaTube shown.' in l for l, _ in said)
              and 'The apps choice was written with it.' in said[-1][0] and 'choose Games' in said[-1][0]
              and said[-1][1] == h.console.writes, 'said before the write, named in the done line; "choose Games" said last')
        check(ap.load_saved(cm.APPS_FILE) == ap.AS_1_6, 'this PC keeps a copy once the console holds it')
        check('apps choice' in core.BY_KEY['install'].help, 'the install\'s Approve names the apps write')
        h = Harness(after, r164, stock)  # no closed job of its own: the record matches no image this tool holds
        m = h.manager()
        check(m.status() == EXIT_OK and 'Apps on the start-up menu: TV shown (Local Weather and Live TV channels), ChromaTube shown.'
              in h.log and not any('match no image' in l for l in h.log) and (cm.APPS[0], 8192) in h.console.part_reads
              and h.console.full_reads == 0, 'status names the rows; the record is read (8 KiB), not called unknown')
        summary = core.summarize(h.log)
        check(ap.from_words(summary.get('apps')) == ap.AS_1_6, 'the window\'s summary reads the rows back')
        owner_164 = after

        # ---- review M1 of 1.6.4: the first move to 1.6.4 through the guided Official update keeps the owner's setup ----
        updated = t.with_app(stock, version='9.9.9', idf='v5.4.1')
        # a PC with a weather migration (a sample place standing in for what a 1.6.3 console showed)
        sample = wl.Place('DENVER, CO', 'CO', 397392, -1049903, -420, 1, 'BOU', 63, 62, (wl.Station('KBJC', 'BROOMFIELD'),))
        import json as _json
        cm.MIGRATION_FILE.write_text(_json.dumps({'showed': ['1.6.3'], 'showed_with_record': [], 'place': sample.to_json()}), 'utf-8')
        for start, label, want_apps, want_place in ((owner_163, '1.6.3 console', ap.AS_1_6, True),
                                                    (owner_164, '1.6.4 console (its own record)', ap.AS_1_6, False)):
            ap.save(None, cm.APPS_FILE)
            wl.save(None, cm.LOCATION_FILE)
            h = Harness(start, r164, stock)

            def person(prompt, h=h):
                if prompt.startswith('Type done'):
                    h.console.flash = t.official_update(h.console.flash, stock, updated)
                    return 'done'
                return 'no'
            check(h.manager().guided_update(person) == EXIT_OK and h.state() == 'installed',
                  f'guided update over the {label} with 1.6.4: the menu back on')
            got = h.flash()
            check(apps_of(got)[0] == want_apps and
                  (wl.current(cm.span(got, cm.LOCATION_A), cm.span(got, cm.LOCATION_B))[0] == sample) == want_place
                  and cm.span(got, cm.APP) == cm.span(updated, cm.APP),
                  f'guided update over the {label}: TV with Live TV, ChromaTube and the weather it showed kept (review M1)')
        cm.MIGRATION_FILE.unlink()
        # a console already switched off before (no version known): nothing invented
        ap.save(None, cm.APPS_FILE)
        wl.save(None, cm.LOCATION_FILE)
        off = cm.put(cm.put(owner_163, cm.SLOT_APP, b''), cm.APPS, b'')
        h = Harness(off, r164, stock)
        check(h.manager().install() == EXIT_OK and apps_of(h.flash()) == (None, 0)
              and cm.span(h.flash(), cm.LOCATION) == b'\xff' * 8192,
              'a console switched off earlier (its launcher not known): the defaults, nothing invented')
        ap.save(None, cm.APPS_FILE)
        wl.save(None, cm.LOCATION_FILE)

        # ---- review L5: a damaged apps record is never overwritten by the install default, and it is said ----
        damaged = cm.put(owner_163, cm.APPS_A, b'PJAP' + b'\x00' * 28)
        h = Harness(damaged, r164, stock)
        check(h.manager().install() == EXIT_OK and cm.span(h.flash(), cm.APPS) == cm.span(damaged, cm.APPS)
              and any('apps choice area holds something this tool cannot read; it is left as it is' in l for l in h.log),
              'a damaged apps record on a 1.6.3 console: left exactly as it is, and said (review L5)')
        ap.save(None, cm.APPS_FILE)
        wl.save(None, cm.LOCATION_FILE)

        # ---- review L4: restoring a backup taken while the menu was switched off erases the apps sectors too ----
        dormant_backup = cm.without_setup(cm.put(owner_164, cm.SLOT_APP, b''))
        check(cm.span(dormant_backup, cm.APPS) != b'\xff' * 8192, 'the switched-off backup still holds the apps record')
        goal = cm.restore_target(owner_164, dormant_backup, r164, stock)
        check(cm.span(goal, cm.APPS) == b'\xff' * 8192 and cm.describe(goal, r164).name in cm.STOCK_LAYOUT,
              'restore of a switched-off backup: plain stock with the apps sectors erased (review L4)')

        # ---- a new console (stock): the defaults, nothing written to the apps sectors ----
        ap.save(None, cm.APPS_FILE)
        h = Harness(stock, r164)
        check(h.manager().install() == EXIT_OK and cm.span(h.flash(), cm.APPS) == b'\xff' * 8192 and ap.load_saved(cm.APPS_FILE) is None,
              'new console: no record (TV with Local Weather only, no ChromaTube), nothing saved on this PC')
        h2 = Harness(h.flash(), r164, stock)
        check(h2.manager().status() == EXIT_OK and 'Apps on the start-up menu: TV shown (Local Weather only), ChromaTube hidden '
              '(the defaults: nothing set).' in h2.log and set(h2.console.part_reads) == t.ALWAYS,
              'status on a new console: the defaults; nothing more is read')
        # 1.5.1 (three rows) upgraded: no record either (it never showed TV or ChromaTube)
        h = Harness(stock, r151)
        h.manager().install()
        h = Harness(h.flash(), r164, stock)
        check(h.manager().install() == EXIT_OK and apps_of(h.flash()) == (None, 0), '1.5.1 -> 1.6.4: the defaults')

        # ---- a record already on the console (its own menu wrote B newer) is never overwritten ----
        console_choice = with_record(owner_164, (ap.AS_1_6, 1), (ap.TV | ap.LIVETV, 2))  # ChromaTube hidden on the console
        ap.save(ap.AS_1_6, cm.APPS_FILE)
        older = cm.put(console_choice, cm.SLOT_APP, r163.launcher)
        h = Harness(older, r164, stock)
        check(h.manager().install() == EXIT_OK and h.flash() == console_choice and apps_of(h.flash()) == (ap.TV | ap.LIVETV, 2)
              and ap.load_saved(cm.APPS_FILE) == ap.TV | ap.LIVETV, 'update: the console\'s own choice kept byte for byte; '
                                                                       'this PC\'s copy follows it')
        h = Harness(console_choice, r164, stock)
        writes = h.console.writes
        check(h.manager().install() == EXIT_OK and h.console.writes == writes and any('already installed. Nothing written' in l
              for l in h.log), 'already installed with a record: nothing written')
        # downgrade to 1.6.3 / 1.5.1 keeps the record (they ignore it); back to 1.6.4: still there
        for back in (r163, r151):
            h = Harness(console_choice, back, stock)
            check(h.manager().install() == EXIT_OK and apps_of(h.flash()) == (ap.TV | ap.LIVETV, 2)
                  and outside_apps_same(h.flash(), cm.install_target(console_choice, back)), f'downgrade to {back.version} keeps it')
            check(h.manager().status() == EXIT_OK and any(f'Launcher {back.version} does not read this' in l for l in h.log),
                  f'status under {back.version}: says that launcher does not read it')
            h = Harness(h.flash(), r164, stock)
            check(h.manager().install() == EXIT_OK and h.flash() == console_choice, f'{back.version} -> 1.6.4: the choice is back')
        # switch-off keeps it; remove and restore erase it; then an install writes this PC's copy back
        h = Harness(console_choice, r164, stock)
        check(h.manager().disable() == EXIT_OK and apps_of(h.flash()) == (ap.TV | ap.LIVETV, 2), 'switch-off keeps it')
        check(h.manager().install() == EXIT_OK and apps_of(h.flash()) == (ap.TV | ap.LIVETV, 2), 'switched on again: still there')
        h = Harness(console_choice, r164, stock)
        check(h.manager().remove() == EXIT_OK and cm.span(h.flash(), cm.APPS) == b'\xff' * 8192, 'remove erases it')
        check(h.manager().install() == EXIT_OK and apps_of(h.flash())[0] == ap.TV | ap.LIVETV,
              'installed again after a removal: this PC\'s copy written back in the install job')
        h = Harness(console_choice, r164, stock)
        check(h.manager().restore_stock() == EXIT_OK and cm.span(h.flash(), cm.APPS) == b'\xff' * 8192 and h.state() == 'stock',
              'restore erases it (exact stock layout)')

        # ---- the apps job: write, verify, the same again, defaults ----
        ap.save(None, cm.APPS_FILE)
        h = Harness(owner_164, r164, stock)
        m = h.manager()
        check(m.apps(ap.TV) == EXIT_OK and apps_of(h.flash()) == (ap.TV, 2) and outside_apps_same(h.flash(), owner_164)
              and (0, cm.SIZE) in h.console.md5_calls and h.console.writes == 2, 'apps job: both copies, newer, checked on the chip')
        check(any('Apps on the start-up menu now: TV shown (Local Weather only), ChromaTube hidden.' in l for l in h.log),
              'said in plain words')
        writes = h.console.writes
        check(m.apps(ap.TV) == EXIT_OK and h.console.writes == writes and any('already shows these apps' in l for l in h.log),
              'the same again: nothing written')
        check(m.apps(None) == EXIT_OK and cm.span(h.flash(), cm.APPS) == b'\xff' * 8192, 'defaults: both copies erased')
        check(m.apps(None) == EXIT_OK and any('already uses the default apps' in l for l in h.log), 'defaults again: nothing')
        check(m.apps(0) == EXIT_OK and apps_of(h.flash())[0] == 0, 'TV and ChromaTube both off (Games, Jellyfin, Other stay)')
        check(core.summary_after_job('apps', 0, h.log[-3:], {'state': 'installed'}).get('apps') ==
              'TV hidden, ChromaTube hidden', 'the summary takes what the job wrote')
        # an older launcher on the console: written, and said that it shows every app until 1.6.4
        h = Harness(owner_163, r164, stock)
        check(h.manager().apps(ap.KNOWN) == EXIT_OK and any('shows every app until launcher 1.6.4' in l for l in h.log),
              'written under 1.6.3: says it is read once 1.6.4 is installed')
        # stock: a choice is refused; the defaults clear a left-over record
        h = Harness(stock, r164, stock)
        check(h.manager().apps(ap.TV) == EXIT_REFUSED and any('Install it first' in l for l in h.log) and h.flash() == stock,
              'stock console: refused, nothing written')
        left = with_record(stock, (ap.KNOWN, 4), (ap.KNOWN, 4))
        h = Harness(left, r164, stock)
        check(h.manager().apps(None) == EXIT_OK and h.flash() == stock, 'stock console: the defaults clear a left-over record')

        # ---- power cut at every write, and the tool dying mid-job: finished by Finish job ----
        cuts = 0
        for old, new in ((None, ap.TV), ((ap.KNOWN, 5), ap.TUBE), ((ap.TV | ap.LIVETV, 7), None), ((ap.KNOWN, 3), 0)):
            for cut in (0, 1):
                start = with_record(owner_164, old, old) if old else cm.put(owner_164, cm.APPS, b'')
                h = Harness(start, r164, stock)
                m = h.manager()
                h.console.cut_after = cut
                m.recover = lambda: (_ for _ in ()).throw(SystemExit('PC died'))
                try:
                    m.apps(new)
                except SystemExit:
                    pass
                seen, _ = apps_of(h.flash())
                check(seen in ((old or (None,))[0], new if new is not None else None),
                      f'cut at write {cut + 1}: the console reads the old choice, the new one or the defaults')
                check(t.Console(h.flash(), r164).boots() == 'launcher', 'a cut apps write leaves the menu starting')
                m = h.manager()
                check(m.pending() and m.resume() in (EXIT_OK, EXIT_RECOVERED) and not m.pending(), f'cut at {cut + 1}: finished')
                check(apps_of(h.flash())[0] == new and outside_apps_same(h.flash(), start), 'finished: the new choice, nothing else')
                cuts += 1
        # the next sequence is newer than both copies, whichever the console's menu wrote last
        odd = with_record(owner_164, (ap.TV, 0x80000005), (ap.KNOWN, 5))
        h = Harness(odd, r164, stock)
        check(h.manager().apps(ap.TUBE) == EXIT_OK and apps_of(h.flash()) == (ap.TUBE, 0x80000006), 'newer than both copies')

        # ---- a job journalled by the previous layout (launcher area to 0x3FC000) is finished by this Manager ----
        saved_slot, saved_segments = cm.SLOT_APP, cm.SEGMENTS
        cm.SLOT_APP = (cm.SLOT[0], cm.LOCATION[0])  # the 1.6.3 Manager's layout
        cm.SEGMENTS = tuple(r for r in saved_segments if r not in (saved_slot, cm.APPS, cm.PACK))
        cm.SEGMENTS = cm.SEGMENTS[:6] + (cm.SLOT_APP,) + cm.SEGMENTS[6:]
        try:
            check(b''.join(b'\0' * (e - s) for s, e in cm.SEGMENTS) == bytes(cm.SIZE), 'old layout covers the flash')
            h = Harness(stock, r163)
            t.crash_mid_install(h, 1)
        finally:
            cm.SLOT_APP, cm.SEGMENTS = saved_slot, saved_segments
        m = h.manager()
        check(m.pending() and m.resume() in (EXIT_OK, EXIT_RECOVERED) and h.state() == 'stock', 'an old-layout install cut '
              'mid-job is undone by the new layout')
        h = Harness(stock, r163)
        h.manager().install()
        check(h.manager().status() == EXIT_OK and h.console.full_reads == 0, 'an old-layout install is identified without a full read')

        # ---- the window's job and the text menu ----
        h = Harness(owner_164, r164, stock)
        lock, fpga = contextlib.nullcontext(), types.SimpleNamespace(pending=lambda: False)
        cm.pending_apps(ap.TV | ap.TUBE)
        check(core.run_action(cm, h.manager(), 'apps', lambda p: '', None, lock, fpga=fpga) == EXIT_OK
              and apps_of(h.flash())[0] == ap.TV | ap.TUBE and ap.load_saved(cm.APPS_FILE) == ap.TV | ap.TUBE
              and not cm.APPS_PENDING.exists(), 'approved choice: written, then saved, hand-over file gone')
        hs = Harness(stock, r164, stock)
        cm.pending_apps(ap.KNOWN)
        check(core.run_action(cm, hs.manager(), 'apps', lambda p: '', None, lock, fpga=fpga) == EXIT_REFUSED
              and ap.load_saved(cm.APPS_FILE) == ap.TV | ap.TUBE and not cm.APPS_PENDING.exists() and hs.flash() == stock,
              'a write that does not happen leaves the PC\'s copy as it was')
        cm.pending_apps(None)
        check(core.run_action(cm, h.manager(), 'apps', lambda p: '', None, lock, fpga=fpga) == EXIT_OK
              and cm.span(h.flash(), cm.APPS) == b'\xff' * 8192 and ap.load_saved(cm.APPS_FILE) is None,
              'approved defaults: erased, then forgotten on this PC')
        check(core.BY_KEY['apps'].confirm == 'yes' and 'apps' in core.PROGRAM_WRITE_KEYS and core.BY_KEY['apps'].tab == 'settings',
              'the job needs an Approve')
        h = Harness(with_record(owner_164, (ap.KNOWN, 4), (ap.KNOWN, 4)), r164, stock)
        writes = h.console.writes
        check(core.run_action(cm, h.manager(), 'apps', lambda p: '', None, lock, fpga=fpga) == EXIT_REFUSED
              and h.console.writes == writes and apps_of(h.flash())[0] == ap.KNOWN,
              'a request with no choice and no saved copy writes nothing (never the defaults by surprise)')
        ap.save(ap.TV, cm.APPS_FILE)
        check(core.run_action(cm, h.manager(), 'apps', lambda p: '', None, lock, fpga=fpga) == EXIT_OK
              and apps_of(h.flash())[0] == ap.TV, 'with a saved copy and no new choice: the saved copy')
        ap.save(None, cm.APPS_FILE)

        def person(answers):
            answers = list(answers)

            def ask(prompt):
                word = re.search(r'Type (yes\d{3})', prompt)
                return word.group(1) if word else answers.pop(0)
            return ask
        h = Harness(owner_164, r164, stock)
        m = h.manager()
        check(cm.apps_menu(m, person(['4'])) == EXIT_OK and apps_of(h.flash())[0] == ap.TV | ap.TUBE
              and ap.load_saved(cm.APPS_FILE) == ap.TV | ap.TUBE, 'text menu: chosen, confirmed, written, saved')
        writes = h.console.writes
        check(cm.apps_menu(m, person([''])) == EXIT_OK and cm.apps_menu(m, lambda p: '9' if p.startswith('Apps') else 'no')
              == EXIT_OK and h.console.writes == writes, 'text menu: nothing chosen or not confirmed: nothing written')
    print(f'PASS: apps record on the simulated console: the owner\'s 1.6.3 console keeps TV (with Live TV) and ChromaTube '
          f'through the 1.6.4 install (one job, said, "choose Games" last); a new console and 1.5.1 get the defaults; a '
          f'record on the console is never overwritten and this PC\'s copy follows it; downgrade to 1.6.3/1.5.1 and back, '
          f'switch-off keep it; remove and restore erase it and the next install writes the PC\'s copy back; the apps job '
          f'(write, same, defaults, both off, under an older launcher, refused on stock, clears a left-over); {cuts} power '
          f'cuts with the tool dying, each finished; sequences newer than both copies; an old-layout job finished; status and '
          f'the window\'s summary; the window\'s job and the text menu. No hardware.')


if __name__ == '__main__':
    main()

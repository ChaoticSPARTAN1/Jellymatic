# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Launcher 1.6.4 apps on/off, on the PC (no hardware, no network, no private file).

  * The apps record: the Manager's writer (scripts/chromatic_apps.py) and the console's reader
    (chromatic/launcher/shared/apps_record.h) agree on every vector: all flag values, every single byte changed,
    blank, zeroed, torn, other versions and lengths, unknown flags, random records, and which of two copies wins.
  * chromatic/launcher/check_apps.c: the start-up menu with every on/off combination, the console's own Hide and
    Show hidden apps (physical presses only) with its write under power cuts, TV without Live TV straight to Local
    Weather (no app, no sign-in, no server), and the screens' previews.
  * Source rules: the reader's sectors equal the Manager's; Remote Pilot's row names are unchanged; the shell reads
    the record at boot and writes it only through the audited writer, after its "Saving..." frame.

    py -3 scripts\\test_chromatic_apps.py [--previews]

Previews go to staging/chromatic-apps/previews/; --previews also refreshes chromatic/launcher/previews-1.6.4/.
"""
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chromatic_apps as ap  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'chromatic' / 'launcher'
OUT = ROOT / 'staging' / 'chromatic-apps' / 'previews'
PREVIEWS = SOURCE / 'previews-1.6.4'
GCC = 'C:/msys64/ucrt64/bin/gcc.exe'


def check(condition, message):
    if not condition:
        raise SystemExit(f'FAIL: {message}')


def build(work):
    exe = work / 'check_apps.exe'
    subprocess.run([GCC, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', 'check_apps.c', 'shared/provision_write.c',
                    '-o', str(exe)], cwd=SOURCE, check=True)
    return exe


def raw_record(flags, sequence, version=1, length=32, pad=0, reserved=bytes(12), magic=b'PJAP', crc=None):
    body = struct.pack('<4sBBHII12s', magic, version, pad, length, sequence, flags, reserved)
    return body + struct.pack('<I', (zlib.crc32(body) & 0xFFFFFFFF) if crc is None else crc)


def vectors():
    rng = random.Random(1604)
    singles, pairs, builds = [], [], []
    for flags in range(8):
        for sequence in (1, 2, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF):
            singles.append(ap.pack(flags, sequence))
            builds.append((flags, sequence))
    base = ap.pack(ap.TV | ap.LIVETV, 9)
    for at in range(32):  # every single byte changed (each of three ways)
        for value in (base[at] ^ 0x01, base[at] ^ 0x80, 0xFF):
            changed = bytearray(base)
            changed[at] = value
            singles.append(bytes(changed))
    singles += [b'\xff' * 32, bytes(32), raw_record(1, 1, version=2), raw_record(1, 1, length=64), raw_record(1, 1, pad=1),
                raw_record(8, 1), raw_record(1, 0), raw_record(1, 1, reserved=b'\x01' + bytes(11)), raw_record(1, 1, magic=b'PJAX'),
                raw_record(1, 1, crc=0), base[:16] + b'\xff' * 16, base[:31] + b'\xff']
    for _ in range(300):
        singles.append(bytes(rng.randrange(256) for _ in range(32)))
        flags, sequence = rng.randrange(8), rng.randrange(1, 1 << 32)
        good = ap.pack(flags, sequence)
        singles.append(good)
    choices = [ap.pack(ap.TV, 5), ap.pack(ap.TUBE, 6), ap.pack(ap.KNOWN, 0xFFFFFFFF), ap.pack(ap.LIVETV, 1),
               ap.pack(ap.TV | ap.TUBE, 0x80000005), b'\xff' * 32, bytes(32), base[:20] + b'\xff' * 12]
    pairs = [(a, b) for a in choices for b in choices]
    for _ in range(200):
        pairs.append((ap.pack(rng.randrange(8), rng.randrange(1, 1 << 32)), ap.pack(rng.randrange(8), rng.randrange(1, 1 << 32))))
    builds += [(8, 1), (1, 0), (0x80000000, 3)]
    return singles, pairs, builds


def agree(exe, work):
    singles, pairs, builds = vectors()
    lines = [f'P {s.hex()}' for s in singles] + [f'C {a.hex()} {b.hex()}' for a, b in pairs] + [f'B {f} {s}' for f, s in builds]
    path = work / 'vectors.txt'
    path.write_text('\n'.join(lines) + '\n', encoding='ascii')
    answers = subprocess.run([str(exe), 'vectors', str(path)], check=True, capture_output=True, text=True).stdout.splitlines()
    check(len(answers) == len(lines), 'one answer per vector')
    n = 0
    for raw, answer in zip(singles, answers):
        got = ap.parse(raw)
        erased = raw[:32] == b'\xff' * 32
        expect = f'P 1 {got[0]} {got[1]}' if got else ('P 0 0 0' if erased else 'P -1 0 0')
        check(answer == expect, f'single copy {raw.hex()}: C says {answer}, Python {expect}')
        n += 1
    for (a, b), answer in zip(pairs, answers[len(singles):]):
        flags, sequence = ap.current(a, b)
        in_b = int(flags is not None and ap.parse(b) == (flags, sequence) and (ap.parse(a) is None or ap.newer(ap.parse(a)[1], sequence)))
        expect = f'C {int(flags is not None)} {ap.shown(a, b)} {sequence} {in_b}'
        check(answer == expect, f'pair {a.hex()} / {b.hex()}: C says {answer}, Python {expect}')
        n += 1
    for (flags, sequence), answer in zip(builds, answers[len(singles) + len(pairs):]):
        try:
            expect = f'B {ap.pack(flags, sequence).hex()}'
        except ap.AppsError:
            expect = 'B refused'
        check(answer == expect, f'build {flags} {sequence}: C says {answer}, Python {expect}')
        n += 1
    return n


def python_rules():
    check(ap.DEFAULTS == ap.TV and ap.AS_1_6 == ap.KNOWN, 'defaults: TV (Local Weather only), no ChromaTube; 1.6.0-1.6.3 showed all')
    check(ap.shown(b'\xff' * 32, b'\xff' * 32) == ap.DEFAULTS and ap.current(bytes(32), bytes(32)) == (None, 0),
          'blank or zeroed: the defaults')
    image = b'\x5a' * 0x3FA000 + b'\xff' * 0x2000 + b'\x5a' * (0x400000 - 0x3FC000)
    a, b = (ap.FLASH_A, ap.FLASH_A + ap.SECTOR), (ap.FLASH_B, ap.FLASH_B + ap.SECTOR)
    one = ap.with_apps(image, ap.KNOWN, a, b)
    check(one[a[0]:a[1]] == one[b[0]:b[1]] == ap.sector(ap.KNOWN, 1) and one[:a[0]] == image[:a[0]] and one[b[1]:] == image[b[1]:],
          'the Manager writes the same record in both copies and nothing else')
    # the console wrote B newer (its Hide); the Manager's next write is newer than both copies
    console = image[:a[0]] + ap.sector(ap.KNOWN, 7) + ap.sector(ap.TV, 8) + image[b[1]:]
    two = ap.with_apps(console, ap.TV | ap.TUBE, a, b)
    check(ap.current(two[a[0]:a[1]], two[b[0]:b[1]]) == (ap.TV | ap.TUBE, 9), 'next sequence is newer than both copies')
    cut = two[:a[0]] + two[a[0]:a[1]] + console[b[0]:b[1]] + two[b[1]:]
    check(ap.current(cut[a[0]:a[1]], cut[b[0]:b[1]])[0] == ap.TV | ap.TUBE, 'a Manager write cut after A: the new A is in force')
    check(ap.showed_both('1.6.0') and ap.showed_both('1.6.3') and not ap.showed_both('1.6.4') and not ap.showed_both('1.5.1')
          and ap.reads_record('1.6.4') and ap.reads_record('1.7.0') and not ap.reads_record('1.6.3') and not ap.reads_record(None),
          'version rules: 1.6.0-1.6.3 showed both rows; 1.6.4+ reads the record')
    for flags in range(8):
        check(ap.from_words(ap.words(flags)) == flags and ap.from_words(ap.words(flags, False)) == flags, 'words read back')
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / 'apps.json'
        check(ap.load_saved(path) is None and ap.save(ap.TV | ap.TUBE, path) == 3 and ap.load_saved(path) == 3, 'saved copy')
        path.write_text('{"flags": 9}', encoding='utf-8')
        check(ap.load_saved(path) is None, 'a damaged saved copy is ignored')
        ap.save(None, path)
        check(not path.exists(), 'forgotten')


def source_rules():
    header = (SOURCE / 'shared' / 'apps_record.h').read_text(encoding='utf-8')
    const = lambda name: int(re.search(rf'#define {name} (0x[0-9A-Fa-f]+)u', header).group(1), 16)
    check(const('PJ_APPS_FLASH_A') == ap.FLASH_A and const('PJ_APPS_FLASH_B') == ap.FLASH_B, 'the reader reads where the writer writes')
    remote = (SOURCE / 'shared' / 'remote.h').read_text(encoding='utf-8')
    check('static const char *const items[PJ_ITEM_COUNT]={"games","jellyfin","tv","chromatube","other"};' in remote,
          'Remote Pilot\'s row names are unchanged')
    main = re.sub(r'/\*.*?\*/', '', (SOURCE / 'main' / 'launcher_main.c').read_text(encoding='utf-8'), flags=re.S)
    check(main.count('read_apps(&found)') == 1 and main.count('pj_launch_set_apps(&launcher,apps);') == 1,
          'the record is read once, at boot, and handed to the menu')
    check(main.count('pj_apps_commit(') == 1 and 'if(pj_apps_write_wanted(&launcher,&want)&&apps_shown_saving) {' in main,
          'the menu\'s write runs in one place, only after its "Saving..." frame')
    at = main.index('pj_apps_commit(')
    start = main.rfind('#if PJ_ENABLE_SETUP', 0, at)
    check(start >= 0 and at < main.find('#endif', start) and '#else' not in main[start:at],
          'the write sits behind PJ_ENABLE_SETUP (with the audited writer)')
    check('launcher.apps_writable=PJ_ENABLE_SETUP;' in main, 'Hide and Show are offered only with the writer built in')
    check('pj_media_app_allowed(&launcher)&&record_jellyfin&&wifi_state==2&&!pj_jf_active()' in main,
          'the TV app is not started when TV has no Live TV (no server request)')
    check(main.count('pj_media_needs_sign_in(&launcher)') == 2, 'no "sign in to Jellyfin" line for Local Weather alone')
    check('pj_tv_weather_start_wanted(&launcher,record_wifi&&wifi_state==2,wx_only!=0,' in main, 'weather-only start in one place')
    logic = (SOURCE / 'shared' / 'launcher_logic.h').read_text(encoding='utf-8')
    check(logic.count('l->by_hand=1;pj_launch_press(l,pressed,now);l->by_hand=0;') == 1 and logic.count('by_hand=1') == 1,
          'only pj_launch_buttons (physical readings) marks a press as by hand')
    rmt_press = remote.split('static inline int pj_rmt_press', 1)[1].split('static inline', 1)[0]
    check('by_hand' not in remote and 'pj_launch_press(l,bits,now);' in rmt_press, 'Remote Pilot presses are never by hand')


def main():
    previews = '--previews' in sys.argv
    os.environ['PATH'] = r'C:\msys64\ucrt64\bin;' + os.environ['PATH']
    python_rules()
    source_rules()
    work = Path(tempfile.mkdtemp(prefix='chromatic-apps-'))
    try:
        exe = build(work)
        count = agree(exe, work)
        shots = work / 'shots'
        shots.mkdir()
        said = subprocess.run([str(exe), 'checks', str(shots)], check=True, capture_output=True, text=True).stdout.strip()
        OUT.mkdir(parents=True, exist_ok=True)
        for ppm in shots.glob('*.ppm'):
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(ppm), '-vf', 'scale=640:576:flags=neighbor',
                            str(OUT / (ppm.stem + '.png'))], check=True)
        if previews:
            PREVIEWS.mkdir(parents=True, exist_ok=True)
            for png in OUT.glob('*.png'):
                shutil.copyfile(png, PREVIEWS / png.name)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f'PASS: the Manager\'s writer and the console\'s reader agree on {count} vectors (every flag value, every single '
          'byte changed three ways, blank, zeroed, torn, other versions/lengths, unknown flags, 600 random, 264 copy pairs)')
    print('PASS: record rules on the PC: defaults, both copies written, next sequence newer than both, a cut after A, '
          'version rules, the saved copy')
    print('PASS: source rules: same sectors, Remote Pilot names unchanged, read at boot, one write behind PJ_ENABLE_SETUP '
          'after "Saving...", no TV app (no server) without Live TV, physical presses only')
    print(said)
    print(f'Previews in {OUT.relative_to(ROOT)}' + (f' and {PREVIEWS.relative_to(ROOT)}' if previews else
                                                    ' (run with --previews to refresh chromatic/launcher/previews-1.6.4)'))


if __name__ == '__main__':
    main()

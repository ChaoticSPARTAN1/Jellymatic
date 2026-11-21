# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""The Local Weather channel (chromatic/weather/wx.h) on the PC.

Builds chromatic/weather/check_weather.c and runs it against the real National Weather Service
replies of the PC reference (the PC reference's kept replies (not published); no network), then:
  - compares every figure the console code kept with the PC reference's normalised.json,
  - compares the almanac (sun, moon, time zone) with the reference's formulas for several dates,
  - compares the frames drawn by the console's own drawing code with the approved reference pages
    and writes them as PNG at 4x under staging/chromatic-weather/previews/ (with --previews: into
    chromatic/launcher/previews-1.6.0/, replacing the weather-*.png there),
  - builds chromatic/launcher/main/weather_esp32.c itself against the stand-in headers of
    chromatic/weather/host_stubs/ and runs the model of the ESP32 side (check_weather_glue.c),
  - reports the struct sizes and the stack each entry point uses on the PC.
No hardware, no network. Exits 0 only when everything passes.

The reference renderer, its kept replies and the private reference place they describe are not published: the
compile-time pack for that place comes from a private folder (private/owner-weather/, written by its own tool).
Without them (in a public clone) this suite says so and stops with SKIP; test_chromatic_weather_place.py covers the
public default (Orlando, FL) and location packs. Its PNG helpers and compiler lookup are also used by that suite.
"""
import datetime as dt
import json
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WX = ROOT / 'chromatic' / 'weather'
REF = ROOT / 'chromatic' / 'iptv' / 'weather'
CACHE = REF / 'cache'
WORK = ROOT / 'staging' / 'chromatic-weather'
PREVIEWS = ROOT / 'chromatic' / 'launcher' / 'previews-1.6.0' if '--previews' in sys.argv else WORK / 'previews'
GCC = shutil.which('gcc') or r'C:\msys64\ucrt64\bin\gcc.exe'
W, H, SCALE = 160, 144, 4
failed = []


# The public default is the Orlando, FL pack. These checks compare the console code with the PC reference and its kept
# replies, so they build with the reference's place as the default pack, from a private compile-time header (never
# part of a public build): exactly the console code that draws that place.
OWNER_HEADER = ROOT / 'private' / 'owner-weather' / 'wx_pack_owner.h'
OWNER = [f'-DWX_EXTRA_PACK_HEADER="{OWNER_HEADER.as_posix()}"', '-DWX_DEFAULT_PACK=WX_EXTRA_PACK']

def result(ok, name, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (f': {detail}' if detail and not ok else ''))
    if not ok:
        failed.append(name)


def build():
    WORK.mkdir(parents=True, exist_ok=True)
    exe = WORK / 'check_weather.exe'
    base = [GCC, '-std=c99', '-Wall', '-Wextra', '-Werror', '-Og', '-g', '-fstack-usage', '-fcallgraph-info=su', *OWNER, f'-I{WX}',
            f'-I{ROOT / "chromatic" / "jellyfin"}', str(WX / 'check_weather.c'), str(WX / 'wx_sound_data.c'), '-o', str(exe), '-lm']
    env = None
    if not shutil.which('gcc'):
        import os
        env = dict(os.environ, PATH=str(Path(GCC).parent) + os.pathsep + os.environ.get('PATH', ''))
    notes = []
    for flags, note in ((['-fsanitize=address,undefined'], 'address and undefined-behaviour sanitizers'),
                        (['-fsanitize=undefined', '-fsanitize=bounds-strict', '-fsanitize-undefined-trap-on-error', '-fstack-protector-all'],
                         'undefined-behaviour and array-bounds checks (trapping) and stack protector; this gcc has no '
                         'address sanitizer library')):
        done = subprocess.run(base[:1] + flags + base[1:], cwd=WORK, capture_output=True, text=True, env=env)
        if done.returncode == 0:
            notes.append(note)
            break
        last = done.stderr
    else:
        print(last)
        raise SystemExit('FAIL build')
    # the same sources once more the way the firmware is built (optimised for debugging, all warnings)
    for opt in ('-O2', '-Os'):
        done = subprocess.run([GCC, '-std=gnu17', '-Wall', '-Wextra', '-Werror', opt, '-fsyntax-only', *OWNER, f'-I{WX}',
                               f'-I{ROOT / "chromatic" / "jellyfin"}', str(WX / 'check_weather.c')], capture_output=True, text=True, env=env)
        result(done.returncode == 0, f'compiles without warnings at {opt} (-Wall -Wextra -Werror)', done.stderr[:800])
    # the way the player suite and the launcher checks include it: from the project root, no -I, C11, -O1 and
    # -O2, using only the channel's entry points; without the sound it must link without wx_sound_data.c
    for opt in ('-O1', '-O2'):
        for sound in (0, 1):
            src = WORK / f'include_check_{sound}.c'   # two folders below the root, as chromatic/jellyfin is
            src.write_text('#include "../../chromatic/weather/wx.h"\nstatic wx_state s;static uint16_t fb[160*144];\n' +
                           ('static size_t sink(void *c,const uint8_t *d,size_t n,int b){(void)c;(void)d;(void)b;return n;}\n' if sound else '') +
                           'int main(void){wx_init(&s);wx_enter(&s,0);int r=wx_tick(&s,16,0);wx_draw(&s,fb,16,0);' +
                           ('wx_audio_feed(&s,16,sink,0);' if sound else '') + 'return r&&fb[0]==0xf81f;}\n', 'utf-8')
            cmd = [GCC, opt, '-Wall', '-Wextra', '-Werror', '-std=c11', f'-I{ROOT / "chromatic" / "launcher" / "shared"}', str(src)]
            if sound:
                cmd.append(str(WX / 'wx_sound_data.c'))
            done = subprocess.run(cmd + ['-o', str(WORK / f'include_check_{sound}.exe')], cwd=ROOT, capture_output=True, text=True, env=env)
            result(done.returncode == 0, f'wx.h included from the project root with no -I, -std=c11 {opt} -Wall -Wextra -Werror, '
                   + ('with sound (links wx_sound_data.c)' if sound else 'without sound (links nothing else)'), done.stderr[:900])
    return exe, notes[0], env


def expected_values(data):
    """The reference's figures in the VAL format of check_weather.c."""
    def na(v):
        return '-' if v is None else str(v)
    icons = lambda s: s
    cur = data['current']
    kind = {None: 0, 'WIND CHILL': 1, 'HEAT INDEX': 2}[cur.get('feels_kind')]
    out = {'cur': '|'.join([na(cur['temp_f']), na(cur['dewpoint_f']), na(cur['humidity']), na(cur['wind_mph']),
                            na(cur['wind_dir']), na(cur['gust_mph']), na(None if cur['pressure_in'] is None else round(cur['pressure_in'] * 100)),
                            na(None if cur['visibility_mi'] is None else round(cur['visibility_mi'] * 10)), na(cur['ceiling_ft']),
                            na(cur['feels_f']), str(kind), cur['sky'], cur['pressure_trend'] or '-', icons(cur['icon'])])}
    for i, o in enumerate(data['observations']):
        out[f'obs{i}'] = '|'.join([o['label'], na(o.get('temp_f')), na(o.get('wind_mph')), na(o.get('wind_dir')), o.get('sky', ''),
                                   o.get('sky_short', '--')])
    for i, p in enumerate(data['local_forecast']):
        out[f'local{i}'] = f"{p['name']}|{int(p['is_day'])}|{p['text']}"
    for i, e in enumerate(data['extended']):
        sky = (e['sky'] + [''])[:2]
        out[f'ext{i}'] = f"{e['day']}|{e['icon']}|{sky[0]}/{sky[1]}|{na(e['hi'])}|{na(e['lo'])}|{-1 if e['pop'] is None else e['pop']}"
    for i, c in enumerate(data['regional']['cities']):
        out[f'city{i}'] = f"{c['name']}|{na(c['temp'])}|{c['icon']}|{c['kind'] or '-'}"
    kind = next((c['kind'] for c in data['regional']['cities'] if c.get('kind')), 'HIGH')
    out['regional'] = f"{data['regional']['period'] or ''}|{kind}"
    out['alerts'] = str(len(data['alerts']))
    out['ticker0'] = 'TEMP: 55^F   MOSTLY CLOUDY'      # the approved contact sheet's five lines
    out['ticker1'] = 'HUMIDITY 62%  DEW PT 43^F'
    out['ticker2'] = 'PRESSURE: 30.41 IN FALLING'
    out['ticker3'] = 'WIND: CALM'
    out['ticker4'] = 'VISIBILITY: 10 MI'
    return out


def reference_almanac(epoch):
    """The reference's almanac (weather_data.py) in the ALM format of check_weather.c."""
    sys.path.insert(0, str(REF))
    import weather_data as wd
    utc = dt.datetime(1970, 1, 1) + dt.timedelta(seconds=epoch)
    local = wd.to_local(utc)
    parts = []
    for k in (0, 1):
        day = local.date() + dt.timedelta(days=k)
        rise, sset = wd.sun_times(day, wd.DEFAULT_LAT, wd.DEFAULT_LON)
        r, s = wd.to_local(rise), wd.to_local(sset)
        parts.append(f"{day.strftime('%a').upper()} {r.hour * 60 + r.minute} {s.hour * 60 + s.minute}")
    moon = wd.moon_info(utc)
    text = ' '.join(parts) + f"|{moon['name']}|{moon['illum_pct']}"
    for n in moon['next']:
        when = wd.to_local(dt.datetime.fromisoformat(n['utc']))
        text += f"|{n['phase']} {when.month}-{when.day}"
    return text + f'|offset {wd.eastern_offset_hours(utc)}'


def png_bytes(width, height, rgb):
    def chunk(tag, body):
        return struct.pack('>I', len(body)) + tag + body + struct.pack('>I', zlib.crc32(tag + body))
    raw = b''.join(b'\x00' + rgb[y * width * 3:(y + 1) * width * 3] for y in range(height))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) + \
        chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b'')


def read_png(path):
    """8-bit palette or RGB PNG -> list of rows of (r, g, b)."""
    data = path.read_bytes()
    at, idat, pal, head = 8, b'', None, None
    while at < len(data):
        size, tag = struct.unpack('>I4s', data[at:at + 8])
        body = data[at + 8:at + 8 + size]
        if tag == b'IHDR':
            head = struct.unpack('>IIBBBBB', body)
        elif tag == b'PLTE':
            pal = [tuple(body[i:i + 3]) for i in range(0, len(body), 3)]
        elif tag == b'IDAT':
            idat += body
        at += 12 + size
    width, height, depth, colour = head[:4]
    if depth != 8 or colour not in (2, 3):
        return None
    bpp = 3 if colour == 2 else 1
    raw, rows, prev = zlib.decompress(idat), [], bytearray(width * bpp)
    for y in range(height):
        line = bytearray(raw[y * (width * bpp + 1) + 1:(y + 1) * (width * bpp + 1)])
        kind = raw[y * (width * bpp + 1)]
        for i in range(len(line)):
            a = line[i - bpp] if i >= bpp else 0
            b, c = prev[i], (prev[i - bpp] if i >= bpp else 0)
            if kind == 1:
                line[i] = (line[i] + a) & 255
            elif kind == 2:
                line[i] = (line[i] + b) & 255
            elif kind == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif kind == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        rows.append([pal[v] for v in line] if colour == 3 else [tuple(line[i:i + 3]) for i in range(0, len(line), 3)])
    return rows


def frame_rgb(path):
    """A frame written by check_weather (RGB565, little-endian) -> rows of (r, g, b), 8 bits each."""
    raw = path.read_bytes()
    assert len(raw) == W * H * 2, path
    px = struct.unpack(f'<{W * H}H', raw)
    return [[((c >> 11) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31) for c in px[y * W:(y + 1) * W]] for y in range(H)]


def enlarge(rows, scale=SCALE):
    out = bytearray()
    for row in rows:
        line = bytearray()
        for r, g, b in row:
            line += bytes((r, g, b)) * scale
        out += line * scale
    return bytes(out)


def contact_sheet(frames, path, columns=4):
    gap, cw, ch = 8, W * SCALE, H * SCALE
    count = len(frames)
    lines = -(-count // columns)
    sw, sh = gap + columns * (cw + gap), gap + lines * (ch + gap)
    sheet = bytearray(bytes((16, 16, 20)) * (sw * sh))
    for n, rows in enumerate(frames):
        big = enlarge(rows)
        x0, y0 = gap + (n % columns) * (cw + gap), gap + (n // columns) * (ch + gap)
        for y in range(ch):
            at = ((y0 + y) * sw + x0) * 3
            sheet[at:at + cw * 3] = big[y * cw * 3:(y + 1) * cw * 3]
    path.write_bytes(png_bytes(sw, sh, bytes(sheet)))


def quantise(rgb):
    return tuple(v >> 3 for v in rgb)


def main():
    if not OWNER_HEADER.exists() or not CACHE.exists():
        print('SKIP: the PC reference\'s kept replies or its private compile-time pack are not on this PC '
              '(private reference suite, not runnable from a public clone; see test_chromatic_weather_place.py for the '
              'public default)')
        return 0
    exe, how, env = build()
    print(f'INFO built with {how}')
    frames = WORK / 'frames'
    if frames.exists():
        shutil.rmtree(frames)
    frames.mkdir()
    run = subprocess.run([str(exe), 'selftest', str(CACHE), str(frames), str(WX / 'fixtures')], capture_output=True, text=True, env=env)
    values, sizes = {}, {}
    for line in run.stdout.splitlines():
        if line.startswith('VAL '):
            key, _, text = line[4:].partition(' ')
            values[key] = text
        elif line.startswith('ALM '):
            values['alm'] = line[4:]
        elif line.startswith('SIZE '):
            words = line.split()
            sizes = {words[i]: int(words[i + 1]) for i in range(1, len(words), 2)}
            print(line)
        else:
            print(line)
            if line.startswith('FAIL'):
                failed.append(line)
    result(run.returncode == 0 and 'ALL PASS' in run.stdout, 'check_weather selftest', run.stderr[-600:] or f'exit {run.returncode}')

    # the ESP32 side: weather_esp32.c itself against stand-in headers, a scripted HTTP client and scheduler
    stubs = WX / 'host_stubs'
    glue = WORK / 'check_weather_glue.exe'
    done = subprocess.run([GCC, '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Og', '-g', *OWNER, '-fsanitize=undefined', '-fsanitize=bounds-strict',
                           '-fsanitize-undefined-trap-on-error', '-fstack-protector-all', '-include', str(stubs / 'host.h'), f'-I{stubs}',
                           str(WX / 'check_weather_glue.c'), str(ROOT / 'chromatic' / 'launcher' / 'main' / 'weather_esp32.c'),
                           '-o', str(glue), '-lm', '-pthread'], cwd=WORK, capture_output=True, text=True, env=env)
    result(done.returncode == 0, 'weather_esp32.c compiles on the PC against the stand-in headers (-Wall -Wextra -Werror)', done.stderr[:1500])
    if done.returncode == 0:
        try:
            run2 = subprocess.run([str(glue), str(CACHE), str(WX / 'fixtures')], capture_output=True, text=True, env=env, timeout=900)
            print(run2.stdout, end='')
            result(run2.returncode == 0 and 'ALL PASS' in run2.stdout, 'model of the ESP32 side (check_weather_glue)', run2.stderr[-600:] or f'exit {run2.returncode}')
        except subprocess.TimeoutExpired:
            result(False, 'model of the ESP32 side (check_weather_glue)', 'did not finish in 15 minutes')
        tool = shutil.which('size') or str(Path(GCC).parent / 'size.exe')
        obj = WORK / 'weather_esp32.o'
        subprocess.run([GCC, '-std=gnu11', '-Og', '-c', *OWNER, '-include', str(stubs / 'host.h'), f'-I{stubs}',
                        str(ROOT / 'chromatic' / 'launcher' / 'main' / 'weather_esp32.c'), '-o', str(obj)], capture_output=True, env=env)
        sections = subprocess.run([tool, '-A', str(obj)], capture_output=True, text=True, env=env).stdout
        static = sum(int(line.split()[1]) for line in sections.splitlines() if line.split()[:1] in (['.bss'], ['.data']))
        print(f'INFO static RAM of weather_esp32.c on the PC: {static} bytes of .bss and .data (seven 8-byte pointers, the pack '
              'mapping\'s handle, a tick count and flags; on the ESP32 the pointers are 4 bytes: about 50)')
        result(0 < static <= 96, f'weather_esp32.c keeps {static} bytes of static RAM on the PC (pointers and flags only)')

    # the figures against the reference
    data = json.loads((CACHE / 'normalised.json').read_text('utf-8'))
    want = expected_values(data)
    wrong = [f'{k}: console "{values.get(k)}" reference "{v}"' for k, v in want.items() if values.get(k) != v]
    result(not wrong, f'{len(want)} lines of figures equal the PC reference (normalised.json)', '; '.join(wrong[:6]))
    extra = [k for k in values if k not in want and k != 'alm']
    result(not extra, 'no figures the reference does not have', ', '.join(extra))

    # the almanac against the reference's formulas
    epochs = [int((dt.datetime.fromisoformat(data['generated_utc']).replace(tzinfo=None) - dt.datetime(1970, 1, 1)).total_seconds())]
    for stamp in ('2026-03-08T06:59:59', '2026-03-08T07:00:00', '2026-11-01T05:59:59', '2026-11-01T06:00:00', '2026-06-21T16:00:00',
                  '2026-12-21T04:59:00', '2026-12-31T23:59:59', '2027-01-01T05:00:00', '2027-07-04T12:34:56', '2028-02-29T10:00:00',
                  '2028-03-12T08:00:00', '2030-10-15T00:00:01', '2026-12-08T23:50:00', '2027-04-20T21:15:00'):
        epochs.append(int((dt.datetime.fromisoformat(stamp) - dt.datetime(1970, 1, 1)).total_seconds()))
    wrong, worst = [], 0
    for epoch in epochs:
        got = subprocess.run([str(exe), 'almanac', str(epoch)], capture_output=True, text=True, env=env).stdout.strip()
        ref = reference_almanac(epoch)
        body, _, tail = got[4:].partition('|steps ')
        steps, _, offset = tail.partition('|')
        worst = max(worst, int(steps))
        if body + '|' + offset != ref:
            wrong.append(f'{epoch}: console "{body}|{offset}" reference "{ref}"')
    result(not wrong, f'almanac (sunrise, sunset, moon phase and dates, Eastern offset) equals the reference on {len(epochs)} dates',
           '; '.join(wrong[:3]))
    print(f'INFO almanac: the moon search took at most {worst} slices of 4 evaluations (the reference steps about 2,000 times)')

    # sizes
    result(0 < sizes.get('wx_state', 0) <= 6144, f"sizeof(wx_state) {sizes.get('wx_state')} <= 6144")
    result(0 < sizes.get('wx_fetch', 0) <= 2048, f"sizeof(wx_fetch) {sizes.get('wx_fetch')} <= 2048")
    # stack: the compiler's own call graph with each function's frame; the deepest chain under each entry point
    graph = next(iter(WORK.glob('*check_weather*.ci')), None)
    if graph is not None:
        import re
        text = graph.read_text('utf-8', 'replace')
        frame = {m.group(1): int(m.group(2)) for m in re.finditer(r'node: \{ title: "([^"]+)" label: "[^"]*?(\d+) bytes', text)}
        calls = {}
        for m in re.finditer(r'edge: \{ sourcename: "([^"]+)" targetname: "([^"]+)"', text):
            calls.setdefault(m.group(1), set()).add(m.group(2))
        short = lambda n: n.rsplit(':', 1)[-1]

        def deepest(name, seen=()):
            if name in seen:
                return 0
            return frame.get(name, 0) + max([deepest(n, seen + (name,)) for n in calls.get(name, ()) if n in frame] or [0])
        probes = {short(n)[6:]: deepest(n) for n in frame if short(n).startswith('probe_')}
        print('INFO stack on the PC (x86-64, -Og): deepest chain of frames under each entry point, bytes: ' +
              ', '.join(f'{k} {v}' for k, v in sorted(probes.items())) + ' (C library calls such as memcpy not counted)')
        result(bool(probes) and max(probes.values()) < 2048, 'each entry point stays under 2 KB of stack on the PC')
    else:
        print('INFO no call-graph file from this compiler: stack not estimated')

    # previews
    PREVIEWS.mkdir(parents=True, exist_ok=True)
    print(f'INFO previews go to {PREVIEWS.relative_to(ROOT)}' + ('' if '--previews' in sys.argv else ' (run with --previews to replace the kept ones)'))
    for old in PREVIEWS.glob('weather-*.png'):
        old.unlink()
    sets = {'weather-': [], 'weather-nodata-': []}
    for path in sorted(frames.glob('*.rgb565')):
        rows = frame_rgb(path)
        stem = path.stem
        if stem.startswith('weather-stress-'):
            (WORK / (stem + '.png')).write_bytes(png_bytes(W * SCALE, H * SCALE, enlarge(rows)))
            continue
        (PREVIEWS / (stem + '.png')).write_bytes(png_bytes(W * SCALE, H * SCALE, enlarge(rows)))
        if stem.startswith('weather-nodata-'):
            sets['weather-nodata-'].append(rows)
        elif not stem.startswith(('weather-alert-', 'weather-roll-', 'weather-radar-', 'weather-fallback-', 'weather-updating-')):
            sets['weather-'].append(rows)
    contact_sheet(sets['weather-'], PREVIEWS / 'weather-contact-sheet.png')
    contact_sheet(sets['weather-nodata-'], PREVIEWS / 'weather-nodata-contact-sheet.png')
    stress = [frame_rgb(p) for p in sorted(frames.glob('weather-stress-*.rgb565'))]
    if stress:
        contact_sheet(stress, WORK / 'weather-stress-contact-sheet.png')
    result(len(sets['weather-']) >= 12 and len(sets['weather-nodata-']) >= 12,
           f"previews written: {len(sets['weather-'])} pages with data, {len(sets['weather-nodata-'])} without, in {PREVIEWS.relative_to(ROOT)}")

    # the page bodies against the approved reference pages (the header clock and the ticker differ by design)
    pairs = [('00-ident', '00-ident', 0), ('01-current-conditions-1', '01-current-conditions-1', 23), ('02-current-conditions-2', '02-current-conditions-2', 23),
             ('03-latest-observations-1', '03-latest-observations-1', 23), ('04-latest-observations-2', '04-latest-observations-2', 23),
             ('05-almanac', '05-almanac-sun-moon', 23), ('06-local-forecast-1', '07-local-forecast-1', 23),
             ('07-local-forecast-2', '08-local-forecast-2', 23), ('08-local-forecast-3', '09-local-forecast-3', 23),
             ('09-extended-forecast-1', '10-extended-forecast-1', 23), ('10-extended-forecast-2', '11-extended-forecast-2', 23),
             ('11-regional-state', '12-regional-forecast-state', 23), ('12-regional-close', '13-regional-forecast-*', 23), ('13-current-radar', '14-current-radar', 23)]
    report, total = [], 0
    for mine, theirs, top in pairs:
        a, b = frames / f'weather-{mine}.rgb565', REF / 'out' / 'pages' / f'{theirs}.png'
        if theirs.endswith('*'):  # the reference's own name for its close-up regional page
            b = next(iter(sorted((REF / 'out' / 'pages').glob(f'{theirs}.png'))), b)
        if not a.exists() or not b.exists():
            report.append(f'{mine} missing')
            total += 99999
            continue
        got, ref = frame_rgb(a), read_png(b)
        if ref is None:  # review L3 of 1.6.4: a reference that cannot be read is a failure, never a silent pass
            report.append(f'{theirs}.png NOT READABLE')
            total += 99999
            continue
        diff = sum(1 for y in range(top, 131) for x in range(W) if quantise(got[y][x]) != quantise(ref[y][x]))
        total += diff
        report.append(f'{mine} {diff}')
    print('INFO pixels that differ from the approved reference pages, body rows only: ' + ', '.join(report))
    result(total == 0, 'page bodies are pixel-identical to the approved reference pages (14 pages)', f'{total} pixels differ')

    # Review L3 of 1.6.4: whole frames against the console code's own 1.6.0 previews (made 2026-11-19, before 1.6.4):
    # pages with data byte-identical; pages without data may differ only in the bottom bar (its 1.6.4 wording).
    kept = ROOT / 'chromatic' / 'launcher' / 'previews-1.6.0'
    if PREVIEWS != kept:
        same, bar_only, wrong = 0, 0, []
        for old in sorted(kept.glob('weather-*.png')):
            new = PREVIEWS / old.name
            if not new.exists():
                continue
            a, b = read_png(new), read_png(old)
            if new.read_bytes() == old.read_bytes() or (a is not None and a == b):
                same += 1  # the same pixels (the PNG bytes also depend on the Python's zlib)
                continue
            if a is None or b is None or len(a) != len(b) or not ('nodata' in old.name or 'no-data' in old.name):
                wrong.append(old.name)
                continue
            rows = {y // SCALE for y in range(len(a)) if a[y] != b[y]}
            if rows and min(rows) >= 131:
                bar_only += 1
            else:
                wrong.append(f'{old.name} rows {min(rows) if rows else "-"}-{max(rows) if rows else "-"}')
        result(same >= 20 and not wrong, f'whole frames against the 1.6.0 previews: {same} byte-identical, {bar_only} '
               'without data differ only in the bottom bar (the 1.6.4 wording)', ', '.join(wrong[:4]))

    print()
    print('ALL PASS' if not failed else f'FAILED: {len(failed)}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())

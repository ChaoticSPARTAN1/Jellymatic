# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""The weather location (launcher 1.6.3), the built-in Orlando, FL default (1.6.4) and location packs (1.6.5): the
record and the pack, their readers on the console and their writers in Chromatic Manager, and the Local Weather channel
for any place.

  - The Python writers (scripts/chromatic_weather_location.py, scripts/chromatic_location_pack.py) and the console's
    readers (chromatic/weather/wx_place.h, wx_lpack.h, built here into check_weather_place.c) agree on every vector:
    good records and packs, every single byte of one changed, torn, blank, zeroed, another version, every field out of
    range (with a correct CRC, so only the field check can refuse it), and which of two copies is used.
  - With no record the request list is exactly the compiled-in Orlando, FL table of wx_location.h; a record naming
    its home exactly gives that table; a record with a location pack made for it gives the pack's table, maps and
    radar; a pack for another place is not used.
  - A place's request list (Denver, CO; Tampa and Winter Park, FL) is what the design says: only api.weather.gov,
    its stations, grid and point; a Florida place also Florida's nine cities; the radar only inside the KMLB view.
  - Time zones: the console's US rule against Python's zoneinfo (or the same rule written here).
  - The channel fed real reply shapes (chromatic/weather/fixtures/places/, captured from api.weather.gov for public
    places): pages, figures, the place's clock, previews; the Orlando default's pages with its KMLB radar picture;
    Local Weather with nothing held yet.
  - The location pack maker (scripts/make_location_pack.py) from recorded public replies (fixtures/packs/: Denver,
    CO and Orlando, FL): valid packs, drawn by the console code with every page (previews and contact sheets).
  - weather_esp32.c itself in the model of the ESP32 side (check_weather_glue.c) with the record and a pack in its
    simulated flash (needs the PC reference's kept replies, owner-only: SKIP elsewhere).
  - The lookup on the PC (ZIP and "City, ST" from the bundled Census table), station labels and completing a place
    from recorded /points and /stations replies; the PC's ORLANDO place and region view equal the console's pack.
No network, no hardware, nothing private. Previews go to staging/chromatic-weather-place/previews/ (with --previews
also to chromatic/launcher/previews-1.6.5/). Exits 0 only when everything passes.
"""
import datetime as dt
import json
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import zlib
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chromatic_location_pack as lp  # noqa: E402
import chromatic_weather_location as wl  # noqa: E402
import make_location_pack as mk  # noqa: E402
import test_chromatic_weather as tw  # noqa: E402  (PNG helpers, compiler)

ROOT = Path(__file__).resolve().parents[1]
WX = ROOT / 'chromatic' / 'weather'
FIX = WX / 'fixtures' / 'places'
PACKS = WX / 'fixtures' / 'packs'
WORK = ROOT / 'staging' / 'chromatic-weather-place'
PREVIEWS = [WORK / 'previews'] + ([ROOT / 'chromatic' / 'launcher' / 'previews-1.6.5'] if '--previews' in sys.argv else [])
GCC = tw.GCC
CACHE = ROOT / 'chromatic' / 'iptv' / 'weather' / 'cache'   # the PC reference's kept replies (owner-only)
failed = []
CAPTURED = {'denver': (39.7392, -104.9903)}


def result(ok, name, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (f': {detail}' if detail and not ok else ''))
    if not ok:
        failed.append(name)


# Florida places that are not the pack's home: Tampa lies outside the KMLB radar view (Regional Forecast maps, no
# radar); Winter Park inside it; "NEAR_ORLANDO" is Orlando's point with another grid (so not the built-in pack).
TAMPA = wl.Place('TAMPA, FL', 'FL', 279506, -824572, -300, 1, 'TBW', 71, 98, (wl.Station('KTPA', 'TAMPA'), wl.Station('KPIE', 'ST PETERSBURG')))
WINTER_PARK = wl.Place('WINTER PARK, FL', 'FL', 286000, -813392, -300, 1, 'MLB', 27, 71,
                       (wl.Station('KORL', 'ORLANDO EXEC'), wl.Station('KMCO', 'ORLANDO'), wl.Station('KSFB', 'SANFORD')))
NEAR_ORLANDO = wl.replace(wl.ORLANDO, grid_y=69)


def env():
    if shutil.which('gcc'):
        return None
    return dict(os.environ, PATH=str(Path(GCC).parent) + os.pathsep + os.environ.get('PATH', ''))


def fixtures(place):
    index = json.loads((FIX / place / 'index.json').read_text('utf-8'))

    def fetch(path):
        if path.startswith('/points/'):  # the recorded reply, whatever the table's point
            path = next(p for p in index if p.startswith('/points/'))
        return json.loads((FIX / place / index[path]).read_text('utf-8'))
    return index, fetch


def captured_place(place):
    lat, lon = CAPTURED[place]
    _, fetch = fixtures(place)
    found = wl.find({'denver': 'Denver, CO'}[place])[0]
    found = wl.Found(found.name, found.state, lat, lon)  # the point the replies were captured for
    return wl.resolve(found, fetch)


def recorded_pack(key, query):
    """(place, pack) made by the pack maker from the recorded public replies of fixtures/packs/<key>."""
    fix = PACKS / key
    index = json.loads((fix / 'index.json').read_text('utf-8'))
    radar = json.loads((fix / 'radar.json').read_text('utf-8'))
    fetch = lambda path: json.loads((fix / index[path]).read_text('utf-8'))
    place = wl.resolve(wl.find(query)[0], fetch)
    return place, mk.make(place, fetch, lambda p: radar.get(p, False))


def slug(url):
    """The file name the models give a request's reply (check_weather_glue.c, check_weather_place.c)."""
    out, gap = [], False
    for ch in url.split('://', 1)[-1]:
        if ch.isascii() and (ch.isalnum() or ch in '.-'):
            if gap and out:
                out.append('_')
            gap = False
            out.append(ch)
        else:
            gap = True
    return ''.join(out)


def build(name='check_weather_place', defines=()):
    WORK.mkdir(parents=True, exist_ok=True)
    exe = WORK / f'{name}.exe'
    base = [GCC, '-std=c99', '-Wall', '-Wextra', '-Werror', '-Og', '-g', *defines, f'-I{WX}',
            str(WX / 'check_weather_place.c'), '-o', str(exe), '-lm']
    for flags in (['-fsanitize=address,undefined'], ['-fsanitize=undefined', '-fsanitize=bounds-strict',
                                                       '-fsanitize-undefined-trap-on-error', '-fstack-protector-all']):
        done = subprocess.run(base[:1] + flags + base[1:], cwd=WORK, capture_output=True, text=True, env=env())
        if done.returncode == 0:
            return exe
    print(done.stderr[:2000])
    raise SystemExit(f'FAIL build of check_weather_place.c ({name})')


def run(exe, *args):
    done = subprocess.run([str(exe), *map(str, args)], capture_output=True, text=True, env=env(), timeout=300)
    if done.returncode not in (0,):
        print(done.stdout[-1500:], done.stderr[-1500:])
    return done.stdout


# ---- record vectors ------------------------------------------------------------------------------------------
def with_crc(body):
    body = bytes(body[:252])
    return body + struct.pack('<I', zlib.crc32(body) & 0xFFFFFFFF)


def vectors(denver, second):
    good = [denver.pack(1), second.pack(7), denver.pack(0xFFFFFFFF),
            wl.Place('HILO, HI', 'HI', 197074, -1550847, -600, 0, 'HFO', 202, 86, (wl.Station('PHTO', 'HILO'),)).pack(3),
            wl.Place("O'FALLON, MO", 'MO', 388106, -906999, -360, 1, 'LSX', 82, 76,
                     tuple(wl.Station(f'K{c}{c}{c}', f'STATION {c}') for c in 'ABCDEF')).pack(12),
            wl.Place('PHOENIX, AZ', 'AZ', 334484, -1120740, -420, 0, 'PSR', 159, 58, (wl.Station('KPHX', 'PHOENIX'),)).pack(2),
            wl.Place('BOISE, ID', 'ID', 436150, -1162023, -420, 1, 'BOI', 132, 87, (wl.Station('KBOI', 'BOISE'),)).pack(5),
            wl.ORLANDO.pack(4), TAMPA.pack(6), NEAR_ORLANDO.pack(10)]
    bad = [b'\xff' * 256, bytes(256), good[0][:128] + b'\xff' * 128, b'PJWX' + b'\xff' * 252]
    r = bytearray(good[0])
    for at, value in ((4, 2), (5, 0), (5, 7), (6, 255), (14, 2), (15, 1), (34, 1), (35, 1), (204, 1), (251, 1)):
        x = bytearray(r)
        x[at] = value
        bad.append(with_crc(x))

    def field(at, data, record=None):
        x = bytearray(record or r)
        x[at:at + len(data)] = data
        return with_crc(x)
    one = good[3]  # a place with one station: the five unused slots must stay zero
    bad += [field(108 + 16 * 5, b'EXTRA'.ljust(16, b'\0'), one), field(60 + 8 * 5, b'KXYZ'.ljust(8, b'\0'), one),
            field(60 + 8, b'KXYZ'.ljust(8, b'\0'), one)]
    bad += [field(0, b'PJWY'), field(8, bytes(4)), field(12, struct.pack('<h', -721)), field(12, struct.pack('<h', 841)),
            field(12, struct.pack('<h', -307)), field(16, struct.pack('<i', 900001)), field(20, struct.pack('<i', -1800001)),
            field(24, struct.pack('<H', 10000)), field(28, b'BO\0\0'), field(28, b'BOUX'), field(28, b'bou\0'),
            field(32, b'C1'), field(36, b'denver, co'.ljust(24, b'\0')), field(36, b' DENVER'.ljust(24, b'\0')),
            field(36, b'DENVER '.ljust(24, b'\0')), field(36, b'A' * 24), field(36, bytes(24)),
            field(36, b'DEN\0VER'.ljust(24, b'\0')), field(36, b'DENVER; CO'.ljust(24, b'\0')),
            field(60, b'KB/C\0\0\0\0'), field(60, b'KB\0\0\0\0\0\0'), field(60, b'KBJCKBJC'), field(108, bytes(16)),
            field(108, b'X' * 16), field(108, b'1ABC'.ljust(16, b'\0')), field(108, b'DENVER '.ljust(16, b'\0'))]
    flips = []
    for at in range(256):
        x = bytearray(good[0])
        x[at] ^= 0x10
        flips.append(bytes(x))
    random.seed(7)
    noise = [bytes(random.randrange(256) for _ in range(256)) for _ in range(200)]
    noise += [b'PJWX\x01' + bytes(random.randrange(256) for _ in range(251)) for _ in range(200)]
    return good, bad, flips, noise


def parse_c(line):
    return dict(re.findall(r'(\w+)=(.*?)(?= \w+=|$)', line.split(' ', 3)[3] if line.count(' ') >= 3 else ''))


def expected_fields(place, sequence):
    out = {'name': place.name, 'state': place.state, 'lat': str(place.lat_e4), 'lon': str(place.lon_e4),
           'tz': str(place.tz_std_min), 'dst': str(place.tz_dst), 'office': place.office,
           'grid': f'{place.grid_x},{place.grid_y}', 'n': str(len(place.stations)),
           'radar': str(int(place.shows()['radar'])), 'seq': str(sequence),
           'pack': place.state if place.shows()['regional'] else '-', 'builtin': str(int(place.builtin() is not None))}
    for i, s in enumerate(place.stations):
        out[f'st{i}'] = f'{s.id}|{s.label}'
    return out


def check_records(exe, denver, second):
    good, bad, flips, noise = vectors(denver, second)
    records = good + bad + flips + noise
    path = WORK / 'records.bin'
    path.write_bytes(b''.join(records))
    lines = [l for l in run(exe, 'records', path).splitlines() if l.startswith(('REC', 'FAIL'))]
    result(len(lines) == len(records) and not any(l.startswith('FAIL') for l in lines),
           f'the console reads all {len(records)} vectors and a refused one leaves nothing', f'{len(lines)} lines')
    disagree = []
    for n, (raw, line) in enumerate(zip(records, lines)):
        py = wl.parse(raw)
        c_ok = line.split()[2] == '1'
        if bool(py) != c_ok:
            disagree.append(f'#{n}: python {bool(py)} console {c_ok}')
            continue
        if py:
            want = expected_fields(*py)
            got = parse_c(line)
            wrong = {k: (got.get(k), v) for k, v in want.items() if got.get(k) != v}
            if wrong:
                disagree.append(f'#{n}: {wrong}')
    result(not disagree, f'writer and reader agree on every vector ({len(good)} good, {len(bad)} with one field wrong, '
           f'{len(flips)} single bytes changed, {len(noise)} random)', '; '.join(disagree[:4]))
    c_good = [l.split()[2] == '1' for l in lines]
    result(all(c_good[:len(good)]) and not any(c_good[len(good):]),
           'every good record is read; every other one (blank, zeroed, torn, changed, out of range, random) means the default',
           str([i for i, ok in enumerate(c_good) if ok != (i < len(good))][:10]))
    result(parse_c(lines[0]).get('area') == 'DENVER AREA' and parse_c(lines[1]).get('area') == 'WINTER PARK AREA'
           and parse_c(lines[4]).get('area') == "O'FALLON AREA", 'opening card area line ("DENVER AREA")')
    a1, a2 = denver.pack(1), second.pack(2)
    pairs = [(a1, b'\xff' * 256, '1', 1), (b'\xff' * 256, a2, '1', 2), (a1, a2, '1', 2), (a2, a1, '1', 2),
             (a1, denver.pack(1), '1', 1), (denver.pack(0xFFFFFFFF), second.pack(1), '1', 1), (second.pack(1), denver.pack(0xFFFFFFFF), '1', 1),
             (a2[:100] + b'\xff' * 156, a1, '1', 1), (a1, a2[:200] + b'\xff' * 56, '1', 1), (b'\xff' * 256, b'\xff' * 256, '0', 0),
             (bytes(256), b'\xff' * 256, '0', 0), (second.pack(0x80000001), denver.pack(1), '1', 0x80000001)]
    (WORK / 'pairs.bin').write_bytes(b''.join(a + b for a, b, _, _ in pairs))
    picks = [l.split() for l in run(exe, 'pick', WORK / 'pairs.bin').splitlines() if l.startswith('PICK')]
    wrong = []
    for (a, b, ok, seq), got in zip(pairs, picks):
        py = wl.current(a, b)
        if got[2] != ok or int(got[3]) != seq or (py[1] if py[0] else 0) != seq:
            wrong.append(f'{got} want {ok} {seq}, python {py[1]}')
    result(len(picks) == len(pairs) and not wrong, f'which of the two copies is used ({len(pairs)} cases: newer sequence, '
           'wrap-around, torn, blank, zeroed)', '; '.join(wrong[:3]))


# ---- request lists -------------------------------------------------------------------------------------------
PREFIX = {'FL': 'wx_fl_'}


def compiled_table(state='FL'):
    """The built-in pack's request table from wx_location.h, read here independently of the C code."""
    text = (WX / 'wx_location.h').read_text('utf-8')
    prefix = PREFIX[state]
    hosts = {'WX_HOST': 'api.weather.gov', 'WX_RADAR_HOST': 'opengeo.ncep.noaa.gov'}
    block = re.search(rf'static const wx_request {prefix}requests\[\d+\]=\{{(.*?)\n\}};', text, re.S).group(1)
    rows = re.findall(r'\{(WX_HOST|WX_RADAR_HOST),"([^"]+)",WX_K_(\w+),(\d+)\}', block)
    periods = [int(p) for p in re.search(rf'{prefix}request_period\[\d+\]=\{{([^}}]*)\}}', text).group(1).split(',')]
    kinds = ['OBS', 'RECENT', 'HISTORY', 'FORECAST', 'CITY', 'ALERTS', 'RADAR']
    assert len(rows) == len(periods), state
    return [(kinds.index(k), int(slot), period, f'https://{hosts[h]}{path}') for (h, path, k, slot), period in zip(rows, periods)]


def pack_table(pack):
    hosts = ('api.weather.gov', 'opengeo.ncep.noaa.gov')
    return [(r.kind, r.slot, r.period, f'https://{hosts[r.host]}{r.path}') for r in pack.requests]


def requests(exe, record=None, pack=None):
    out = run(exe, 'requests', *([record] if record else []), *([f'--pack={pack}'] if pack else []))
    rows = []
    for line in out.splitlines():
        if line.startswith('REQ'):
            _, i, kind, slot, period, host_ok, url = line.split(' ', 6)
            rows.append((int(kind), int(slot), int(period), url, host_ok == '1'))
    end = re.search(r'END (\d+) (\d)', out)
    used = re.search(r'PACK good=(\d) used=(\d)', out)
    return rows, end and end.group(2) == '0', (used.group(1, 2) if used else None)


def expected_list(place):
    """What the design says a place asks for, written out independently of wx_place.h."""
    api = 'https://api.weather.gov'
    s = place.stations
    if place.builtin():  # the pack's home: that pack's whole table
        return compiled_table(place.state)
    lat = lambda v: f'{"-" if v < 0 else ""}{abs(v) // 10000}.{abs(v) % 10000:04d}'
    out = [(0, 0, 600, f'{api}/stations/{s[0].id}/observations/latest'),
           (3, 255, 1800, f'{api}/gridpoints/{place.office}/{place.grid_x},{place.grid_y}/forecast'),
           (5, 0, 600, f'{api}/alerts/active?point={lat(place.lat_e4)},{lat(place.lon_e4)}'),
           (2, 0, 3600, f'{api}/stations/{s[0].id}/observations?limit=40')]
    table = compiled_table(place.state) if place.state in PREFIX else []
    out += [(4, slot, 3600, url) for kind, slot, _, url in table if kind in (3, 4)]  # its state's nine cities
    out += [(0, i, 1800, f'{api}/stations/{st.id}/observations/latest') for i, st in enumerate(s) if i]
    out += [(1, i, 1800, f'{api}/stations/{st.id}/observations?limit=8') for i, st in enumerate(s) if i]
    if place.shows()['radar']:
        out += [(6, 0, 600, url) for kind, _, _, url in table if kind == 6]
    return out


def check_requests(exe, places):
    rows, closed, _ = requests(exe)
    fl = compiled_table('FL')
    result(len(fl) == 33 and [r[:4] for r in rows] == fl and closed and all(r[4] for r in rows)
           and any('/gridpoints/MLB/26,68/forecast' in r[3] for r in rows) and any('kmlb_sr_bref' in r[3] for r in rows),
           'no record: the request list is exactly the compiled-in Orlando, FL table (33 requests: KMCO, MLB 26,68, '
           'the Florida cities and stations, the KMLB radar picture)')
    record = WORK / 'orlando.rec'
    record.write_bytes(wl.ORLANDO.pack(1))
    got, closed, _ = requests(exe, record)
    result([r[:4] for r in got] == fl and closed and all(r[4] for r in got),
           'the ORLANDO record (the pack\'s home exactly): that pack\'s whole table, byte for byte')
    for name, place in places:
        record = WORK / f'{name}.rec'
        record.write_bytes(place.pack(1))
        rows, closed, _ = requests(exe, record)
        want = expected_list(place)
        hosts = {re.match(r'https://([^/]+)', r[3]).group(1) for r in rows}
        result([r[:4] for r in rows] == want and closed and all(r[4] for r in rows)
               and (hosts == {'api.weather.gov'} or (place.shows()['radar'] and hosts == {'api.weather.gov', 'opengeo.ncep.noaa.gov'})),
               f'{name}: {len(rows)} requests, as the design says (its stations, grid and point; region cities: '
               f'{place.state if place.state in PREFIX else "none"}; radar: {"yes" if place.shows()["radar"] else "no"})',
               f'{[r[:4] for r in rows][:3]} vs {want[:3]}')
        point = f'{place.lat_e4 / 10000:.4f},{place.lon_e4 / 10000:.4f}'
        result(all(point not in r[3] or 'api.weather.gov' in r[3] for r in rows),
               f'{name}: the place\'s point goes to api.weather.gov only')


# ---- location packs ------------------------------------------------------------------------------------------
def framed(chunks):
    return b''.join(struct.pack('<I', len(c)) + c for c in chunks)


def pack_vectors(packs):
    """Good packs; each one with one field wrong (CRC recomputed so only the field check refuses it); every header
    byte and many body bytes changed; torn; random."""
    good = [p.pack(s) for p, s in zip(packs, (1, 7, 0xFFFFFFFF, 3))]
    base = good[0]

    def recrc(raw):
        total = struct.unpack_from('<I', raw, 8)[0]
        body = bytes(raw[:total - 4])
        return body + struct.pack('<I', zlib.crc32(body) & 0xFFFFFFFF)

    def field(at, data, raw=base):
        x = bytearray(raw)
        x[at:at + len(data)] = data
        return recrc(x)
    p = packs[0]
    data_at = lp.HEADER + 4 * len(p.cities) + len(p.obs_labels)
    req_at = data_at
    bad = [b'\xff' * 200, bytes(200), base[:len(base) // 2], base[:-1], b'PJLP' + bytes(100),
           field(0, b'PJLQ'), field(4, b'\x02'), field(5, b'\x00'), field(5, bytes([34])), field(6, b'\x00'), field(6, bytes([10])),
           field(7, b'\x00'), field(7, bytes([12])), field(12, bytes(4)), field(16, struct.pack('<i', 900001)),
           field(24, struct.pack('<H', 10000)), field(28, b'bou\0'), field(28, b'BOUX'), field(32, b'c1'),
           field(34, b'\x00'), field(34, bytes([13])), field(35, bytes([len(p.cities)])), field(36, bytes([160])),
           field(37, bytes([10])), field(37, bytes([123])), field(39, bytes([10])), field(40, bytes([10])),
           field(41, bytes([200])), field(42, bytes([22])), field(44, struct.pack('<i', 1800001)), field(60, bytes([160])),
           field(61, bytes([10])), field(74, b'\x01'), field(79, b'\x01'),
           field(lp.HEADER, bytes([200, 10])),                                    # a city spot off the map
           field(lp.HEADER + 4 * len(p.cities), bytes([p.stations])),             # an obs slot past the stations
           field(req_at, bytes([7])),                                             # a request kind that does not exist
           field(req_at + 1, bytes([30])),                                        # an OBS slot past the stations
           field(req_at + 2, bytes([1])),                                         # an OBS request on the map host
           field(req_at + 3, struct.pack('<H', 10))]                              # a period under a minute
    # the strings and maps: a bad character in the name, a wrong run in a map
    strings_at = len(base) - 4 - struct.unpack_from('<H', base, 72)[0]
    bad += [field(strings_at, b'd'), field(strings_at, b' ')]
    map_at = data_at + 5 * len(p.requests)
    bad += [field(map_at, b'\x00'), field(map_at + 1, b'\x20'), field(map_at, b'\xff')]
    flips = []
    for at in list(range(lp.HEADER)) + list(range(lp.HEADER, len(base), max(1, len(base) // 300))):
        x = bytearray(base)
        x[at] ^= 0x10
        flips.append(bytes(x))
    random.seed(11)
    noise = [bytes(random.randrange(256) for _ in range(300)) for _ in range(100)]
    noise += [base[:16] + bytes(random.randrange(256) for _ in range(len(base) - 16)) for _ in range(100)]
    return good, bad, flips, noise


def check_packs(exe, denver_place, denver_pack, orlando_place, orlando_pack, builtin):
    packs = [denver_pack, orlando_pack, builtin, replace(builtin, radar_map=b'', radar_marks=(0,) * 6, radar_towns=('', '', ''),
                                                         radar_view=(0, 0, 0, 0),
                                                         requests=tuple(r for r in builtin.requests if r.kind != lp.RADAR))]
    result(all(not p.problems() for p in packs) and all(p.size() <= lp.COPY for p in packs),
           f'the made packs (Denver {denver_pack.size()} B, Orlando {orlando_pack.size()} B), the built-in Orlando tables '
           f'as a pack ({builtin.size()} B) and one without radar are valid and fit one 8 KiB copy')
    good, bad, flips, noise = pack_vectors(packs)
    every = good + bad + flips + noise
    path = WORK / 'packs.bin'
    path.write_bytes(framed(every))
    lines = [l for l in run(exe, 'lpacks', path).splitlines() if l.startswith(('LP', 'FAIL'))]
    disagree = []
    for n, (raw, line) in enumerate(zip(every, lines)):
        py = lp.parse(raw)
        c_ok = line.split()[2] == '1'
        if bool(py) != c_ok:
            disagree.append(f'#{n}: python {bool(py)} console {c_ok}')
        elif py:
            fields = dict(re.findall(r'(\w+)=(\S+)', line))
            if fields.get('seq') != str(py[1]) or fields.get('reqs') != str(len(py[0].requests)):
                disagree.append(f'#{n}: {fields}')
    c_good = [l.split()[2] == '1' for l in lines if l.startswith('LP')]
    result(len(lines) == len(every) and not any(l.startswith('FAIL') for l in lines) and not disagree
           and all(c_good[:len(good)]) and not any(c_good[len(good):len(good) + len(bad)]),
           f'pack writer and console reader agree on every vector ({len(good)} good, {len(bad)} with one field wrong, '
           f'{len(flips)} single bytes changed, {len(noise)} random); a refused pack leaves nothing', '; '.join(disagree[:4]))
    a1, a2 = denver_pack.pack(1), denver_pack.pack(2)
    torn = a2[:len(a2) // 2] + b'\xff' * (len(a2) - len(a2) // 2)
    pairs = [(a1, b'\xff' * 64), (b'\xff' * 64, a2), (a1, a2), (a2, a1), (torn, a1), (a1, torn), (b'\xff' * 64, b'\xff' * 64),
             (denver_pack.pack(0xFFFFFFFF), denver_pack.pack(1)), (bytes(64), a1)]
    (WORK / 'pack-pairs.bin').write_bytes(b''.join(framed([a, b]) for a, b in pairs))
    picks = [l.split() for l in run(exe, 'lpick', WORK / 'pack-pairs.bin').splitlines() if l.startswith('LPICK')]
    wrong = []
    for (a, b), got in zip(pairs, picks):
        py = lp.current(a, b)
        want_seq = py[1] if py[0] else 0
        if int(got[3]) != want_seq:
            wrong.append(f'{got} python {want_seq}')
    result(len(picks) == len(pairs) and not wrong, f'which of the two pack copies is used ({len(pairs)} cases: newer, wrap, '
           'torn, blank), as the console reads them (the header first, then the copy in full)', '; '.join(wrong[:3]))
    # a record with its pack: the pack's table; a pack for another place: not used
    record = WORK / 'denver-pack.rec'
    record.write_bytes(denver_place.pack(1))
    pack_file = WORK / 'denver.pjlp'
    lp.save_file(denver_pack, pack_file)
    rows, closed, used = requests(exe, record, pack_file)
    result(used == ('1', '1') and [r[:4] for r in rows] == pack_table(denver_pack) and closed and all(r[4] for r in rows),
           f'a record with its pack: the pack\'s whole table ({len(rows)} requests: its towns, stations and radar)')
    other = WORK / 'winter.rec'
    other.write_bytes(WINTER_PARK.pack(1))
    rows, closed, used = requests(exe, other, pack_file)
    result(used == ('1', '0') and [r[:4] for r in rows] == expected_list(WINTER_PARK), 'a pack for another place is not used')
    result(lp.load_file(pack_file) == denver_pack and denver_pack.binds(denver_place) and not denver_pack.binds(WINTER_PARK),
           'a pack saved as a file reads back the same, checked; it binds to its own place only')
    bad_file = WORK / 'bad.pjlp'
    bad_file.write_bytes(pack_file.read_bytes()[:-2] + b'\x00\x00')
    try:
        lp.load_file(bad_file)
        result(False, 'a damaged pack file is refused')
    except lp.PackError:
        result(True, 'a damaged pack file is refused in plain words')
    # every page of the made packs, drawn by the console code (no data: the maps, call-outs and radar view)
    for key, place, pack in (('denver', denver_place, denver_pack), ('orlando', orlando_place, orlando_pack)):
        frames = WORK / f'frames-pack-{key}'
        shutil.rmtree(frames, ignore_errors=True)
        frames.mkdir()
        replies = WORK / f'replies-pack-{key}'
        shutil.rmtree(replies, ignore_errors=True)
        replies.mkdir()
        shutil.copyfile(WX / 'fixtures' / 'radar-pattern.gif', replies / 'radar.gif')
        rec = WORK / f'{key}-pack.rec'
        rec.write_bytes(place.pack(1))
        pf = WORK / f'{key}.pjlp'
        lp.save_file(pack, pf)
        out = run(exe, 'pages', rec, replies, frames, 1795131996, f'--pack={pf}')
        kinds = set(re.search(r'PAGES \d+ (.*)', out).group(1).split())
        result({'ident', 'regional-state', 'regional-close', 'current-radar'} <= kinds and 'FAIL' not in out
               and f'NAME {place.name}|' in out, f'{place.name} with its pack: every page drawn, the two maps and the radar '
                                                 'among them; no see-through colour')
        files = sorted(frames.glob('*.rgb565'))
        for target in PREVIEWS:
            target.mkdir(parents=True, exist_ok=True)
            for old in target.glob(f'location-pack-{key}-*.png'):
                old.unlink()
            for f in files:
                (target / f'location-pack-{key}-{f.stem[6:]}.png').write_bytes(
                    tw.png_bytes(tw.W * tw.SCALE, tw.H * tw.SCALE, tw.enlarge(tw.frame_rgb(f))))
            tw.contact_sheet([tw.frame_rgb(f) for f in files], target / f'location-pack-{key}-contact-sheet.png')
    check_orlando_look(exe, orlando_place, orlando_pack)


def rows_of(data):
    px = struct.unpack(f'<{tw.W * tw.H}H', data)
    return [[((c >> 11) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31) for c in px[y * tw.W:(y + 1) * tw.W]]
            for y in range(tw.H)]


def check_orlando_look(exe, place, pack):
    """The tool's Orlando pack beside the built-in (hand-made) Orlando pages: the same pages in the same look; the
    figures pages identical; the maps, the radar labels and the station rows chosen by the tool, not by hand."""
    import chromatic_weather_preview as wp
    builtin = wp.pages(None, None, exe)
    made = wp.pages(place, pack, exe)
    same = [a for (a, da), (b, db) in zip(builtin, made) if a == b and da == db]
    differ = {a: sum(da[i:i + 2] != db[i:i + 2] for i in range(0, len(da), 2)) for (a, da), (b, db) in zip(builtin, made) if da != db}
    print(f'INFO the tool\'s Orlando pack beside the built-in pages: identical {len(same)}; differing pixels per page {differ}')
    result([n for n, _ in builtin] == [n for n, _ in made] and len(builtin) == 12
           and {'00-ident-1', '06-local-forecast-1', '07-extended-forecast-1'} <= set(same)
           and all(p < tw.W * tw.H // 4 for p in differ.values()),
           'the tool\'s Orlando pack shows the same 12 pages as the built-in Orlando in the same look (opening card and '
           'forecasts identical; maps, radar call-outs and nearby stations chosen by the tool, under a quarter of a page each)',
           str(differ))
    for target in PREVIEWS:
        target.mkdir(parents=True, exist_ok=True)
        pairs = [rows_of(d) for (a, da), (_, db) in zip(builtin, made) if a in differ for d in (da, db)]
        tw.contact_sheet(pairs, target / 'location-pack-orlando-vs-built-in.png', columns=2)


# ---- time zones ----------------------------------------------------------------------------------------------
def us_offset(epoch, std, dst):
    if not dst:
        return std
    day = dt.datetime(1970, 1, 1) + dt.timedelta(seconds=epoch)

    def sunday(month, n):
        first = dt.date(day.year, month, 1)
        return first + dt.timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))
    start = dt.datetime.combine(sunday(3, 2), dt.time()) - dt.datetime(1970, 1, 1)
    end = dt.datetime.combine(sunday(11, 1), dt.time()) - dt.datetime(1970, 1, 1)
    s, e = start.total_seconds() + (120 - std) * 60, end.total_seconds() + (60 - std) * 60
    return std + 60 if s <= epoch < e else std


def check_zones(exe):
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo('America/Denver')
    except Exception:
        ZoneInfo = None
    epochs = []
    for year in (2026, 2027, 2028):
        for month, n in ((3, 2), (11, 1)):
            first = dt.date(year, month, 1)
            sunday = first + dt.timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))
            base = int((dt.datetime.combine(sunday, dt.time()) - dt.datetime(1970, 1, 1)).total_seconds())
            epochs += [base + h * 3600 + d for h in range(4, 15) for d in (-1, 0, 1)]
    epochs += [1795131996, 1767225600, 1800000000, 1830000000]
    wrong = []
    for zone_name, (std, dst) in sorted(wl.ZONES.items()):
        out = run(exe, 'tz', std, dst, *epochs)
        for line in out.splitlines():
            _, epoch, offset, day, clock, _ = line.split()
            epoch, offset = int(epoch), int(offset)
            want = us_offset(epoch, std, dst)
            if ZoneInfo is not None:
                local = dt.datetime.fromtimestamp(epoch, ZoneInfo(zone_name))
                want = int(local.utcoffset().total_seconds() // 60)
            text = (dt.datetime(1970, 1, 1) + dt.timedelta(seconds=epoch + want * 60)).strftime('%Y-%m-%d %H:%M:%S')
            if offset != want or f'{day} {clock}' != text:
                wrong.append(f'{zone_name} {epoch}: console {offset} {day} {clock}, want {want} {text}')
    result(not wrong, f'time zones: {len(wl.ZONES)} US zones x {len(epochs)} times around each daylight change, against '
           + ('Python\'s zoneinfo' if ZoneInfo else 'the US rule written here (zoneinfo has no data on this PC)'),
           '; '.join(wrong[:3]))


# ---- the channel for a place ---------------------------------------------------------------------------------
def latest_time(place):
    index, _ = fixtures(place)
    stamps = [json.loads((FIX / place / name).read_text('utf-8'))['properties']['timestamp'] for path, name in index.items()
              if path.endswith('/observations/latest')]
    return max(int(dt.datetime.fromisoformat(s).timestamp()) for s in stamps)


def replies_folder():
    folder = WORK / 'replies'
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    for place in CAPTURED:
        index, _ = fixtures(place)
        for path, name in index.items():
            shutil.copyfile(FIX / place / name, folder / (slug('https://api.weather.gov' + path) + '.json'))
    return folder


def check_pages(exe, places, folder):
    frames = WORK / 'frames'
    if frames.exists():
        shutil.rmtree(frames)
    frames.mkdir()
    shown = {}
    for name, place in places:
        record = WORK / f'{name}.rec'
        record.write_bytes(place.pack(1))
        epoch = latest_time(name) + 600
        sub = frames / name
        sub.mkdir()
        out = run(exe, 'pages', record, folder, sub, epoch)
        pages = re.search(r'PAGES \d+ (.*)', out).group(1).split()
        home = dict(re.findall(r'(\w+)=(\S+)', re.search(r'HOME (.*)', out).group(1)))
        rows = re.findall(r'ROW \d+ (.+?) (-?\d+) ', out + ' ')
        missing = re.search(r'MISSING (\d+)', out.split('FETCHED')[-1]).group(1)
        clock = re.search(r'CLOCK (.*)', out).group(1)
        local = dt.datetime(1970, 1, 1) + dt.timedelta(seconds=epoch + us_offset(epoch, place.tz_std_min, place.tz_dst) * 60)
        want_clock = f'{local.hour % 12 or 12}:{local.minute:02d}:{local.second:02d} {"AM" if local.hour < 12 else "PM"}'
        want_kinds = {'ident', 'current-conditions', 'latest-observations', 'almanac', 'local-forecast', 'extended-forecast',
                      'rain-outlook'}
        result(missing == '0' and set(pages) == want_kinds and pages.count('latest-observations') == 1
               and home.get('temp') != '-32768' and home.get('local') == '3' and home.get('ext') in ('5', '6') and home.get('moon') == '1'
               and len(rows) == len(place.stations) and 'FAIL' not in out and clock == want_clock,
               f'{name}: every reply used; pages {len(pages)} (without Regional Forecast maps, Rain Outlook in place of the '
               'radar); its stations; clock in its own zone; no see-through colour',
               f'{missing} missing, {pages}, {home}, rows {len(rows)}, clock {clock} want {want_clock}')
        shown[name] = sorted(sub.glob('*.rgb565'))
    for target in PREVIEWS:
        target.mkdir(parents=True, exist_ok=True)
        for name, files in shown.items():
            for old in target.glob(f'weather-{name}-*.png'):
                old.unlink()
            for f in files:
                (target / f'weather-{name}-{f.stem[6:]}.png').write_bytes(tw.png_bytes(tw.W * tw.SCALE, tw.H * tw.SCALE, tw.enlarge(tw.frame_rgb(f))))
            tw.contact_sheet([tw.frame_rgb(f) for f in files], target / f'weather-{name}-contact-sheet.png')
    print(f'INFO previews: {", ".join(str(p.relative_to(ROOT)) for p in PREVIEWS)}')


def latest_stamp(paths):
    stamps = [json.loads(p.read_text('utf-8'))['properties']['timestamp'] for p in paths]
    return max(int(dt.datetime.fromisoformat(s).timestamp()) for s in stamps)


def pages_out(out):
    return {'pages': re.search(r'PAGES \d+ (.*)', out).group(1).split(),
            'home': dict(re.findall(r'(\w+)=(\S+)', re.search(r'HOME (.*)', out).group(1))),
            'name': re.search(r'NAME (.*)', out).group(1).split('|'),
            'rows': re.findall(r'ROW \d+ (.+?) (-?\d+) ', out + ' '),
            'missing': re.search(r'MISSING (\d+)', out).group(1), 'clock': re.search(r'CLOCK (.*)', out).group(1),
            'radar': re.findall(r'RADAR (\d)', out)}


def eastern_clock(epoch):
    local = dt.datetime(1970, 1, 1) + dt.timedelta(seconds=epoch + us_offset(epoch, -300, 1) * 60)
    return f'{local.hour % 12 or 12}:{local.minute:02d}:{local.second:02d} {"AM" if local.hour < 12 else "PM"}'


ALL_PAGES = {'ident', 'current-conditions', 'latest-observations', 'almanac', 'local-forecast', 'extended-forecast',
             'regional-state', 'regional-close', 'current-radar'}


def check_default_pages(exe):
    """The default, Orlando, FL, from replies captured from api.weather.gov (and the KMLB radar picture)."""
    fix = FIX / 'orlando'
    index = json.loads((fix / 'index.json').read_text('utf-8'))
    folder = WORK / 'replies-orlando'
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    for path, name in index.items():
        if name.endswith('.gif'):
            shutil.copyfile(fix / name, folder / 'radar.gif')
        else:
            shutil.copyfile(fix / name, folder / (slug('https://api.weather.gov' + path) + '.json'))
    epoch = latest_stamp([fix / n for p, n in index.items() if p.endswith('/observations/latest')]) + 600
    frames = WORK / 'frames-orlando'
    shutil.rmtree(frames, ignore_errors=True)
    frames.mkdir()
    out = run(exe, 'pages', '-', folder, frames, epoch)
    got = pages_out(out)
    labels = [r[0] for r in got['rows']]
    result(got['missing'] == '0' and got['radar'] == ['1'] and set(got['pages']) == ALL_PAGES
           and got['name'] == ['ORLANDO, FL', 'ORLANDO AREA', 'FL', 'FL'] and got['home'].get('temp') != '-32768'
           and got['home'].get('local') == '3' and got['home'].get('moon') == '1' and len(labels) == 11
           and labels[:3] == ['ORLANDO', 'ORLANDO EXEC', 'SANFORD'] and got['rows'][0][1] != '-32768'
           and sum(r[1] != '-32768' for r in got['rows']) >= 9
           and got['clock'] == eastern_clock(epoch) and 'FAIL' not in out,
           f'Orlando, FL (no record): every captured reply used, all {len(got["pages"])} pages (the Florida state map, the '
           'Central Florida close-up, the KMLB Current Radar), "ORLANDO, FL" / "ORLANDO AREA", 11 Florida rows, US Eastern',
           f'{got} {out[-400:]}')
    files = sorted(frames.glob('*.rgb565'))
    for target in PREVIEWS:
        target.mkdir(parents=True, exist_ok=True)
        for old in target.glob('weather-orlando-*.png'):
            old.unlink()
        for f in files:
            (target / f'weather-orlando-{f.stem[6:]}.png').write_bytes(
                tw.png_bytes(tw.W * tw.SCALE, tw.H * tw.SCALE, tw.enlarge(tw.frame_rgb(f))))
        tw.contact_sheet([tw.frame_rgb(f) for f in files], target / 'weather-orlando-contact-sheet.png')


def check_nodata(exe):
    """Local Weather with nothing held yet: the bottom bar says so gently instead of a bar of dashes."""
    frames = WORK / 'frames-nodata'
    shutil.rmtree(frames, ignore_errors=True)
    frames.mkdir()
    epoch = 1795131996
    words = {0: ('WEATHER WILL APPEAR', 'WHEN WI-FI CONNECTS'), 1: ('CONNECTING...', 'GETTING THE WEATHER...')}
    wrong = []
    for updating in (0, 1):
        out = run(exe, 'nodata', frames, epoch, updating)
        lines = re.findall(r'TICKER (\d+) (.*)\|(\d+)', out)
        for i, text, width in lines:
            if text != words[updating][int(i) % 2] or int(width) > 142 or '--' in text:
                wrong.append((updating, i, text, width))
        if len(lines) != 6:
            wrong.append(('lines', updating, len(lines)))
    files = sorted(frames.glob('*.rgb565'))
    result(not wrong and len(files) == 6, 'Local Weather with nothing held yet: the bottom bar says "WEATHER WILL APPEAR / '
           'WHEN WI-FI CONNECTS" (or "CONNECTING... / GETTING THE WEATHER..." while asking), never dashes; every line fits',
           str(wrong[:3]))


OWNER_HEADER = ROOT / 'private' / 'owner-weather' / 'wx_pack_owner.h'


def check_glue(folder, denver, second, second_pack):
    """The model of the ESP32 side: its default rounds need the PC reference's kept replies, so it runs with that
    reference's private compile-time pack as the default (owner-only; SKIP elsewhere)."""
    if not OWNER_HEADER.exists() or not CACHE.exists():
        print('SKIP model of the ESP32 side (needs the PC reference\'s kept replies, owner-only)')
        return
    stubs = WX / 'host_stubs'
    glue = WORK / 'check_weather_glue.exe'
    owner = [f'-DWX_EXTRA_PACK_HEADER="{OWNER_HEADER.as_posix()}"', '-DWX_DEFAULT_PACK=WX_EXTRA_PACK']
    done = subprocess.run([GCC, '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Og', '-g', *owner, '-fsanitize=undefined',
                           '-fsanitize=bounds-strict', '-fsanitize-undefined-trap-on-error', '-fstack-protector-all',
                           '-include', str(stubs / 'host.h'), f'-I{stubs}', str(WX / 'check_weather_glue.c'),
                           str(ROOT / 'chromatic' / 'launcher' / 'main' / 'weather_esp32.c'), '-o', str(glue), '-lm', '-pthread'],
                          cwd=WORK, capture_output=True, text=True, env=env())
    result(done.returncode == 0, 'weather_esp32.c with the record and pack readers compiles in the model (-Wall -Wextra -Werror)',
           done.stderr[:1500])
    if done.returncode:
        return
    (folder / 'denver.rec').write_bytes(denver.pack(1))
    (folder / 'second.rec').write_bytes(second.pack(2))
    lp.save_file(second_pack, folder / 'second.pjlp')
    epoch = latest_time('denver') + 600
    date = dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
    out = subprocess.run([str(glue), str(CACHE), str(WX / 'fixtures'), str(folder), str(epoch), date],
                         capture_output=True, text=True, env=env(), timeout=900)
    for line in out.stdout.splitlines():
        if line.startswith(('PASS', 'FAIL', 'ALL', 'LOG', 'MODEL')):
            print('  ' + line)
    result(out.returncode == 0 and 'ALL PASS' in out.stdout and 'glue: weather location record' in out.stdout,
           'model of the ESP32 side with the record and a pack in flash (all scenarios: read at open, used, held, freed, '
           'another place\'s pack, damaged copies, no memory)', out.stderr[-500:])


# ---- the PC side ---------------------------------------------------------------------------------------------
def check_pc(denver):
    labels = {
        'Dallas / Fort Worth, Dallas-Fort Worth International Airport': 'DALLAS', 'Jefferson County Airport': 'JEFFERSON CO',
        'Broomfield / Jeffco': 'BROOMFIELD', 'Buckley Space Force Base': 'BUCKLEY SFB',
        'Denver - Centennial Airport': 'CENTENNIAL', 'Erie Municipal Airport': 'ERIE',
        'Denver, Denver International Airport': 'DENVER', 'Boulder Municipal Airport': 'BOULDER',
        'Orlando Executive Airport': 'ORLANDO EXEC', 'San Juan, Luis Muñoz Marín International Airport': 'SAN JUAN',
        '': 'KXYZ', 'Airport': 'KXYZ', 'Albuquerque International Sunport': 'ALBUQUERQUE'}
    wrong = {k: wl.station_label(k, 'KXYZ') for k, v in labels.items() if wl.station_label(k, 'KXYZ') != v}
    result(not wrong and all(len(wl.station_label(k, 'KXYZ')) <= wl.LABEL_MAX for k in labels),
           f'station labels from the API names ({len(labels)} names, at most {wl.LABEL_MAX} letters)', str(wrong))
    found = {q: wl.find(q) for q in ('80202', '32801', '80202-1234', 'Denver, CO', 'denver co', 'Orlando, FL', "O'Fallon, MO",
                                     'Salt Lake City, UT', 'Springfield', 'Nowhere Special, ZZ', '00000', '', 'Orlando')}
    result(found['80202'] and found['80202'][0].zip == '80202' and found['80202-1234'][0].zip == '80202'
           and found['Denver, CO'][0].name == 'DENVER, CO' and found['denver co'][0].name == 'DENVER, CO'
           and found['Orlando, FL'][0].name == 'ORLANDO, FL' and found["O'Fallon, MO"][0].name == "O'FALLON, MO"
           and found['Salt Lake City, UT'][0].name == 'SALT LAKE CITY, UT' and 2 <= len(found['Springfield']) <= 8
           and not found['Nowhere Special, ZZ'] and not found['00000'] and not found['']
           and abs(found['Denver, CO'][0].lat - 39.7) < 0.1 and 'ORLANDO, FL' in [f.name for f in found['Orlando']],
           'lookup on this PC: ZIP codes and "City, ST" from the bundled Census table, best match first, none for nonsense')
    result(denver.name == 'DENVER, CO' and denver.office == 'BOU' and (denver.grid_x, denver.grid_y) == (63, 62)
           and (denver.tz_std_min, denver.tz_dst) == (-420, 1) and [s.id for s in denver.stations][:2] == ['KBJC', 'KBKF']
           and len(denver.stations) == 6 and denver.shows() == {'regional': False, 'radar': False}
           and WINTER_PARK.shows() == {'regional': True, 'radar': True} and TAMPA.shows() == {'regional': True, 'radar': False},
           'a place completed from recorded /points and /stations replies (office, grid, zone, six stations, labels)')
    _, fetch = fixtures('denver')
    zip_place = wl.resolve(wl.find('80202')[0], fetch)
    result(zip_place.name == 'DENVER, CO' and zip_place.state == 'CO', 'a ZIP code takes the weather service\'s place name',
           zip_place.name)
    bad = []
    for broken in ({'timeZone': 'Europe/London'},):
        def fetch_bad(path, broken=broken):
            reply = fetch(path)
            if path.startswith('/points/'):
                reply = json.loads(json.dumps(reply))
                reply['properties'].update(broken)
            return reply
        try:
            wl.resolve(wl.find('Denver, CO')[0], fetch_bad)
            bad.append(broken)
        except wl.LocationError:
            pass
    for reply in ({'properties': {}}, {}, {'features': []}):
        try:
            wl.resolve(wl.find('Denver, CO')[0], lambda path, reply=reply: reply if path.startswith('/points/') else {'features': []})
            bad.append(reply)
        except wl.LocationError:
            pass
    try:
        wl.resolve(wl.find('Denver, CO')[0], lambda path: fetch(path) if path.startswith('/points/') else {'features': []})
        bad.append('no stations')
    except wl.LocationError:
        pass
    result(not bad, 'replies that cannot be used (a zone outside the US, missing fields, no stations) are refused in plain words',
           str(bad))
    zones = all(wl.zone(z) == v for z, v in wl.ZONES.items()) and wl.zone('America/Indiana/Knox') == (-360, 1) \
        and wl.zone('America/Indiana/Indianapolis') == (-300, 1) and wl.zone('America/Kentucky/Louisville') == (-300, 1)
    result(zones, 'US time zones: the table and the Indiana / Kentucky / North Dakota names')
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / 'loc.json'
        result(wl.load_saved(path) is None and wl.save(denver, path) == denver and wl.load_saved(path) == denver
               and wl.save(None, path) is None and not path.exists() and wl.load_saved(path) is None
               and (path.write_text('{nonsense', 'utf-8') or True) and wl.load_saved(path) is None,
               'the PC\'s saved copy: save, load, forget; a damaged file reads as the default')
        missing = Path(folder) / 'no-migration.json'
        result(wl.migration(missing) is None and wl.default_name('1.6.3', missing) == 'its built-in place'
               and wl.default_name('1.6.4', missing) == 'Orlando, FL' and wl.default_name(None, missing) == 'Orlando, FL',
               'the default a console shows: Orlando, FL on 1.6.4+; an older launcher\'s own place only from a PC\'s migration')
    words = wl.describe(denver)
    result(wl.describe(None) == ['Orlando, FL (default)'] and words[0] == 'DENVER, CO' and 'US Mountain' in words[1]
           and 'Regional Forecast' in words[3], 'the confirmation lines', str(words))
    location_h = (WX / 'wx_location.h').read_text('utf-8')
    head = re.search(r'static const wx_pack wx_pack_fl=\{\s*"([^"]+)","([^"]+)","(\w\w)",[-\d.]+,[-\d.]+,(-?\d+),(-?\d+),"(\w+)",(\d+),(\d+),',
                     location_h)
    view = tuple(int(v) for v in re.search(r'static const wx_pack wx_pack_fl=\{.*?(-?\d+),(-?\d+),(-?\d+),(-?\d+)\};', location_h, re.S).groups())
    o = wl.ORLANDO
    result(head and (head.group(1), head.group(3), int(head.group(4)), int(head.group(5)), head.group(6), int(head.group(7)),
                     int(head.group(8))) == (o.name, o.state, o.lat_e4, o.lon_e4, o.office, o.grid_x, o.grid_y)
           and wl.REGIONS == {'FL': view} and o.builtin() is o and NEAR_ORLANDO.builtin() is None,
           'the PC\'s ORLANDO place and Florida region view equal the console\'s built-in pack')


def main():
    exe = build()
    print('INFO built check_weather_place.exe (the public build: Orlando, FL built in)')
    denver = captured_place('denver')
    check_pc(denver)
    check_records(exe, denver, WINTER_PARK)
    places = [('denver', denver), ('tampa', TAMPA), ('winter-park', WINTER_PARK), ('near-orlando', NEAR_ORLANDO),
              ('boise', wl.Place('BOISE, ID', 'ID', 436150, -1162023, -420, 1, 'BOI', 132, 87,
                                 (wl.Station('KBOI', 'BOISE'), wl.Station('KMUO', 'MOUNTAIN HOME'))))]
    check_requests(exe, places)
    check_zones(exe)
    folder = replies_folder()
    check_pages(exe, places[:1], folder)
    check_default_pages(exe)
    check_nodata(exe)
    builtin = lp.from_header((WX / 'wx_location.h').read_text('utf-8'), (WX / 'wx_art_fl.h').read_text('utf-8'))
    denver_place, denver_pack = recorded_pack('denver', 'Denver, CO')
    orlando_place, orlando_pack = recorded_pack('orlando', 'Orlando, FL')
    check_packs(exe, denver_place, denver_pack, orlando_place, orlando_pack, builtin)
    second_pack = replace(builtin, name=WINTER_PARK.name, area='WINTER PARK AREA', lat_e4=WINTER_PARK.lat_e4,
                          lon_e4=WINTER_PARK.lon_e4, office=WINTER_PARK.office, grid_x=WINTER_PARK.grid_x, grid_y=WINTER_PARK.grid_y)
    check_glue(folder, denver, WINTER_PARK, second_pack)
    print()
    print('ALL PASS' if not failed else f'FAILED: {len(failed)}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())

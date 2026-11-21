# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Location packs for the Local Weather channel (launcher 1.6.5 and newer).

A location pack gives any US weather location the pages that need area art: Regional Forecast (a state map and an
area close-up with nearby cities' highs) and Current Radar (the nearest NWS radar's small picture over an area map),
plus the place's full observation list. It is a serialized wx_pack (chromatic/weather/wx_location.h), checked as a
whole by the console (chromatic/weather/wx_lpack.h) and ignored when anything is wrong: the console then shows the
generic pages, as before.

Made on the PC by scripts/make_location_pack.py (or Chromatic Manager > Settings > Weather location > Make the area
pages); written by Chromatic Manager to two 8 KiB copies just below the apps record (PACK_A, PACK_B), A then B, each
with a sequence (the newer good copy wins, as for every other record); erased by "Use default"; kept by installs. A
pack belongs to one weather location: its home point, forecast office and grid must equal the location record's, or
the console does not use it. A pack can be saved as a file and imported again: the same checks apply.

Layout (little-endian), version 1:
   0 "PJLP"   4 version  5 requests  6 cities  7 stations  8 total length (all bytes, CRC included)  12 sequence
  16 lat 1e-4  20 lon 1e-4  24 grid x  26 grid y  28 office (3 letters, NUL)  32 state (2 letters)  34 obs rows
  35 home city  36 close-up title x, y  38 words x, y  40 state map y0, rows  42 close-up y0, rows
  44 radar view west, east, south, north (1e-4 degrees; all 0 without radar)  60 radar marks (x, y) x 3
  66 bytes of: state map, close-up map, radar map, strings  74 six reserved bytes (0)
  80 city spots (state x, y, close-up x, y; 255, 255 = not on that map) x cities, obs slots x obs rows,
     requests (kind, slot, host, period u16) x requests, the three maps (run-length rows: count, colour; colour 255
     leaves the pixel), the strings (each ending in NUL: name, area, close-up title, three radar towns, city names,
     obs labels, request paths), CRC-32 of everything before it.
Hosts are never in a pack: 0 is api.weather.gov, 1 the NWS map server; the console fills them in itself.
"""
import re
import struct
import zlib
from dataclasses import dataclass, field, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from user_paths import PRIVATE  # noqa: E402  (per-user folder, see user_paths.py)
SAVED = PRIVATE / 'chromatic-weather-pack.pjlp'      # this PC's copy of the pack on the console (private)
MAGIC = b'PJLP'
VERSION = 1
FLASH_A, FLASH_B = 0x3F6000, 0x3F8000                       # two copies, 8 KiB each, just below the apps record
COPY = 0x2000
HEADER = 80
MAX_TOTAL = COPY
FIRST_LAUNCHER = (1, 6, 5)                                   # the first launcher that reads a pack
W, MAP_TOP, MAP_BOTTOM = 160, 23, 131                       # map rows live in 23..130 (the page body)
RADAR_Y0, RADAR_ROWS = 23, 97
MAX_REQUESTS, MAX_CITIES, MAX_STATIONS, MAX_OBS_ROWS = 33, 9, 11, 12
OBS, RECENT, HISTORY, FORECAST, CITY, ALERTS, RADAR = range(7)
KIND_NAMES = ('OBS', 'RECENT', 'HISTORY', 'FORECAST', 'CITY', 'ALERTS', 'RADAR')
API, MAPS = 0, 1
HOSTS = ('api.weather.gov', 'opengeo.ncep.noaa.gov')
NAME_MAX, AREA_MAX, LABEL_MAX, PATH_MAX = 23, 29, 15, 255
_TEXT = re.compile(r"[A-Z0-9][A-Z0-9 ,.'/-]*")
_PATH_CHARS = re.compile(r'[A-Za-z0-9/?=&.,:_%-]+')
_FMT = '<4sBBBBIIiiHH4s2sBBBBBBBBBBiiii6sHHHH6s'
assert struct.calcsize(_FMT) == HEADER


class PackError(ValueError):
    """A pack that cannot be made or stored, in plain words."""


@dataclass(frozen=True)
class Request:
    kind: int
    slot: int
    host: int
    period: int
    path: str


@dataclass(frozen=True)
class Pack:
    name: str
    area: str
    state: str
    lat_e4: int
    lon_e4: int
    office: str
    grid_x: int
    grid_y: int
    stations: int
    obs_labels: tuple
    obs_slots: tuple
    cities: tuple                # names
    home_city: int
    city_spots: tuple            # (state x, y, close x, y) per city
    requests: tuple              # Request
    state_map: bytes
    state_y0: int
    state_rows: int
    close_map: bytes
    close_y0: int
    close_rows: int
    close_title: str
    close_title_x: int
    close_title_y: int
    words_x: int
    words_y: int
    radar_map: bytes = b''
    radar_marks: tuple = (0, 0, 0, 0, 0, 0)
    radar_towns: tuple = ('', '', '')
    radar_view: tuple = (0, 0, 0, 0)  # west, east, south, north (1e-4 degrees)
    note: str = field(default='', compare=False)  # PC only (where it came from); never stored

    # ---- the rules (wx_lpack.h checks exactly these) ----
    def problems(self):
        out = []
        text = lambda v, lo, hi: isinstance(v, str) and lo <= len(v) <= hi and (not v or (_TEXT.fullmatch(v) and not v.endswith(' ')))
        if not text(self.name, 1, NAME_MAX) or not text(self.area, 1, AREA_MAX) or not text(self.close_title, 1, NAME_MAX):
            out.append('name, area or close-up title')
        if not re.fullmatch(r'[A-Z]{2}', self.state or '') or not re.fullmatch(r'[A-Z]{3}', self.office or ''):
            out.append('state or office')
        if not (-900000 <= self.lat_e4 <= 900000 and -1800000 <= self.lon_e4 <= 1800000 and 0 <= self.grid_x <= 9999
                and 0 <= self.grid_y <= 9999):
            out.append('point or grid')
        if not 1 <= self.stations <= MAX_STATIONS or not 1 <= len(self.obs_labels) <= MAX_OBS_ROWS \
                or len(self.obs_slots) != len(self.obs_labels) or any(not 0 <= s < self.stations for s in self.obs_slots) \
                or any(not text(l, 1, LABEL_MAX) for l in self.obs_labels):
            out.append('observation rows')
        n = len(self.cities)
        if not 1 <= n <= MAX_CITIES or not 0 <= self.home_city < n or any(not text(c, 1, LABEL_MAX) for c in self.cities) \
                or len(self.city_spots) != n:
            out.append('cities')
        for spot in self.city_spots:
            for x, y in ((spot[0], spot[1]), (spot[2], spot[3])):
                if (x, y) != (255, 255) and not (0 <= x < W and MAP_TOP <= y <= MAP_BOTTOM - 24):
                    out.append('city spots')
        if not (0 <= self.close_title_x < W and MAP_TOP <= self.close_title_y <= MAP_BOTTOM - 9 and 0 <= self.words_x < W
                and MAP_TOP <= self.words_y <= MAP_BOTTOM - 9):
            out.append('title or words place')
        for data, y0, rows in ((self.state_map, self.state_y0, self.state_rows), (self.close_map, self.close_y0, self.close_rows)):
            if not (MAP_TOP <= y0 and rows >= 1 and y0 + rows <= MAP_BOTTOM) or not rle_ok(data, rows):
                out.append('a map')
        radar = [r for r in self.requests if r.kind == RADAR]
        if radar:
            w, e, s, nn = self.radar_view
            if not rle_ok(self.radar_map, RADAR_ROWS) or not (-1800000 <= w < e <= 1800000 and -900000 <= s < nn <= 900000) \
                    or any(not text(t, 1, LABEL_MAX) for t in self.radar_towns) \
                    or any(not (0 <= self.radar_marks[i] < W and MAP_TOP <= self.radar_marks[i + 1] < RADAR_Y0 + RADAR_ROWS)
                           for i in (0, 2, 4)):
                out.append('radar view')
        elif self.radar_map or any(self.radar_marks) or any(self.radar_towns) or any(self.radar_view):
            out.append('radar parts without a radar request')
        out += request_problems(self.requests, self.stations, n, self.home_city)
        return out

    def check(self):
        bad = self.problems()
        if bad:
            raise PackError('The location pack is not valid (' + ', '.join(dict.fromkeys(bad)) + ').')
        return self

    def binds(self, place):
        """Does this pack belong to `place` (a chromatic_weather_location.Place): the same state, point, office, grid?"""
        return place is not None and (self.state, self.lat_e4, self.lon_e4, self.office, self.grid_x, self.grid_y) == \
            (place.state, place.lat_e4, place.lon_e4, place.office, place.grid_x, place.grid_y)

    # ---- bytes ----
    def body(self):
        strings = [self.name, self.area, self.close_title, *self.radar_towns, *self.cities, *self.obs_labels,
                   *(r.path for r in self.requests)]
        blob = b''.join(s.encode('ascii') + b'\0' for s in strings)
        data = b''.join(bytes(spot) for spot in self.city_spots) + bytes(self.obs_slots)
        data += b''.join(struct.pack('<BBBH', r.kind, r.slot, r.host, r.period) for r in self.requests)
        data += self.state_map + self.close_map + self.radar_map + blob
        return data, len(blob)

    def pack(self, sequence):
        self.check()
        if not 0 < sequence <= 0xFFFFFFFF:
            raise PackError('Bad pack sequence.')
        data, strings_n = self.body()
        total = HEADER + len(data) + 4
        if total > MAX_TOTAL:
            raise PackError(f'The location pack is too large ({total} bytes; at most {MAX_TOTAL}).')
        head = struct.pack(_FMT, MAGIC, VERSION, len(self.requests), len(self.cities), self.stations, total, sequence,
                           self.lat_e4, self.lon_e4, self.grid_x, self.grid_y, self.office.encode('ascii') + b'\0',
                           self.state.encode('ascii'), len(self.obs_labels), self.home_city, self.close_title_x,
                           self.close_title_y, self.words_x, self.words_y, self.state_y0, self.state_rows, self.close_y0,
                           self.close_rows, *self.radar_view, bytes(self.radar_marks), len(self.state_map),
                           len(self.close_map), len(self.radar_map), strings_n, bytes(6))
        raw = head + data
        return raw + struct.pack('<I', zlib.crc32(raw) & 0xFFFFFFFF)

    def sector(self, sequence):
        """One copy as written to flash: the pack, then erased bytes up to 8 KiB."""
        return self.pack(sequence).ljust(COPY, b'\xff')

    def size(self):
        return HEADER + len(self.body()[0]) + 4


def wx_text_ok(text):
    """Can the console show this name (capitals, digits, " ,.'-/", a capital or digit first, no trailing space)?"""
    return bool(_TEXT.fullmatch(text or '')) and not text.endswith(' ')


def request_problems(requests, stations, cities, home_city):
    out = []
    if not 1 <= len(requests) <= MAX_REQUESTS:
        return ['request count']
    count = {k: sum(1 for r in requests if r.kind == k) for k in range(7)}
    if count[FORECAST] != 1 or count[ALERTS] != 1 or count[HISTORY] > 1 or count[RADAR] > 1 or \
            sum(1 for r in requests if r.kind == OBS and r.slot == 0) != 1:
        out.append('request kinds')
    for r in requests:
        if not (0 <= r.kind <= RADAR and r.host in (API, MAPS) and (r.host == MAPS) == (r.kind == RADAR)
                and 60 <= r.period <= 65535 and 1 <= len(r.path) <= PATH_MAX and _PATH_CHARS.fullmatch(r.path)):
            out.append('a request')
            continue
        slot_ok = {OBS: r.slot < stations, RECENT: 1 <= r.slot < stations, HISTORY: r.slot == 0,
                   FORECAST: r.slot == home_city, CITY: r.slot < cities and r.slot != home_city, ALERTS: r.slot == 0,
                   RADAR: r.slot == 0}[r.kind]
        prefix = {OBS: '/stations/', RECENT: '/stations/', HISTORY: '/stations/', FORECAST: '/gridpoints/',
                  CITY: '/gridpoints/', ALERTS: '/alerts/active?point=', RADAR: '/geoserver/'}[r.kind]
        shape = r.path.startswith(prefix) and (r.kind != RADAR or ('request=GetMap' in r.path and 'format=image/gif' in r.path
                                                                  and r.path.endswith('width=160&height=97')))
        if not slot_ok or not shape:
            out.append('a request')
    return out


def rle_ok(data, rows):
    """Run-length rows: (count >= 1, colour < 16 or 255) pairs, each row exactly 160 pixels, nothing left over."""
    at = 0
    for _ in range(rows):
        x = 0
        while x < W:
            if at + 2 > len(data):
                return False
            n, colour = data[at], data[at + 1]
            if n < 1 or not (colour < 16 or colour == 255) or x + n > W:
                return False
            x += n
            at += 2
    return at == len(data)


def parse(raw):
    """(Pack, sequence) from one copy, or None: exactly the console's rules (wx_lpack.h)."""
    if raw is None or len(raw) < HEADER + 4:
        return None
    raw = bytes(raw)
    (magic, version, n_req, n_cities, stations, total, sequence, lat, lon, gx, gy, office, state, obs_rows, home,
     tx, ty, wx_, wy, sy0, srows, cy0, crows, rw, re_, rs, rn, marks, sn, cn, rmn, strn, reserved) = struct.unpack_from(_FMT, raw)
    if magic != MAGIC or version != VERSION or not HEADER + 4 <= total <= MAX_TOTAL or total > len(raw) or sequence == 0 \
            or any(reserved):
        return None
    if zlib.crc32(raw[:total - 4]) & 0xFFFFFFFF != struct.unpack_from('<I', raw, total - 4)[0]:
        return None
    if not (1 <= n_req <= MAX_REQUESTS and 1 <= n_cities <= MAX_CITIES and 1 <= stations <= MAX_STATIONS
            and 1 <= obs_rows <= MAX_OBS_ROWS):
        return None
    need = HEADER + 4 * n_cities + obs_rows + 5 * n_req + sn + cn + rmn + strn + 4
    if need != total or office[3] != 0 or not office[:3].isalpha() or not state.isalpha():
        return None
    at = HEADER
    spots = tuple(tuple(raw[at + 4 * i:at + 4 * i + 4]) for i in range(n_cities))
    at += 4 * n_cities
    slots = tuple(raw[at:at + obs_rows])
    at += obs_rows
    reqs = []
    for i in range(n_req):
        kind, slot, host, period = struct.unpack_from('<BBBH', raw, at)
        reqs.append((kind, slot, host, period))
        at += 5
    maps = []
    for size in (sn, cn, rmn):
        maps.append(raw[at:at + size])
        at += size
    blob = raw[at:at + strn]
    if not blob.endswith(b'\0'):
        return None
    parts = blob[:-1].split(b'\0')
    if len(parts) != 6 + n_cities + obs_rows + n_req:
        return None
    try:
        parts = [p.decode('ascii') for p in parts]
    except UnicodeDecodeError:
        return None
    name, area, title, *rest = parts
    towns, rest = tuple(rest[:3]), rest[3:]
    cities, rest = tuple(rest[:n_cities]), rest[n_cities:]
    labels, paths = tuple(rest[:obs_rows]), rest[obs_rows:]
    try:
        pack = Pack(name, area, state.decode('ascii'), lat, lon, office[:3].decode('ascii'), gx, gy, stations, labels, slots,
                    cities, home, spots, tuple(Request(k, s, h, p, path) for (k, s, h, p), path in zip(reqs, paths)),
                    maps[0], sy0, srows, maps[1], cy0, crows, title, tx, ty, wx_, wy, maps[2], tuple(marks), towns,
                    (rw, re_, rs, rn))
    except (TypeError, ValueError):
        return None
    if pack.problems():
        return None
    return pack, sequence


def current(a, b):
    """(Pack, sequence) of the newer good copy (A when equal), or (None, 0)."""
    pa, pb = parse(a), parse(b)
    if pb and (not pa or 0 < ((pb[1] - pa[1]) & 0xFFFFFFFF) < 0x80000000):
        return pb
    return pa or (None, 0)


def next_sequence(sequence):
    return (sequence + 1) & 0xFFFFFFFF or 1


def sequence_for(a, b):
    pa, pb = parse(a), parse(b)
    return next_sequence(pb[1] if pb else pa[1] if pa else 0)


def with_pack(image, pack, a_range, b_range):
    """`image` with `pack` in both copies (None: both erased)."""
    data = bytearray(image)
    if pack is None:
        for start, end in (a_range, b_range):
            data[start:end] = b'\xff' * (end - start)
        return bytes(data)
    sector = pack.sector(sequence_for(image[a_range[0]:a_range[1]], image[b_range[0]:b_range[1]]))
    for start, end in (a_range, b_range):
        data[start:end] = sector
    return bytes(data)


def reads_packs(version):
    try:
        return tuple(int(p) for p in str(version).split('.')) >= FIRST_LAUNCHER
    except ValueError:
        return False


def from_header(location_h, art_h, name='wx_pack_fl'):
    """A built-in pack read back from the generated C headers (wx_location.h and its art header) as a location pack:
    the same tables, maps and radar view (tests compare the two ways of showing a place)."""
    init = re.search(rf'static const wx_pack {name}=\{{(.*?)\}};', location_h, re.S).group(1)
    words = [w.strip() for w in re.split(r',(?=(?:[^"]*"[^"]*")*[^"]*$)', init.replace('\n', ' ')) if w.strip()]
    s = lambda w: w.strip('"')
    both = location_h + '\n' + art_h

    def array(sym, kind=int):
        body = re.search(rf'{re.escape(sym)}\[\d+\]=\{{(.*?)\}};', both, re.S).group(1)
        return [s(v.strip()) for v in re.findall(r'"[^"]*"', body)] if kind is str else [int(v) for v in re.findall(r'-?\d+', body)]

    def number(word):
        word = word.strip()
        return int(re.search(rf'#define {word} (\d+)', both).group(1)) if word.isidentifier() else int(word)
    (pname, area, state, _lat, _lon, lat_e4, lon_e4, office, gx, gy, stations, rows, cities, home, nreq, req, period, labels,
     slots, city, smap, _sn, sy0, srows, cmap, _cn, cy0, crows, title, tx, ty, wx_, wy, spots, rmap, _rn, marks, towns,
     rw, re_, rs, rn) = words
    body = re.search(rf'static const wx_request {req}\[\d+\]=\{{(.*?)\n\}};', location_h, re.S).group(1)
    rows_ = re.findall(r'\{(WX_HOST|WX_RADAR_HOST),"([^"]+)",WX_K_(\w+),(\d+)\}', body)
    periods = array(period)
    spot = array(spots)
    return Pack(s(pname), s(area), s(state), int(lat_e4), int(lon_e4), s(office), int(gx), int(gy), int(stations),
                tuple(array(labels, str)), tuple(array(slots)), tuple(array(city, str)), int(home),
                tuple(tuple(spot[i * 4:i * 4 + 4]) for i in range(int(cities))),
                tuple(Request(KIND_NAMES.index(k), int(sl), MAPS if h == 'WX_RADAR_HOST' else API, p, path)
                      for (h, path, k, sl), p in zip(rows_, periods)),
                bytes(array(smap)), number(sy0), number(srows), bytes(array(cmap)), number(cy0), number(crows),
                s(title), int(tx), int(ty), int(wx_), int(wy), bytes(array(rmap)), tuple(array(marks)),
                tuple(array(towns, str)), (int(rw), int(re_), int(rs), int(rn)))


# ---- files: this PC's saved copy, and packs shared as files ----
def load_file(path):
    """The pack in a file (as exported), checked exactly as the console checks it; PackError otherwise."""
    try:
        if Path(path).stat().st_size > MAX_TOTAL:   # before reading it (review L6 of 1.6.5)
            raise PackError('That file is not a location pack (too large).')
        with open(path, 'rb') as f:
            raw = f.read(MAX_TOTAL + 1)
    except OSError:
        raise PackError('The pack file could not be read.')
    if len(raw) > MAX_TOTAL:
        raise PackError('That file is not a location pack (too large).')
    found = parse(raw)
    if not found:
        raise PackError('That file is not a valid location pack.')
    return found[0]


def save_file(pack, path):
    """Write `pack` as a file (sequence 1), durably; read back and checked."""
    from chromatic_trial_runner import write_durable
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = pack.pack(1)
    write_durable(path, data)
    if path.read_bytes() != data:
        raise PackError('The pack file did not read back the same.')
    return path


def load_saved(path=None):
    try:
        return load_file(path or SAVED)
    except PackError:
        return None


def save(pack, path=None):
    path = Path(path or SAVED)
    if pack is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return None
    save_file(pack, path)
    return pack


def describe(pack):
    """Plain lines for the window (no point, no stations: the name is shown masked by the caller)."""
    radar = any(r.kind == RADAR for r in pack.requests)
    return [f'Area pages for {pack.name}: a state map with {sum(1 for s in pack.city_spots if s[0] != 255)} cities, an area '
            f'close-up with {sum(1 for s in pack.city_spots if s[2] != 255)}, ' +
            ('Current Radar.' if radar else 'no radar (Rain Outlook instead).'),
            f'{len(pack.obs_labels)} observation rows; {len(pack.requests)} requests to the weather service; '
            f'{pack.size():,} bytes on the console.']

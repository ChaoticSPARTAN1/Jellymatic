# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""The weather location of the console's Local Weather channel (launcher 1.6.3 and newer).

With no location on the console launcher 1.6.4 and newer show ORLANDO, FL (the compiled-in default
pack; a record naming its home exactly shows it whole). A place in Florida gets the Florida Regional
Forecast maps (and Current Radar inside the Orlando radar view); since launcher 1.6.5 any place can
have its own area pages from a location pack (scripts/chromatic_location_pack.py) made for it;
elsewhere the generic pages. The owner may choose another US place in Chromatic Manager: a ZIP code or "City, ST", looked up on this
PC in a bundled public-domain table (chromatic/weather/places/, US Census 2024 Gazetteer), then
completed from api.weather.gov (its forecast office and grid, time zone and the six nearest
observation stations). api.weather.gov is the only service the place is sent to.

The console keeps it as one 256-byte record, the same in two flash sectors (LOCATION_A then
LOCATION_B), read by chromatic/weather/wx_place.h; pack() here and wx_place_parse() there agree on
the vectors of scripts/test_chromatic_weather_place.py. A record that is absent, blank, torn,
corrupt or of another version means the compiled-in default. Older launchers never read those
sectors.

The location is personal data: it stays in this PC's private folder (SAVED, Git-ignored) and on
the console. Nothing here prints or logs it; callers say "your location".
"""
import gzip
import json
import re
import struct
import unicodedata
import zlib
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from user_paths import PRIVATE  # noqa: E402  (per-user folder, see user_paths.py)
TABLE = ROOT / 'chromatic' / 'weather' / 'places' / 'us-places.txt.gz'
SAVED = PRIVATE / 'chromatic-weather-location.json'
FLASH_A, FLASH_B = 0x3FC000, 0x3FD000
SECTOR = 0x1000
RECORD = 256
MAGIC = b'PJWX'
VERSION = 1
STATIONS = 6
NAME_LEN, ID_LEN, LABEL_LEN = 24, 8, 16
LABEL_MAX = 14          # what fits a row of Latest Observations beside the temperature
DEFAULT_NAME = 'Orlando, FL'          # the compiled-in default of launcher 1.6.4 and newer
DEFAULT_TEXT = 'Orlando, FL (default)'
# The built-in pack's region (chromatic/weather/wx_location.h, checked against it by test_chromatic_weather_place.py):
# state -> radar view (west, east, south, north) in 1e-4 degrees. A place in that state gets its Regional Forecast maps;
# inside the view, Current Radar too.
REGIONS = {'FL': (-823000, -799000, 279725, 292500)}
# Private, never in Git: what a console showed before its first launcher with a record of its own (a migration Chromatic
# Manager carries out at an install: chromatic_manager.py, "migration"). Absent on most PCs.
MIGRATION = PRIVATE / 'chromatic-weather-migration.json'

API = 'https://api.weather.gov'
AGENT = 'chromatic-manager (weather location)'  # nothing personal
_NAME_OK = re.compile(r"[A-Z][A-Z0-9 ,.'/-]*")
_ID_OK = re.compile(r'[A-Z0-9]{3,7}')
STATES = ('AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC '
          'ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR VI GU AS MP').split()

# US time zones (IANA name from /points) -> (standard offset in minutes, the US daylight rule applies).
ZONES = {
    'America/New_York': (-300, 1), 'America/Detroit': (-300, 1), 'America/Chicago': (-360, 1),
    'America/Menominee': (-360, 1), 'America/Denver': (-420, 1), 'America/Boise': (-420, 1),
    'America/Phoenix': (-420, 0), 'America/Los_Angeles': (-480, 1), 'America/Anchorage': (-540, 1),
    'America/Juneau': (-540, 1), 'America/Sitka': (-540, 1), 'America/Nome': (-540, 1),
    'America/Yakutat': (-540, 1), 'America/Metlakatla': (-540, 1), 'America/Adak': (-600, 1),
    'Pacific/Honolulu': (-600, 0), 'America/Puerto_Rico': (-240, 0), 'America/St_Thomas': (-240, 0),
    'Pacific/Guam': (600, 0), 'Pacific/Saipan': (600, 0), 'Pacific/Pago_Pago': (-660, 0),
}
ZONE_WORDS = {(-300, 1): 'US Eastern', (-360, 1): 'US Central', (-420, 1): 'US Mountain',
              (-420, 0): 'Arizona (no daylight time)', (-480, 1): 'US Pacific', (-540, 1): 'Alaska',
              (-600, 1): 'Hawaii-Aleutian', (-600, 0): 'Hawaii (no daylight time)',
              (-240, 0): 'Atlantic (no daylight time)', (600, 0): 'Chamorro', (-660, 0): 'Samoa'}


class LocationError(ValueError):
    """A place that cannot be used, said in plain words (never the place itself)."""


def zone(name):
    """(standard minutes, daylight rule) for a US IANA zone name, or LocationError."""
    name = str(name or '')
    if name in ZONES:
        return ZONES[name]
    if name.startswith('America/Indiana/'):
        return (-360, 1) if name.endswith(('/Knox', '/Tell_City')) else (-300, 1)
    if name.startswith('America/Kentucky/'):
        return -300, 1
    if name.startswith('America/North_Dakota/'):
        return -360, 1
    raise LocationError('That place\'s time zone is not one this app knows (US time zones only).')


def zone_words(std_min, dst):
    return ZONE_WORDS.get((std_min, dst), f'UTC{std_min // 60:+d}')


def plain(text):
    """Capitals, digits and ` ,.'-/` only (accents dropped), single spaces."""
    text = unicodedata.normalize('NFKD', str(text)).encode('ascii', 'ignore').decode('ascii').upper()
    text = re.sub(r"[^A-Z0-9 ,.'/-]", ' ', text)
    return re.sub(r'\s+', ' ', text).strip(" ,.'/-")


_LABEL_WORDS = ((r'\bINTERNATIONAL\b', ''), (r'\bREGIONAL\b', ''), (r'\bMUNICIPAL\b', ''), (r'\bAIRPORT\b', ''),
                (r'\bAIRFIELD\b', ''), (r'\bAIR PARK\b', ''), (r'\bJETPORT\b', ''), (r'\bFIELD\b', ''),
                (r'\bAIR FORCE BASE\b', 'AFB'), (r'\bSPACE FORCE BASE\b', 'SFB'), (r'\bNAVAL AIR STATION\b', 'NAS'),
                (r'\bCOUNTY\b', 'CO'), (r'\bMOUNTAIN\b', 'MTN'), (r'\bMOUNT\b', 'MT'), (r'\bFORT\b', 'FT'),
                (r'\bSAINT\b', 'ST'), (r'\bEXECUTIVE\b', 'EXEC'), (r'\bMETROPOLITAN\b', 'METRO'))


def station_label(name, station_id):
    """A station's name from the API as a short row label: "Dallas / Fort Worth, Dallas-Fort Worth
    International Airport" -> "DALLAS"; at most LABEL_MAX letters; the id if nothing is left."""
    text = plain(name)
    head = text.split(',', 1)[0] if ',' in text else text
    head = re.sub(r'\s*/\s*', '-', head)
    if ' - ' in head:
        parts = [p.strip() for p in head.split(' - ')]
        head = parts[-1] or parts[0]
    for pattern, short in _LABEL_WORDS:
        head = re.sub(pattern, short, head)
    head = re.sub(r'\s+', ' ', head).strip(" ,.'/-")
    if len(head) > LABEL_MAX and '-' in head:
        head = head.split('-')[0].strip()
    if len(head) > LABEL_MAX:
        cut = head[:LABEL_MAX + 1].rsplit(' ', 1)[0]
        head = (cut if len(cut) >= 4 else head[:LABEL_MAX]).strip(" ,.'/-")
    return head if head and head[0].isalpha() else station_id


@dataclass(frozen=True)
class Station:
    id: str
    label: str


@dataclass(frozen=True)
class Place:
    """What the console stores. lat/lon in 1e-4 degrees; tz as standard minutes and US-rule flag.
    `time_zone` (the IANA name) is kept on the PC only, for the window's confirmation."""
    name: str
    state: str
    lat_e4: int
    lon_e4: int
    tz_std_min: int
    tz_dst: int
    office: str
    grid_x: int
    grid_y: int
    stations: tuple = ()
    time_zone: str = field(default='', compare=False)

    def check(self):
        if not (_NAME_OK.fullmatch(self.name or '') and len(self.name) < NAME_LEN and not self.name.endswith(' ')):
            raise LocationError('The place name cannot be stored on the console.')
        if not re.fullmatch(r'[A-Z]{2}', self.state or ''):
            raise LocationError('The place has no US state.')
        if not (-900000 <= self.lat_e4 <= 900000 and -1800000 <= self.lon_e4 <= 1800000):
            raise LocationError('The place\'s coordinates are out of range.')
        if not (-720 <= self.tz_std_min <= 840 and self.tz_std_min % 15 == 0 and self.tz_dst in (0, 1)):
            raise LocationError('The place\'s time zone cannot be stored.')
        if not (re.fullmatch(r'[A-Z]{3}', self.office or '') and 0 <= self.grid_x <= 9999 and 0 <= self.grid_y <= 9999):
            raise LocationError('The weather service gave no forecast grid for that place.')
        if not 1 <= len(self.stations) <= STATIONS:
            raise LocationError('The weather service gave no observation station near that place.')
        for s in self.stations:
            if not (_ID_OK.fullmatch(s.id or '') and _NAME_OK.fullmatch(s.label or '') and len(s.label) < LABEL_LEN
                    and not s.label.endswith(' ')):
                raise LocationError('A station near that place cannot be stored on the console.')
        return self

    def pack(self, sequence):
        """The 256-byte record (wx_place.h's layout)."""
        self.check()
        if not 0 < sequence <= 0xFFFFFFFF:
            raise LocationError('Bad record sequence.')
        text = lambda value, size: value.encode('ascii').ljust(size, b'\0')
        body = struct.pack('<4sBBHIhBBiiHH4s2sBB', MAGIC, VERSION, len(self.stations), RECORD, sequence,
                           self.tz_std_min, self.tz_dst, 0, self.lat_e4, self.lon_e4, self.grid_x, self.grid_y,
                           text(self.office, 4), self.state.encode('ascii'), 0, 0)
        body += text(self.name, NAME_LEN)
        body += b''.join(text(s.id, ID_LEN) for s in self.stations).ljust(STATIONS * ID_LEN, b'\0')
        body += b''.join(text(s.label, LABEL_LEN) for s in self.stations).ljust(STATIONS * LABEL_LEN, b'\0')
        body = body.ljust(RECORD - 4, b'\0')
        assert len(body) == RECORD - 4
        return body + struct.pack('<I', zlib.crc32(body) & 0xFFFFFFFF)

    def sector(self, sequence):
        """One 4 KiB flash sector: the record, then erased bytes."""
        return self.pack(sequence).ljust(SECTOR, b'\xff')

    def lat(self):
        return self.lat_e4 / 10000

    def lon(self):
        return self.lon_e4 / 10000

    def shows(self, pack=None):
        """Which pages the console shows for this place (docs/TV-and-Local-Weather.md): with a location pack made
        for it (1.6.5+), the pack's maps and radar; else the Regional Forecast maps of its state's built-in pack
        (Florida), Current Radar inside that pack's radar view."""
        if pack is not None and pack.binds(self):
            return {'regional': True, 'radar': any(r.kind == 6 for r in pack.requests)}
        view = REGIONS.get(self.state)
        radar = bool(view) and view[0] <= self.lon_e4 <= view[1] and view[2] <= self.lat_e4 <= view[3]
        return {'regional': bool(view), 'radar': radar}

    def builtin(self):
        """The built-in pack this record names exactly (its home point, office and grid), or None. The console then
        shows that pack whole."""
        for place in (ORLANDO,):
            if (self.state, self.lat_e4, self.lon_e4, self.office, self.grid_x, self.grid_y) == \
                    (place.state, place.lat_e4, place.lon_e4, place.office, place.grid_x, place.grid_y):
                return place
        return None

    def to_json(self):
        data = asdict(self)
        data['stations'] = [asdict(s) for s in self.stations]
        return data

    @staticmethod
    def from_json(data):
        stations = tuple(Station(s['id'], s['label']) for s in data.get('stations', ()))
        return Place(data['name'], data['state'], int(data['lat_e4']), int(data['lon_e4']), int(data['tz_std_min']),
                     int(data['tz_dst']), data['office'], int(data['grid_x']), int(data['grid_y']), stations,
                     data.get('time_zone', '')).check()


# The built-in pack's home as a record (chromatic/weather/wx_location.h wx_pack_fl; the test checks they match).
ORLANDO = Place('ORLANDO, FL', 'FL', 285383, -813792, -300, 1, 'MLB', 26, 68,
                (Station('KMCO', 'ORLANDO'), Station('KORL', 'ORLANDO EXEC'), Station('KSFB', 'SANFORD'),
                 Station('KISM', 'KISSIMMEE'), Station('KLEE', 'LEESBURG'), Station('KTIX', 'TITUSVILLE')),
                'America/New_York')


ORLANDO_DEFAULT_LAUNCHER = (1, 6, 4)     # the first launcher whose compiled-in default is Orlando, FL


def _version(version):
    try:
        return tuple(int(part) for part in str(version).split('.'))
    except ValueError:
        return None


def shows_orlando(version):
    """Is this launcher's compiled-in default Orlando, FL (1.6.4 and newer)?"""
    found = _version(version)
    return bool(found) and found >= ORLANDO_DEFAULT_LAUNCHER


def migration(path=None):
    """This PC's private migration (MIGRATION), or None: {'place': Place, 'pack': path or None, 'showed': [versions with no
    record], 'showed_with_record': [versions that showed it from their own tables]}. Never printed."""
    path = Path(path or MIGRATION)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        pack = data.get('pack')
        return {'place': Place.from_json(data['place']), 'pack': (path.parent / pack) if pack else None,
                'showed': [str(v) for v in data.get('showed', [])], 'showed_with_record': [str(v) for v in data.get('showed_with_record', [])]}
    except (OSError, ValueError, KeyError, TypeError, LocationError):
        return None


def default_name(version=None, path=None):
    """The place a console shows with no record: Orlando, FL on launcher 1.6.4 and newer (or not known). An older
    launcher this PC's migration names showed that place; any other launcher, its own built-in place."""
    if version is None or shows_orlando(version):
        return DEFAULT_NAME
    found = migration(path)
    if found and str(version) in found['showed']:
        name, _, state = found['place'].name.partition(',')
        return f'{name.title()},{state}' if state else name.title()
    return 'its built-in place'


def parse(raw):
    """(Place, sequence) from one copy of the record, or None: the same rules as wx_place_parse()."""
    if raw is None or len(raw) < RECORD:
        return None
    raw = bytes(raw[:RECORD])
    try:
        (magic, version, n, length, sequence, std, dst, pad, lat, lon, gx, gy, office, state, r1, r2) = struct.unpack_from(
            '<4sBBHIhBBiiHH4s2sBB', raw)
    except struct.error:
        return None
    if magic != MAGIC or version != VERSION or length != RECORD or sequence == 0:
        return None
    if zlib.crc32(raw[:RECORD - 4]) & 0xFFFFFFFF != struct.unpack_from('<I', raw, RECORD - 4)[0]:
        return None
    if pad or r1 or r2 or any(raw[204:RECORD - 4]) or not 1 <= n <= STATIONS:
        return None

    def text(data):
        value = data.rstrip(b'\0')
        if b'\0' in value or not value:
            return None
        try:
            return value.decode('ascii')
        except UnicodeDecodeError:
            return None
    stations = []
    for i in range(STATIONS):
        sid, label = raw[60 + i * ID_LEN:60 + (i + 1) * ID_LEN], raw[108 + i * LABEL_LEN:108 + (i + 1) * LABEL_LEN]
        if i < n:
            if sid[-1] or label[-1]:
                return None
            stations.append(Station(text(sid) or '', text(label) or ''))
        elif any(sid) or any(label):
            return None
    if office[-1] or raw[59]:
        return None
    try:
        place = Place(text(raw[36:60]) or '', (state.decode('ascii') if state.isalpha() else ''), lat, lon, std, dst,
                      text(office) or '', gx, gy, tuple(stations)).check()
    except (LocationError, UnicodeDecodeError):
        return None
    return place, sequence


def current(a, b):
    """(Place, sequence) of the newer good copy of the two sectors (A when equal), or (None, 0)."""
    pa, pb = parse(a), parse(b)
    if pb and (not pa or 0 < ((pb[1] - pa[1]) & 0xFFFFFFFF) < 0x80000000):  # B newer, as (int32_t)(b-a) > 0
        return pb
    return pa or (None, 0)


def next_sequence(sequence):
    return (sequence + 1) & 0xFFFFFFFF or 1


def sequence_for(a, b):
    """The sequence of the next write of both copies (A first, then B). A write cut after A leaves
    the new A against the old B, so it must be newer than B's; when B is not good, newer than A's."""
    pa, pb = parse(a), parse(b)
    return next_sequence(pb[1] if pb else pa[1] if pa else 0)


def with_place(image, place, a_range, b_range):
    """`image` (bytes) with `place` in both copies (None: both erased); the ranges are the sectors."""
    data = bytearray(image)
    if place is None:
        for start, end in (a_range, b_range):
            data[start:end] = b'\xff' * (end - start)
        return bytes(data)
    sector = place.sector(sequence_for(image[a_range[0]:a_range[1]], image[b_range[0]:b_range[1]]))
    for start, end in (a_range, b_range):
        data[start:end] = sector
    return bytes(data)


# ---- looking a place up on this PC ------------------------------------------------------------------
@dataclass(frozen=True)
class Found:
    """A match in the bundled table: what the owner picks from."""
    name: str        # "DENVER, CO", or "ZIP 80202"
    state: str       # '' for a ZIP code (the weather service names its state)
    lat: float
    lon: float
    zip: str = ''


_TABLE = []


def table():
    if not _TABLE:
        with gzip.open(TABLE, 'rt', encoding='utf-8') as f:
            for line in f:
                if line.startswith('#'):
                    continue
                _TABLE.append(line.rstrip('\n').split('\t'))
    return _TABLE


def find(query, limit=8):
    """Matches for a ZIP code ("80202") or a place ("Denver, CO", "Denver CO", "Denver"), best first."""
    query = str(query or '').strip()
    if re.fullmatch(r'\d{5}(-\d{4})?', query):
        code = query[:5]
        return [Found(f'ZIP {code}', '', float(r[2]), float(r[3]), code) for r in table() if r[0] == 'Z' and r[1] == code]
    text = plain(query)
    state = ''
    found = re.fullmatch(r'(.*?)[ ,]+([A-Z]{2})', text)
    if found and found.group(2) in STATES:
        text, state = found.group(1).strip(' ,'), found.group(2)
    if not text:
        return []
    exact, starts = [], []
    for r in table():
        if r[0] != 'P' or (state and r[1] != state):
            continue
        name = plain(r[2])
        if name == text:
            exact.append(r)
        elif name.startswith(text + ' ') or (len(text) >= 3 and name.startswith(text)):
            starts.append(r)
    rows = exact + sorted(starts, key=lambda r: (len(r[2]), r[2], r[1]))
    return [Found(f'{plain(r[2])}, {r[1]}', r[1], float(r[3]), float(r[4])) for r in rows[:limit]]


def more_hint(query, shown):
    """'' when the `shown` matches are all there are; else a plain hint to add the state, with a real
    place that was left out as the example ('More places match: add the state, for example "Springfield, MO".')."""
    extra = find(query, limit=shown + 1)
    if len(extra) <= shown:
        return ''
    left_out = extra[shown]
    town = left_out.name.split(',')[0].title() if left_out.state else 'Springfield'
    state = left_out.state or 'MO'
    return f'More places match: add the state, for example "{town}, {state}".'


# ---- completing it from api.weather.gov -------------------------------------------------------------
def fetch_json(path, timeout=15):
    """GET https://api.weather.gov{path} (that host only), as JSON."""
    import urllib.request
    if not path.startswith('/') or '//' in path:
        raise LocationError('Bad weather service request.')
    request = urllib.request.Request(API + path, headers={'User-Agent': AGENT, 'Accept': 'application/geo+json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return json.loads(reply.read(2_000_000).decode('utf-8'))
    except Exception as error:
        status = getattr(error, 'code', None)
        if status == 404:
            raise LocationError('The weather service has no forecast for that place (US places only).')
        raise LocationError('The weather service did not answer. Check this PC\'s internet connection and try '
                            'again.')


def resolve(found, fetch=fetch_json):
    """The Place for a table match: /points for its office, grid, zone and name, then the grid's six
    nearest stations. `fetch(path)` gives the reply as JSON (tests pass recorded replies)."""
    lat, lon = round(found.lat, 4), round(found.lon, 4)
    try:
        points = fetch(f'/points/{lat:.4f},{lon:.4f}')['properties']
        office, gx, gy = str(points['gridId']), int(points['gridX']), int(points['gridY'])
        std, dst = zone(points.get('timeZone'))
        where = (points.get('relativeLocation') or {}).get('properties') or {}
        replies = fetch(f'/gridpoints/{office}/{gx},{gy}/stations?limit={STATIONS}')
        features = replies.get('features') or []
    except LocationError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise LocationError('The weather service sent something this app does not understand.')
    state = found.state or plain(where.get('state', ''))[:2]
    if found.zip:
        city = plain(where.get('city', '')) or f'ZIP {found.zip}'
        name = f'{city}, {state}' if state else city
    else:
        name = found.name
    while len(name) >= NAME_LEN and ' ' in name.split(',')[0]:
        head, _, tail = name.partition(',')
        name = head.rsplit(' ', 1)[0] + (',' + tail if tail else '')
    name = name[:NAME_LEN - 1].rstrip(' ,')
    stations, labels = [], set()
    for feature in features:
        props = (feature or {}).get('properties') or {}
        sid = plain(props.get('stationIdentifier', '')).replace(' ', '')
        if not _ID_OK.fullmatch(sid) or any(s.id == sid for s in stations):
            continue
        label = station_label(props.get('name', ''), sid)
        if label in labels:  # two stations of one town: the second names its id too
            label = f'{label[:LABEL_MAX - len(sid) - 1].rstrip()} {sid}' if len(sid) < LABEL_MAX - 2 else sid
        labels.add(label)
        stations.append(Station(sid, label))
        if len(stations) == STATIONS:
            break
    place = Place(plain(name), state, int(round(lat * 10000)), int(round(lon * 10000)), std, dst, office, gx, gy,
                  tuple(stations), str(points.get('timeZone') or ''))
    return place.check()


# ---- the PC's saved copy ----------------------------------------------------------------------------
def load_saved(path=None):
    """The location saved on this PC, or None (the console's compiled-in default)."""
    path = Path(path or SAVED)
    try:
        return Place.from_json(json.loads(path.read_text(encoding='utf-8')))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save(place, path=None):
    """Keep `place` in this PC's private file (None: forget it, back to the default)."""
    from chromatic_trial_runner import write_durable
    path = Path(path or SAVED)
    if place is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return None
    place.check()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_durable(path, json.dumps(place.to_json(), indent=1).encode('utf-8'))
    return place


def describe(place, pack=None):
    """The window's lines for the confirmation (shown masked while private details are hidden)."""
    if place is None:
        return [DEFAULT_TEXT]
    shows = place.shows(pack)
    pages = ('Pages: all, the Regional Forecast maps and Current Radar included.' if shows['regional'] and shows['radar'] else
             'Pages: all except Current Radar (Rain Outlook instead).' if shows['regional'] else
             'Pages: all except the two Regional Forecast maps and Current Radar (Rain Outlook instead).')
    return [place.name, f'Time zone: {zone_words(place.tz_std_min, place.tz_dst)}',
            'Stations: ' + ', '.join(s.label for s in place.stations), pages]

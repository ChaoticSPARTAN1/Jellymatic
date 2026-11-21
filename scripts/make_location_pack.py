# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Make a location pack: the Local Weather channel's area pages for any US place (launcher 1.6.5 and newer).

    py -3 scripts\\make_location_pack.py "Denver, CO" denver.pjlp        (or a ZIP code)

(Chromatic Manager's Settings > Weather location > Make the area pages shows a preview of every page; this script
writes only the pack file.)

What goes in (chromatic_location_pack.py has the format):
  - the place: looked up on this PC in the bundled Census places table and completed from api.weather.gov, exactly
    as Chromatic Manager's Weather location does (chromatic_weather_location.py);
  - a state map: the state's outline from the bundled Census cartographic boundary table (us-states.txt.gz), the place
    and up to five of the state's larger towns with their highs (size: the town's land area from the bundled Census
    2024 Gazetteer table, us-town-sizes.txt.gz; a census-designated place counts at a third), and a yellow box where
    the close-up is;
  - an area close-up: about 1.6 degrees of longitude around the place, the state line, the place and three nearby towns;
  - Current Radar: the nearest NWS radar (the place's /points reply names it), as the small 160 x 97 picture of the
    NWS map server's single-radar layer over a 2.4-degree box around the place; the picture is asked for once here to
    check the site answers (otherwise the pack has no radar and the console shows Rain Outlook instead);
  - the observation list: the place's six stations and the nearest station of each state-map town.
Only api.weather.gov (the place and its towns' /points, their nearest stations) and the map server (one picture)
are asked. Nothing private is used or written; the pack holds only public places and NWS identifiers.
"""
import gzip
import json
import math
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'chromatic' / 'weather' / 'tools'))
import chromatic_location_pack as lp  # noqa: E402
import chromatic_weather_location as wl  # noqa: E402
import wx_mapdraw as M  # noqa: E402

STATES = ROOT / 'chromatic' / 'weather' / 'places' / 'us-states.txt.gz'
ART = ROOT / 'chromatic' / 'weather' / 'wx_art.h'
MAPS = 'https://opengeo.ncep.noaa.gov'
PERIODS = {lp.OBS: 1800, lp.RECENT: 1800, lp.HISTORY: 3600, lp.FORECAST: 1800, lp.CITY: 3600, lp.ALERTS: 600, lp.RADAR: 600}
CLOSE_SPAN, RADAR_SPAN = 1.6, 2.4            # degrees of longitude across the 160 pixels


class MakeError(ValueError):
    """Why a pack could not be made, in plain words."""


# ---- the console's fonts (wx_art.h): text widths, so call-outs never overlap ----
def _advances(name):
    body = re.search(rf'{name}\[\d+\]=\{{(.*?)\}};', ART.read_text('utf-8'), re.S).group(1)
    return [int(v) for v in re.findall(r'\d+', body)]


_SILK, _PS = None, None


def text_w(text, font='silk'):
    global _SILK, _PS
    if _SILK is None:
        _SILK, _PS = _advances('wx_silk_adv'), _advances('wx_ps_adv')
    adv = _SILK if font == 'silk' else _PS
    return sum(adv[(ord(c) - 32) if 32 <= ord(c) < 32 + 64 else 31] for c in text) - 1


# ---- the bundled Census tables ----
def outlines(state):
    rings = []
    with gzip.open(STATES, 'rt', encoding='utf-8') as f:
        for line in f:
            if line.startswith(state + '\t'):
                rings.append([tuple(float(v) for v in p.split(',')) for p in line.split('\t', 1)[1].split()])
    if not rings:
        raise MakeError('No outline for that state in the bundled table.')
    return rings


SIZES = ROOT / 'chromatic' / 'weather' / 'places' / 'us-town-sizes.txt.gz'


def towns(state):
    """[(name, lat, lon, size)] of the state's places of 2 square miles or more (us-town-sizes.txt.gz); size = land
    area, a census-designated place counted at a third (the bundled tables hold no population)."""
    out = []
    with gzip.open(SIZES, 'rt', encoding='utf-8') as f:
        for line in f:
            if line.startswith(state + '\t'):
                st, name, lat, lon, area, kind = line.rstrip('\n').split('\t')
                out.append((name, float(lat), float(lon), float(area) * (1 / 3 if kind == 'C' else 1)))
    return out


def far_enough(lat, lon, chosen, apart):
    return all(math.hypot(lat - c[1], (lon - c[2]) * math.cos(math.radians(lat))) >= apart for c in chosen)


# ---- the weather service (only the area asked about) ----
def fetch_json(path):
    return wl.fetch_json(path)


def radar_ok(path, timeout=20):
    """Does the map server answer this picture request with a 160 x 97 GIF?"""
    request = urllib.request.Request(MAPS + path, headers={'User-Agent': wl.AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            data = reply.read(200_000)
    except Exception:
        return False
    return data[:6] in (b'GIF87a', b'GIF89a') and data[6:10] == bytes((160, 0, 97, 0))


# ---- call-out layout ----
def callout_box(x, y, name):
    w = text_w(name)
    return (min(x - w // 2, x - 17), y, max(x + (w + 1) // 2, x + 26), y + 23)


def overlaps(a, b, pad=2):
    return not (a[2] + pad < b[0] or b[2] + pad < a[0] or a[3] + pad < b[1] or b[3] + pad < a[1])


class Placer:
    """Call-outs on one map, one at a time: each at the nearest free spot to its dot (never over another call-out, a
    kept-out box or any dot), or not at all. side 'above': the call-out sits under the dot, its leader from its top;
    'below': over the dot, its leader from its bottom (wx_draw.h draws the name at y, the icon and figure under it)."""

    def __init__(self, hard, top, bottom, soft=()):
        # hard: words or a title (no call-out, leader or dot over them); soft: the close-up's box (no call-out over it)
        self.hard, self.soft = list(hard), list(soft)
        self.boxes, self.dots, self.spots, self.top, self.bottom = [], [], {}, top, bottom
        self.lines = []                              # the leaders placed so far: sampled points

    @staticmethod
    def _points(x0, y0, x1, y1):
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / 2))
        return [(x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n) for k in range(n + 1)]

    @staticmethod
    def _inside(p, box, pad):
        return box[0] - pad <= p[0] <= box[2] + pad and box[1] - pad <= p[1] <= box[3] + pad

    def place(self, name, dx, dy, avoid=()):
        if any(self._inside((dx, dy), b, 2) for b in self.boxes) or any(self._inside((dx, dy), b, 0) for b in self.hard):
            return None                              # its dot would sit under a call-out (or the words) already there
        best = None
        for oy in list(range(4, 64, 4)) + list(range(-4, -64, -4)):
            for ox in range(-132, 133, 6):
                x = int(round(dx + ox))
                y = int(round(dy + oy)) if oy > 0 else int(round(dy + oy - 23))
                if not (self.top <= y <= self.bottom - 24):
                    continue
                box = callout_box(x, y, name)
                if box[0] < 1 or box[2] > 158:
                    continue
                if any(overlaps(box, b) for b in self.boxes + self.hard + self.soft) or \
                        any(self._inside(p, box, 2) for p in self.dots + list(avoid) + [(dx, dy)]):
                    continue
                side = 'above' if oy > 0 else 'below'
                lx, ly = leader_end(x, y, side)
                line = self._points(lx, ly, dx, dy)
                # its leader crosses no call-out or kept-out box but its own; no leader crosses it
                if any(self._inside(p, b, 1) for p in line[1:-2] for b in self.boxes) or \
                        any(self._inside(p, b, 0) for p in line[1:-2] for b in self.hard) or \
                        any(self._inside(p, box, 1) for other in self.lines for p in other):
                    continue
                cost = math.hypot(ox, oy) + (6 if oy < 0 else 0)
                if best is None or cost < best[0]:
                    best = (cost, x, y, side, box, line)
        if not best:
            return None
        self.spots[name] = best[1:4]
        self.boxes.append(best[4])
        self.dots.append((dx, dy))
        self.lines.append(best[5])
        return best[1:4]


def leader_end(x, y, side):
    return (x, y - 2) if side == 'above' else (x, y + 24)


# ---- the pack ----
def views(lat, lon):
    """The close-up's and the radar page's projections around the place, and their heights in degrees."""
    lat_ref = round(lat, 1)
    k = math.cos(math.radians(lat_ref))
    close_h = 108 / (160 / CLOSE_SPAN / k)
    radar_h = 97 / (160 / RADAR_SPAN / k)
    close = dict(lon0=lon - CLOSE_SPAN / 2, lat_top=lat + close_h / 2, px_per_lon=160 / CLOSE_SPAN, x0=0, y0=23, lat_ref=lat_ref)
    radar = dict(lon0=lon - RADAR_SPAN / 2, lat_top=lat + radar_h / 2, px_per_lon=160 / RADAR_SPAN, x0=0, y0=23, lat_ref=lat_ref)
    return close, close_h, radar, radar_h


def state_projection(rings):
    lons = [p[0] for r in rings for p in r]
    lats = [p[1] for r in rings for p in r]
    lon_w, lon_e, lat_s, lat_n = min(lons), max(lons), min(lats), max(lats)
    mid = (lat_n + lat_s) / 2
    avail_w, avail_h, top = 150.0, 98.0, 30
    scale = min(avail_w / (lon_e - lon_w), avail_h * math.cos(math.radians(mid)) / (lat_n - lat_s))
    width = (lon_e - lon_w) * scale
    return M.Projection(lon_w, lat_n, scale, x0=int(round(155 - width)), y0=top, lat_ref=mid), max(lon_e - lon_w, lat_n - lat_s)


def make(place, fetch=fetch_json, radar_check=radar_ok, log=lambda line: None):
    """The location pack for `place` (a chromatic_weather_location.Place). `fetch(path)` answers api.weather.gov
    requests (tests pass recorded replies); `radar_check(path)` says whether the map server answers the picture."""
    state = place.state
    lat, lon = place.lat(), place.lon()
    points = fetch(f'/points/{lat:.4f},{lon:.4f}')['properties']
    site = str(points.get('radarStation') or '').upper()
    rings = outlines(state)
    everyone = sorted((t for t in towns(state) if len(t[0]) <= 15 and lp.wx_text_ok(t[0])), key=lambda t: -t[3])
    state_pool = everyone[:30]                       # the state map: only among the larger towns
    home_name = place.name.split(',')[0][:15].rstrip()
    area = (place.name.split(',')[0].strip() + ' AREA')[:29].rstrip()   # as the console names it (wx_place.h)
    close, close_h, radar_view, radar_h = views(lat, lon)
    inside = lambda la, lo: abs(lo - lon) < CLOSE_SPAN / 2 * 0.85 and abs(la - lat) < close_h / 2 * 0.8
    # ---- the close-up: the place, then up to three larger nearby towns that fit with their call-outs ----
    title = (area if len(area) <= 23 else home_name + ' AREA')[:23].rstrip()
    cp = M.Projection(**close)
    tw = max(text_w(title), text_w('TONIGHT LOWS'))
    hx, hy = cp.xy(lon, lat)
    # the title in the bottom-right corner, or the top-right one when the place sits there
    title_xy = (157, 106) if not (hx > 157 - tw - 10 and hy > 96) else (157, 26)
    close_place = Placer([(157 - tw - 1, title_xy[1] - 1, 158, title_xy[1] + 17)], 23, 131)
    if not close_place.place(home_name, hx, hy, avoid=[cp.xy(t[2], t[1]) for t in everyone[:40] if inside(t[1], t[2]) and t[0] != home_name][:6]):
        raise MakeError('The place\'s own call-out does not fit its close-up.')
    close_towns = []
    for t in everyone:
        if len(close_towns) == 3:
            break
        if t[0] == home_name or not inside(t[1], t[2]) or not far_enough(t[1], t[2], close_towns + [(home_name, lat, lon)], 0.12):
            continue
        if close_place.place(t[0], *cp.xy(t[2], t[1])):
            close_towns.append(t)
    # ---- the state map: the place, then up to five larger towns, spread out, outside the close-up ----
    sp, extent = state_projection(rings)
    bx0, by0 = sp.xy(close['lon0'], close['lat_top'])
    bx1, by1 = sp.xy(close['lon0'] + CLOSE_SPAN, close['lat_top'] - close_h)
    words = (4, 106)
    yellow = (min(bx0, bx1) - 1, min(by0, by1) - 1, max(bx0, bx1) + 1, max(by0, by1) + 1)
    state_place = Placer([(words[0] - 1, words[1] - 1, words[0] + 50, words[1] + 17)], 23, 131, soft=[yellow])
    others = [sp.xy(t[2], t[1]) for t in state_pool[:12] if t[0] != home_name and not inside(t[1], t[2])]
    if not state_place.place(home_name, *sp.xy(lon, lat), avoid=others):
        raise MakeError('The place\'s own call-out does not fit the state map.')
    state_towns = []
    for apart in (0.22, 0.15, 0.1):
        for t in state_pool:
            if len(state_towns) == 5:
                break
            if t[0] == home_name or inside(t[1], t[2]) or t in state_towns or t in close_towns:
                continue
            if not far_enough(t[1], t[2], state_towns + [(home_name, lat, lon)], extent * apart):
                continue
            if state_place.place(t[0], *sp.xy(t[2], t[1])):
                state_towns.append(t)
    cities = [(home_name, lat, lon)] + [(t[0], t[1], t[2]) for t in close_towns] + [(t[0], t[1], t[2]) for t in state_towns]
    log(f'{len(close_towns)} towns on the close-up, {len(state_towns)} on the state map')
    # ---- their forecast grids and (for the state map's) nearest stations ----
    grids, extra_stations = {}, []
    for i, (name, la, lo) in enumerate(cities):
        p = points if i == 0 else fetch(f'/points/{la:.4f},{lo:.4f}')['properties']
        grids[name] = (str(p['gridId']), int(p['gridX']), int(p['gridY']))
        if i > len(close_towns):
            found = fetch(f"/gridpoints/{p['gridId']}/{p['gridX']},{p['gridY']}/stations?limit=1").get('features') or []
            sid = wl.plain(((found[0] or {}).get('properties') or {}).get('stationIdentifier', '')).replace(' ', '') if found else ''
            if re.fullmatch(r'[A-Z0-9]{3,7}', sid):
                extra_stations.append((sid, name))
    # observation rows: the place's stations, then the state-map towns' (a station once)
    stations = [s.id for s in place.stations]
    rows = [(s.label, i) for i, s in enumerate(place.stations)]
    for sid, name in extra_stations:
        if len(rows) >= lp.MAX_OBS_ROWS:
            break
        if sid not in stations:
            if len(stations) >= lp.MAX_STATIONS:
                continue
            stations.append(sid)
        rows.append((name[:15].rstrip(), stations.index(sid)))
    # the requests, in the built-in pack's order
    req = [lp.Request(lp.OBS, 0, lp.API, 600, f'/stations/{stations[0]}/observations/latest'),
           lp.Request(lp.FORECAST, 0, lp.API, PERIODS[lp.FORECAST], f'/gridpoints/{place.office}/{place.grid_x},{place.grid_y}/forecast'),
           lp.Request(lp.ALERTS, 0, lp.API, PERIODS[lp.ALERTS], f'/alerts/active?point={lat:.4f},{lon:.4f}'),
           lp.Request(lp.HISTORY, 0, lp.API, PERIODS[lp.HISTORY], f'/stations/{stations[0]}/observations?limit=40')]
    for i, (name, _, _) in enumerate(cities[1:], 1):
        office, gx, gy = grids[name]
        req.append(lp.Request(lp.CITY, i, lp.API, PERIODS[lp.CITY], f'/gridpoints/{office}/{gx},{gy}/forecast'))
    req += [lp.Request(lp.OBS, s, lp.API, PERIODS[lp.OBS], f'/stations/{stations[s]}/observations/latest') for s in range(1, len(stations))]
    req += [lp.Request(lp.RECENT, s, lp.API, PERIODS[lp.RECENT], f'/stations/{stations[s]}/observations?limit=8') for s in range(1, len(stations))]
    # the radar picture: the nearest radar's single-radar layer over 2.4 degrees around the place
    radar = None
    if re.fullmatch(r'[KTP][A-Z]{3}', site):
        s = site.lower()
        west, east = radar_view['lon0'], radar_view['lon0'] + RADAR_SPAN
        north, south = radar_view['lat_top'], radar_view['lat_top'] - radar_h
        path = (f'/geoserver/{s}/ows?service=WMS&version=1.1.1&request=GetMap&layers={s}_sr_bref&styles='
                f'&format=image/gif&transparent=true&srs=EPSG:4326&bbox={west:.4f},{south:.4f},{east:.4f},{north:.4f}&width=160&height=97')
        if radar_check(path):
            radar = (path, (west, east, south, north))
            req.append(lp.Request(lp.RADAR, 0, lp.MAPS, PERIODS[lp.RADAR], path))
        else:
            log('the radar picture did not answer: no radar page (Rain Outlook instead)')
    if len(req) > lp.MAX_REQUESTS:
        raise MakeError('Too many requests for one pack.')
    # ---- the art: the state map ----
    c = M.Raster()
    for ring in rings:
        c.d.polygon([(x + 1, y + 1) for x, y in (sp.xy(*p) for p in ring)], fill=M.BLACK)
    for ring in rings:
        c.d.polygon([sp.xy(*p) for p in ring], fill=M.DGREEN, outline=M.GREEN)
    for name, la, lo in [cities[0]] + cities[1 + len(close_towns):]:
        x, y, side = state_place.spots[name]
        dx, dy = sp.xy(lo, la)
        M.leader(c, *leader_end(x, y, side), int(round(dx)), int(round(dy)))
    c.d.rectangle((bx0, by0, bx1, by1), outline=M.YELLOW)
    state_y0, state_rows, state_map = M.rows_of(c)
    if state_y0 < 23 or state_y0 + state_rows > 131:
        raise MakeError('The state map does not fit the page.')
    # ---- the close-up: green land, the state line, the towns ----
    c = M.Raster()
    c.d.rectangle((0, 23, 159, 130), fill=M.DGREEN)
    for ring in rings:
        pts = [cp.xy(*p) for p in ring]
        c.d.line(pts + [pts[0]], fill=M.GREEN)
    for name, la, lo in cities[:1 + len(close_towns)]:
        x, y, side = close_place.spots[name]
        dx, dy = cp.xy(lo, la)
        M.leader(c, *leader_end(x, y, side), int(round(dx)), int(round(dy)))
    close_map = bytes(M.rle(c.rows(23, 131)))
    # ---- the radar map: the state line in blue on navy, and three marks ----
    radar_map, marks, rtowns, view = b'', (0, 0, 0, 0, 0, 0), ('', '', ''), (0, 0, 0, 0)
    if radar:
        rp = M.Projection(**radar_view)
        ry0, ry1 = M.RADAR_ROWS
        c = M.Raster()
        c.d.rectangle((0, ry0, 159, ry1 - 1), fill=M.NAVY)
        for ring in rings:
            pts = [rp.xy(*p) for p in ring]
            c.d.line(pts + [pts[0]], fill=M.BLUE)
        radar_map = bytes(M.rle(c.rows(ry0, ry1)))
        marked = [(cities[0][0], lat, lon)]
        for n, la, lo in cities[1:]:
            x, y = rp.xy(lo, la)
            hx_, hy_ = rp.xy(lon, lat)
            fits = x - 4 - text_w(n) >= 1           # its name is drawn right-aligned, left of the mark
            if fits and 3 < x < 156 and 32 < y < 112 and all(abs(y - rp.xy(o, l)[1]) > 9 for _, l, o in marked) and len(marked) < 3:
                marked.append((n, la, lo))
        while len(marked) < 3:
            marked.append(marked[-1])
        xy = [rp.xy(lo, la) for _, la, lo in marked]
        marks = tuple(v for x, y in xy for v in (min(159, max(0, int(round(x)))), min(119, max(23, int(round(y))))))
        rtowns = tuple(n for n, _, _ in marked)
        e4 = lambda v: int(round(v * 10000))
        w_, e_, s_, n_ = radar[1]
        view = (e4(w_), e4(e_), e4(s_), e4(n_))
    spots = []
    for name, _, _ in cities:
        s = state_place.spots.get(name)
        t = close_place.spots.get(name)
        spots.append(((s[0], s[1]) if s else (255, 255)) + ((t[0], t[1]) if t else (255, 255)))
    pack = lp.Pack(place.name, area, state, place.lat_e4, place.lon_e4, place.office, place.grid_x, place.grid_y,
                   len(stations), tuple(r[0] for r in rows), tuple(r[1] for r in rows), tuple(n for n, _, _ in cities), 0,
                   tuple(spots), tuple(req), state_map, state_y0, state_rows, close_map, 23, 108, title, *title_xy, *words,
                   radar_map, marks, rtowns, view, note='made by make_location_pack.py')
    bad = pack.problems()
    if bad:
        raise MakeError('The pages could not be made for that place (' + ', '.join(dict.fromkeys(bad)) + ').')
    return pack


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description='Make a location pack for a US place (public data only).')
    ap.add_argument('place', help='a ZIP code or "City, ST"')
    ap.add_argument('out', help='the pack file to write (.pjlp)')
    args = ap.parse_args(argv)
    found = wl.find(args.place)
    if not found:
        print('No US place matched. Try a ZIP code or "City, ST".')
        return 2
    place = wl.resolve(found[0])
    pack = make(place, log=print)
    lp.save_file(pack, args.out)
    for line in lp.describe(pack):
        print(line)
    print(f'wrote {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

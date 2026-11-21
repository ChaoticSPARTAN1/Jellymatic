# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Writes the Local Weather channel's built-in place ("pack"), Orlando, FL, the default with no weather location:

  chromatic/weather/wx_location.h  its tables and the wx_pack struct that names them (wx_pack_fl, tables wx_fl_*),
                                   and the wx_pack type the location packs (wx_lpack.h) use too
  chromatic/weather/wx_art_fl.h    the Orlando pack's art: an original Florida outline, a Central Florida close-up and
                                   the dark map under its radar picture, drawn here from short lon/lat point lists
                                   (FL_OUTLINE etc. below; coordinates rounded from public-domain US geography).

Its grid points and stations come from chromatic/weather/fixtures/places/orlando/ (tools/capture_orlando_pack.py). No
network. Any other place's area pages come from a location pack (scripts/make_location_pack.py), not from here.

    py -3 chromatic\\weather\\tools\\make_location.py
"""
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
WX = ROOT / 'chromatic' / 'weather'
OUT = WX / 'wx_location.h'
OUT_ART = WX / 'wx_art_fl.h'
FIX_FL = WX / 'fixtures' / 'places' / 'orlando'
sys.path.insert(0, str(Path(__file__).resolve().parent))
import wx_mapdraw as M  # noqa: E402

KINDS = 'enum {WX_K_OBS,WX_K_RECENT,WX_K_HISTORY,WX_K_FORECAST,WX_K_CITY,WX_K_ALERTS,WX_K_RADAR};'

# ---- the Orlando pack -------------------------------------------------------------------------------------------
FL_HOME = ('ORLANDO, FL', 'ORLANDO AREA', 28.5383, -81.3792)
# Regional Forecast cities (the table's order): state map ones, then the Central Florida close-up's; Orlando on both
FL_CITIES = [('PENSACOLA', 30.4213, -87.2169), ('TALLAHASSEE', 30.4383, -84.2807), ('JACKSONVILLE', 30.3322, -81.6557),
             ('TAMPA', 27.9506, -82.4572), ('MIAMI', 25.7617, -80.1918), ('ORLANDO', 28.5383, -81.3792),
             ('SANFORD', 28.8003, -81.2731), ('KISSIMMEE', 28.2920, -81.4076), ('TITUSVILLE', 28.6122, -80.8076)]
FL_CLOSE_UP = {'SANFORD', 'KISSIMMEE', 'TITUSVILLE'}  # the close-up's cities are asked for first
# Latest Observations rows: label, station (slot order = first appearance); ids checked against api.weather.gov
FL_ROWS = [('ORLANDO', 'KMCO'), ('ORLANDO EXEC', 'KORL'), ('SANFORD', 'KSFB'), ('KISSIMMEE', 'KISM'), ('LEESBURG', 'KLEE'),
           ('TITUSVILLE', 'KTIX'), ('DAYTONA BEACH', 'KDAB'), ('MELBOURNE', 'KMLB'), ('LAKELAND', 'KLAL'), ('TAMPA', 'KTPA'),
           ('JACKSONVILLE', 'KJAX')]
# the radar rows' view: 2.4 degrees across 160 pixels from 82.30 W, north 29.25 N
RADAR_FL = dict(lon0=-82.30, lat_top=29.25, px_per_lon=160 / 2.4, x0=0, y0=23, lat_ref=28.6)
RADAR_FL_TOWNS = [('ORLANDO', -81.3792, 28.5383), ('SANFORD', -81.2731, 28.8003), ('KISSIMMEE', -81.4076, 28.2920)]

# Florida's outline, clockwise from the north-west corner (lon, lat), rounded to about 0.05 degree
FL_OUTLINE = [(-87.63, 30.99), (-86.80, 30.99), (-85.90, 31.00), (-85.00, 31.00), (-85.02, 30.80), (-84.86, 30.70),
              (-83.60, 30.64), (-82.21, 30.57), (-82.05, 30.37), (-82.02, 30.75), (-81.45, 30.71), (-81.43, 30.40),
              (-81.30, 29.90), (-81.15, 29.55), (-81.02, 29.20), (-80.87, 28.95), (-80.65, 28.68), (-80.53, 28.45),
              (-80.60, 28.25), (-80.40, 27.70), (-80.20, 27.25), (-80.04, 26.75), (-80.08, 26.20), (-80.13, 25.77),
              (-80.30, 25.40), (-80.45, 25.20), (-80.90, 25.14), (-81.12, 25.33), (-81.35, 25.80), (-81.80, 26.10),
              (-81.95, 26.50), (-82.25, 26.75), (-82.45, 27.10), (-82.70, 27.45), (-82.83, 27.95), (-82.80, 28.20),
              (-82.70, 28.70), (-82.80, 29.00), (-83.03, 29.14), (-83.40, 29.67), (-83.70, 29.95), (-84.20, 30.08),
              (-84.40, 29.92), (-84.98, 29.70), (-85.37, 29.67), (-85.42, 29.94), (-85.66, 30.15), (-86.20, 30.38),
              (-86.80, 30.38), (-87.15, 30.33), (-87.52, 30.28), (-87.60, 30.60)]
FL_OKEECHOBEE = [(-81.08, 26.95), (-80.95, 27.18), (-80.72, 27.14), (-80.62, 26.92), (-80.74, 26.70), (-80.98, 26.74)]
# the Central Florida coast (the barrier islands' sea side), north to south
FL_COAST = [(-81.15, 29.55), (-81.02, 29.20), (-80.93, 29.05), (-80.86, 28.95), (-80.78, 28.85), (-80.68, 28.73),
            (-80.58, 28.58), (-80.53, 28.45), (-80.58, 28.38), (-80.60, 28.25), (-80.56, 28.08), (-80.48, 27.90),
            (-80.40, 27.70)]
FL_LAKES = [[(-81.70, 28.62), (-81.65, 28.67), (-81.58, 28.66), (-81.55, 28.60), (-81.60, 28.56), (-81.67, 28.57)],  # Apopka
            [(-81.33, 28.83), (-81.25, 28.845), (-81.21, 28.81), (-81.30, 28.795)],                                 # Monroe
            [(-81.25, 28.735), (-81.18, 28.755), (-81.14, 28.72), (-81.22, 28.70)],                                 # Jesup
            [(-81.30, 28.33), (-81.24, 28.335), (-81.21, 28.28), (-81.27, 28.25), (-81.31, 28.28)],                 # East Toho
            [(-81.43, 28.28), (-81.37, 28.27), (-81.36, 28.18), (-81.42, 28.15), (-81.45, 28.20)],                  # Toho
            [(-81.32, 28.10), (-81.24, 28.08), (-81.22, 27.95), (-81.30, 27.90), (-81.35, 28.00)]]                  # Kissimmee
# call-outs (centre x, top y, side of the name the leader leaves from) on each map
FL_STATE_SPOTS = {'PENSACOLA': (29, 42, 'above'), 'TALLAHASSEE': (66, 68, 'above'), 'JACKSONVILLE': (126, 24, 'below'),
                  'TAMPA': (80, 98, 'above'), 'ORLANDO': (132, 79, 'above'), 'MIAMI': (122, 105, 'below')}
FL_STATE_VIEW = dict(x0=46, y0=30, width=106)  # the outline's box on the state map
FL_STATE_WORDS = (4, 106)                   # "THURSDAY" / "HIGHS"
FL_CLOSE_SPOTS = {'SANFORD': (100, 26, 'below'), 'ORLANDO': (28, 60, 'below'), 'KISSIMMEE': (28, 100, 'below'),
                  'TITUSVILLE': (126, 78, 'above')}
FL_CLOSE_TITLE = ('CENTRAL FLORIDA', 157, 106)  # right-aligned
FL_CLOSE_VIEW = dict(lon0=-82.05, lat_top=29.05, px_per_lon=100.0, x0=0, y0=23, lat_ref=28.6)


def radar_request(site, west, north, span, lat_ref):
    east = west + span
    south = north - 97 / (160 / span / math.cos(math.radians(lat_ref)))
    path = (f'/geoserver/{site}/ows?service=WMS&version=1.1.1&request=GetMap&layers={site}_sr_bref&styles='
            f'&format=image/gif&transparent=true&srs=EPSG:4326&bbox={west:.4f},{south:.4f},{east:.4f},{north:.4f}&width=160&height=97')
    return path, (west, east, south, north)


def fl_tables():
    """Orlando: the request plan every pack uses, from the captured replies (fixtures/places/orlando)."""
    index = json.loads((FIX_FL / 'index.json').read_text('utf-8'))

    def grid(lat, lon):
        p = json.loads((FIX_FL / index[f'/points/{lat:.4f},{lon:.4f}']).read_text('utf-8'))['properties']
        return f"/gridpoints/{p['gridId']}/{p['gridX']},{p['gridY']}/forecast", p
    name, area, lat, lon = FL_HOME
    stations, rows = [], []
    for label, sid in FL_ROWS:
        assert f'/stations/{sid}/observations/latest' in index, f'{sid} was not checked against api.weather.gov'
        if sid not in stations:
            stations.append(sid)
        rows.append((label, stations.index(sid)))
    cities = [c[0] for c in FL_CITIES]
    home_city = cities.index('ORLANDO')
    radar, bounds = radar_request('kmlb', RADAR_FL['lon0'], RADAR_FL['lat_top'], 2.4, RADAR_FL['lat_ref'])
    assert radar in index, 'the radar request differs from the one the capture proved'
    home_grid, p = grid(lat, lon)
    assert p['radarStation'] == 'KMLB' and p['timeZone'] == 'America/New_York'  # US Eastern with daylight time
    req = [(f'/stations/{stations[0]}/observations/latest', 'OBS', 0, 600), (home_grid, 'FORECAST', home_city, 1800),
           (f'/alerts/active?point={lat:.4f},{lon:.4f}', 'ALERTS', 0, 600),
           (f'/stations/{stations[0]}/observations?limit=40', 'HISTORY', 0, 3600)]
    for i in sorted((i for i in range(len(cities)) if i != home_city), key=lambda i: cities[i] not in FL_CLOSE_UP):
        req.append((grid(FL_CITIES[i][1], FL_CITIES[i][2])[0], 'CITY', i, 3600))
    for s in range(1, len(stations)):
        req.append((f'/stations/{stations[s]}/observations/latest', 'OBS', s, 1800))
    for s in range(1, len(stations)):
        req.append((f'/stations/{stations[s]}/observations?limit=8', 'RECENT', s, 1800))
    req.append((radar, 'RADAR', 0, 600))
    return dict(name=name, area=area, state='FL', lat=lat, lon=lon, office=p['gridId'], grid=(p['gridX'], p['gridY']),
                stations=stations, rows=rows, cities=cities, home_city=home_city, req=req, radar_bounds=bounds)


def fl_art():
    """The Orlando pack's three maps and call-out spots (wx_mapdraw: the reference's colours and rules)."""
    out, sizes = [], {}
    where = {c[0]: (c[2], c[1]) for c in FL_CITIES}
    # state map: the outline (shadow, dark green, green edge), Lake Okeechobee, leaders, the Central Florida box
    lon_w, lon_e = min(p[0] for p in FL_OUTLINE), max(p[0] for p in FL_OUTLINE)
    lat_n, lat_s = max(p[1] for p in FL_OUTLINE), min(p[1] for p in FL_OUTLINE)
    proj = M.Projection(lon_w, lat_n, FL_STATE_VIEW['width'] / (lon_e - lon_w), x0=FL_STATE_VIEW['x0'], y0=FL_STATE_VIEW['y0'],
                        lat_ref=28.0)
    assert proj.xy(lon_e, lat_s)[1] <= 128, 'the outline must end above the ticker'
    c = M.blank()
    pts = [proj.xy(*p) for p in FL_OUTLINE]
    c.d.polygon([(x + 1, y + 1) for x, y in pts], fill=M.BLACK)
    c.d.polygon(pts, fill=M.DGREEN, outline=M.GREEN)
    c.d.polygon([proj.xy(*p) for p in FL_OKEECHOBEE], fill=M.NAVY, outline=M.GREEN)
    for name, (sx, sy_, side) in FL_STATE_SPOTS.items():
        px, py = proj.xy(*where[name])
        M.leader(c, sx, sy_ + 24 if side == 'below' else sy_ - 2, int(round(px)), int(round(py)))
    x0, y0 = proj.xy(FL_CLOSE_VIEW['lon0'], FL_CLOSE_VIEW['lat_top'])
    x1, y1 = proj.xy(FL_CLOSE_VIEW['lon0'] + 160 / FL_CLOSE_VIEW['px_per_lon'],
                     FL_CLOSE_VIEW['lat_top'] - 108 / (FL_CLOSE_VIEW['px_per_lon'] / math.cos(math.radians(FL_CLOSE_VIEW['lat_ref']))))
    c.d.rectangle((x0, y0, x1, y1), outline=M.YELLOW)
    a = np.asarray(c.im)
    ys = np.nonzero((a != M.SKIP).any(1))[0]
    top, bot = int(ys.min()), int(ys.max())
    assert top >= 23 and bot <= 130, (top, bot)
    state = M.rle(a[top:bot + 1])
    out.append(f'#define WX_FL_STATE_Y0 {top}\n#define WX_FL_STATE_ROWS {bot - top + 1}')
    out.append(M.carray('wx_fl_state_map', state))
    sizes['state map'] = len(state)
    # Central Florida close-up: water, the land with its sea coast, the big lakes; rows 23..130
    proj = M.Projection(**FL_CLOSE_VIEW)
    c = M.blank()
    c.d.rectangle((0, 23, 159, 130), fill=M.NAVY)
    land = [proj.xy(-82.4, 29.6)] + [proj.xy(*p) for p in FL_COAST] + [proj.xy(-82.4, 27.6)]
    c.d.polygon(land, fill=M.DGREEN, outline=M.GREEN)
    for lake in FL_LAKES:
        c.d.polygon([proj.xy(*p) for p in lake], fill=M.NAVY, outline=M.GREEN)
    for name, (sx, sy_, side) in FL_CLOSE_SPOTS.items():
        px, py = proj.xy(*where[name])
        M.leader(c, sx, sy_ + 24 if side == 'below' else sy_ - 2, int(round(px)), int(round(py)))
    close = M.rle(np.asarray(c.im)[23:131])
    out.append('#define WX_FL_CLOSE_Y0 23\n#define WX_FL_CLOSE_ROWS 108')
    out.append(M.carray('wx_fl_close_map', close))
    sizes['Central Florida map'] = len(close)
    # radar page: the dark map (sea coast, lakes) under the picture, and the three town marks
    proj = M.Projection(**RADAR_FL)
    ry0, ry1 = M.RADAR_ROWS
    c = M.blank()
    c.d.rectangle((0, ry0, 159, ry1 - 1), fill=M.NAVY)
    c.d.line([proj.xy(*p) for p in FL_COAST], fill=M.BLUE)
    for lake in FL_LAKES:
        p = [proj.xy(*q) for q in lake]
        c.d.line(p + [p[0]], fill=M.BLUE)
    a = np.asarray(c.im).copy()
    a[:ry0] = M.SKIP
    a[ry1:] = M.SKIP
    radar = M.rle(a[ry0:ry1])
    out.append(M.carray('wx_fl_radar_map', radar))
    sizes['radar map'] = len(radar)
    marks = []
    for _, lon, lat in RADAR_FL_TOWNS:
        marks += [int(round(v)) for v in proj.xy(lon, lat)]
    out.append('static const uint8_t wx_fl_radar_marks[6]={' + ','.join(str(v) for v in marks) + '}; /* x, y of '
               + ', '.join(t[0].title() for t in RADAR_FL_TOWNS) + ' */')
    out.append('static const char *const wx_fl_radar_town[3]={' + ','.join(f'"{t[0]}"' for t in RADAR_FL_TOWNS) + '};')
    rows = []
    for name, _, _ in FL_CITIES:
        s, t = FL_STATE_SPOTS.get(name), FL_CLOSE_SPOTS.get(name)
        rows += [s[0] if s else 255, s[1] if s else 255, t[0] if t else 255, t[1] if t else 255]
    out.append('/* call-out centre x, top y on the state map and on the Central Florida map; 255 = not on that map */')
    out.append(M.carray('wx_fl_city_spots', rows, per=4))
    total = sum(sizes.values()) + len(rows)
    out.append('/* Flash bytes: ' + ', '.join(f'{k} {v}' for k, v in sizes.items()) + f', call-outs {len(rows)}; total {total}. */')
    return out, total


def request_rows(prefix, t):
    host = lambda kind: 'WX_RADAR_HOST' if kind == 'RADAR' else 'WX_HOST'
    o = [f'static const wx_request {prefix}requests[{len(t["req"])}]={{']
    for path, kind, slot, _ in t['req']:
        o.append(f'    {{{host(kind)},"{path}",WX_K_{kind},{slot}}},')
    o.append('};')
    o.append(f'static const uint16_t {prefix}request_period[{len(t["req"])}]={{' + ','.join(str(r[3]) for r in t['req']) + '};')
    o.append(f'static const char *const {prefix}obs_label[{len(t["rows"])}]={{' + ','.join(f'"{r[0]}"' for r in t['rows']) + '};')
    o.append(f'static const uint8_t {prefix}obs_slot[{len(t["rows"])}]={{' + ','.join(str(r[1]) for r in t['rows']) + '};')
    o.append(f'static const char *const {prefix}city_name[{len(t["cities"])}]={{' + ','.join(f'"{c}"' for c in t['cities']) + '};')
    return o


def pack(name, prefix, t, art):
    w, e, s, n = t['radar_bounds']
    e4 = lambda v: int(round(v * 10000))
    return [f'static const wx_pack {name}={{',
            f'    "{t["name"]}","{t["area"]}","{t["state"]}",{t["lat"]},{t["lon"]},{e4(t["lat"])},{e4(t["lon"])},"{t["office"]}",'
            f'{t["grid"][0]},{t["grid"][1]},',
            f'    {len(t["stations"])},{len(t["rows"])},{len(t["cities"])},{t["home_city"]},{len(t["req"])},',
            f'    {prefix}requests,{prefix}request_period,{prefix}obs_label,{prefix}obs_slot,{prefix}city_name,',
            f'    {art["state"]},sizeof({art["state"]}),{art["state_y0"]},{art["state_rows"]},'
            f'{art["close"]},sizeof({art["close"]}),{art["close_y0"]},{art["close_rows"]},',
            f'    "{art["title"]}",{art["title_x"]},{art["title_y"]},{art["words"][0]},{art["words"][1]},{art["spots"]},',
            f'    {art["radar"]},sizeof({art["radar"]}),{art["marks"]},{art["towns"]},',
            f'    {e4(w)},{e4(e)},{e4(s)},{e4(n)}}};']


def main():
    fl = fl_tables()
    assert len(fl['req']) <= 33 and len(fl['stations']) <= 11 and len(fl['rows']) <= 12 and len(fl['cities']) <= 9
    art_fl, art_bytes = fl_art()
    OUT_ART.write_text('\n'.join(['/* SPDX-License-Identifier: GPL-3.0-or-later', ' * Additional terms: ADDITIONAL-TERMS.md',
                                  ' * Local Weather channel: the ORLANDO, FL pack\'s art (launcher 1.6.4). WRITTEN BY',
                                  ' * chromatic/weather/tools/make_location.py from its own short lon/lat point lists (an original',
                                  ' * drawing of Florida\'s outline, the Central Florida coast and lakes). Do not edit by hand. */',
                                  '#ifndef WX_ART_FL_H', '#define WX_ART_FL_H', '#include <stdint.h>', ''] + art_fl
                                 + [f'#define WX_FL_ART_BYTES {art_bytes}', '#endif']) + '\n', 'utf-8', newline='\n')
    o = ['/* SPDX-License-Identifier: GPL-3.0-or-later', ' * Additional terms: ADDITIONAL-TERMS.md',
         ' * Local Weather channel: the built-in place ("pack") and its request list. WRITTEN BY',
         ' * chromatic/weather/tools/make_location.py from the US National Weather Service replies kept in',
         ' * chromatic/weather/fixtures/places/orlando/. Do not edit by hand.',
         ' * With no weather location record the channel shows WX_DEFAULT_PACK (Orlando, FL). Any other place\'s area pages',
         ' * (Regional Forecast maps, Current Radar) come from a location pack Chromatic Manager writes (wx_lpack.h). */',
         '#ifndef WX_LOCATION_H', '#define WX_LOCATION_H', '#include <stdint.h>', '#include "wx_art_fl.h"', '',
         '/* named arrays, so every table shares the same two host pointers (weather_esp32.c tells hosts apart by pointer) */',
         'static const char wx_host_api[]="api.weather.gov",wx_host_radar[]="opengeo.ncep.noaa.gov";',
         '#define WX_HOST wx_host_api', '#define WX_RADAR_HOST wx_host_radar',
         '/* the largest tables a pack may have (built-in or a location pack); the channel\'s state is sized by them */',
         '#define WX_STATIONS 11', '#define WX_OBS_ROWS 12', '#define WX_CITIES 9', '#define WX_REQUESTS 33',
         KINDS,
         'typedef struct {const char *host,*path;uint8_t kind,slot;} wx_request;',
         '/* ---- Orlando, FL ---- */']
    o += request_rows('wx_fl_', fl)
    o += ['/* A place\'s pack: its tables, its maps and its radar view (built in, or a location pack: wx_lpack.h). */',
          'typedef struct wx_pack {',
          '    const char *name,*area,*state;           /* "ORLANDO, FL", the opening card\'s area line, "FL" */',
          '    double lat,lon;                           /* home point (almanac) */',
          '    int32_t lat_e4,lon_e4;const char *office;uint16_t grid_x,grid_y; /* a record naming exactly these is this pack */',
          '    uint8_t stations,obs_rows,cities,home_city,requests;',
          '    const wx_request *req;const uint16_t *period;const char *const *obs_label;const uint8_t *obs_slot;',
          '    const char *const *city_name;',
          '    const uint8_t *state_map;uint16_t state_map_n;uint8_t state_y0,state_rows;',
          '    const uint8_t *close_map;uint16_t close_map_n;uint8_t close_y0,close_rows;',
          '    const char *close_title;uint8_t close_title_x,close_title_y,words_x,words_y;const uint8_t *city_spots;',
          '    const uint8_t *radar_map;uint16_t radar_map_n;const uint8_t *radar_marks;const char *const *radar_town;',
          '    int32_t radar_w,radar_e,radar_s,radar_n;  /* the radar rows\' view, 1e-4 degrees */',
          '} wx_pack;']
    o += pack('wx_pack_fl', 'wx_fl_', fl, dict(state='wx_fl_state_map', state_y0='WX_FL_STATE_Y0', state_rows='WX_FL_STATE_ROWS',
                                              close='wx_fl_close_map', close_y0='WX_FL_CLOSE_Y0', close_rows='WX_FL_CLOSE_ROWS',
                                              title=FL_CLOSE_TITLE[0], title_x=FL_CLOSE_TITLE[1], title_y=FL_CLOSE_TITLE[2],
                                              words=FL_STATE_WORDS, spots='wx_fl_city_spots', radar='wx_fl_radar_map',
                                              marks='wx_fl_radar_marks', towns='wx_fl_radar_town'))
    o += ['/* PC checks only: one more compile-time pack from a header outside these sources (-DWX_EXTRA_PACK_HEADER="..."',
          ' * defining WX_EXTRA_PACK), to model an older launcher\'s built-in place. Never set for the console. */',
          '#ifdef WX_EXTRA_PACK_HEADER', '#include WX_EXTRA_PACK_HEADER', '#endif',
          '/* The place shown with no weather location record: Orlando, FL. */',
          '#ifndef WX_DEFAULT_PACK', '#define WX_DEFAULT_PACK (&wx_pack_fl)', '#endif',
          '#ifdef WX_EXTRA_PACK',
          '#define WX_PACKS 2', 'static const wx_pack *const wx_packs[WX_PACKS]={&wx_pack_fl,WX_EXTRA_PACK};',
          '#else',
          '#define WX_PACKS 1', 'static const wx_pack *const wx_packs[WX_PACKS]={&wx_pack_fl};',
          '#endif',
          '#endif']
    OUT.write_text('\n'.join(o) + '\n', 'utf-8', newline='\n')
    print(f'wrote {OUT} and {OUT_ART} ({art_bytes} bytes of Orlando art)')
    print(f"  {fl['name']}: {len(fl['stations'])} stations, {len(fl['rows'])} rows, {len(fl['cities'])} cities, "
          f"{len(fl['req'])} requests, office {fl['office']} {fl['grid']}")


if __name__ == '__main__':
    main()

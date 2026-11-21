# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Captures the National Weather Service replies of the built-in ORLANDO, FL pack (launcher 1.6.4, the public
default of the Local Weather channel), so make_location.py can write its tables and the tests can feed the console
code real reply shapes with no network.

  * /points for Orlando and the eight other Regional Forecast cities (their forecast office and grid),
  * each observation station's latest report (the ids are checked: a station that does not answer stops the run),
    recent reports where the latest lacks a temperature or sky wording, and the home station's history,
  * the forecasts of Orlando's grid and of the eight cities, the alerts for Orlando's point,
  * ONE radar picture of the Central Florida view from the NWS map server (single-radar KMLB base reflectivity),
    to prove the layer and keep a real reply for the decoder tests.

About 35 requests, one a second, to api.weather.gov (and one to opengeo.ncep.noaa.gov), with a generic
User-Agent and nothing personal: Orlando and the cities are public places. Not run by any test.

    py -3 chromatic\\weather\\tools\\capture_orlando_pack.py

Writes chromatic/weather/fixtures/places/orlando/<file>.json (+ radar-kmlb.gif) and index.json.
"""
import json
import math
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / 'chromatic' / 'weather' / 'fixtures' / 'places' / 'orlando'
AGENT = 'chromatic-weather-test/1.0 (fixture capture)'
HOME = (28.5383, -81.3792)
CITIES = {'PENSACOLA': (30.4213, -87.2169), 'TALLAHASSEE': (30.4383, -84.2807), 'JACKSONVILLE': (30.3322, -81.6557),
          'TAMPA': (27.9506, -82.4572), 'MIAMI': (25.7617, -80.1918), 'ORLANDO': HOME, 'SANFORD': (28.8003, -81.2731),
          'KISSIMMEE': (28.2920, -81.4076), 'TITUSVILLE': (28.6122, -80.8076)}
STATIONS = ['KMCO', 'KORL', 'KSFB', 'KISM', 'KLEE', 'KTIX', 'KDAB', 'KMLB', 'KLAL', 'KTPA', 'KJAX']
# the radar rows' view (make_location.py RADAR_FL): 2.4 degrees across 160 pixels from 82.30 W, north 29.25 N
RADAR = dict(west=-82.30, north=29.25, span=2.4, lat_ref=28.6)


def get(url):
    request = urllib.request.Request(url, headers={'User-Agent': AGENT, 'Accept': 'application/geo+json'})
    with urllib.request.urlopen(request, timeout=30) as reply:
        body = reply.read()
    time.sleep(1.0)
    return body


def name_of(path):
    return path.strip('/').replace('/', '_').replace('?', '_').replace('=', '-').replace(',', '_') + '.json'


def radar_path():
    west, north, span = RADAR['west'], RADAR['north'], RADAR['span']
    east = west + span
    south = north - 97 / (160 / span / math.cos(math.radians(RADAR['lat_ref'])))
    return ('/geoserver/kmlb/ows?service=WMS&version=1.1.1&request=GetMap&layers=kmlb_sr_bref&styles='
            f'&format=image/gif&transparent=true&srs=EPSG:4326&bbox={west:.4f},{south:.4f},{east:.4f},{north:.4f}&width=160&height=97')


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    index = {}

    def keep(path):
        body = get('https://api.weather.gov' + path)
        (OUT / name_of(path)).write_bytes(body)
        index[path] = name_of(path)
        return json.loads(body)
    grids = {}
    for city, (lat, lon) in CITIES.items():
        p = keep(f'/points/{lat:.4f},{lon:.4f}')['properties']
        grids[city] = f"/gridpoints/{p['gridId']}/{p['gridX']},{p['gridY']}"
    for city, grid in grids.items():
        if grid + '/forecast' not in index:
            keep(grid + '/forecast')
    keep(f'/alerts/active?point={HOME[0]:.4f},{HOME[1]:.4f}')
    for n, sid in enumerate(STATIONS):
        latest = keep(f'/stations/{sid}/observations/latest')['properties']
        if n and ((latest.get('temperature') or {}).get('value') is None or not latest.get('textDescription')):
            keep(f'/stations/{sid}/observations?limit=8')
    keep(f'/stations/{STATIONS[0]}/observations?limit=40')
    gif = get('https://opengeo.ncep.noaa.gov' + radar_path())
    assert gif[:6] in (b'GIF87a', b'GIF89a'), 'the map server did not send a GIF'
    (OUT / 'radar-kmlb.gif').write_bytes(gif)
    index[radar_path()] = 'radar-kmlb.gif'
    (OUT / 'index.json').write_text(json.dumps(index, indent=1), 'utf-8')
    print(f'orlando: {len(index)} replies; grids: {grids}')


if __name__ == '__main__':
    main()

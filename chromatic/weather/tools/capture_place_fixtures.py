# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Captures the National Weather Service replies the Local Weather channel uses for a place other
than the compiled-in default, for a PUBLIC test place (Denver, CO), so the
tests can feed the console code real reply shapes with no network.

Each place: /points, the grid's nearest stations, the forecast, the alerts, each station's latest
report, the home station's history (limit=40) and, for a station whose latest report lacks a
temperature or sky wording, its recent reports (limit=8). About a dozen requests a place, one a
second, to api.weather.gov only, with a generic User-Agent and nothing personal.

    py -3 chromatic\\weather\\tools\\capture_place_fixtures.py

Writes chromatic/weather/fixtures/places/<place>/<file>.json and an index.json naming each
file's request path. Not run by any test.
"""
import json
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / 'chromatic' / 'weather' / 'fixtures' / 'places'
PLACES = {'denver': (39.7392, -104.9903)}
AGENT = 'chromatic-weather-test/1.0 (fixture capture)'


def get(path):
    request = urllib.request.Request('https://api.weather.gov' + path,
                                     headers={'User-Agent': AGENT, 'Accept': 'application/geo+json'})
    with urllib.request.urlopen(request, timeout=30) as reply:
        body = reply.read()
    time.sleep(1.0)
    return body


def name_of(path):
    return path.strip('/').replace('/', '_').replace('?', '_').replace('=', '-').replace(',', '_') + '.json'


def main():
    for place, (lat, lon) in PLACES.items():
        folder = OUT / place
        folder.mkdir(parents=True, exist_ok=True)
        index = {}

        def keep(path):
            body = get(path)
            (folder / name_of(path)).write_bytes(body)
            index[path] = name_of(path)
            return json.loads(body)
        points = keep(f'/points/{lat:.4f},{lon:.4f}')['properties']
        grid = f"/gridpoints/{points['gridId']}/{points['gridX']},{points['gridY']}"
        stations = keep(grid + '/stations?limit=6')['features']
        ids = [s['properties']['stationIdentifier'] for s in stations][:6]
        keep(grid + '/forecast')
        keep(f'/alerts/active?point={lat:.4f},{lon:.4f}')
        for n, sid in enumerate(ids):
            latest = keep(f'/stations/{sid}/observations/latest')['properties']
            if n and ((latest.get('temperature') or {}).get('value') is None or not latest.get('textDescription')):
                keep(f'/stations/{sid}/observations?limit=8')
        keep(f'/stations/{ids[0]}/observations?limit=40')
        (folder / 'index.json').write_text(json.dumps(index, indent=1), 'utf-8')
        print(f'{place}: {len(index)} replies')


if __name__ == '__main__':
    main()

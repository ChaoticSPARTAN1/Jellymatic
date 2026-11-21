# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Records what the location pack maker asks api.weather.gov (and whether the map server answers the radar picture)
for two public sample places, so the tests make the same packs with no network:
chromatic/weather/fixtures/packs/<name>/ (index.json: request -> file; radar.json: the picture's answer).

    py -3 chromatic\\weather\\tools\\capture_pack_fixtures.py

A few dozen polite requests, once. Public places only (Denver, CO; Orlando, FL); nothing private.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
import chromatic_weather_location as wl  # noqa: E402
import make_location_pack as mk  # noqa: E402

OUT = ROOT / 'chromatic' / 'weather' / 'fixtures' / 'packs'
PLACES = {'denver': 'Denver, CO', 'orlando': 'Orlando, FL'}


def main():
    for key, query in PLACES.items():
        folder = OUT / key
        folder.mkdir(parents=True, exist_ok=True)
        index, radar = {}, {}

        def fetch(path):
            data = wl.fetch_json(path)
            name = re.sub(r'[^A-Za-z0-9.-]+', '_', path).strip('_')[:120] + '.json'
            (folder / name).write_text(json.dumps(data), 'utf-8')
            index[path] = name
            return data

        def check(path):
            ok = mk.radar_ok(path)
            radar[path] = ok
            return ok
        place = wl.resolve(wl.find(query)[0], fetch)
        pack = mk.make(place, fetch, check, print)
        (folder / 'index.json').write_text(json.dumps(index, indent=1), 'utf-8')
        (folder / 'radar.json').write_text(json.dumps(radar, indent=1), 'utf-8')
        print(f'{key}: {len(index)} replies, radar {list(radar.values())}, pack {pack.size()} bytes')


if __name__ == '__main__':
    main()

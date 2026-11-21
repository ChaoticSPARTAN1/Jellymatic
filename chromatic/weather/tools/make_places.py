# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Writes chromatic/weather/places/us-places.txt.gz: the small table Chromatic Manager looks a weather
location up in (a US ZIP code, or "City, ST"), on the PC, with no network.

From the US Census Bureau's 2024 Gazetteer files (public domain, works of the US Government):
  ZCTA:   2024_Gaz_zcta_national.zip  (ZIP Code Tabulation Areas and their internal points)
  Places: 2024_Gaz_place_national.zip (incorporated places and CDPs, with their states)
downloaded by hand from the URLs in SOURCES and checked against the SHA-256 there. Only the code
or name, the state and the internal point (2 decimals, about 1 km: finer than the forecast grid of 2.5 km) are kept.

    py -3 -I chromatic\\weather\\tools\\make_places.py <folder holding the two .txt files>

Lines of the table (tab-separated, UTF-8, gzip):
  Z <zip> <lat> <lon>
  P <state> <NAME> <lat> <lon>      NAME in capitals, without the Census kind ("Winter Park city" -> "WINTER PARK")
"""
import gzip
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / 'chromatic' / 'weather' / 'places'
BASE = 'https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/'
SOURCES = {
    '2024_Gaz_zcta_national.zip': '7b85c04a131672f58b38a950eb82855d5fd4054f0bd9bc3f69aa28897a714e3d',
    '2024_Gaz_place_national.zip': 'cf262fc92b2326f7a8c62a89d156a60eb17d64d6d35f7a62310c43bb08972c06',
}
KIND = re.compile(r'\s+(city and borough|unified government|consolidated government|metro government|'
                  r'metropolitan government|zona urbana|comunidad|CDP|city|town|village|borough|township|'
                  r'municipality|plantation|corporation|urban county)$')


def place_name(name):
    name = re.sub(r'\s*\(balance\)$', '', name.strip())
    while True:
        cut = KIND.sub('', name)
        if cut == name:
            return name.upper()
        name = cut


def main(folder):
    folder = Path(folder)
    out = [f'# US Census Bureau 2024 Gazetteer (public domain): {BASE}{n} sha256 {h}' for n, h in SOURCES.items()]
    zips = places = 0
    with open(folder / '2024_Gaz_zcta_national.txt', encoding='utf-8') as f:
        head = [h.strip() for h in next(f).split('\t')]
        at = {h: i for i, h in enumerate(head)}
        for line in f:
            r = [v.strip() for v in line.split('\t')]
            out.append(f"Z\t{r[at['GEOID']]}\t{float(r[at['INTPTLAT']]):.2f}\t{float(r[at['INTPTLONG']]):.2f}")
            zips += 1
    with open(folder / '2024_Gaz_place_national.txt', encoding='utf-8') as f:
        head = [h.strip() for h in next(f).split('\t')]
        at = {h: i for i, h in enumerate(head)}
        for line in f:
            r = [v.strip() for v in line.split('\t')]
            name = place_name(r[at['NAME']])
            if not name or '\t' in name:
                continue
            out.append(f"P\t{r[at['USPS']]}\t{name}\t{float(r[at['INTPTLAT']]):.2f}\t{float(r[at['INTPTLONG']]):.2f}")
            places += 1
    OUT.mkdir(parents=True, exist_ok=True)
    data = ('\n'.join(out) + '\n').encode('utf-8')
    with gzip.GzipFile(OUT / 'us-places.txt.gz', 'wb', compresslevel=9, mtime=0) as g:
        g.write(data)
    print(f'wrote {OUT / "us-places.txt.gz"}: {zips} ZIP codes, {places} places, '
          f'{(OUT / "us-places.txt.gz").stat().st_size} bytes')


if __name__ == '__main__':
    main(sys.argv[1])

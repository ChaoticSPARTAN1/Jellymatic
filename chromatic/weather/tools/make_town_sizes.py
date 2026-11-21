# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Writes chromatic/weather/places/us-town-sizes.txt.gz: each Census place's land area and kind, for the location pack
maker to pick a state's larger towns for its maps (scripts/make_location_pack.py). No population is bundled; a
town's land area, with census-designated places (CDPs, not incorporated) counted at a third, ranks them well enough.

From the same US Census Bureau 2024 Gazetteer place file as us-places.txt.gz (public domain; URL and SHA-256 in
SOURCES.txt), read straight from the downloaded zip:

    py -3 -I chromatic\\weather\\tools\\make_town_sizes.py <2024_Gaz_place_national.zip>

Places under 2 square miles are left out (never a state map's town). Lines (tab-separated, UTF-8, gzip): <state> <NAME> <lat> <lon> <land area, square miles, 1 decimal> <C or I>
(NAME exactly as in us-places.txt.gz, so the two tables join on state, name and point).
"""
import gzip
import hashlib
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_places  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / 'places' / 'us-town-sizes.txt.gz'
NAME = '2024_Gaz_place_national.zip'


def main(source):
    raw = Path(source).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == make_places.SOURCES[NAME], 'not the reviewed Gazetteer file'
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        text = z.read(next(n for n in z.namelist() if n.endswith('.txt'))).decode('utf-8')
    lines = text.splitlines()
    head = [h.strip() for h in lines[0].split('\t')]
    at = {h: i for i, h in enumerate(head)}
    out = [f'# US Census Bureau 2024 Gazetteer (public domain): {make_places.BASE}{NAME} sha256 {digest}']
    for line in lines[1:]:
        r = [v.strip() for v in line.split('\t')]
        name = make_places.place_name(r[at['NAME']])
        if not name or '\t' in name:
            continue
        kind = 'C' if r[at['NAME']].endswith(' CDP') else 'I'
        if float(r[at['ALAND_SQMI']]) < 2.0:   # too small to be one of a state's map towns
            continue
        out.append(f"{r[at['USPS']]}\t{name}\t{float(r[at['INTPTLAT']]):.2f}\t{float(r[at['INTPTLONG']]):.2f}\t"
                   f"{float(r[at['ALAND_SQMI']]):.1f}\t{kind}")
    data = ('\n'.join(out) + '\n').encode('utf-8')
    OUT.write_bytes(gzip.compress(data, compresslevel=9, mtime=0))
    assert gzip.decompress(OUT.read_bytes()) == data
    print(f'{len(out) - 1} places; {OUT.name} {OUT.stat().st_size} bytes, sha256 {hashlib.sha256(OUT.read_bytes()).hexdigest()}')


if __name__ == '__main__':
    main(sys.argv[1])

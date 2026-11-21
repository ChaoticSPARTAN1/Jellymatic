# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Writes chromatic/weather/places/us-states.txt.gz: every US state's outline as short lon/lat point lists, for the
location pack maker (scripts/make_location_pack.py) to draw a state map and area close-ups from.

From the US Census Bureau's cartographic boundary file of the states at 1:20,000,000 (public domain), downloaded once:
    https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_state_20m.zip
The shapefile is read here with nothing but the standard library (polygons, and the STUSPS field of its table), every
ring simplified (Douglas-Peucker, 0.01 degree) and rounded to 0.01 degree; rings smaller than 0.02 square degrees
(small islands) are left out.

    py -3 chromatic\\weather\\tools\\make_states.py <the downloaded zip>

Lines: "<state>\\t<lon,lat lon,lat ...>", one ring a line, the largest first per state.
"""
import gzip
import hashlib
import io
import struct
import sys
import zipfile
from pathlib import Path

URL = 'https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_state_20m.zip'
OUT = Path(__file__).resolve().parents[1] / 'places' / 'us-states.txt.gz'
TOLERANCE, KEEP_AREA = 0.01, 0.02


def dbf_states(data):
    """The STUSPS value of every record of a dBase III table."""
    count, header, record = struct.unpack_from('<IHH', data, 4)
    fields, at = [], 32
    while data[at] != 0x0D:
        name = data[at:at + 11].split(b'\0')[0].decode('ascii')
        fields.append((name, data[at + 16]))
        at += 32
    out = []
    for i in range(count):
        row = data[header + i * record:header + (i + 1) * record]
        pos = 1
        values = {}
        for name, size in fields:
            values[name] = row[pos:pos + size].decode('latin-1').strip()
            pos += size
        out.append(values['STUSPS'])
    return out


def shp_rings(data):
    """Each record's rings (lists of (lon, lat)) of a polygon shapefile."""
    at, out = 100, []
    while at < len(data):
        _, length = struct.unpack_from('>II', data, at)
        body = data[at + 8:at + 8 + length * 2]
        at += 8 + length * 2
        kind = struct.unpack_from('<i', body, 0)[0]
        if kind == 0:
            out.append([])
            continue
        assert kind == 5, kind
        parts, points = struct.unpack_from('<ii', body, 36)
        starts = list(struct.unpack_from(f'<{parts}i', body, 44)) + [points]
        xy = struct.unpack_from(f'<{points * 2}d', body, 44 + 4 * parts)
        pts = [(xy[2 * i], xy[2 * i + 1]) for i in range(points)]
        out.append([pts[starts[k]:starts[k + 1]] for k in range(parts)])
    return out


def simplify(points, tol):
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        (x0, y0), (x1, y1) = points[a], points[b]
        dx, dy = x1 - x0, y1 - y0
        norm = (dx * dx + dy * dy) ** 0.5
        best, at = -1.0, None
        for i in range(a + 1, b):
            x, y = points[i]
            d = abs(dy * (x - x0) - dx * (y - y0)) / norm if norm else ((x - x0) ** 2 + (y - y0) ** 2) ** 0.5
            if d > best:
                best, at = d, i
        if at is not None and best > tol:
            keep[at] = True
            stack += [(a, at), (at, b)]
    return [p for p, k in zip(points, keep) if k]


def area(ring):
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]))) / 2


def main():
    source = Path(sys.argv[1])
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        shp = z.read('cb_2023_us_state_20m.shp')
        dbf = z.read('cb_2023_us_state_20m.dbf')
    states, rings = dbf_states(dbf), shp_rings(shp)
    assert len(states) == len(rings), (len(states), len(rings))
    lines, kept = ['# US Census Bureau cartographic boundary file, states 1:20,000,000, 2023 (public domain): '
                   f'{URL} sha256 {digest}'], 0
    for state, parts in sorted(zip(states, rings)):
        out = []
        for ring in parts:
            small = [(round(x, 2), round(y, 2)) for x, y in simplify(ring, TOLERANCE)]
            small = [p for i, p in enumerate(small) if i == 0 or p != small[i - 1]]
            if len(small) >= 4 and area(small) >= KEEP_AREA:
                out.append(small)
        for ring in sorted(out, key=area, reverse=True):
            lines.append(state + '\t' + ' '.join(f'{x:.2f},{y:.2f}' for x, y in ring))
            kept += len(ring)
    data = ('\n'.join(lines) + '\n').encode('utf-8')
    OUT.write_bytes(gzip.compress(data, mtime=0))
    check = gzip.decompress(OUT.read_bytes())
    assert check == data
    print(f'{len(states)} states, {len(lines) - 1} rings, {kept} points; {OUT.name} {OUT.stat().st_size} bytes, '
          f'sha256 {hashlib.sha256(OUT.read_bytes()).hexdigest()}; source {len(raw)} bytes sha256 {digest}')


if __name__ == '__main__':
    main()

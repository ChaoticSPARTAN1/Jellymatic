# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Generate pocket/ui_font.h, the start-up menu's and Jellyfin app's text font, from Google's Roboto font.

    py -3 scripts/build_ui_font.py --fetch      # download Roboto 2.138 (pinned in upstream.lock.json) first
    py -3 scripts/build_ui_font.py              # rasterize upstream/roboto/Roboto-Medium.ttf -> pocket/ui_font.h
    py -3 scripts/build_ui_font.py --check      # only compare with the pocket/ui_font.h in the repository

Needs Python 3 with fontTools (`py -3 -m pip install fonttools`). The font file is checked against the
SHA-256 in upstream.lock.json, and the rasterizer is plain Python, so the same font always gives the same
table on any PC. The same approach and numbers as the Pocket app (PocketFin's build_roboto_atlas.py and
build_ui_font.py), in one script:

1. The 95 printable ASCII characters (32..126) are rasterized at 18 px per em, drawn 1.125 times wider than
   tall, on a fixed baseline, with the x-height and cap height on whole pixels, into 29 x 25 cells of 8-bit
   coverage (nonzero winding, 16 vertical samples per pixel, exact horizontal coverage).
2. Each cell is box-scaled to 13 x 13 and its rows 2..12 kept: 13 x 11 = 143 values per character, the
   table format the launcher reads (pj_font_alpha[95 * 143], pj_font_advance[95]).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'pocket/ui_font.h'
DEFAULT_FONT = ROOT / 'upstream/roboto/Roboto-Medium.ttf'

PX = 18             # em size in pixels
STRETCH = 1.125     # horizontal scale
CELL_W, CELL_H = 29, 25
ORIGIN_X = 1        # pen position inside the cell
BASELINE = 17       # baseline row inside the cell
INK_ROWS = (2, 20)  # the band the layout reserves; the faint overshoot outside it is dropped
SUBROWS = 16        # vertical samples per pixel; horizontal coverage is exact
CURVE_STEPS = 12    # line segments per quadratic or cubic curve
BODY_W, BODY_H, FIRST_ROW = 13, 13, 2  # the launcher's cells: 13 x 13, rows 2..12 kept


def lock_entry():
    lock = json.loads((ROOT / 'upstream.lock.json').read_text(encoding='utf-8-sig'))
    return lock['roboto']


def fetch(entry):
    """Download the pinned release archive and take Roboto-Medium.ttf out of it, both checked by SHA-256."""
    if DEFAULT_FONT.is_file() and hashlib.sha256(DEFAULT_FONT.read_bytes()).hexdigest() == entry['ttf_sha256']:
        print(f'Roboto: already in {DEFAULT_FONT.relative_to(ROOT)}')
        return
    with urllib.request.urlopen(entry['url'], timeout=120) as response:
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != entry['zip_sha256']:
        raise SystemExit(f'{entry["url"]} does not match the pinned SHA-256')
    member = zipfile.ZipFile(io.BytesIO(archive)).read(entry['member'])
    if hashlib.sha256(member).hexdigest() != entry['ttf_sha256']:
        raise SystemExit(f'{entry["member"]} does not match the pinned SHA-256')
    DEFAULT_FONT.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_FONT.write_bytes(member)
    print(f'Roboto: {entry["tag"]} {entry["member"]} in {DEFAULT_FONT.relative_to(ROOT)}')


def edge_pen(glyph_set):
    from fontTools.pens.basePen import BasePen

    class EdgePen(BasePen):
        """Collects an outline as straight edges in font units."""

        def __init__(self):
            super().__init__(glyph_set)
            self.edges, self.start, self.last = [], None, None

        def _moveTo(self, point):
            self.start = self.last = point

        def _lineTo(self, point):
            self.edges.append((self.last, point))
            self.last = point

        def _qCurveToOne(self, control, point):
            (x0, y0), (x1, y1), (x2, y2) = self.last, control, point
            prev = self.last
            for step in range(1, CURVE_STEPS + 1):
                t = step / CURVE_STEPS
                u = 1 - t
                here = (u * u * x0 + 2 * u * t * x1 + t * t * x2, u * u * y0 + 2 * u * t * y1 + t * t * y2)
                self.edges.append((prev, here))
                prev = here
            self.last = point

        def _curveToOne(self, c1, c2, point):
            (x0, y0), (x1, y1), (x2, y2), (x3, y3) = self.last, c1, c2, point
            prev = self.last
            for step in range(1, CURVE_STEPS + 1):
                t = step / CURVE_STEPS
                u = 1 - t
                here = (u ** 3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t ** 3 * x3,
                        u ** 3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t ** 3 * y3)
                self.edges.append((prev, here))
                prev = here
            self.last = point

        def _closePath(self):
            if self.last != self.start:
                self.edges.append((self.last, self.start))
            self.last = self.start
    return EdgePen()


def vertical_map(font):
    """Font units (y up) -> pixels above the baseline, with x-height and cap height on whole pixels."""
    scale = PX / font['head'].unitsPerEm
    os2 = font['OS/2']
    x_height, cap_height = os2.sxHeight, os2.sCapHeight
    x_px, cap_px = round(x_height * scale), round(cap_height * scale)

    def to_px(y):
        if y <= 0:
            return y * scale
        if y <= x_height:
            return y * x_px / x_height
        if y <= cap_height:
            return x_px + (y - x_height) * (cap_px - x_px) / (cap_height - x_height)
        return cap_px + (y - cap_height) * scale
    return to_px


def add_span(acc, a, b):
    a, b = max(0.0, a), min(float(CELL_W), b)
    if b <= a:
        return
    ia, ib = int(math.floor(a)), int(math.floor(b))
    if ia == ib:
        acc[ia] += b - a
        return
    acc[ia] += ia + 1 - a
    for column in range(ia + 1, min(ib, CELL_W)):
        acc[column] += 1.0
    if ib < CELL_W:
        acc[ib] += b - ib


def rasterize(edges):
    """Nonzero-winding coverage of pixel-space edges into a CELL_W x CELL_H byte cell."""
    cell = bytearray(CELL_W * CELL_H)
    edges = [(x0, y0, x1, y1) for (x0, y0), (x1, y1) in edges if y0 != y1]
    for row in range(CELL_H):
        acc = [0.0] * CELL_W
        for sub in range(SUBROWS):
            yc = row + (sub + 0.5) / SUBROWS
            crossings = []
            for x0, y0, x1, y1 in edges:
                if (y0 <= yc < y1) or (y1 <= yc < y0):
                    crossings.append((x0 + (yc - y0) * (x1 - x0) / (y1 - y0), 1 if y1 > y0 else -1))
            crossings.sort()
            winding, span_start = 0, 0.0
            for x, direction in crossings:
                before = winding
                winding += direction
                if before == 0 and winding != 0:
                    span_start = x
                elif before != 0 and winding == 0:
                    add_span(acc, span_start, x)
        if not INK_ROWS[0] <= row <= INK_ROWS[1]:
            continue
        for column in range(CELL_W):
            cell[row * CELL_W + column] = max(0, min(255, round(255 * acc[column] / SUBROWS)))
    return cell


def atlas(font_path, entry):
    """(29 x 25 coverage cells of the 95 characters, their advances at 18 px), from the pinned font."""
    from fontTools.ttLib import TTFont
    data = Path(font_path).read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != entry['ttf_sha256']:
        raise SystemExit(f'{font_path}: SHA-256 {actual} is not the pinned Roboto file ({entry["ttf_sha256"]})')
    font = TTFont(font_path)
    glyph_set, cmap = font.getGlyphSet(), font.getBestCmap()
    scale_x = PX / font['head'].unitsPerEm * STRETCH
    to_px = vertical_map(font)
    cells, advances = bytearray(), []
    for code in range(32, 127):
        name = cmap[code]
        pen = edge_pen(glyph_set)
        glyph_set[name].draw(pen)
        edges = [((ORIGIN_X + x0 * scale_x, BASELINE - to_px(y0)), (ORIGIN_X + x1 * scale_x, BASELINE - to_px(y1)))
                 for (x0, y0), (x1, y1) in pen.edges]
        cells += rasterize(edges)
        advances.append(round(font['hmtx'][name][0] * scale_x))
    return bytes(cells), advances


def header(cells, advances, entry):
    """The launcher's table: each cell box-scaled to 13 x 13, rows 2..12 kept (143 values per character)."""
    pixels = []
    for glyph in range(95):
        cell = cells[glyph * CELL_W * CELL_H:(glyph + 1) * CELL_W * CELL_H]
        for y in range(FIRST_ROW, BODY_H):
            y0, y1 = y * CELL_H // BODY_H, (y + 1) * CELL_H // BODY_H
            for x in range(BODY_W):
                x0, x1 = x * CELL_W // BODY_W, (x + 1) * CELL_W // BODY_W
                values = [cell[sy * CELL_W + sx] for sy in range(y0, y1) for sx in range(x0, x1)]
                pixels.append(sum(values) // len(values))
    body_advances = [max(2, round(advance * 4 / 9)) for advance in advances]
    assert len(pixels) == 95 * BODY_W * (BODY_H - FIRST_ROW) == 13585
    text = ('/* SPDX-License-Identifier: Apache-2.0\n'
            ' * Roboto Medium raster atlas, scaled to 13 x 13 cells for body text (rows 2..12 kept).\n'
            f' * Made by scripts/build_ui_font.py from Roboto-Medium.ttf of Roboto {entry["tag"].lstrip("v")}\n'
            ' * (Copyright 2011 Google Inc., Apache License 2.0, see LICENSES/Apache-2.0.txt).\n'
            ' * Changed from the original font: rasterized to 8-bit coverage, widened 1.125x and scaled. */\n'
            '#ifndef POCKET_UI_FONT_H\n#define POCKET_UI_FONT_H\n'
            'static const unsigned char pj_font_advance[95]={' + ','.join(map(str, body_advances)) + '};\n'
            'static const unsigned char pj_font_alpha[13585]={\n')
    for start in range(0, len(pixels), 52):
        text += ','.join(map(str, pixels[start:start + 52])) + ',\n'
    return text + '};\n#endif\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--font', default=str(DEFAULT_FONT), help='Roboto-Medium.ttf (default: upstream/roboto)')
    parser.add_argument('--fetch', action='store_true', help='download the pinned Roboto release first')
    parser.add_argument('--check', action='store_true', help='compare with pocket/ui_font.h instead of writing it')
    args = parser.parse_args()
    entry = lock_entry()
    if args.fetch:
        fetch(entry)
    text = header(*atlas(args.font, entry), entry)
    if args.check:
        same = OUT.read_bytes() == text.encode('ascii')
        print('PASS: pocket/ui_font.h is exactly what the pinned Roboto font gives.' if same else
              'FAIL: pocket/ui_font.h differs from what the pinned Roboto font gives.')
        return 0 if same else 1
    OUT.write_bytes(text.encode('ascii'))
    print(f'Wrote {OUT.relative_to(ROOT)} (sha256 {hashlib.sha256(text.encode("ascii")).hexdigest()[:16]}...).')
    return 0


if __name__ == '__main__':
    sys.exit(main())

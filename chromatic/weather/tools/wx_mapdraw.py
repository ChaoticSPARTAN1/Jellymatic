# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Map drawing for the Local Weather channel's area art (make_location.py's built-in Orlando pack,
make_location_pack.py's location packs): the console's 16-colour palette, the lon/lat projection, call-out leaders,
and the run-length rows the console reads (wx_draw.h, wx_layer). The same rules and colours as the approved PC
reference, so maps drawn here look like the built-in ones.

Two canvases with the same few calls (polygon, line, rectangle): blank() draws with Pillow (the built-in pack's art,
exactly as made before), Raster() in plain Python (location packs, so Chromatic Manager needs no imaging library).
"""
import math

W, H = 160, 144
SKIP = 255                                     # "leave the pixel" in map layers
PALETTE = [(0, 0, 0), (8, 8, 48), (16, 24, 104), (32, 48, 168), (72, 32, 136), (112, 64, 184), (96, 152, 240),
           (240, 240, 240), (248, 216, 40), (240, 128, 24), (208, 32, 32), (160, 168, 200), (48, 184, 72),
           (16, 104, 48), (120, 216, 240), (56, 64, 112)]
(BLACK, NAVY, DBLUE, BLUE, PURPLE, VIOLET, SKY, WHITE, YELLOW, ORANGE, RED,
 GREY, GREEN, DGREEN, CYAN, SLATE) = range(16)
_FLAT = [c for rgb in PALETTE for c in rgb] + [0] * (768 - 48)
RADAR_ROWS = (23, 120)                         # the radar page's map rows (160 x 97)


class Projection:
    """Plain lon/lat to pixels with the east-west squeeze for the latitude."""

    def __init__(self, lon0, lat_top, px_per_lon, x0=0, y0=0, lat_ref=35.6):
        self.lon0, self.lat_top, self.sx = lon0, lat_top, px_per_lon
        self.sy = px_per_lon / math.cos(math.radians(lat_ref))
        self.x0, self.y0 = x0, y0

    def xy(self, lon, lat):
        return (self.x0 + (lon - self.lon0) * self.sx, self.y0 + (self.lat_top - lat) * self.sy)

    def lonlat(self, x, y):
        return (self.lon0 + (x - self.x0) / self.sx, self.lat_top - (y - self.y0) / self.sy)


class Canvas:
    def __init__(self, im):
        from PIL import ImageDraw
        self.im = im
        self.d = ImageDraw.Draw(im)
        self.d.fontmode = '1'

    def rows(self, top, bottom):
        import numpy as np
        return [list(r) for r in np.asarray(self.im)[top:bottom]]


def blank():
    """A see-through 160 x 144 layer in the console's palette (Pillow)."""
    from PIL import Image
    im = Image.new('P', (W, H), SKIP)
    im.putpalette(_FLAT)
    return Canvas(im)


class Raster:
    """A see-through 160 x 144 layer drawn in plain Python: c.d.polygon / line / rectangle as Pillow's ImageDraw takes
    them (points as (x, y) pairs, colours as palette indexes), c.rows(top, bottom) for the run-length rows."""

    def __init__(self):
        self.px = bytearray([SKIP]) * (W * H)
        self.d = self

    def _set(self, x, y, colour):
        if 0 <= x < W and 0 <= y < H:
            self.px[y * W + x] = colour

    def line(self, xy, fill):
        pts = [(xy[i], xy[i + 1]) for i in range(0, len(xy), 2)] if xy and not isinstance(xy[0], (tuple, list)) else list(xy)
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            x0, y0, x1, y1 = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
            dx, dy = abs(x1 - x0), -abs(y1 - y0)
            sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
            err = dx + dy
            while True:
                self._set(x0, y0, fill)
                if x0 == x1 and y0 == y1:
                    break
                e2 = 2 * err
                if e2 >= dy:
                    err += dy
                    x0 += sx
                if e2 <= dx:
                    err += dx
                    y0 += sy

    def polygon(self, pts, fill=None, outline=None):
        pts = [(float(x), float(y)) for x, y in pts]
        if fill is not None and len(pts) >= 3:
            ys = [p[1] for p in pts]
            for y in range(max(0, int(math.floor(min(ys)))), min(H - 1, int(math.ceil(max(ys)))) + 1):
                cy = y + 0.5
                xs = []
                for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
                    if (y0 <= cy < y1) or (y1 <= cy < y0):
                        xs.append(x0 + (cy - y0) * (x1 - x0) / (y1 - y0))
                xs.sort()
                for a, b in zip(xs[0::2], xs[1::2]):
                    for x in range(max(0, int(math.ceil(a - 0.5))), min(W - 1, int(math.floor(b - 0.5))) + 1):
                        self.px[y * W + x] = fill
        if outline is not None and pts:
            self.line(pts + pts[:1], outline)

    def rectangle(self, box, fill=None, outline=None):
        x0, y0, x1, y1 = (int(round(v)) for v in box)
        x0, x1 = min(x0, x1), max(x0, x1)
        y0, y1 = min(y0, y1), max(y0, y1)
        if fill is not None:
            for y in range(max(0, y0), min(H - 1, y1) + 1):
                for x in range(max(0, x0), min(W - 1, x1) + 1):
                    self.px[y * W + x] = fill
        if outline is not None:
            self.line([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], outline)

    def rows(self, top, bottom):
        return [list(self.px[y * W:(y + 1) * W]) for y in range(top, bottom)]


def leader(c, x0, y0, x1, y1):
    """A call-out's leader: a white line with a black shadow, ending in a dot on the city."""
    c.d.line((x0 + 1, y0 + 1, x1 + 1, y1 + 1), fill=BLACK)
    c.d.line((x0, y0, x1, y1), fill=WHITE)
    c.d.rectangle((x1 - 1, y1 - 1, x1 + 1, y1 + 1), fill=WHITE, outline=BLACK)


def rle(a):
    """Run-length rows: (count, colour) pairs, colour 255 leaves the pixel; each row is 160 pixels."""
    out = []
    for row in a:
        x = 0
        while x < len(row):
            n = 1
            while x + n < len(row) and row[x + n] == row[x] and n < 255:
                n += 1
            out += [n, int(row[x])]
            x += n
    return out


def carray(name, data, kind='uint8_t', per=24):
    lines = [f'static const {kind} {name}[{len(data)}]={{']
    for i in range(0, len(data), per):
        lines.append('    ' + ','.join(str(v) for v in data[i:i + per]) + ',')
    lines.append('};')
    return '\n'.join(lines)


def rows_of(c, top=None, bottom=None):
    """(y0, rows, run-length bytes) of the drawn rows of a layer (or of rows top..bottom-1)."""
    if top is None:
        drawn = [y for y, row in enumerate(c.rows(0, H)) if any(v != SKIP for v in row)]
        top, bottom = drawn[0], drawn[-1] + 1
    return top, bottom - top, bytes(rle(c.rows(top, bottom)))

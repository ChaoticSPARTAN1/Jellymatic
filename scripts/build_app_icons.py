# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Builds chromatic/launcher/shared/launcher_icons.h: the project's own 12x12 pixel icons.

TV is the project's rabbit-ear set (chromatic/launcher/art/tv-icon-12.png). ChromaTube is drawn here: a stack of three violet video
cards, the front one a dark screen with two light lines (original art; 2026-11-20, publication audit C2: no play
triangle, no red, no rounded screen, nothing like another service's logo), so the two rows cannot be mistaken for each
other at 12 pixels. One 12x12 image serves the menu row, the app's header and its tab.

The TV and ChromaTube rows of the Chromatic start-up menu (launcher 1.6.0). Run from the project root:
    py -3 scripts/build_app_icons.py
"""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
TV_SOURCE = 'chromatic/launcher/art/tv-icon-12.png'
# B: the back card (deep violet), M: the middle card (the Chromatic violet), F: the front card's frame (pale violet),
# D: its dark screen, W: two light lines on it, '.': see-through
TUBE_ART = ('....BBBBBBBB',
            '....BBBBBBBB',
            '..MMMMMMMMBB',
            '..MMMMMMMMBB',
            'FFFFFFFFMMBB',
            'FDDDDDDFMMB.',
            'FDWWWWDFMM..',
            'FDDDDDDFMM..',
            'FDWWWDDF....',
            'FDDDDDDF....',
            'FFFFFFFF....',
            '............')
TUBE_COLOURS = {'B': (0x5a, 0x48, 0xc8, 255), 'M': (0x8f, 0x7b, 0xff, 255), 'F': (0xc4, 0xb8, 0xff, 255),
                'D': (0x10, 0x10, 0x16, 255), 'W': (255, 255, 255, 255), '.': (0, 0, 0, 0)}


def images():
    tv = Image.open(ROOT / TV_SOURCE).convert('RGBA')
    tube = Image.new('RGBA', (12, 12))
    assert len(TUBE_ART) == 12 and all(len(row) == 12 for row in TUBE_ART)
    for y, row in enumerate(TUBE_ART):
        for x, cell in enumerate(row):
            tube.putpixel((x, y), TUBE_COLOURS[cell])
    return (('tv', tv), ('tube', tube))


def main():
    out = ['/* SPDX-License-Identifier: GPL-3.0-or-later', ' * Additional terms: ADDITIONAL-TERMS.md',
           " * The TV and ChromaTube rows' pixel icons (launcher 1.6.0): the project's own drawings",
           ' * (TV: chromatic/launcher/art/tv-icon-12.png; ChromaTube: a stack of three video cards, scripts/build_app_icons.py),',
           ' * 12x12, as RGB565 + alpha like launcher_art.h. Made by scripts/build_app_icons.py. */',
           '#ifndef PJ_LAUNCHER_ICONS_H', '#define PJ_LAUNCHER_ICONS_H', '#include <stdint.h>']
    for name, image in images():
        assert image.size == (12, 12), (name, image.size)
        rgb, alpha = [], []
        for y in range(12):
            for x in range(12):
                r, g, b, a = image.getpixel((x, y))
                rgb.append(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3) if a else 0)
                alpha.append(a)
        upper = name.upper()
        out += [f'#define PJ_ICON_{upper}_W 12', f'#define PJ_ICON_{upper}_H 12',
                f'static const uint16_t pj_icon_{name}_rgb[144]={{']
        out += [','.join(str(v) for v in rgb[i:i + 24]) + ',' for i in range(0, 144, 24)]
        out += ['};', f'static const uint8_t pj_icon_{name}_alpha[144]={{']
        out += [','.join(str(v) for v in alpha[i:i + 24]) + ',' for i in range(0, 144, 24)]
        out.append('};')
    out.append('#endif')
    (ROOT / 'chromatic/launcher/shared/launcher_icons.h').write_text('\n'.join(out) + '\n', encoding='utf-8', newline='\r\n')
    print('launcher_icons.h written')


if __name__ == '__main__':
    main()

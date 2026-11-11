# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""Shared real bottom-LCD touch contract for all game adapters."""


def encode_sample(data):
    x, y, down = data.get('x'), data.get('y'), data.get('down')
    if type(x) is not int or type(y) is not int or not 0 <= x < 320 or not 0 <= y < 240 \
            or type(down) is not bool:
        raise ValueError('Touch needs x=0..319, y=0..239 and boolean down')
    return f'x={x},y={y},down={int(down)}'

# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Official Jellyfin logo in the Pocket's formats.

Source: jellyfin/jellyfin-ux branding, CC BY-SA 4.0, pinned below by commit and
SHA-256, downloaded read-only to upstream/jellyfin-ux-branding. Produces:
  pocket/ui_brand.h            RGB565 + alpha logo for the app's own screens
  staging/brand/icon.bin       36x36 core icon for the Pocket's core list
  staging/brand/platform.bin   521x165 platform image for the Pocket's library
  staging/brand/*.png          previews of the two Pocket images, as the Pocket shows them
Pocket images are 16-bit little-endian gray (low byte), rotated 90 degrees
counter-clockwise; this matches the platform images already on the card.
"""
from __future__ import annotations

import hashlib
import subprocess

from connection_config import ROOT

COMMIT = '69a2b7b1237732a134dd501ff55b9404cf2d1601'
SOURCE = ROOT / 'upstream/jellyfin-ux-branding'
PINS = {
    'icon-transparent.png': 'f3a12b9fdf5b5dfcf38d2b1cb2eebfb2c55b33d3db8a5776420feb4aeb230ffc',
    'banner-light.png': 'ec1f8120bcad24956c1efa14acb2b8e031c6327717d413b188946314424b9e13',
    'LICENSE': '6d4e2239c9605f95e1031309e8bd156dec62c55817427655003c57baceafb1e2',
}


def source(name):
    path = SOURCE / name
    if hashlib.sha256(path.read_bytes()).hexdigest() != PINS[name]:
        raise SystemExit(f'{name} does not match the pinned official file')
    return path


def rgba(name, width, height):
    """Scaled RGBA, letterboxed with transparent margins to exactly width x height."""
    vf = (f'scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,'
          f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black@0,format=rgba')
    data = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(source(name)), '-vf', vf,
                           '-f', 'rawvideo', '-pix_fmt', 'rgba', '-'], check=True, capture_output=True).stdout
    if len(data) != width * height * 4:
        raise SystemExit(f'{name}: unexpected size from ffmpeg')
    return data


def pocket_gray(name, width, height, margin):
    """Gray over black, rotated for the Pocket. Returns (bin bytes, upright gray)."""
    pixels = rgba(name, width - 2 * margin, height - 2 * margin)
    inner = width - 2 * margin
    gray = bytearray(width * height)
    for y in range(height - 2 * margin):
        for x in range(inner):
            r, g, b, a = pixels[(y * inner + x) * 4:(y * inner + x) * 4 + 4]
            luma = (r * 299 + g * 587 + b * 114) // 1000
            gray[(y + margin) * width + x + margin] = luma * a // 255
    peak = max(gray) or 1
    gray = bytearray(v * 255 // peak for v in gray)  # brightest stroke reads as white
    out = bytearray()
    for row in range(width):            # stored row = original column, right to left
        for column in range(height):    # stored column = original row, top to bottom
            out += bytes((gray[column * width + (width - 1 - row)], 0))
    return bytes(out), bytes(gray)


def preview(gray, width, height, path):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'gray', '-s', f'{width}x{height}',
                    '-i', '-', str(path)], input=gray, check=True)


def c_image(name, file, width, height):
    pixels = rgba(file, width, height)
    color, alpha = [], []
    for i in range(width * height):
        r, g, b, a = pixels[i * 4:i * 4 + 4]
        color.append(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3))
        alpha.append(a)
    lines = [f'#define PJ_BRAND_{name.upper()}_W {width}', f'#define PJ_BRAND_{name.upper()}_H {height}',
             f'static const uint16_t pj_brand_{name}_rgb[{width * height}]={{']
    lines += [','.join(map(str, color[i:i + 24])) + ',' for i in range(0, len(color), 24)]
    lines.append('};')
    lines.append(f'static const uint8_t pj_brand_{name}_alpha[{width * height}]={{')
    lines += [','.join(map(str, alpha[i:i + 32])) + ',' for i in range(0, len(alpha), 32)]
    lines.append('};')
    return '\n'.join(lines)


def build():
    source('LICENSE')
    out = ROOT / 'staging/brand'
    out.mkdir(parents=True, exist_ok=True)
    icon, icon_gray = pocket_gray('icon-transparent.png', 36, 36, 2)
    platform, platform_gray = pocket_gray('banner-light.png', 521, 165, 12)
    (out / 'icon.bin').write_bytes(icon)
    (out / 'platform.bin').write_bytes(platform)
    preview(icon_gray, 36, 36, out / 'icon-preview.png')
    preview(platform_gray, 521, 165, out / 'platform-preview.png')
    header = ['/* Official Jellyfin logo, from jellyfin/jellyfin-ux branding at commit',
              f' * {COMMIT}.',
              ' * Copyright the Jellyfin contributors. Licensed CC BY-SA 4.0:',
              ' * https://creativecommons.org/licenses/by-sa/4.0/ . Scaled to RGB565 + alpha',
              ' * by scripts/build_brand_assets.py; no other change. */',
              '#ifndef PJ_UI_BRAND_H', '#define PJ_UI_BRAND_H', '#include <stdint.h>',
              c_image('banner', 'banner-light.png', 124, 36),   # source screen
              c_image('mark', 'banner-light.png', 55, 16),      # header over the hero
              '#endif', '']
    (ROOT / 'pocket/ui_brand.h').write_text('\n'.join(header), encoding='ascii')
    # Chromatic start-up menu: the same official artwork at its 160x144 sizes.
    launcher = header[:5] + ['#ifndef PJ_LAUNCHER_ART_H', '#define PJ_LAUNCHER_ART_H', '#include <stdint.h>',
                             c_image('jf12', 'icon-transparent.png', 12, 12),     # tab bar
                             c_image('jf14', 'icon-transparent.png', 14, 14),     # menu row
                             c_image('jfbanner', 'banner-light.png', 112, 33),    # Jellyfin screen
                             '#endif', '']
    (ROOT / 'chromatic/launcher/shared/launcher_art.h').write_text('\n'.join(launcher), encoding='ascii')
    print('Built official Jellyfin logo assets: in-app header, 36x36 core icon, 521x165 platform image.')


if __name__ == '__main__':
    build()

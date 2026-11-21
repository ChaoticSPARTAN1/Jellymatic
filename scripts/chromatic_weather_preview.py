# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Every Local Weather page a place would show on the console, drawn on this PC by the console's own drawing code
(chromatic/weather, built into a small checker: check_weather_place.c): for Chromatic Manager's "Make the area pages"
preview and for the docs' sample pictures. No network: the pages show the place's name, maps and radar view with
dashes for the figures (the console fills them in from the US weather service).

The renderer is wx_preview.exe beside the Manager app, or, when gcc is on this PC, built once into the user's temp
folder. Nothing private is written: the record and pack go to a temporary folder that is deleted afterwards.
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WX = ROOT / 'chromatic' / 'weather'
W, H = 160, 144
GCC = shutil.which('gcc') or r'C:\msys64\ucrt64\bin\gcc.exe'
EPOCH = 1795131996


def sources_digest():
    """sha256 of the C sources the renderer is built from (check_weather_place.c and every wx*.h), by name and bytes."""
    import hashlib
    digest = hashlib.sha256()
    for path in sorted([WX / 'check_weather_place.c'] + list(WX.glob('wx*.h'))):
        digest.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


def _sha256(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verified(exe):
    """`exe` when its record (the .json beside it, written by build()) names its own sha256 and the CURRENT sources'
    digest; else None (a renderer swapped, damaged or built from older sources is never run). Review L5 of 1.6.5."""
    import json
    exe = Path(exe)
    try:
        record = json.loads(exe.with_suffix('.json').read_text(encoding='utf-8'))
        ok = exe.is_file() and record.get('sha256') == _sha256(exe) and record.get('sources') == sources_digest()
    except (OSError, ValueError, AttributeError):
        ok = False
    return exe if ok else None


def renderer():
    """The page renderer's path, or None (the preview is then not shown). The window app uses only the renderer bundled
    beside it, and only when verified() (its sha256 and the sources it was built from, recorded at build time); never
    one found elsewhere. Run from the scripts (no app), a renderer for the current sources is built into the temp
    folder, named by their digest, and verified the same way."""
    import sys
    if getattr(sys, 'frozen', False):
        return verified(Path(sys.executable).parent / 'wx_preview.exe')
    built = Path(tempfile.gettempdir()) / 'chromatic-manager' / f'wx_preview-{sources_digest()[:16]}.exe'
    return verified(built) or (build(built) and verified(built))


def build(target):
    """Compile the renderer (the public sources only: Orlando, FL built in) to `target` and write its record beside it
    (`target` with .json: its sha256 and the sources' digest); its path, or None without gcc."""
    import json
    if not Path(GCC).exists():
        return None
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    sources = sources_digest()
    env = dict(os.environ, PATH=str(Path(GCC).parent) + os.pathsep + os.environ.get('PATH', ''))
    done = subprocess.run([GCC, '-std=c99', '-O1', '-static', f'-I{WX}', str(WX / 'check_weather_place.c'), '-o', str(target),
                           '-lm'], capture_output=True, env=env, timeout=300)
    if done.returncode != 0 or not target.exists():
        return None
    record = json.dumps({'sha256': _sha256(target), 'sources': sources, 'about': 'Chromatic Manager page renderer '
                         '(chromatic/weather/check_weather_place.c); used only while both values match'}, indent=1)
    target.with_suffix('.json').write_text(record, encoding='utf-8')
    return target if target.with_suffix('.json').read_text(encoding='utf-8') == record else None


def pages(place, pack=None, exe=None, radar_gif=None):
    """[(page name, 160 x 144 RGB565 little-endian bytes)] of every page `place` (a chromatic_weather_location.Place,
    or None for the default) shows with `pack` (a chromatic_location_pack.Pack made for it, or None)."""
    exe = exe or renderer()
    if exe is None:
        raise RuntimeError('The page preview needs its renderer (wx_preview.exe beside Chromatic Manager, made from the '
                           'same weather code), which is missing or does not match.')
    with tempfile.TemporaryDirectory() as work:
        work = Path(work)
        record = work / 'place.rec'
        args = ['-']
        if place is not None:
            record.write_bytes(place.pack(1))
            args = [str(record)]
        if pack is not None:
            pack_file = work / 'place.pjlp'
            pack_file.write_bytes(pack.pack(1))
            args.append('--pack=' + str(pack_file))
        replies, frames = work / 'replies', work / 'frames'
        replies.mkdir()
        frames.mkdir()
        radar_gif = radar_gif or WX / 'fixtures' / 'radar-empty.gif'   # an empty picture: the radar page shows its map
        if Path(radar_gif).exists():
            shutil.copyfile(radar_gif, replies / 'radar.gif')
        cmd = [str(exe), 'pages', args[0], str(replies), str(frames), str(EPOCH)] + args[1:]
        subprocess.run(cmd, capture_output=True, timeout=120)
        return [(f.stem[6:], f.read_bytes()) for f in sorted(frames.glob('*.rgb565'))]


def png(rgb565, scale=2):
    """A PNG of one page (for files; the window shows the bytes directly)."""
    import struct
    import zlib
    rows = []
    for y in range(H):
        line = bytearray()
        for x in range(W):
            v = rgb565[2 * (y * W + x)] | rgb565[2 * (y * W + x) + 1] << 8
            r, g, b = (v >> 11) << 3, ((v >> 5) & 63) << 2, (v & 31) << 3
            line += bytes((r, g, b)) * scale
        rows += [b'\0' + bytes(line)] * scale
    data = zlib.compress(b''.join(rows), 9)
    chunk = lambda tag, body: struct.pack('>I', len(body)) + tag + body + struct.pack('>I', zlib.crc32(tag + body) & 0xFFFFFFFF)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', W * scale, H * scale, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', data) + chunk(b'IEND', b''))

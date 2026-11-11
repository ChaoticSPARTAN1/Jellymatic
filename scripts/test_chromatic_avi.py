# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Edge cases for the Chromatic AVI demuxer (chromatic/jellyfin/jf_avi.h), fed in every split.

- An odd-length chunk's pad byte is skipped and the next picture is still found.
- A chunk claiming 0xFFFFFFFF bytes with a pad byte (sync lost) never spins: before the fix
  body+pad wrapped to 0 and the loop never advanced, which would trip the console watchdog.
PC only, no hardware.
"""
import os
import struct
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GCC = r'C:\msys64\ucrt64\bin\gcc.exe'


def chunk(fourcc, body, length=None):
    length = len(body) if length is None else length
    return fourcc + struct.pack('<I', length) + body + (b'\0' if len(body) % 2 else b'')


def avi(movi):
    lst = b'LIST' + struct.pack('<I', 4 + len(movi)) + b'movi' + movi
    return b'RIFF' + struct.pack('<I', 4 + len(lst)) + b'AVI ' + lst


def run(exe, data, seed):
    with tempfile.NamedTemporaryFile(delete=False, suffix='.avi') as f:
        f.write(data)
    try:
        out = subprocess.run([str(exe), f.name, str(seed)], capture_output=True, text=True, timeout=10)
        return out.returncode, out.stdout.split('\n')
    finally:
        os.unlink(f.name)


def main():
    with tempfile.TemporaryDirectory() as work:
        exe = Path(work) / 'check_avi.exe'
        subprocess.run([GCC, '-O1', '-Wall', '-Wextra', '-Werror', '-std=c11', '-o', str(exe),
                        'chromatic/jellyfin/check_avi.c'], cwd=ROOT, check=True)
        jpeg = bytes((0xff, 0xd8)) + bytes(range(1, 40)) + bytes((0xff, 0xd9))  # odd length: padded
        padded = avi(chunk(b'JUNK', b'abc') + chunk(b'00dc', jpeg) + chunk(b'01wb', b'x' * 7) + chunk(b'00dc', jpeg))
        wrap = avi(chunk(b'00dc', jpeg) + b'01wb' + struct.pack('<I', 0xFFFFFFFF) + b'y' * 300)
        for seed in [0, 1, 2, 3, 6, 9, 12, 101, 4097]:
            code, lines = run(exe, padded, seed)
            frames = [l for l in lines if l.startswith('F ')]
            assert code == 0 and len(frames) == 2 and all(l.split()[2] == str(len(jpeg)) for l in frames), (seed, lines)
            code, lines = run(exe, wrap, seed)  # a hang raises TimeoutExpired
            assert code == 0 and sum(l.startswith('F ') for l in lines) == 1 and lines[-2].startswith('H '), (seed, lines)
    print('PASS: AVI demuxer: pad bytes skipped in every split (2 pictures found); a 0xFFFFFFFF chunk with a pad '
          'byte is consumed without spinning (no watchdog hang). PC only.')


if __name__ == '__main__':
    os.environ['PATH'] = r'C:\msys64\ucrt64\bin;' + os.environ['PATH']
    main()

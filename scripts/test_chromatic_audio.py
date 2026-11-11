# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""The Chromatic player's sound decoder on the PC (chromatic/jellyfin/jf_audio.h + jf_avi.h).

Decodes the IMA ADPCM track of an MJPEG/AVI stream through the player's demuxer and
decoder, in random network splits and with a bounded sink, and compares the PCM
byte-for-byte with FFmpeg's own decode of the same file. Uses the real server sample
private/chromatic-samples/sample-adpcm.avi when present (scripts/capture_chromatic_sample.py
--adpcm; nothing from it is printed but counts), else a synthetic FFmpeg stream. No hardware.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from user_paths import PRIVATE  # noqa: E402  (per-user folder, see user_paths.py)

ROOT = Path(__file__).resolve().parents[1]
GCC = shutil.which('gcc') or r'C:\msys64\ucrt64\bin\gcc.exe'


def main():
    with tempfile.TemporaryDirectory() as work:
        work = Path(work)
        exe = work / 'check_audio.exe'
        subprocess.run([GCC, '-O1', '-Wall', '-Wextra', '-Werror', '-std=c11', '-o', str(exe),
                        'chromatic/jellyfin/check_audio.c'], cwd=ROOT, check=True)
        sample = PRIVATE / 'chromatic-samples' / 'sample-adpcm.avi'
        real = sample.exists()
        if not real:
            sample = work / 'synthetic.avi'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=s=160x120:r=15',
                            '-f', 'lavfi', '-i', 'sine=f=440:r=48000', '-t', '20', '-c:v', 'mjpeg', '-q:v', '5',
                            '-pix_fmt', 'yuvj420p', '-c:a', 'adpcm_ima_wav', '-ar', '16000', '-ac', '1',
                            str(sample)], check=True)
        ref = work / 'ref.raw'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(sample), '-vn', '-f', 's16le', '-c:a', 'pcm_s16le',
                        str(ref)], check=True)
        ref_bytes = ref.read_bytes()
        assert len(ref_bytes) > 16000 * 2 * 5, 'reference decode too short'
        facts = None
        for seed, env in [(0, {}), (1, {}), (2, {'HOLD': '1'}), (3, {'SINK': '7'}), (6, {'HOLD': '1', 'SINK': '1'}),
                          (101, {'HOLD': '1'}), (4097, {})]:
            out = work / f'pcm-{seed}.raw'
            result = subprocess.run([str(exe), str(sample), str(seed), str(out)], capture_output=True, text=True,
                                    timeout=120, env={**os.environ, **env})
            assert result.returncode == 0 and result.stdout.startswith('A '), (seed, result.stdout, result.stderr)
            got = out.read_bytes()
            assert got == ref_bytes, (f'seed {seed} {env}: PCM differs from FFmpeg', len(got), len(ref_bytes),
                                      next((i for i in range(min(len(got), len(ref_bytes))) if got[i] != ref_bytes[i]), None))
            line = dict(part.split('=') for part in result.stdout.split()[1:])
            facts = facts or line
            assert line == facts or env, (line, facts)
            assert line['format'] == '17' and line['channels'] == '1' and line['rate'] == '16000', line
            assert int(line['blocks']) == int(line['chunks']) > 0, line
            if 'HOLD' in env:
                assert int(line['held_stops']) >= int(line['frames']) - 1 > 0, ('sound ran ahead of held pictures', line)
        samples = int(facts['samples'])
        print(f'PASS: IMA ADPCM through jf_avi.h + jf_audio.h is byte-identical to FFmpeg: {samples} samples '
              f'({samples / 16000:.1f} s) in {facts["blocks"]} blocks of {facts["block"]} bytes, 7 network splits '
              f'including a 1-byte sink and held pictures; {"real server sample" if real else "synthetic stream"}.')


if __name__ == '__main__':
    os.environ['PATH'] = r'C:\msys64\ucrt64\bin;' + os.environ['PATH']
    main()

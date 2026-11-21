# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Writes chromatic/weather/wx_sound_data.c and wx_sound_data.h: the Local Weather channel's
sound (the short music loop, five announcer lines, the alert tone) as IMA ADPCM, mono, in 256-byte
blocks of the format chromatic/jellyfin/jf_audio.h decodes: the music and the tone at 8,000 Hz, the
announcer lines at 16,000 Hz (measured below: a line stored at 8,000 Hz comes out 8 to 11 dB worse).

Source: the seven .wav clips (22,050 Hz) in AUDIO below, made on your own PC: the music loop and the alert tone by the
project's sound script, the five announcer lines with Piper and its public-domain en_US-norman-medium voice (see
VOICE_LICENCE). They are not in this repository. The console plays through its proven 16,000 Hz sink: wx_sound.h decodes
these blocks, doubles the rate of the 8,000 Hz clips with a 4-point interpolator, mixes, and encodes
16,000 Hz blocks again.
This script also measures that whole path against the source and prints the figures.

    py -3 chromatic\\weather\\tools\\make_sound.py   (with the clips in staging/weather-sound/)

Needs numpy. No network.
"""
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
AUDIO = ROOT / 'staging' / 'weather-sound'   # your own clips (Git-ignored folder)
OUT = ROOT / 'chromatic' / 'weather'
BLOCK = 256
# The origin and licence of the clips, written into wx_sound_data.c beside the data.
VOICE_LICENCE = [
    "Sound for the Local Weather channel. Nothing is taken from anyone else's recordings.",
    '',
    "- music-loop-short.wav, alert-tone.wav: written in code by the project's sound script (original).",
    '- voice-*.wav: generated with the Piper speech tool (https://github.com/OHF-Voice/piper1-gpl, GPL-3.0)',
    '  and its voice en_US-norman-medium, from https://huggingface.co/rhasspy/piper-voices . Its model card:',
    '  dataset recordings from LibriVox.org, licence public domain; "trained from scratch" (not built on',
    '  another voice).',
    '- The wording of the five lines is our own.',
    '- The retro treatment (band limit, light hiss and saturation) and the IMA ADPCM coding are done by',
    '  the sound script and chromatic/weather/tools/make_sound.py.',
]
PER = 1 + 2 * (BLOCK - 4)          # samples a block holds: 505
# levels of the approved preview mix: bed, voice, tone (RMS)
BED, VOICE, TONE, DUCK = 0.085, 0.17, 0.12, 0.30
CLIPS = [('MUSIC', 'music-loop-short.wav', BED, 8000), ('CURRENT', 'voice-current.wav', VOICE, 16000),
         ('LOCAL', 'voice-local.wav', VOICE, 16000), ('EXTENDED', 'voice-extended.wav', VOICE, 16000),
         ('REGIONAL', 'voice-regional.wav', VOICE, 16000), ('RADAR', 'voice-radar.wav', VOICE, 16000),
         ('ALERT', 'alert-tone.wav', TONE, 8000)]
STEPS = [7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107,
         118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876,
         963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871,
         5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385,
         24623, 27086, 29794, 32767]
CHANGES = [-1, -1, -1, -1, 2, 4, 6, 8]


def read(name):
    with wave.open(str(AUDIO / name), 'rb') as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
        return np.frombuffer(w.readframes(w.getnframes()), '<i2').astype(np.float64) / 32768, w.getframerate()


def resample(x, n):
    """Band-limited resampling to n samples through the spectrum (exact for a loop)."""
    spec = np.fft.rfft(x)
    keep = min(len(spec), n // 2 + 1)
    out = np.zeros(n // 2 + 1, complex)
    out[:keep] = spec[:keep]
    if n < len(x) and n % 2 == 0:
        out[-1] = out[-1].real
    return np.fft.irfft(out, n) * (n / len(x))


def rms(x):
    return float(np.sqrt(np.mean(np.square(x))))


def step(pred, index, nibble):
    s = STEPS[index]
    diff = s >> 3
    if nibble & 1:
        diff += s >> 2
    if nibble & 2:
        diff += s >> 1
    if nibble & 4:
        diff += s
    pred = max(-32768, min(32767, pred - diff if nibble & 8 else pred + diff))
    return pred, max(0, min(88, index + CHANGES[nibble & 7]))


def choose(pred, index, sample):
    s = STEPS[index]
    delta = sample - pred
    nibble = 0
    if delta < 0:
        nibble, delta = 8, -delta
    if delta >= s:
        nibble |= 4
        delta -= s
    if delta >= s >> 1:
        nibble |= 2
        delta -= s >> 1
    if delta >= s >> 2:
        nibble |= 1
    return nibble


def encode(pcm, per=PER):
    """int16 list (a whole number of blocks) -> bytes; the step index runs on from block to block."""
    assert len(pcm) % per == 0
    out, index = bytearray(), 0
    for at in range(0, len(pcm), per):
        pred = pcm[at]
        out += bytes((pred & 255, (pred >> 8) & 255, index, 0))
        for k in range(at + 1, at + per, 2):
            lo = choose(pred, index, pcm[k])
            pred, index = step(pred, index, lo)
            hi = choose(pred, index, pcm[k + 1])
            pred, index = step(pred, index, hi)
            out.append(lo | (hi << 4))
    return bytes(out)


def encode_search(pcm, per=PER):
    """The same IMA ADPCM format, but each nibble is the one with the smallest error over this sample and
    the next (the standard choice for that next one). Any IMA decoder plays it; 1.5 to 4 dB cleaner on the
    announcer lines than the standard greedy choice (measured 2026-11-20)."""
    assert len(pcm) % per == 0
    out, index = bytearray(), 0
    for at in range(0, len(pcm), per):
        pred = pcm[at]
        out += bytes((pred & 255, (pred >> 8) & 255, index, 0))
        nibbles = []
        for k in range(at + 1, at + per):
            best = None
            for n in range(16):
                p1, i1 = step(pred, index, n)
                err = abs(pcm[k] - p1)
                if k + 1 < at + per:
                    p2, _ = step(p1, i1, choose(p1, i1, pcm[k + 1]))
                    err += abs(pcm[k + 1] - p2)
                if best is None or err < best[0]:
                    best = (err, n, p1, i1)
            _, n, pred, index = best
            nibbles.append(n)
        for j in range(0, len(nibbles), 2):
            out.append(nibbles[j] | (nibbles[j + 1] << 4))
    return bytes(out)


def decode(data, block=BLOCK):
    out = []
    for at in range(0, len(data), block):
        pred = data[at] | (data[at + 1] << 8)
        pred = pred - 65536 if pred >= 32768 else pred
        index = min(data[at + 2], 88)
        out.append(pred)
        for b in data[at + 4:at + block]:
            pred, index = step(pred, index, b & 15)
            out.append(pred)
            pred, index = step(pred, index, b >> 4)
            out.append(pred)
    return out


def console_path(stored, loop, half):
    """What the console's sink decodes: stored blocks -> 16-bit -> 2x (4-point) -> 16 kHz IMA -> 16-bit."""
    x = decode(stored)
    n = len(x)
    if not half:
        return decode(encode(x))

    def at(i):
        return x[i % n] if loop else (x[i] if 0 <= i < n else 0)
    up = []
    for i in range(n):
        up.append(x[i])
        v = (9 * (x[i] + at(i + 1)) - at(i - 1) - at(i + 2) + 8) >> 4
        up.append(max(-32768, min(32767, v)))
    whole = len(up) // PER * PER
    return decode(encode(up[:whole]))


def snr(ref, got):
    n = min(len(ref), len(got))
    ref, got = np.asarray(ref[:n], float), np.asarray(got[:n], float)
    noise = rms(ref - got)
    return 99.0 if noise == 0 else 20 * np.log10(rms(ref) / noise)


def main():
    loaded = []
    for tag, name, level, RATE in CLIPS:
        x, rate = read(name)
        blocks = round(len(x) * RATE / rate / PER) if tag == 'MUSIC' else -(-int(len(x) * RATE / rate) // PER)
        if tag == 'MUSIC':   # a loop: stretched by under 0.1 % to a whole number of blocks, so the join is exact
            y = resample(x, blocks * PER)
        else:                # a line: resampled at the true ratio, silence to the end of its last block
            pad = int(round(blocks * PER * rate / RATE))
            y = resample(np.concatenate([x, np.zeros(pad - len(x))]), blocks * PER)
        loaded.append((tag, name, y * (level / rms(x)), x * (level / rms(x)), rate, blocks, RATE))
    # one gain for everything: the loudest moment (the bed alone, or a ducked bed under a line) at 90 %
    bed_peak = max(abs(loaded[0][2]).max(), 1e-9)
    worst = max([bed_peak] + [DUCK * bed_peak + abs(c[2]).max() for c in loaded[1:]])
    gain = 0.90 / worst
    rows, report, total = [], [], 0
    for tag, name, y, x, rate, blocks, RATE in loaded:
        pcm = [int(v) for v in np.clip(np.round(y * gain * 32767), -32768, 32767)]
        data = encode_search(pcm)
        assert len(data) == blocks * BLOCK
        played = console_path(data, tag == 'MUSIC', RATE == 8000)
        n16 = len(pcm) * 16000 // RATE
        ideal_band = resample(y * gain * 32767, n16)                    # the 8 kHz clip, ideally doubled
        want = len(x) if tag == 'MUSIC' else int(round(len(pcm) * rate / RATE))
        src = np.concatenate([x, np.zeros(max(0, want - len(x)))])[:want]
        ideal_full = resample(src * gain * 32767, n16)                  # the 22 kHz source at 16 kHz
        direct = decode(encode([int(v) for v in np.clip(np.round(ideal_full), -32768, 32767)][:n16 // PER * PER]))
        a, b, c = snr(ideal_band, played), snr(ideal_full, played), snr(ideal_full, direct)
        lost = 20 * np.log10(rms(ideal_full) / max(rms(ideal_full - ideal_band), 1e-9))
        rows.append((tag, name, data, len(pcm), RATE))
        total += len(data)
        report.append(f'{tag:<9}{name:<22}{RATE:6d} Hz {len(pcm) / RATE:6.2f} s {len(data):7d} B   path {a:5.1f} dB   '
                      f'vs 16 kHz source {b:5.1f} dB   (one 16 kHz encode alone: {c:5.1f} dB; '
                      f'what an 8 kHz clip cannot hold is {lost:4.1f} dB down)')
    licence = VOICE_LICENCE
    h = ['/* SPDX-License-Identifier: GPL-3.0-or-later', ' * Additional terms: ADDITIONAL-TERMS.md',
         ' * Local Weather channel sound: clip table. WRITTEN BY chromatic/weather/tools/make_sound.py. */',
         '#ifndef WX_SOUND_DATA_H', '#define WX_SOUND_DATA_H', '#include <stdint.h>',
         '#define WX_SND_OUT_RATE 16000u /* what wx_sound.h plays; a clip is stored at this rate or at half of it */',
         f'#define WX_SND_BLOCK {BLOCK}u     /* bytes a block: 4 header bytes, then 2 samples a byte */',
         f'#define WX_SND_PER {PER}u       /* samples a block */',
         f'#define WX_SND_DUCK {int(round(DUCK * 256))}u       /* the bed under a line, of 256 */',
         'enum {' + ','.join(f'WX_CLIP_{r[0]}' for r in rows) + ',WX_CLIPS};',
         'typedef struct {const uint8_t *data;uint32_t bytes,samples;uint8_t half;} wx_clip; /* half: stored at 8,000 Hz */',
         'extern const wx_clip wx_clips[WX_CLIPS];',
         f'#define WX_SND_BYTES {total}u', '#endif']
    (OUT / 'wx_sound_data.h').write_text('\n'.join(h) + '\n', 'utf-8', newline='\n')
    c = ['/* SPDX-License-Identifier: GPL-3.0-or-later', ' * Additional terms: ADDITIONAL-TERMS.md',
         ' * Local Weather channel sound, IMA ADPCM mono in 256-byte blocks (jf_audio.h format), 8,000 or 16,000 Hz.',
         ' * WRITTEN BY chromatic/weather/tools/make_sound.py from the project\'s .wav clips. Do not edit.',
         ' *',
         ' * Origin and licence of the sound, kept with the data:',
         ' *']
    c += [' *   ' + ln for ln in licence]
    c += [' *', ' * Clips (seconds, bytes; signal-to-noise of what the console plays, measured by the script):']
    c += [' *   ' + r for r in report]
    c += [f' *   total {total} bytes', ' */', '#include "wx_sound_data.h"']
    for tag, name, data, n, RATE in rows:
        c.append(f'static const uint8_t wx_snd_{tag.lower()}[{len(data)}]={{')
        for i in range(0, len(data), 32):
            c.append(','.join(str(b) for b in data[i:i + 32]) + ',')
        c.append('};')
    c.append('const wx_clip wx_clips[WX_CLIPS]={')
    for tag, name, data, n, RATE in rows:
        c.append(f'    {{wx_snd_{tag.lower()},{len(data)}u,{n}u,{int(RATE == 8000)}}},')
    c.append('};')
    (OUT / 'wx_sound_data.c').write_text('\n'.join(c) + '\n', 'utf-8', newline='\n')
    print(f'gain {gain:.3f}; total {total} bytes')
    print('\n'.join(report))


if __name__ == '__main__':
    main()

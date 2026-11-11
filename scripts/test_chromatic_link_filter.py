# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""The link input filter of the Chromatic bridge (rtl/chromatic_pocket_link.v, GLITCH_SAMPLES), PC only.

Icarus runs rtl/chromatic_pocket_link_glitch_tb.sv: the bridge against the Pocket's real link block
(pocket_media_link at 90 MHz) with glitches put on LINK_CLK and LINK_IN at the Chromatic. One run per
line below; a run is intact only when both directions carried exactly the bytes sent.

  required  the filter (3 samples, the built image) must keep the stream intact
  control   the same glitch on a bridge without the filter must damage it (so the glitch is a real test)
  limit     a documented limit of the filter: the run must be damaged (if it is not, the text is wrong)
  report    printed, not judged: glitches of 238 to 358 ns pass the filter or not by phase

Also checks that the three shared engine files are byte-identical to the ones the approved trial
image was built from, and (when it exists) that the staged stream image carries this bridge source.
No hardware. Tools: Icarus under private/fpga-tools (executed only).
"""
import hashlib
from user_paths import TOOLS  # noqa: E402  (per-user folder, see user_paths.py)
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from connection_config import ROOT

BASE = TOOLS / 'fpga-tools/native/ucrt64'
OUT = ROOT / 'staging/chromatic-link-filter'
OUT.mkdir(parents=True, exist_ok=True)  # build output, ignored by Git
RTL = ROOT / 'rtl'
STOCK_RX = ROOT / 'upstream/chromatic-fpga/esp32t/src/rtl/BSP/uart_packet_wrapper_rx.sv'
LINK = ROOT / 'staging/chromatic-fpga-media-link'
TRIAL = LINK / 'esp32t-18.41-trial-20261109/src/rtl/BSP/pocket_link'
STREAM = LINK / 'esp32t-18.41-stream-20261109/src/rtl/BSP/pocket_link'
ENGINE = ('byte_fifo.v', 'gb_link_byte.v', 'gb_link_stream.v')
SOURCES = ('byte_fifo.v', 'gb_link_byte.v', 'gb_link_stream.v', 'link_mode_guard.v', 'pocket_media_link.v',
           'chromatic_pocket_link.v', 'chromatic_pocket_link_glitch_tb.sv')
TOP = 'chromatic_pocket_link_glitch_tb'

# gmode bits of the bench
FALL_BOUNCE, RISE_BOUNCE, LOW_PULSE, HIGH_PULSE, DATA_AT_RISE, DATA_MID = 1, 2, 4, 8, 16, 32
NAMES = {0: 'clean', FALL_BOUNCE: 'bounce after each clock fall', RISE_BOUNCE: 'bounce after each clock rise',
         LOW_PULSE: 'pulse in each low clock phase', HIGH_PULSE: 'pulse in each high clock phase',
         DATA_AT_RISE: 'data glitch at each clock rise', DATA_MID: 'data glitch mid bit',
         LOW_PULSE | HIGH_PULSE | DATA_AT_RISE | DATA_MID: 'clock pulses in both phases and both data glitches together'}


def env():
    e = dict(os.environ)
    e['PATH'] = os.pathsep.join([str(BASE / 'bin'), r'C:\msys64\ucrt64\bin', e['PATH']])
    return e


def run(args, check=True, timeout=900):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True, env=env(), timeout=timeout,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if check and result.returncode:
        print(result.stdout[-3000:]); print(result.stderr[-3000:])
        raise SystemExit(f'FAIL: {args[0]}')
    return result


def build(samples):
    vvp = OUT / f'{TOP}-g{samples}.vvp'
    run([BASE / 'bin/iverilog.exe', '-g2012', '-B', BASE / 'lib/ivl', '-s', TOP, f'-P{TOP}.GLITCH={samples}', '-o', vvp,
         *[RTL / s for s in SOURCES], STOCK_RX])
    return vvp


def cases():
    """(kind, filter samples, kHz, gmode, width ns, offset ns, gClk phase ns)"""
    rows = []
    for khz in (256, 512):                       # clean streams at several gClk phases
        for phase in (0, 30, 60, 90):
            rows.append(('required', 3, khz, 0, 0, 60, phase))
    for khz in (256, 512, 1000):                 # the earlier behaviour is still there with the filter off
        rows.append(('required', 0, khz, 0, 0, 60, 0))
    rows.append(('limit', 3, 1000, 0, 0, 60, 0))  # 1 MHz needs the filter off
    combined = LOW_PULSE | HIGH_PULSE | DATA_AT_RISE | DATA_MID
    for khz in (256, 512):
        for width in (150, 200, 230):            # every pulse under 238.4 ns, at any phase
            for mode in (LOW_PULSE, HIGH_PULSE, DATA_AT_RISE, DATA_MID, combined):
                rows.append(('required', 3, khz, mode, width, 60, 0))
            rows.append(('required', 3, khz, RISE_BOUNCE, width, 60, 0))
        for width in (250, 300):                 # a bounce on the rising edge only delays the edge
            rows.append(('required', 3, khz, RISE_BOUNCE, width, 60, 0))
    for width in (150, 200, 230, 250, 300):      # a bounce on the falling edge delays the answer bit:
        rows.append(('required', 3, 256, FALL_BOUNCE, width, 60, 0))   # room for it at 256 kHz,
        rows.append(('report', 3, 512, FALL_BOUNCE, width, 60, 0))     # not always at 512 kHz
    for mode in (LOW_PULSE, HIGH_PULSE, DATA_AT_RISE, RISE_BOUNCE, FALL_BOUNCE):  # the same glitches without the filter
        rows.append(('control', 0, 256, mode, 200, 60, 0))
    for khz in (256, 512):                       # the phase-dependent window, and 4 samples as an option
        for width in (250, 300, 340):
            for mode in (LOW_PULSE, HIGH_PULSE, DATA_AT_RISE):
                rows.append(('report', 3, khz, mode, width, 60, 0))
    for khz in (256, 512):
        rows.append(('report', 4, khz, 0, 0, 60, 0))
        rows.append(('report', 4, khz, combined, 300, 60, 0))
    return rows


def one(job):
    vvps, (kind, samples, khz, mode, width, offset, phase) = job
    expect = {'required': 1, 'control': 0, 'limit': 0, 'report': 2}[kind]
    result = run([BASE / 'bin/vvp.exe', '-M', BASE / 'lib/ivl', vvps[samples], f'+khz={khz}', f'+gmode={mode}',
                  f'+gw_ps={width * 1000}', f'+goff_ps={offset * 1000}', f'+phase_ps={phase * 1000}', f'+expect={expect}'],
                 check=False)
    lines = result.stdout.splitlines()
    res = next((l for l in lines if l.startswith('RESULT ')), '')
    fields = dict(f.split('=', 1) for f in res.split()[1:])
    passed = result.returncode == 0 and any(l.startswith('PASS:') for l in lines)
    return job[1], passed, fields, [l for l in lines if l.startswith('INFO:')], (result.stdout + result.stderr)[-1500:]


def describe(row, fields):
    kind, samples, khz, mode, width, offset, phase = row
    what = NAMES[mode] + (f' {width} ns' if mode else '') + (f', gClk phase {phase} ns' if phase else '')
    state = 'intact' if fields.get('intact') == '1' else 'DAMAGED'
    detail = (f"to the Pocket {fields.get('down')} wrong {fields.get('down_wrong')}, from it {fields.get('up')} wrong "
              f"{fields.get('up_wrong')}, faults {fields.get('faults')}, {fields.get('glitches')} glitches"
              + (', timed out' if fields.get('timeout') == '1' else '')) if fields else 'no result line'
    return f'{kind:8} filter {samples} at {khz:4} kHz, {what}: {state} ({detail})'


def sources():
    """The engine shared with the Pocket core is untouched; the staged stream image carries this bridge."""
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    if TRIAL.is_dir():
        for name in ENGINE:
            if digest(RTL / name) != digest(TRIAL / name):
                raise SystemExit(f'FAIL: rtl/{name} differs from the approved trial image source')
        print('PASS: the shared link engine (byte_fifo, gb_link_byte, gb_link_stream) is byte-identical to the trial image source')
    else:
        print('INFO: the trial image folder is not here; engine files not compared')
    if STREAM.is_dir():
        for name in ENGINE + ('link_mode_guard.v', 'chromatic_pocket_link.v'):
            if digest(RTL / name) != digest(STREAM / name):
                raise SystemExit(f'FAIL: the staged stream image was not built from rtl/{name} as it stands')
        print('PASS: the staged 18.41 stream image carries rtl/chromatic_pocket_link.v and the engine as tested here')
    else:
        print('INFO: no staged stream image yet; its sources were not compared')


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    sources()
    vvps = {samples: build(samples) for samples in (0, 3, 4)}
    rows = cases()
    with ThreadPoolExecutor(max(2, min(12, (os.cpu_count() or 4) // 2))) as pool:
        results = list(pool.map(one, [(vvps, row) for row in rows]))
    failed, counts, info = 0, {}, []
    for row, passed, fields, infos, tail in results:
        counts[row[0]] = counts.get(row[0], 0) + 1
        print(('ok   ' if passed else 'FAIL ') + describe(row, fields))
        if row[3] == 0 and row[6] == 0 and row[0] == 'required':
            info += [f'filter {row[1]}: {l[6:]}' for l in infos]
        if not passed:
            failed += 1
            print(tail)
    for line in info:
        print('INFO: ' + line)
    if failed:
        raise SystemExit(f'FAIL: {failed} of {len(rows)} filter runs')
    print(f"PASS: link input filter: {counts.get('required', 0)} required runs intact (clean 256 and 512 kHz at four gClk phases; "
          f"150, 200 and 230 ns pulses on the clock in either phase, on the data at the sampling edge and mid bit, and all together; "
          f"rising-edge bounces to 300 ns; falling-edge bounces to 300 ns at 256 kHz; filter off still good at 256, 512 and 1000 kHz), "
          f"{counts.get('control', 0)} controls damaged without the filter, {counts.get('limit', 0)} documented limit confirmed "
          f"(1 MHz with the filter on), {counts.get('report', 0)} reported")


if __name__ == '__main__':
    sys.exit(main())

# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Writes build/build-receipt.json for a Gowin build of a staged Chromatic FPGA tree (the media
staging or the stock comparison staging): the .fs/.bin hashes, the STA summary (violated setup
and hold endpoints), the resource table, and every QSPI timing path the evt1_x2.sdc report_timing
commands put in the timing report (worst slack per report). Chromatic Manager pins the image it
writes or tries to this receipt (hash and 0/0 timing); the receipt is refused when the timing report
is not from this build. Reads only the tree's own impl/ and build/ outputs; no hardware.

  python scripts/record_chromatic_fpga_build.py staging/chromatic-fpga-media/esp32t [--stock staging/chromatic-fpga-stock/esp32t]
"""
import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = 'Gowin V1.9.12.04 (gw_sh build.tcl via build_evt1_x2.bat), builder\'s own Gowin licence'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return Path(path).read_text(encoding='utf-8', errors='replace')


def sta_summary(tr):
    out = {}
    for key, name in (('Numbers of Setup Violated Endpoints', 'setup_violated_endpoints'),
                      ('Numbers of Hold Violated Endpoints', 'hold_violated_endpoints'),
                      ('Numbers of Paths Analyzed', 'paths_analyzed'), ('Numbers of Endpoints Analyzed', 'endpoints_analyzed')):
        match = re.search(rf'<{key}>:(\d+)', tr)
        out[name] = int(match.group(1)) if match else None
    created = re.search(r'<Created Time>: (.+)', tr)
    out['report_created'] = created.group(1).strip() if created else None
    return out


def fmax(tr):
    """{clock: (constraint MHz, actual MHz)} from the Max Frequency Summary."""
    out = {}
    block = tr.rsplit('2.3 Max Frequency Summary', 1)[-1].split('2.4 Total Negative Slack', 1)[0]
    for line in block.splitlines():
        match = re.match(r'\s*\d+\s+(\S+)\s+([\d.]+)\(MHz\)\s+([\d.]+)\(MHz\)', line)
        if match:
            out[match.group(1)] = {'constraint_mhz': float(match.group(2)), 'actual_mhz': float(match.group(3))}
    return out


def report_blocks(tr):
    """Every 'Report Command:report_timing ...' block of section 3.3 with its path table rows:
    (number, slack, from, to, from_clock, to_clock)."""
    blocks = []
    parts = re.split(r'\n(?:<)?Report Command(?:>)?:', tr)
    for part in parts[1:]:
        command, _, body = part.partition('\n')
        if not command.startswith('report_timing'):
            continue
        rows = []
        for line in body.splitlines():
            if line.startswith(('3.', '4.', 'Report Command')):
                break
            # A clock column reads N/A for a path that starts or ends at a port (the cartridge outputs): those
            # rows count too, and a numbered slack row this cannot read stops the gate instead of passing unseen.
            match = re.match(r'\s+(\d+)\s+(-?[\d.]+)\s+(\S+)\s+(\S+)\s+(\S+:\[[RF]\]|N/A)\s+(\S+:\[[RF]\]|N/A)\s', line)
            if not match and re.match(r'\s+\d+\s+-?\d+\.\d+\s+\S+\s+\S+\s+\S+\s+\S+\s+-?\d+\.\d+\s', line):
                raise ValueError(f'timing report row not understood ({command.strip()[:60]}): {line.strip()[:120]}')
            if match:
                rows.append({'n': int(match.group(1)), 'slack_ns': float(match.group(2)), 'from': match.group(3),
                             'to': match.group(4), 'launch': match.group(5), 'latch': match.group(6)})
        blocks.append({'command': command.strip(), 'paths': rows})
    return blocks


def qspi_paths(tr):
    """The SDC's QSPI report_timing commands (ports -> sclk registers, sclk -> sclk), summarised."""
    out = []
    merged = {}  # the same command appears as a slack table (rows) and as a detailed section (no rows): one entry
    for block in report_blocks(tr):
        command = block['command']
        if 'QSPI' not in command and 'sclk' not in command:
            continue
        if command not in merged or len(block['paths']) > len(merged[command]):
            merged[command] = block['paths']
    for command, paths in merged.items():
        out.append({'command': command, 'paths_reported': len(paths),
                    'worst_slack_ns': min((p['slack_ns'] for p in paths), default=None),
                    'worst_path': min(paths, key=lambda p: p['slack_ns']) if paths else None,
                    'all_positive': all(p['slack_ns'] >= 0 for p in paths) if paths else None})
    return out


def resources(rpt):
    out = {}
    for name in ('Logic', 'Register', 'CLS', 'BSRAM', 'DSP'):
        match = re.search(rf'^\s*{name}\s*\|\s*([\d.]+/\d+)\s*\|', rpt, re.M)
        if match:
            out[name] = match.group(1)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('tree', help='the staged esp32t folder that was built')
    parser.add_argument('--stock', help='the stock comparison tree (its resources are recorded as stock_reference)')
    parser.add_argument('--note', default='Never written to a console by this script. Owner-present session only: '
                                          'Try FPGA image (temporary, SRAM) first, docs/CHROMATIC-AUDIO-PLAN.md.')
    args = parser.parse_args()
    tree = Path(args.tree)
    tr = read(tree / 'impl/pnr/evt1_x2.tr')
    rpt = read(tree / 'impl/pnr/evt1_x2.rpt.txt')
    fs, bin_ = tree / 'build/evt1_x2.fs', tree / 'build/evt1_x2.bin'
    if not fs.is_file() or sha(fs) != sha(tree / 'impl/pnr/evt1_x2.fs'):
        raise SystemExit('build/evt1_x2.fs is missing or is not the impl/pnr output of this build (run build_evt1_x2.bat)')
    summary = sta_summary(tr)
    qspi = qspi_paths(tr)
    sources = {}
    for rel in ('src/rtl/BSP/chromatic_media.v', 'src/rtl/BSP/system_monitor.sv', 'src/top.v', 'src/rtl/EMU/emu_system_top.v',
                'src/rtl/EMU/cart.v', 'src/rtl/EMU/CORE/gb.v', 'src/board/evt1_x2/evt1_x2.sdc'):
        path = tree / rel
        if path.is_file():
            sources[rel] = sha(path)
    receipt = {
        'built': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(fs.stat().st_mtime)),
        'tool': TOOL,
        'source': f'{tree.relative_to(ROOT).as_posix() if tree.is_relative_to(ROOT) else tree}',
        'sources_sha256': sources,
        'outputs': {'build/evt1_x2.fs': {'sha256': sha(fs), 'size': fs.stat().st_size},
                    'build/evt1_x2.bin': {'sha256': sha(bin_), 'size': bin_.stat().st_size} if bin_.is_file() else None},
        'timing': {k: summary[k] for k in ('setup_violated_endpoints', 'hold_violated_endpoints')},
        'sta': summary,
        'fmax': fmax(tr),
        'qspi_timing': qspi,
        'qspi_paths_all_positive': all(b['all_positive'] for b in qspi if b['paths_reported']) and any(b['paths_reported'] for b in qspi),
        'resources': resources(rpt),
        'installed': False,
        'note': args.note,
    }
    stage = tree / 'stage-receipt.json'
    if stage.is_file():
        receipt['stage_receipt_sha256'] = sha(stage)
        staged = json.loads(stage.read_text(encoding='utf-8'))
        receipt['version_register'] = staged.get('version_register')
        receipt['game_hold'] = staged.get('game_hold')
    if args.stock:
        receipt['stock_reference'] = resources(read(Path(args.stock) / 'impl/pnr/evt1_x2.rpt.txt'))
    out = tree / 'build/build-receipt.json'
    out.write_text(json.dumps(receipt, indent=1), encoding='utf-8')
    brief = {k: receipt[k] for k in ('built', 'timing', 'resources', 'qspi_paths_all_positive')}
    brief['fs_sha256'] = receipt['outputs']['build/evt1_x2.fs']['sha256']
    brief['qspi'] = [{'command': b['command'].split(' -max_paths')[0], 'paths': b['paths_reported'], 'worst_slack_ns': b['worst_slack_ns']}
                     for b in qspi]
    brief['sclk_fmax'] = receipt['fmax'].get('sclk')
    print(json.dumps(brief, indent=1))
    return 0 if receipt['timing']['setup_violated_endpoints'] == 0 and receipt['timing']['hold_violated_endpoints'] == 0 else 1


if __name__ == '__main__':
    sys.exit(main())

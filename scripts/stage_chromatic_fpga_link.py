# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Stages (and optionally builds) the combined Chromatic FPGA image 18.41: stock + sound + Pocket link.

Copies the pinned read-only upstream (upstream/chromatic-fpga, tag v18.14, never edited) into
staging/chromatic-fpga-media-link/esp32t and applies, as exact text patches (every anchor must
match exactly once, or the script stops):

  1-13  every sound hook of scripts/stage_chromatic_fpga_media.py, imported from it unchanged,
        except the version register (6), whose minor is PJ_FPGA_LINK_MINOR (41) from
        chromatic/launcher/shared/launcher_logic.h instead of PJ_FPGA_MEDIA_MINOR (40), debug bit
        set, major 18 (debug bit 12, minor 11..6, major 5..0)
  14-32 the 19 pocket-link hooks of scripts/check_pocket_link_patch.py (imported, unchanged)
  33    evt1_x2.sdc: LINK_IN / LINK_CLK, the asynchronous link-port inputs, are false paths
        (see link_sdc_patch for the reasoning that is written into the SDC itself)
  34    impl/evt1_x2_process_config.json: the Gowin place-and-route process options of
        PROCESS_OPTIONS (see the note there; no constraint and no RTL is involved)
plus chromatic/fpga-media/chromatic_media.v and the five RTL files rtl/{chromatic_pocket_link,
byte_fifo, gb_link_byte, gb_link_stream, link_mode_guard}.v under src/rtl/BSP/pocket_link/.

The 18.40 sound staging (staging/chromatic-fpga-media) is never read or written here; this
script only imports the media script's functions. `--check` verifies that the staged tree is
what this script would produce. `--build` runs Gowin (gw_sh build.tcl, no window) in the staged
tree only, copies impl/pnr/evt1_x2.fs/.bin to build/, and writes build/build-receipt.json in the
schema chromatic_fpga.MediaImage pins, plus build/NEVER-INSTALL.txt beside the image (the
combined image is unreviewed: chromatic_fpga.LinkImage refuses it while that file is there).
`--export NAME` copies a built tree that passed the gate (0 setup / 0 hold violated endpoints and
every reported setup path at or over SLACK_FLOOR_NS) to staging/chromatic-fpga-media-link/NAME:
the pinned sources, both receipts, the .fs/.bin, the build log, Gowin's timing and resource
reports, and both NEVER-INSTALL.txt markers. It never writes an approval marker; a failing or
stale build is refused.
No hardware, no programmer, no download.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_pocket_link_patch as plink  # noqa: E402  (the 19 hooks; importing it has no side effects)
import record_chromatic_fpga_build as record  # noqa: E402  (STA/resource parsers of the media receipt)
import stage_chromatic_fpga_media as media  # noqa: E402  (pinned upstream, sound hooks, apply, digest)

ROOT = media.ROOT
OUT = ROOT / 'staging' / 'chromatic-fpga-media-link'
TREE = OUT / 'esp32t'
LINK_RTL = ROOT / 'rtl'
RTL_DIR = 'src/rtl/BSP/pocket_link'
GW_SH = Path(os.environ.get('GOWIN_SH') or r'C:\Gowin\Gowin_V1.9.12.04_x64\IDE\bin\gw_sh.exe')  # your Gowin install
TOOL = 'Gowin V1.9.12.04 (gw_sh build.tcl via scripts/stage_chromatic_fpga_link.py --build), builder\'s own Gowin licence'
LINK_SDC_MARK = '# Chromatic pocket link staging: asynchronous Game Boy link inputs'
VERSION_ANCHOR = "    reg [13:0] version = {\n"
NEVER_INSTALL = ('Combined Chromatic FPGA image 18.41 (stock 18.14 + sound hooks + Pocket link bridge),\n'
                 'staged/built by scripts/stage_chromatic_fpga_link.py. UNREVIEWED: never written to a console,\n'
                 'neither to its flash nor as a temporary SRAM trial. chromatic_fpga.LinkImage refuses to load\n'
                 'it while this file is here. Removing it is a review decision (Gowin 0/0 timing, the Icarus\n'
                 'benches on this tree, the owner-present try-in-memory plan of docs/CHROMATIC-POCKET-TETHER.md).\n')


def link_minor():
    """PJ_FPGA_LINK_MINOR from launcher_logic.h (41): the launcher's own test for the link image."""
    text = media.LAUNCHER_LOGIC.read_text(encoding='utf-8')
    major = int(re.search(r'#define PJ_FPGA_MEDIA_MAJOR (\d+)u', text).group(1))
    minor = int(re.search(r'#define PJ_FPGA_LINK_MINOR (\d+)u', text).group(1))
    assert major == 18 and 0 < minor < 64 and minor != media.media_minor(), (major, minor)
    return minor


def version_new(minor):
    return ("    reg [13:0] version = {\n        1'd0,  // 1 bit reserved\n"
            "        1'd1,  // 1 bit debug: set, this is not an official ModRetro release (Chromatic sound + Pocket link build)\n"
            f"        6'd{minor}, // 6 bits minor version ({minor}: stock 18.14 + chromatic_media.v sound hooks + pocket_link bridge; "
            "launcher_logic.h PJ_FPGA_LINK_MINOR)\n"
            "        6'd18  // 6 bits major version\n    };\n")


def sound_patches(minor=None):
    """The media script's 13 sound hooks, unchanged, but with the version register's minor set
    to the link minor. Exactly one entry (the version register) is replaced."""
    minor = link_minor() if minor is None else minor
    out, replaced = [], 0
    for rel, old, new in media.patches():
        if old.startswith(VERSION_ANCHOR):
            assert rel == 'src/rtl/BSP/system_monitor.sv', rel
            new, replaced = version_new(minor), replaced + 1
        out.append((rel, old, new))
    assert replaced == 1, replaced
    return out


# Evidence only (stream image): the worst hold paths of the whole design, so the receipt can name the
# worst hold slack and not just the QSPI ones. A report line constrains nothing: the same netlist placed and routed
# to the same slacks with and without it (run n-p0 against the build staged
# before it: same worst setup, recovery, removal and resource use).
HOLD_REPORT = "report_timing -hold -max_paths 20 -max_common_paths 1\n"


def link_sdc_patch():
    """Anchored on the last line of the media SDC patch (11)."""
    last = "report_timing -hold -from_clock [get_clocks {sclk}] -to_clock [get_clocks {sclk}] -max_paths 24 -max_common_paths 24\n"
    return (media.SDC, last, last +
            f"{LINK_SDC_MARK} (scripts/stage_chromatic_fpga_link.py, 2026-11-09)\n"
            "# LINK_IN (serial data in) and LINK_CLK (the bidirectional serial clock, an input whenever the far\n"
            "# end clocks) come from the other end of the link cable: another Game Boy or the Analogue Pocket,\n"
            "# each on its own oscillator, with no phase or frequency relationship to any clock here, and no\n"
            "# clock travels with the data into this design that an input delay could describe. Both captures\n"
            "# are asynchronous samples followed by synchronisers, not synchronous captures:\n"
            "#  - the stock game path registers both once on hclk (emu_system_top.v LINK_IN_r1/LINK_CLK_r1);\n"
            "#    link.v then shifts LINK_CLK_r1 through two more flops (serial_clk_in_last) and samples the\n"
            "#    data line only on the edge it detects there, microseconds after LINK_IN settled (the game\n"
            "#    link runs at 8 kHz, 256 kHz at most in GBC fast mode), and in internal-clock mode it samples\n"
            "#    at its own divider edge, a half bit after its own clock edge;\n"
            "#  - the pocket-link bridge two-flop synchronises both on gClk (pocket_link/gb_link_byte.v\n"
            "#    si_sync/sck_sync) and acts on the second flop only.\n"
            "# Until this patch the ports had no set_input_delay, so Gowin left them unconstrained; the false\n"
            "# path states that by design. Outputs (LINK_OUT, LINK_CLK when driven, LINK_CLK_DIR_LV) and\n"
            "# register-to-register paths behind the first flops stay timed as before.\n"
            "set_false_path -from [get_ports {LINK_IN LINK_CLK}]\n"
            "# Review evidence: the worst setup paths of the whole design, and the stock cartridge-bus port limits\n"
            "# (the set_max_delay lines above), printed in the timing report so a violation is named, not just counted.\n"
            "report_timing -setup -max_paths 20 -max_common_paths 1\n"
            + HOLD_REPORT +
            "report_timing -setup -to [get_ports {CART_A[*] CART_D[*] CART_WR CART_RD CART_CS LINK_SD}] -max_paths 10 -max_common_paths 1\n"
            "report_timing -setup -from [get_ports {CART_D[*]}] -max_paths 10 -max_common_paths 1\n")


def link_hooks():
    return plink.patches()


# Placement (second 18.41 build). The stock cart path CART_DIN_r1 -> media_hold quiet CE (a pclk-fall to
# hclk-rise half-cycle relation) is placement-sensitive at ~90 % CLS: +1.595 ns in the first 18.41 build, +0.377 ns
# in the rebuild with the fast-UART fix (Place_Option 0, the IDE default), under the 1.0 ns floor. The placer option
# is a Gowin process setting, so it is staged here (impl/evt1_x2_process_config.json, read by build_options.tcl)
# and recorded in the receipts, never set by hand in the tree. Every other process option stays as upstream's.
#
# Stream image. With the fixed bridge the
# defaults give +0.377 ns (filter off) / +0.949 ns (filter on), both under the floor. Two documented Gowin
# place-and-route options close it, with nothing else touched (no constraint, no stock RTL, same SDC, same floor):
#   Replicate_Resources true  Gowin may duplicate a high-fan-out register so each copy sits next to its loads. A copy
#                             is the same register on the same clock with the same D; the tool keeps every copy under
#                             the full SDC. This is what moves the half-cycle cart path (+0.949 -> +1.067 ns).
#   Route_Maxfan 10           Gowin's router fan-out limit (upstream 23). A routing setting only: no logic, no
#                             constraint. Alone it does not help (+0.580 ns); with replication it gives the best
#                             result: +1.447 ns, hold +0.174, recovery +0.688, removal +0.266 ns, the same figures in
#                             two exploration builds and in this tool's own build. Its neighbours 8 and 12 also pass
#                             (+1.361, +1.261 ns); 5 and 15 do not (+0.402, +0.592 ns); 50 and 100 equal 23 (+1.067).
# The result belongs to this exact netlist and these options: at 91 % CLS any RTL change re-rolls placement (one
# extra status bit in the bridge took the same recipe to -0.959 ns), so every change needs its own gated build.
# Tried and rejected: Place_Option 1..4 (0.333 .. -1.114 without replication; with it 4 passes at +1.234 ns but
# leaves recovery +0.030 ns and removal +0.098 ns, and 3 has a removal violation), Route_Option 0 and 2
# (violations / +0.384 ns), Clock_Route_Order 0 (no change). The gate below is unchanged and is applied to this
# build exactly as to every earlier one.
PROCESS_FILE = 'impl/evt1_x2_process_config.json'
PROCESS_OPTIONS = (  # (option, upstream line, staged line): each upstream line must occur exactly once
    ('Replicate_Resources', '"Replicate_Resources": false,', '"Replicate_Resources": true,'),
    ('Route_Maxfan', '"Route_Maxfan": 23,', '"Route_Maxfan": 10,'),
)


def link_process_patches():
    """impl/evt1_x2_process_config.json (read by build_options.tcl): PROCESS_OPTIONS, nothing else."""
    return [(PROCESS_FILE, old, new) for _, old, new in PROCESS_OPTIONS if old != new]


def rtl_names():
    return list(plink.RTL)


def staged_sources(tree):
    """Every source file this staging patched or added, relative to the tree (sorted, unique)."""
    rels = {rel for rel, _, _ in sound_patches()} | {rel for rel, _, _ in link_hooks()} | {media.SDC}
    rels |= {'src/rtl/BSP/chromatic_media.v'} | {f'{RTL_DIR}/{name}' for name in rtl_names()}
    rels |= {rel for rel, _, _ in link_process_patches()}  # the P&R options are part of what the image was built from
    return sorted(rel for rel in rels if (Path(tree) / rel).is_file())


def clear(out):
    def writable(function, path, _):  # Gowin leaves its outputs read-only; a re-stage replaces them
        os.chmod(path, stat.S_IWRITE)
        function(path)
    built = [p for p in ('build/evt1_x2.fs', 'build/build-receipt.json', 'impl/pnr/evt1_x2.fs') if (out / p).exists()]
    if built:
        print(f'NOTE: re-staging {out} removes its Gowin outputs ({", ".join(built)}); rebuild before any use',
              file=sys.stderr)
    for child in out.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, onerror=writable)
        else:
            writable(os.unlink, child, None)


def stage(out):
    if out.resolve() == (media.OUT / 'esp32t').resolve() or media.OUT.resolve() in out.resolve().parents:
        raise SystemExit('refusing to stage into the 18.40 sound staging')
    head = media.git('-C', str(media.UPSTREAM), 'rev-parse', 'HEAD')
    if head != media.PINNED:
        raise SystemExit(f'upstream/chromatic-fpga is at {head[:12]}, not the pinned {media.PINNED[:12]}')
    if media.git('-C', str(media.UPSTREAM), 'status', '--porcelain'):
        raise SystemExit('upstream/chromatic-fpga has local changes; it must stay the pinned read-only tree')
    if out.exists():
        clear(out)
    out.mkdir(parents=True, exist_ok=True)
    media.copy_tree(media.UPSTREAM / 'esp32t', out)
    mister = out / 'src' / 'Gameboy_MiSTer'
    mister_from = 'upstream'
    if not any(mister.glob('rtl/*')):
        stock = media.STOCK_STAGING / 'src' / 'Gameboy_MiSTer'
        if not any(stock.glob('rtl/*')):
            raise SystemExit('Gameboy_MiSTer submodule missing in upstream and in the stock staging copy')
        pin = subprocess.run(['git', '-C', str(stock), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
        if pin != media.MISTER_PIN:
            raise SystemExit(f'stock staging Gameboy_MiSTer is {pin[:12]}, expected {media.MISTER_PIN[:12]}')
        if mister.exists():
            shutil.rmtree(mister)
        media.copy_tree(stock, mister)  # read only from the stock staging
        mister_from = f'staging/chromatic-fpga-stock (submodule {media.MISTER_PIN[:12]})'
    minor = link_minor()
    sound = [{'file': rel, **media.apply(out / rel, old, new)} for rel, old, new in sound_patches(minor)]
    (out / 'src/rtl/BSP/chromatic_media.v').write_bytes(media.MEDIA_V.read_bytes())
    hooks = [{'file': rel, **media.apply(out / rel, old, new)} for rel, old, new in link_hooks()]
    rtl = {}
    (out / RTL_DIR).mkdir(parents=True, exist_ok=True)
    for name in rtl_names():
        data = (LINK_RTL / name).read_bytes()
        (out / RTL_DIR / name).write_bytes(data)
        rtl[name] = hashlib.sha256(data).hexdigest()
    rel, old, new = link_sdc_patch()
    sdc = {'file': rel, **media.apply(out / rel, old, new)}
    process = [{'file': rel, 'upstream': old, 'staged': new, **media.apply(out / rel, old, new)}
               for rel, old, new in link_process_patches()] or None
    (out / 'NEVER-INSTALL.txt').write_text(NEVER_INSTALL, encoding='utf-8')
    receipt = {
        'what': 'stock 18.14 + Chromatic sound hooks + Pocket link bridge (combined image)',
        'upstream_commit': head, 'upstream_tag': 'v18.14', 'gameboy_mister': mister_from,
        'chromatic_media_v_sha256': media.sha256(media.MEDIA_V),
        'version_register': {'major': 18, 'minor': minor, 'debug': 1},
        'version_source': 'chromatic/launcher/shared/launcher_logic.h PJ_FPGA_LINK_MINOR',
        'game_hold': 'gb.v cpu_hold (CPU clock-enable stall with no access in flight); speedcontrol pause is stock, '
                     'the core\'s LCD timing never stops',
        'qspi_timing': 'evt1_x2.sdc: set_input_delay -clock_fall max 5 / min -2 on QSPI data, QSPI_CS false path, '
                       'report_timing for the QSPI paths',
        'link_timing': 'evt1_x2.sdc: set_false_path -from [get_ports {LINK_IN LINK_CLK}] (asynchronous inputs, '
                       'synchronised on hclk by the stock game path and on gClk by gb_link_byte)',
        'patches': sound, 'link_hooks': hooks, 'link_sdc': sdc, 'link_process': process, 'rtl': rtl,
        'link_hooks_from': 'scripts/check_pocket_link_patch.py patches()',
        'process_options_unchanged': ['JTAG false', 'RECONFIG_N false', 'Multi_Boot false'],
        'built': False, 'reviewed': False, 'device': 'GW5A-EV25UG256CC1/I0 (set_device in build.tcl, unchanged)',
    }
    (out / 'stage-receipt.json').write_text(json.dumps(receipt, indent=1), encoding='utf-8')
    return receipt


def check():
    if not TREE.exists():
        raise SystemExit('nothing staged yet')
    fresh = OUT / 'esp32t.check'
    try:
        stage(fresh)
        same = media.tree_digest(fresh) == media.tree_digest(TREE)
        receipt_same = (json.loads((fresh / 'stage-receipt.json').read_text(encoding='utf-8'))
                        == json.loads((TREE / 'stage-receipt.json').read_text(encoding='utf-8')))
    finally:
        shutil.rmtree(fresh, ignore_errors=True)
    if not same or not receipt_same:
        raise SystemExit('staged tree differs from what this script produces: re-run without --check')
    print(f'PASS: {TREE.relative_to(ROOT)} matches a fresh staging of upstream {media.PINNED[:12]} + sound hooks '
          f'+ {len(link_hooks())} pocket-link hooks + link SDC, version 18.{link_minor()} debug')


def gowin_build(timeout=3600):
    """gw_sh build.tcl in the staged combined tree only, then build/ outputs and the receipt."""
    if not (TREE / 'stage-receipt.json').is_file():
        raise SystemExit('nothing staged yet')
    if not GW_SH.is_file():
        raise SystemExit(f'Gowin not found at {GW_SH}')
    for sub in ('impl/gwsynthesis', 'impl/pnr', 'impl/temp', 'build'):
        if (TREE / sub).exists():
            shutil.rmtree(TREE / sub, onerror=lambda f, p, _: (os.chmod(p, stat.S_IWRITE), f(p)))
    log = TREE / 'link-build.log'
    started = time.time()
    with log.open('w', encoding='utf-8', errors='replace') as stream:
        rc = subprocess.run([str(GW_SH), 'build.tcl'], cwd=str(TREE), stdout=stream, stderr=subprocess.STDOUT,
                            timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).returncode
    took = time.time() - started
    pnr = TREE / 'impl/pnr'
    if rc or not (pnr / 'evt1_x2.fs').is_file():
        print('\n'.join(log.read_text(encoding='utf-8', errors='replace').splitlines()[-40:]))
        raise SystemExit(f'Gowin failed (returncode {rc}, {took:.0f} s); see {log}')
    build = TREE / 'build'
    build.mkdir()
    shutil.copyfile(pnr / 'evt1_x2.fs', build / 'evt1_x2.fs')
    shutil.copyfile(pnr / 'evt1_x2.bin', build / 'evt1_x2.bin')
    (build / 'NEVER-INSTALL.txt').write_text(NEVER_INSTALL, encoding='utf-8')
    receipt = write_receipt(took)
    return receipt


def write_receipt(seconds=None):
    """build/build-receipt.json in the schema of record_chromatic_fpga_build.py (which
    chromatic_fpga.MediaImage pins), with every patched/added source of this staging."""
    tr = record.read(TREE / 'impl/pnr/evt1_x2.tr')
    rpt = record.read(TREE / 'impl/pnr/evt1_x2.rpt.txt')
    fs, bin_ = TREE / 'build/evt1_x2.fs', TREE / 'build/evt1_x2.bin'
    if not fs.is_file() or record.sha(fs) != record.sha(TREE / 'impl/pnr/evt1_x2.fs'):
        raise SystemExit('build/evt1_x2.fs is missing or is not the impl/pnr output of this build')
    if not bin_.is_file() or record.sha(bin_) != record.sha(TREE / 'impl/pnr/evt1_x2.bin'):
        raise SystemExit('build/evt1_x2.bin is missing or is not the impl/pnr output of this build')
    summary = record.sta_summary(tr)
    qspi = record.qspi_paths(tr)
    # Review: at 90 % CLS the stock cart path CART_DIN_r1 -> CART_WR is placement-sensitive (+1.595 ns
    # here, -0.153 ns in an intermediate build). Every setup path the SDC reports must keep SLACK_FLOOR_NS, so a
    # rebuild cannot pass at +0.01 ns unnoticed.
    setup_rows = [p for b in record.report_blocks(tr) if '-setup' in b['command'] for p in b['paths']]  # not hold/recovery/removal
    worst = min(setup_rows, key=lambda p: p['slack_ns']) if setup_rows else None
    # Evidence beside the gate (the gate itself is the two lines above and Gowin's violated-endpoint counts, whose
    # "hold" count also covers removal checks): the worst reported hold, recovery and removal rows, by name.
    others = {}
    for kind in ('hold', 'recovery', 'removal'):
        rows = [p for b in record.report_blocks(tr) if f'-{kind}' in b['command'] for p in b['paths']]
        others[kind] = min(rows, key=lambda p: p['slack_ns']) if rows else None
    stage_receipt = TREE / 'stage-receipt.json'
    staged = json.loads(stage_receipt.read_text(encoding='utf-8'))
    receipt = {
        'built': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(fs.stat().st_mtime)),
        'tool': TOOL,
        'seconds': round(seconds, 1) if seconds is not None else None,
        'source': TREE.relative_to(ROOT).as_posix(),
        'sources_sha256': {rel: record.sha(TREE / rel) for rel in staged_sources(TREE)},
        'outputs': {'build/evt1_x2.fs': {'sha256': record.sha(fs), 'size': fs.stat().st_size},
                    'build/evt1_x2.bin': {'sha256': record.sha(bin_), 'size': bin_.stat().st_size}},
        'timing': {k: summary[k] for k in ('setup_violated_endpoints', 'hold_violated_endpoints')},
        'sta': summary,
        'fmax': record.fmax(tr),
        'qspi_timing': qspi,
        'qspi_paths_all_positive': all(b['all_positive'] for b in qspi if b['paths_reported']) and any(b['paths_reported'] for b in qspi),
        'slack_floor_ns': SLACK_FLOOR_NS,
        'worst_reported_setup': worst,
        'slack_floor_ok': worst is not None and worst['slack_ns'] >= SLACK_FLOOR_NS,
        'setup_paths_reported': len(setup_rows),
        'worst_reported_hold': others['hold'],
        'worst_reported_recovery': others['recovery'],
        'worst_reported_removal': others['removal'],
        'process_options': {name: new.split(':', 1)[1].strip(' ,') for name, old, new in PROCESS_OPTIONS if old != new},
        'resources': record.resources(rpt),
        'stage_receipt_sha256': record.sha(stage_receipt),
        'version_register': staged.get('version_register'),
        'game_hold': staged.get('game_hold'),
        'link_timing': staged.get('link_timing'),
        'media_reference': media_reference(),
        'installed': False,
        'reviewed': False,
        'note': 'UNREVIEWED combined image: never written to a console (flash or SRAM). build/NEVER-INSTALL.txt '
                'stays beside it until review; chromatic_fpga.LinkImage refuses it meanwhile.',
    }
    (TREE / 'build/build-receipt.json').write_text(json.dumps(receipt, indent=1), encoding='utf-8')
    return receipt


SLACK_FLOOR_NS = 1.0


def media_reference():
    """The 18.40 sound build's resources and timing, read (only) from its receipt for comparison."""
    try:
        ref = json.loads((media.OUT / 'esp32t/build/build-receipt.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return {'fs_sha256': ref['outputs']['build/evt1_x2.fs']['sha256'], 'resources': ref.get('resources'),
            'timing': ref.get('timing'), 'fmax': ref.get('fmax'),
            'qspi_worst_setup_slack_ns': min((b['worst_slack_ns'] for b in ref.get('qspi_timing', [])
                                              if b['worst_slack_ns'] is not None and '-setup' in b['command']), default=None)}


def gate_ok(receipt):
    """The timing gate, unchanged: 0 setup / 0 hold violated endpoints and every reported setup path at or over
    SLACK_FLOOR_NS."""
    return (receipt['timing']['setup_violated_endpoints'] == 0 and receipt['timing']['hold_violated_endpoints'] == 0
            and receipt['slack_floor_ok'] is True)


EXPORT_NAME = re.compile(r'esp32t-18\.\d{2}-[a-z0-9]+(?:-[a-z0-9]+)*')
EXPORT_REPORTS = {'impl/pnr/evt1_x2.tr': 'reports/evt1_x2.tr', 'impl/pnr/evt1_x2.rpt.txt': 'reports/evt1_x2.rpt.txt'}
EXPORT_PLAIN = ('stage-receipt.json', 'NEVER-INSTALL.txt', 'link-build.log', 'build/evt1_x2.fs', 'build/evt1_x2.bin',
                'build/build-receipt.json', 'build/NEVER-INSTALL.txt')


def export(name):
    """A kept copy of the built tree's image with what pins it (chromatic_fpga.LinkImage reads exactly these: the
    .fs/.bin, both receipts and every source named by the build receipt), plus the build log and Gowin's reports.
    Only for a build that passes the gate and still matches its receipts and a fresh staging; both NEVER-INSTALL.txt
    markers are copied as the build wrote them, and no approval marker is ever written here."""
    dest = OUT / name
    if not EXPORT_NAME.fullmatch(name) or dest.resolve().parent != OUT.resolve():
        raise SystemExit('export name must look like esp32t-18.41-stream-20261109')
    if dest.exists():
        raise SystemExit(f'{dest.relative_to(ROOT).as_posix()} exists; a kept image folder is never rewritten')
    check()  # the staged tree is what this script produces (sources untouched since staging)
    try:
        built = json.loads((TREE / 'build/build-receipt.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise SystemExit('no build receipt: run --build first')
    # The receipt must describe this very tree and build, and the gate is read again from Gowin's own report.
    tr = record.read(TREE / 'impl/pnr/evt1_x2.tr')
    summary = record.sta_summary(tr)
    setup_rows = [p for b in record.report_blocks(tr) if '-setup' in b['command'] for p in b['paths']]
    worst = min(setup_rows, key=lambda p: p['slack_ns']) if setup_rows else None
    problems = []
    if built.get('stage_receipt_sha256') != record.sha(TREE / 'stage-receipt.json'):
        problems.append('the tree was re-staged after the build')
    if built.get('sources_sha256') != {rel: record.sha(TREE / rel) for rel in staged_sources(TREE)}:
        problems.append('a staged source differs from the build receipt')
    for rel in ('build/evt1_x2.fs', 'build/evt1_x2.bin'):
        if not (TREE / rel).is_file() or built['outputs'][rel]['sha256'] != record.sha(TREE / rel) \
                or record.sha(TREE / rel) != record.sha(TREE / 'impl/pnr' / Path(rel).name):
            problems.append(f'{rel} is not the output the build receipt names')
    if built['timing'] != {k: summary[k] for k in ('setup_violated_endpoints', 'hold_violated_endpoints')} \
            or built.get('worst_reported_setup') != worst or built.get('slack_floor_ns') != SLACK_FLOOR_NS:
        problems.append('the build receipt does not match the timing report')
    if not gate_ok(built) or worst is None or worst['slack_ns'] < SLACK_FLOOR_NS:
        problems.append(f'the build does not pass the gate (0/0 violated endpoints, every reported setup path '
                        f'>= {SLACK_FLOOR_NS} ns)')
    if built.get('installed') is not False or built.get('reviewed') is not False:
        problems.append('the build receipt is not that of an unreviewed, never installed build')
    for rel in ('NEVER-INSTALL.txt', 'build/NEVER-INSTALL.txt'):
        if not (TREE / rel).is_file() or (TREE / rel).read_text(encoding='utf-8') != NEVER_INSTALL:
            problems.append(f'{rel} is not the marker this script writes')
    if problems:
        raise SystemExit('not exported: ' + '; '.join(problems))
    copies = {rel: rel for rel in (*EXPORT_PLAIN, *built['sources_sha256'])}
    copies.update(EXPORT_REPORTS)
    for rel, to in sorted(copies.items()):
        (dest / to).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TREE / rel, dest / to)
        os.chmod(dest / to, stat.S_IWRITE | stat.S_IREAD)
        if record.sha(dest / to) != record.sha(TREE / rel):
            raise SystemExit(f'copy of {rel} did not read back')
    others = {kind: built.get(f'worst_reported_{kind}') for kind in ('hold', 'recovery', 'removal')}
    extract = {
        'from': 'reports/evt1_x2.tr (Gowin timing report of this build), read by scripts/stage_chromatic_fpga_link.py',
        'sta': summary, 'slack_floor_ns': SLACK_FLOOR_NS, 'setup_paths_reported': len(setup_rows),
        'worst_reported_setup': worst, **{f'worst_reported_{k}': v for k, v in others.items()},
        'setup_paths_by_slack': sorted(({k: p[k] for k in ('slack_ns', 'from', 'to', 'launch', 'latch')} for p in setup_rows),
                                       key=lambda p: p['slack_ns'])[:40],
        'fmax': built.get('fmax'), 'resources': built.get('resources'), 'process_options': built.get('process_options'),
    }
    (dest / 'reports/timing-extract.json').write_text(json.dumps(extract, indent=1), encoding='utf-8')
    hashes = {rel: record.sha(dest / rel) for rel in ('build/evt1_x2.fs', 'build/evt1_x2.bin', 'stage-receipt.json',
                                                      'build/build-receipt.json')}
    (dest / 'EXPORTED.txt').write_text(
        f'Exported {time.strftime("%Y-%m-%d %H:%M")} by scripts/stage_chromatic_fpga_link.py --export {name} from\n'
        f'{TREE.relative_to(ROOT).as_posix()} (build of {built["built"]}). The build passed the timing gate (0 setup / 0 hold\n'
        f'violated endpoints, worst reported setup path {worst["slack_ns"]:+.3f} ns against the {SLACK_FLOOR_NS} ns floor).\n'
        'UNREVIEWED: NEVER-INSTALL.txt (here and in build/) is the marker the build wrote; this export approves nothing.\n'
        f'Contents: the {len(built["sources_sha256"])} sources named by build/build-receipt.json sources_sha256, both receipts, the\n'
        'image (.fs and Gowin\'s .bin), the build log, and Gowin\'s timing and resource reports under reports/ with\n'
        'reports/timing-extract.json. This is not a complete Gowin project tree.\n'
        + ''.join(f'{rel}  sha256 {digest}\n' for rel, digest in hashes.items()), encoding='utf-8')
    return {'exported': dest.relative_to(ROOT).as_posix(), 'files': len(copies) + 2, 'sha256': hashes,
            'worst_reported_setup': worst, **{f'worst_reported_{k}': v for k, v in others.items()},
            'timing': built['timing'], 'resources': built.get('resources'), 'process_options': built.get('process_options'),
            'markers': ['NEVER-INSTALL.txt', 'build/NEVER-INSTALL.txt']}


def brief(receipt):
    out = {k: receipt[k] for k in ('built', 'seconds', 'timing', 'resources', 'qspi_paths_all_positive', 'version_register',
                                   'slack_floor_ok', 'process_options')}
    worst = receipt.get('worst_reported_setup')
    out['worst_reported_setup'] = {k: worst[k] for k in ('slack_ns', 'from', 'to')} if worst else None
    for kind in ('hold', 'recovery', 'removal'):
        row = receipt.get(f'worst_reported_{kind}')
        out[f'worst_reported_{kind}'] = {k: row[k] for k in ('slack_ns', 'from', 'to')} if row else None
    out['fs_sha256'] = receipt['outputs']['build/evt1_x2.fs']['sha256']
    out['bin_sha256'] = receipt['outputs']['build/evt1_x2.bin']['sha256']
    out['fmax'] = {k: v['actual_mhz'] for k, v in receipt['fmax'].items()}
    out['qspi'] = [{'command': b['command'].split(' -max_paths')[0], 'paths': b['paths_reported'],
                    'worst_slack_ns': b['worst_slack_ns']} for b in receipt['qspi_timing']]
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--check', action='store_true', help='verify the staged tree matches a fresh staging (no changes)')
    parser.add_argument('--build', action='store_true', help='run Gowin in the staged tree (stages first if nothing is staged)')
    parser.add_argument('--receipt', action='store_true', help='rewrite build/build-receipt.json from the existing build')
    parser.add_argument('--export', metavar='NAME', help='copy the built, gate-passing image with its sources, receipts '
                        'and reports to staging/chromatic-fpga-media-link/NAME (kept unreviewed; never overwrites)')
    args = parser.parse_args()
    if args.check:
        check()
        return 0
    if args.export:
        print(json.dumps(export(args.export), indent=1))
        return 0
    if args.receipt:
        print(json.dumps(brief(write_receipt()), indent=1))
        return 0
    if not args.build or not TREE.exists():
        receipt = stage(TREE)
        print(json.dumps({'staged': str(TREE.relative_to(ROOT)), 'sound_patches': len(receipt['patches']),
                          'link_hooks': len(receipt['link_hooks']), 'rtl': len(receipt['rtl']),
                          'version': receipt['version_register'], 'built': False}, indent=1))
    if args.build:
        receipt = gowin_build()
        print(json.dumps(brief(receipt), indent=1))
        ok = gate_ok(receipt)
        if not receipt['slack_floor_ok']:
            print(f'FAIL: a reported setup path is under the {SLACK_FLOOR_NS} ns floor; review before any use.')
        return 0 if ok else 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

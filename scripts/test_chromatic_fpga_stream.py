# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""The staged 18.41 stream image (staging/chromatic-fpga-media-link/esp32t-18.41-stream-20261109), PC only.

Reads the exported folder, never writes it. Checks, from the files themselves:

  gate      Gowin's own timing report in the folder: 0 setup / 0 hold violated endpoints (the hold count
            also covers removal), every reported setup path at or over the 1.0 ns floor of
            scripts/stage_chromatic_fpga_link.py, and positive worst hold, recovery and removal rows
  honest    the floor is still 1.0; the staged SDC carries exactly the constraints of the approved trial
            image's SDC (only report lines were added); the process file differs from upstream in the
            two documented place-and-route options only; cart.v, gb.v, the engine and every hook are the
            staged ones (the build receipt pins each by hash)
  bridge    the image was built from rtl/chromatic_pocket_link.v as it stands (input filter on) and the
            frozen engine files
  loader    this is the image chromatic_fpga.py pins (LINK_TREE, LINK_PINS) and it is reviewed: both markers
            the build tool wrote were renamed to REVIEWED-UNLOCKED.txt naming the review, and
            chromatic_fpga.LinkImage loads it without allow_unreviewed; a NEVER-INSTALL.txt put back (in a
            temporary copy, at the top or beside the image) refuses it again; the earlier trial image is
            refused by hash
No hardware, no Gowin run. The pins below name this one image; scripts/chromatic_fpga.py pins the same three
hashes in its LINK_TREE block (after an independent review).
"""
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import chromatic_fpga as cf  # noqa: E402  (image checks only; nothing here opens a port)
import record_chromatic_fpga_build as record  # noqa: E402
import stage_chromatic_fpga_link as link  # noqa: E402
import stage_chromatic_fpga_media as media  # noqa: E402

ROOT = link.ROOT
NAME = 'esp32t-18.41-stream-20261109'
STREAM = link.OUT / NAME
TRIAL = link.OUT / 'esp32t-18.41-trial-20261109'
PINS = {'fs': '8feb7929fb4cfdca6d84469df73b06649ca8ac6a973ed53ca087586cac32ab23',
        'bin': '42dbec7a42ec763a45c1d95b8e56bc1b69fd93abafacbb7391048fc46f063504',
        'stage': 'dbd3e9238477ef98262f3313f97d56f69498ded6088cd7fc39b9aaf8c1345b60'}
OPTIONS = {'Replicate_Resources': 'true', 'Route_Maxfan': '10'}
MARKERS = ('NEVER-INSTALL.txt', 'build/NEVER-INSTALL.txt')
UNLOCKED = ('REVIEWED-UNLOCKED.txt', 'build/REVIEWED-UNLOCKED.txt')
REVIEW = 'Independent review 2026-11-09 evening: approve for install, see docs/CHROMATIC-FPGA-STREAM-IMAGE.md'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def constraints(sdc_text):
    """The lines of an SDC that constrain something: no comments, no blank lines, no report commands."""
    lines = [l.strip() for l in sdc_text.replace('\r\n', '\n').split('\n')]
    return [l for l in lines if l and not l.startswith('#') and not l.startswith('report_timing')]


def check_gate():
    built = json.loads((STREAM / 'build/build-receipt.json').read_text(encoding='utf-8'))
    tr = record.read(STREAM / 'reports/evt1_x2.tr')
    summary = record.sta_summary(tr)
    blocks = record.report_blocks(tr)
    assert link.SLACK_FLOOR_NS == 1.0 and built['slack_floor_ns'] == 1.0, 'the floor moved'
    assert summary['setup_violated_endpoints'] == 0 and summary['hold_violated_endpoints'] == 0, summary
    assert built['timing'] == {'setup_violated_endpoints': 0, 'hold_violated_endpoints': 0}, built['timing']
    worst = {}
    for kind in ('setup', 'hold', 'recovery', 'removal'):
        rows = [p for b in blocks if f'-{kind}' in b['command'] for p in b['paths']]
        assert rows, f'no {kind} rows in the report'
        worst[kind] = min(rows, key=lambda p: p['slack_ns'])
        assert built[f'worst_reported_{kind}'] == worst[kind], f'the receipt does not name the worst {kind} row of the report'
    setup = [p for b in blocks if '-setup' in b['command'] for p in b['paths']]
    under = [p for p in setup if p['slack_ns'] < link.SLACK_FLOOR_NS]
    assert not under and built['slack_floor_ok'] is True and link.gate_ok(built), under[:3]
    assert all(worst[k]['slack_ns'] > 0 for k in worst), worst
    whole = [b for b in blocks if b['command'].startswith('report_timing -setup -max_paths 20') and b['paths']]
    assert len(whole) == 1 and len(whole[0]['paths']) == 20, 'the whole-design setup report is not there'
    assert built['qspi_paths_all_positive'] is True
    extract = json.loads((STREAM / 'reports/timing-extract.json').read_text(encoding='utf-8'))
    assert extract['worst_reported_setup'] == worst['setup'] and extract['sta'] == summary
    resources = record.resources(record.read(STREAM / 'reports/evt1_x2.rpt.txt'))
    assert resources == built['resources'], (resources, built['resources'])
    row = lambda p: f"{p['slack_ns']:+.3f} ns {p['from']} -> {p['to']} [{p['launch']} -> {p['latch']}]"
    print(f"PASS: gate from Gowin's report in the folder: 0 setup / 0 hold violated endpoints; {len(setup)} reported setup "
          f"paths, none under {link.SLACK_FLOOR_NS} ns")
    for kind in ('setup', 'hold', 'recovery', 'removal'):
        print(f'      worst {kind:8} {row(worst[kind])}')
    print(f"      resources {resources}")
    return built


def check_honest(built):
    staged = json.loads((STREAM / 'stage-receipt.json').read_text(encoding='utf-8'))
    assert built['stage_receipt_sha256'] == sha(STREAM / 'stage-receipt.json')
    assert set(built['sources_sha256']) == set(link.staged_sources(STREAM)) and len(built['sources_sha256']) == 16
    for rel, digest in built['sources_sha256'].items():
        assert sha(STREAM / rel) == digest, f'{rel} is not the source the image was built from'
    # Constraints: exactly those of the approved trial image; the only SDC difference is report lines.
    sdc = (STREAM / media.SDC).read_bytes().decode('utf-8')
    trial = (TRIAL / media.SDC).read_bytes().decode('utf-8')
    assert constraints(sdc) == constraints(trial), 'the stream SDC constrains differently from the trial SDC'
    added = [l for l in sdc.replace('\r\n', '\n').split('\n') if l not in trial.replace('\r\n', '\n').split('\n')]
    assert added == [link.HOLD_REPORT.strip()], added
    assert all(l.startswith('report_timing') for l in added), added
    # Process options: the upstream file with the two documented lines changed, nothing else.
    upstream = (media.UPSTREAM / 'esp32t' / link.PROCESS_FILE).read_bytes().decode('utf-8')
    ours = (STREAM / link.PROCESS_FILE).read_bytes().decode('utf-8')
    want = upstream
    for _, old, new in link.PROCESS_OPTIONS:
        assert want.count(old) == 1, old
        want = want.replace(old, new)
    assert ours == want and ours != upstream, 'the process file is not upstream + PROCESS_OPTIONS'
    config = json.loads(ours)
    assert config['Replicate_Resources'] is True and config['Route_Maxfan'] == 10 and config['Place_Option'] == '0' \
        and config['Route_Option'] == '1' and config['Correct_Hold_Violation'] is True and config['Run_Timing_Driven'] is True
    assert built['process_options'] == OPTIONS and len(staged['link_process']) == 2, built['process_options']
    # Stock game logic and the sound hooks: the same staged files as the approved trial image.
    same = ('src/rtl/EMU/cart.v', 'src/rtl/EMU/CORE/gb.v', 'src/rtl/EMU/emu_system_top.v', 'src/rtl/BSP/system_monitor.sv',
            'src/rtl/BSP/uart/uart.v', 'src/rtl/BSP/chromatic_media.v', 'src/top.v', 'build_options.tcl', 'evt1_x2.gprj')
    for rel in same:
        assert sha(STREAM / rel) == sha(TRIAL / rel), f'{rel} differs from the approved trial image source'
    assert staged['version_register'] == {'major': 18, 'minor': 41, 'debug': 1} and len(staged['link_hooks']) == 19
    # The receipts are the build tool's own and are pinned by hash: they still say what was true when it
    # wrote them. The review is recorded beside them (REVIEWED-UNLOCKED.txt), never by editing a receipt.
    assert built['installed'] is False and built['reviewed'] is False and staged['reviewed'] is False
    print(f'PASS: floor still {link.SLACK_FLOOR_NS} ns; SDC constraints equal the trial image\'s (one hold report line added, '
          f'no new exception); process options {OPTIONS} and nothing else; {len(same)} stock/hook files equal the trial '
          'image\'s; 16 sources pinned; version 18.41 debug, 19 hooks')
    return staged


def check_bridge(staged):
    rtl = STREAM / link.RTL_DIR
    for name in link.rtl_names():
        assert sha(rtl / name) == sha(ROOT / 'rtl' / name) == staged['rtl'][name], f'{name} is not rtl/{name} as it stands'
    for name in ('byte_fifo.v', 'gb_link_byte.v', 'gb_link_stream.v', 'link_mode_guard.v'):
        assert sha(rtl / name) == sha(TRIAL / link.RTL_DIR / name), f'{name} differs from the trial image source'
    bridge = (rtl / 'chromatic_pocket_link.v').read_text(encoding='utf-8')
    assert 'parameter integer GLITCH_SAMPLES=3 ' in bridge, 'the input filter is not on by default'
    assert sha(rtl / 'chromatic_pocket_link.v') != sha(TRIAL / link.RTL_DIR / 'chromatic_pocket_link.v')
    print('PASS: built from rtl/chromatic_pocket_link.v as it stands (GLITCH_SAMPLES=3: input filter on) and the frozen '
          'engine and guard files, byte-identical to the trial image\'s')


def check_loader():
    fs, stage, receipt = STREAM / 'build/evt1_x2.fs', STREAM / 'stage-receipt.json', STREAM / 'build/build-receipt.json'
    found = {'fs': sha(fs), 'bin': sha(STREAM / 'build/evt1_x2.bin'), 'stage': sha(stage)}
    assert found == PINS, f'the folder is not the image this test pins: {found}'
    # The pinned one: chromatic_fpga.py names this folder and these three hashes, and nothing else.
    assert cf.LINK_TREE == STREAM and cf.LINK_PINS == PINS and (cf.LINK_FS, cf.LINK_RECEIPT, cf.LINK_BUILD_RECEIPT) \
        == (fs, stage, receipt), 'chromatic_fpga.py does not pin the stream image'
    # The reviewed one: both build-tool markers renamed, the review named, the old marker text kept under it.
    for rel in MARKERS:
        assert not (STREAM / rel).exists(), f'{rel} is still there: the image is not unlocked'
    for rel in UNLOCKED:
        text = (STREAM / rel).read_text(encoding='utf-8-sig').replace('\r\n', '\n')
        assert REVIEW in text.split('\n')[0] and f'Image .fs {PINS["fs"][:8]}.' in text.split('\n')[0] \
            and text.rstrip('\n').endswith(link.NEVER_INSTALL.rstrip('\n')), f'{rel} is not the unlock note of this image'
    image = cf.LinkImage()  # chromatic_fpga's own paths and pins, no allow_unreviewed: as the Manager's jobs load it
    assert image.reviewed is True and image.fingerprint == PINS['fs'] and image.version == '18.41'
    assert hashlib.sha256(image.bitstream).hexdigest() == PINS['bin']
    assert cf.link_ready_text() == image.short() == f'sound + Pocket link build 18.41 (image {PINS["fs"][:8]})'
    built = json.loads(receipt.read_text(encoding='utf-8'))
    with tempfile.TemporaryDirectory() as work:
        flash_file = image.write_file(Path(work) / 'job')  # the 4 MiB file a write job would hand the loader (PC only)
        assert flash_file.stat().st_size == cf.SIZE and flash_file.read_bytes() == cf.full_flash_image(image.bitstream)
        # NEVER-INSTALL.txt put back, in a temporary copy (the folder itself is never written here): refused again.
        base = Path(work) / NAME
        for rel in ['build/evt1_x2.fs', 'build/evt1_x2.bin', 'build/build-receipt.json', 'stage-receipt.json',
                    *built['sources_sha256']]:
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(STREAM / rel, base / rel)
        load = lambda allow=False: cf.LinkImage(base / 'build/evt1_x2.fs', base / 'stage-receipt.json',
                                                base / 'build/build-receipt.json', allow_unreviewed=allow)
        clean = load()
        assert clean.reviewed is True and clean.fingerprint == PINS['fs']
        for rel in MARKERS:
            (base / rel).write_text(link.NEVER_INSTALL, encoding='utf-8')
            try:
                load()
                raise AssertionError(f'LinkImage loaded the image with {rel} put back')
            except cf.Refused as error:
                assert 'unreviewed' in str(error), error
            seen = load(allow=True)  # inspection only: never as reviewed, never a flash file
            assert seen.reviewed is False
            for held in (seen, clean):  # also the image loaded before the marker came back
                try:
                    held.write_file(Path(work) / 'never')
                    raise AssertionError(f'a flash file was written with {rel} put back')
                except cf.Refused:
                    pass
            assert not (Path(work) / 'never').exists()
            (base / rel).unlink()
        assert load().reviewed is True
    # The earlier trial image: refused by hash, marker or not, inspection or not.
    assert PINS['fs'] != sha(TRIAL / 'build/evt1_x2.fs')
    for allow in (False, True):
        try:
            cf.LinkImage(TRIAL / 'build/evt1_x2.fs', TRIAL / 'stage-receipt.json', TRIAL / 'build/build-receipt.json',
                         allow_unreviewed=allow)
            raise AssertionError('LinkImage loaded the earlier trial image')
        except cf.Refused as error:
            assert 'not the approved 18.41 stream image' in str(error) and 'unreviewed' not in str(error), error
    print(f'PASS: chromatic_fpga.py pins this image (.fs {PINS["fs"][:8]}, .bin {PINS["bin"][:8]}, stage receipt '
          f'{PINS["stage"][:8]}; receipts, 16 sources, .bin packing, 18.41 debug, 19 hooks) and it is reviewed: both '
          'markers renamed to REVIEWED-UNLOCKED.txt naming the review; LinkImage loads it without allow_unreviewed and '
          'writes the 4 MiB flash file; NEVER-INSTALL.txt put back (top or build/, in a copy) refuses it and its flash '
          'file again; the earlier trial image is refused by hash')


def main():
    if not STREAM.is_dir():
        print(f'SKIP: {STREAM.relative_to(ROOT).as_posix()} is not there. This test checks a Gowin build of the 18.41 image '
              'with its timing reports (stage_chromatic_fpga_link.py --build, then --export ' + NAME + '); '
              'it needs Gowin EDA and a licence of your own.')
        return 0
    built = check_gate()
    staged = check_honest(built)
    check_bridge(staged)
    check_loader()
    print('PASS: 18.41 stream image: gate met from the report, constraints unchanged, filtered bridge; the pinned, reviewed '
          'image of chromatic_fpga.py (a NEVER-INSTALL.txt put back refuses it; the trial image is refused by hash). '
          'Not proven here: anything on a console.')
    return 0


if __name__ == '__main__':
    sys.exit(main())

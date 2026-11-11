# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Simulates the Chromatic FPGA media additions (chromatic/fpga-media) with Icarus.

1. chromatic_media_tb: the new modules alone (I2S receiver, mux, control register).
2. chromatic_media_sysmon_tb: the STAGED stock system_monitor with the hooks applied
   (scripts/stage_chromatic_fpga_media.py), over its real UART bytes: media register,
   stock addresses, SysCtrl clear, version 18.<minor> with the debug bit.
3. chromatic_media_audio_tb: the staged audio path end to end against an unmodified stock
   instance: bit-identical game sound with the media bit off, sample-exact media sound at the
   codec lines under stock's mute/volume/headphone rules, stall silence, resume; the paused
   game silenced while our screen is up (media_menu), stock again after.
4. chromatic_media_cart_tb: the STAGED cart.v (the only driver of the cartridge strobes) with
   chromatic_cpu_hold and a Verilog model of stock speedcontrol (sim/speedcontrol_model.v: the
   GB CPU is VHDL, which Icarus cannot run; the CPU stall of gb.v cpu_hold is modelled): stock
   identical to a reference instance; a hold asked mid-write lands after the write; held: 0 WR,
   data never driven, 0 machine cycles, the clock enable KEEPS RUNNING, even with a forced write;
   a long access delays the stall; release: hold off, CPU back, no reset, identical again.
5. chromatic_media_display_tb: the STAGED stock ST7785_panel_master with a model of the core's LCD
   timing: right while the timing runs; with the core's clock enables stopped (what the first
   sound image did while our screen was up) the panel repeats its 1024-pixel ring down the screen,
   the last lines sheared 64 pixels every 6.4 rows (the scrambled start-up menu of);
   right again once the timing runs. The fixed design never stops that timing.
6. The combined sound + Pocket link staging 18.41 (scripts/stage_chromatic_fpga_link.py): its
   --check, version 18.41 with the debug bit, every sound hook unchanged, the 19 pocket-link hooks,
   byte equality with the reviewed proposal (bar the version lines), the LINK_IN/LINK_CLK false
   path; chromatic_fpga.LinkImage pinning, tamper refusals and the NEVER-INSTALL refusal (on a
   temporary copy); benches 2-5 plus rtl/chromatic_pocket_link_tb.sv on the combined staged
   sources, and a generated bench of the bridge inside the combined system_monitor. The 18.40
   staging, image and build receipt are checked unchanged before and after; the combined tree
   is never re-staged by this test (that would delete its build).
The staged tree is regenerated from the pinned upstream if it is missing or stale.
No hardware and no Gowin tools; prints each testbench's pass/fail line."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from user_paths import TOOLS  # noqa: E402  (per-user folder, see user_paths.py)

from connection_config import ROOT

BASE = TOOLS / 'fpga-tools/native/ucrt64'
SOURCE = ROOT / 'chromatic/fpga-media'
STAGED = ROOT / 'staging/chromatic-fpga-media/esp32t'
BSP = STAGED / 'src/rtl/BSP'
OUT = ROOT / 'staging/fpga-media'
OUT.mkdir(parents=True, exist_ok=True)  # build output, ignored by Git


def media_minor():
    text = (ROOT / 'chromatic/launcher/shared/launcher_logic.h').read_text(encoding='utf-8')
    return int(re.search(r'#define PJ_FPGA_MEDIA_MINOR (\d+)u', text).group(1))


def simulate(env, top, files, cwd, defines=(), out=None):
    vvp = (out or OUT) / f'{top}.vvp'
    subprocess.run([str(BASE / 'bin/iverilog.exe'), '-g2012', '-B', str(BASE / 'lib/ivl'), '-s', top, '-o', str(vvp),
                    *[f'-D{d}' for d in defines], *[str(f) for f in files]], check=True, env=env)
    result = subprocess.run([str(BASE / 'bin/vvp.exe'), '-M', str(BASE / 'lib/ivl'), str(vvp)],
                            capture_output=True, text=True, env=env, timeout=1800, cwd=cwd)
    lines = [line for line in result.stdout.strip().splitlines() if '$finish called' not in line]
    print('\n'.join(lines[-6:]))
    if not any(line.startswith('PASS:') for line in lines) or any(line.startswith('FAIL') for line in lines):
        print(result.stderr[-1000:])
        sys.exit(1)


def icarus_copy(path, out=None):
    """Simulation-only copy of a stock file Icarus cannot parse: the arbiter's unpacked-array
    assignment pattern ('{default: 0}) becomes an initial loop with the same meaning. The
    staged (buildable) source is untouched."""
    text = path.read_text(encoding='utf-8')
    old = "    reg         channelsDataRefreshRequest [NUM_CH-1:0] = '{default: 0};"
    assert text.count(old) == 1, path
    text = text.replace(old, "    reg         channelsDataRefreshRequest [NUM_CH-1:0];\n"
                             "    integer init_i; initial for (init_i = 0; init_i < NUM_CH; init_i = init_i + 1) "
                             "channelsDataRefreshRequest[init_i] = 1'b0; // Icarus: same as '{default: 0}")
    sim = (out or OUT) / 'sim'
    sim.mkdir(parents=True, exist_ok=True)
    out = sim / path.name
    out.write_text(text, encoding='utf-8')
    return out


# The reviewed 18.40 sound image (the one on the console) and its build receipt: never to change.
MEDIA_IMAGE_SHA256 = '21d557ac1c32591e90b409f2834bf07e4b660333f5040c55653034ddcd2516e7'
MEDIA_BUILD_RECEIPT_SHA256 = 'f7e1974ff4e1af2a909d99aedfda8da63f800d1958eae3a326fa404e7ec966e6'
LINK_OUT = ROOT / 'staging/fpga-media-link'  # simulation outputs of the combined-tree benches
LINK_OUT.mkdir(parents=True, exist_ok=True)  # build output, ignored by Git


def crlf(text):
    return text.replace('\n', '\r\n')


def check_media_unchanged():
    """The 18.40 staging still matches its script, its image and build receipt are the reviewed
    ones, and MediaImage's defaults still load exactly that image."""
    import chromatic_fpga as cf
    import hashlib
    result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/stage_chromatic_fpga_media.py'), '--check'],
                            capture_output=True, text=True)
    assert result.returncode == 0, f'18.40 stage --check failed: {result.stdout}{result.stderr}'
    # The shipped image folder (fpga/images/sound-18.40) must hold exactly the sources a fresh staging makes,
    # as its build receipt lists them.
    shipped = cf.MEDIA_TREE
    receipt = json.loads((shipped / 'build/build-receipt.json').read_text(encoding='utf-8'))
    for rel, digest in receipt['sources_sha256'].items():
        assert hashlib.sha256((STAGED / rel).read_bytes()).hexdigest() == digest, f'fresh staging differs: {rel}'
        assert hashlib.sha256((shipped / rel).read_bytes()).hexdigest() == digest, f'shipped source differs: {rel}'
    assert hashlib.sha256((shipped / 'build/build-receipt.json').read_bytes()).hexdigest() == MEDIA_BUILD_RECEIPT_SHA256, \
        '18.40 build receipt changed'
    assert cf.MEDIA_FS == shipped / 'build/evt1_x2.fs' and cf.MEDIA_RECEIPT == shipped / 'stage-receipt.json' \
        and cf.MEDIA_BUILD_RECEIPT == shipped / 'build/build-receipt.json', 'MediaImage default paths moved'
    if not cf.MEDIA_FS.is_file():
        print('PASS: 18.40 sources: a fresh staging equals the shipped fpga/images/sound-18.40 sources and its receipt. '
              'SKIP: the built image itself (put evt1_x2.fs/.bin in fpga/images/sound-18.40/build to check it).')
        return
    assert hashlib.sha256(cf.MEDIA_FS.read_bytes()).hexdigest() == MEDIA_IMAGE_SHA256, '18.40 image changed'
    image = cf.MediaImage()
    assert image.fingerprint == MEDIA_IMAGE_SHA256 and image.version == '18.40', (image.fingerprint, image.version)
    assert cf.media_text() == f'sound build 18.40 (image {MEDIA_IMAGE_SHA256[:8]})', cf.media_text()
    assert not isinstance(image, cf.LinkImage)
    print(f'PASS: 18.40 sound staging unchanged (stage --check), image {MEDIA_IMAGE_SHA256[:8]} and its build receipt '
          'byte-identical, MediaImage defaults still load it.')


def check_combined_tree():
    """Static checks of the combined 18.41 staging (scripts/stage_chromatic_fpga_link.py)."""
    import check_pocket_link_patch as plink
    import stage_chromatic_fpga_link as link
    import stage_chromatic_fpga_media as media
    result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/stage_chromatic_fpga_link.py'), '--check'],
                            capture_output=True, text=True)
    assert result.returncode == 0, f'combined stage --check failed (never re-staged by this test): {result.stdout}{result.stderr}'
    tree = link.TREE
    read = lambda rel: (tree / rel).read_bytes().decode('utf-8')
    minor = link.link_minor()
    assert minor == 41, minor
    sysmon = read('src/rtl/BSP/system_monitor.sv')
    assert crlf(link.version_new(41)) in sysmon and "6'd40" not in sysmon, 'combined version register is not 18.41 debug'
    assert re.search(r"1'd1,\s+// 1 bit debug.*\r\n\s+6'd41,", sysmon), 'debug bit + minor 41'
    hooks = plink.patches()
    assert len(hooks) == 19
    # Every sound hook is in the combined tree unchanged, except where a pocket-link hook extends
    # its text by design (the hook's own anchor replaced inside it); the version minor is 41.
    sound = 0
    for rel, old, new in media.patches():
        want = link.version_new(41) if old.startswith(link.VERSION_ANCHOR) else new
        for hrel, hold, hnew in hooks:
            if hrel == rel and hold in want:
                want = want.replace(hold, hnew)
        assert crlf(want) in read(rel), f'sound hook missing in combined {rel}: {old.splitlines()[0][:60]!r}'
        sound += 1
    for rel, old, new in hooks:
        assert crlf(new) in read(rel), f'pocket-link hook missing in combined {rel}: {old.splitlines()[0][:60]!r}'
    # Byte for byte: the proposal (18.40 staged tree + the 19 hooks) equals the combined tree,
    # system_monitor.sv apart from its two version lines; files no hook touches equal 18.40.
    proposal = plink.OUT
    for rel in {r for r, _, _ in hooks}:
        ours, theirs = (tree / rel).read_bytes(), (proposal / rel).read_bytes()
        if rel == 'src/rtl/BSP/system_monitor.sv':
            v40 = [l for l in theirs.decode().split('\r\n') if "6'd40" in l or 'Chromatic media audio build' in l]
            v41 = [l for l in ours.decode().split('\r\n') if "6'd41" in l or 'Pocket link build)' in l]
            assert len(v40) == 2 and len(v41) == 2
            ours = ours.replace(v41[0].encode(), v40[0].encode()).replace(v41[1].encode(), v40[1].encode())
        assert ours == theirs, f'combined {rel} is not the reviewed proposal'
    for name in link.rtl_names():
        assert (tree / link.RTL_DIR / name).read_bytes() == (ROOT / 'rtl' / name).read_bytes() \
            == (proposal / link.RTL_DIR / name).read_bytes(), name
    for rel in ('src/rtl/EMU/cart.v', 'src/rtl/EMU/CORE/gb.v', 'src/rtl/BSP/chromatic_media.v', 'build_options.tcl',
                'src/board/evt1_x2/evt1_x2.cst', 'build.tcl'):
        assert (tree / rel).read_bytes() == (STAGED / rel).read_bytes(), f'{rel} differs from the 18.40 staging'
    sdc, sdc40 = read(media.SDC), (STAGED / media.SDC).read_bytes().decode('utf-8')
    false_path = 'set_false_path -from [get_ports {LINK_IN LINK_CLK}]\r\n'
    # 1.3.1 review: report lines after the false path name the worst paths and the cartridge-port limits
    tail = 'report_timing -setup -from [get_ports {CART_D[*]}] -max_paths 10 -max_common_paths 1\r\n'
    assert sdc.startswith(sdc40) and sdc.count(false_path) == 1 and sdc.endswith(tail) \
        and sdc.index(false_path) < sdc.index('report_timing -setup -max_paths 20 -max_common_paths 1'), 'link SDC patch'
    cst, top = read('src/board/evt1_x2/evt1_x2.cst'), read('src/top.v')
    for port in ('LINK_IN', 'LINK_CLK'):
        assert f'IO_LOC "{port}"' in cst and re.search(rf'\b(input|inout)\s+{port},', top), f'{port} is not a top-level input'
    staged = json.loads(read('stage-receipt.json'))
    assert staged['version_register'] == {'major': 18, 'minor': 41, 'debug': 1} and len(staged['link_hooks']) == 19 \
        and staged['built'] is False and (tree / 'NEVER-INSTALL.txt').is_file()
    print(f'PASS: combined staging matches its script; version 18.41 debug; {sound} sound hooks unchanged; 19 pocket-link '
          'hooks; equals the reviewed proposal except the version lines; LINK_IN/LINK_CLK false path; NEVER-INSTALL.txt.')


def check_link_image():
    """chromatic_fpga.LinkImage: pinned like MediaImage, refused on every tamper and while
    NEVER-INSTALL.txt is there. Tampering happens on a temporary copy only."""
    import chromatic_fpga as cf
    import hashlib
    import stage_chromatic_fpga_link as link
    # The 18.41 trees: cf.LINK_TREE is the approved image (the tree and its hashes are named in
    # chromatic_fpga's LINK_TREE block only; kept with its receipts, outputs and the sources it was built
    # from) and the only image LinkImage loads (LINK_PINS, by hash). link.TREE is the staging script's
    # working tree and keeps NEVER-INSTALL.txt whatever it holds: another build is refused by hash before
    # any receipt, a byte copy of the approved image is refused as unreviewed (its marker) and is never
    # what the Manager loads. Every other tree beside them (the earlier trial image) is refused by hash.
    tree = cf.LINK_TREE
    assert tree != link.TREE and (link.TREE / 'NEVER-INSTALL.txt').is_file(), (tree, link.TREE)
    others = [t for t in sorted(tree.parent.iterdir()) if t.is_dir() and t != tree and (t / 'build/evt1_x2.fs').is_file()]
    for other in others:
        same = hashlib.sha256((other / 'build/evt1_x2.fs').read_bytes()).hexdigest() == cf.LINK_FS_SHA256
        marker = (other / 'NEVER-INSTALL.txt').is_file() or (other / 'build/NEVER-INSTALL.txt').is_file()
        assert not same or other == link.TREE, f'{other.name}: a second copy of the approved image outside the working tree'
        for allow in (False, True):
            try:
                got = cf.LinkImage(other / 'build/evt1_x2.fs', other / 'stage-receipt.json',
                                   other / 'build/build-receipt.json', allow_unreviewed=allow)
            except cf.Refused as error:
                if same:
                    assert not allow and marker and 'unreviewed' in str(error), (other.name, allow, error)
                else:
                    assert 'not the approved 18.41 stream image' in str(error), (other.name, error)
                continue
            # Only the working tree's byte copy of the approved image, for inspection: never as reviewed.
            assert same and allow and marker and got.reviewed is False, f'LinkImage loaded {other.name} (allow={allow})'
    # Until the trial the marker sits beside the image and nothing gets it; it is removed at
    # trial time, after the review, and then the image loads as reviewed (the Manager's guide uses it).
    if not cf.LINK_FS.is_file():
        # No bitstream in fpga/images/link-18.41/build (it is not kept in Git): nothing may be offered or loaded.
        try:
            cf.LinkImage()
            raise AssertionError('LinkImage loaded without its bitstream')
        except cf.Refused as error:
            assert 'is missing' in str(error), error
        assert cf.link_ready_text() is None, 'the Manager offers a missing image'
        print('PASS: no 18.41 bitstream in fpga/images/link-18.41/build: refused, nothing offered. '
              'SKIP: the image checks (put evt1_x2.fs/.bin there to run them).')
        return
    marked = (tree / 'build/NEVER-INSTALL.txt').is_file() or (tree / 'NEVER-INSTALL.txt').is_file()
    if marked:
        try:
            cf.LinkImage()
            raise AssertionError('LinkImage loaded while NEVER-INSTALL.txt is there')
        except cf.Refused as error:
            assert 'unreviewed' in str(error), error
        assert cf.link_ready_text() is None, 'the Manager offers an unreviewed image'
    else:
        assert cf.LinkImage().reviewed and cf.link_ready_text() == cf.LinkImage().short()
    image = cf.LinkImage(allow_unreviewed=True)
    pinned = json.loads((tree / 'build/build-receipt.json').read_text(encoding='utf-8'))
    assert image.fingerprint == pinned['outputs']['build/evt1_x2.fs']['sha256'] != MEDIA_IMAGE_SHA256
    assert image.version == '18.41' and image.reviewed is not marked, (image.version, image.reviewed)
    assert image.short() == f'sound + Pocket link build 18.41 (image {image.fingerprint[:8]})'
    assert cf.link_text() == image.short() + (', unreviewed: never installed' if marked else ''), cf.link_text()
    assert cf.link_fingerprint() == image.fingerprint
    assert pinned['timing'] == {'setup_violated_endpoints': 0, 'hold_violated_endpoints': 0}, pinned['timing']
    assert set(pinned['sources_sha256']) == set(link.staged_sources(tree)) and len(pinned['sources_sha256']) >= 16
    assert pinned['stage_receipt_sha256'] == hashlib.sha256((tree / 'stage-receipt.json').read_bytes()).hexdigest()
    if marked:
        try:
            image.write_file(LINK_OUT / 'never')
            raise AssertionError('LinkImage wrote a flash file')
        except cf.Refused:
            pass
        assert not (LINK_OUT / 'never').exists()
    refused = 0
    with tempfile.TemporaryDirectory() as work:
        base = Path(work) / 'esp32t'

        def copy():
            if base.exists():
                shutil.rmtree(base)
            for rel in ['build/evt1_x2.fs', 'build/evt1_x2.bin', 'build/build-receipt.json', 'stage-receipt.json',
                        *pinned['sources_sha256']]:
                (base / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(tree / rel, base / rel)

        def load(allow=True):
            return cf.LinkImage(base / 'build/evt1_x2.fs', base / 'stage-receipt.json', base / 'build/build-receipt.json',
                                allow_unreviewed=allow)

        def refuses(what, change, allow=True, text=None):
            nonlocal refused
            copy()
            change()
            try:
                load(allow)
            except cf.Refused as error:
                assert text is None or text in str(error), (what, error)
                refused += 1
                return
            raise AssertionError(f'LinkImage accepted a tampered copy: {what}')

        def edit_json(rel, fn, fix_stage=False):
            path = base / rel
            data = json.loads(path.read_text(encoding='utf-8'))
            fn(data)
            path.write_text(json.dumps(data, indent=1), encoding='utf-8')
            if fix_stage:  # re-pin the stage receipt so only the semantic check can refuse
                edit_json('build/build-receipt.json',
                          lambda b: b.__setitem__('stage_receipt_sha256', hashlib.sha256(path.read_bytes()).hexdigest()))

        def flip(rel, at):
            data = bytearray((base / rel).read_bytes())
            data[at] ^= 1
            (base / rel).write_bytes(bytes(data))

        copy()
        clean = load(allow=False)
        assert clean.reviewed, 'a clean copy without the marker should load'
        flash_file = clean.write_file(Path(work) / 'job')  # the 4 MiB .bin the write job hands the loader
        assert flash_file.read_bytes() == cf.full_flash_image(cf.fs_to_bin(base / 'build/evt1_x2.fs')), 'flash file'
        (base / 'build/NEVER-INSTALL.txt').write_text('x')
        try:
            clean.write_file(Path(work) / 'job2')
            raise AssertionError('write_file ignored a marker put back after loading')
        except cf.Refused:
            (base / 'build/NEVER-INSTALL.txt').unlink()
        refuses('marker beside the image', lambda: (base / 'build/NEVER-INSTALL.txt').write_text('x'), allow=False,
                text='unreviewed')
        refuses('marker at the tree top', lambda: (base / 'NEVER-INSTALL.txt').write_text('x'), allow=False, text='unreviewed')
        refuses('.fs bit flipped', lambda: flip('build/evt1_x2.fs', -3))
        refuses('.bin byte changed', lambda: flip('build/evt1_x2.bin', 5000))
        refuses('.fs missing', lambda: (base / 'build/evt1_x2.fs').unlink(), text='missing')
        refuses('setup violation', lambda: edit_json('build/build-receipt.json',
                                                     lambda b: b['timing'].__setitem__('setup_violated_endpoints', 1)))
        refuses('hold violation', lambda: edit_json('build/build-receipt.json',
                                                    lambda b: b['timing'].__setitem__('hold_violated_endpoints', 2)))
        refuses('re-staged', lambda: edit_json('stage-receipt.json', lambda s: s.__setitem__('built', True)))
        for rel in pinned['sources_sha256']:
            refuses(f'source {rel}', lambda rel=rel: (base / rel).write_bytes((base / rel).read_bytes() + b'\n'))
        refuses('version 18.40', lambda: edit_json('stage-receipt.json',
                                                   lambda s: s['version_register'].__setitem__('minor', 40), fix_stage=True),
                text='18.41')
        refuses('debug bit clear', lambda: edit_json('stage-receipt.json',
                                                     lambda s: s['version_register'].__setitem__('debug', 0), fix_stage=True))
        refuses('link hooks dropped', lambda: edit_json('stage-receipt.json', lambda s: s.pop('link_hooks'), fix_stage=True))
        refuses('sources unpinned', lambda: edit_json('build/build-receipt.json', lambda b: b.pop('sources_sha256')))
        refuses('the 18.40 image in its place', lambda: [shutil.copyfile(STAGED / 'build' / n, base / 'build' / n)
                                                         for n in ('evt1_x2.fs', 'evt1_x2.bin')])
    print(f'PASS: LinkImage pins image {image.fingerprint[:8]} (18.41, 0/0 timing, stage receipt, '
          f'{len(pinned["sources_sha256"])} sources, .bin packing); refused while NEVER-INSTALL.txt is there '
          f'({"present" if marked else "removed: reviewed"}); a clean copy writes the 4 MiB flash file, not with the '
          f'marker back; {refused} tampered copies refused.')


# The bridge inside the combined system_monitor, over its real UART bytes (generated here, sim only):
# game pass-through, keyed entry, channel 10 status frames from the stock arbiter, the fast-UART
# switch, and stock SysCtrl handing the pins and the rate back.
LINK_SYSMON_TB = r"""`timescale 1ns/1ps
module chromatic_link_sysmon_tb;
    reg clk = 0, reset = 1; always #59.6 clk = ~clk;   // gClk 8.388 MHz
    reg [7:0] rx_data = 0; reg rx_val = 0; wire [7:0] tx_data; wire tx_val;
    wire menuDisabled, media_audio, media_overlay, media_menu, so, sck, sck_oe, fast;
    wire [8:0] mcu_buttons; wire [15:0] system_control;
    reg game_so = 0, game_clk = 0, game_clk_oe = 1;    // the game drives the port (internal clock)
    reg btn_menu = 1;                                   // active low, as stock
    // The stock transmitter's own busy (uart.v TX_BUSY = uart_tx.vhd BUSY): a byte takes 10 bit times, 72.75 gClk
    // cycles per bit at 115,200 (divider 72 + correction) and 16 at the fast rate; it rises the cycle after the
    // byte was accepted, which is the one-cycle gap hook 16 covers. The old bench tied it to 0, which is why the
    // "switched while TX busy" guard looked satisfiable with the overlay open (console trial).
    reg tx_busy = 0; integer busy_left = 0;
    always @(posedge clk) begin
        if (tx_val) busy_left = fast ? 160 : 728;
        tx_busy <= busy_left > 0;
        if (busy_left > 0) busy_left = busy_left - 1;
    end
    system_monitor dut (
        .appear_off(1'b0), .clk(clk), .reset(reset),
        .BTN_A(1'b0), .BTN_B(1'b0), .BTN_DPAD_DOWN(1'b0), .BTN_DPAD_LEFT(1'b0), .BTN_DPAD_RIGHT(1'b0), .BTN_DPAD_UP(1'b0),
        .BTN_MENU(btn_menu), .BTN_SEL(1'b0), .BTN_START(1'b0),
        .menuDisabled(menuDisabled), .ADC_SEL(), .hAdcReq_ext(), .LCD_INIT_DONE(1'b1), .LCD_PWM(), .LCD_BACKLIGHT_INIT(),
        .hAdcReady_r1(1'b0), .hAdcValue_r1(14'd2000), .hButtons(9'd0), .MCU_buttons(mcu_buttons),
        .hVolume(7'd32), .pmic_sys_status(8'd0), .temperature(8'd25), .hHeadphones(1'b0),
        .gSecondEna(1'b0), .gHalfSecondEna(1'b0), .low_battery(), .LED_Green(), .LED_Red(), .LED_Yellow(), .LED_White(),
        .system_control(system_control), .debug_system(), .paletteBGIn(), .paletteOBJ0In(), .paletteOBJ1In(),
        .gbc_color_temp(), .gbc_mode(1'b0), .gpd(64'd0),
        .uart_rx_data(rx_data), .uart_rx_val(rx_val), .uart_tx_busy(tx_busy), .uart_tx_data(tx_data), .uart_tx_val(tx_val),
        .VERSION_DET(1'b1), .POWERED_BY_LIPO(), .media_audio(media_audio), .media_overlay(media_overlay), .media_menu(media_menu),
        .link_game_so_i(game_so), .link_game_clk_i(game_clk), .link_game_clk_oe_i(game_clk_oe), .link_si_i(1'b1), .link_clk_i(1'b1),
        .link_so_o(so), .link_clk_o(sck), .link_clk_oe_o(sck_oe), .media_uart_fast(fast));
    integer errors = 0, i, k, n, from, status = 0, last = -1, longest_idle = 0, switch_cycles = 0;
    function [7:0] crc_step(input [7:0] c, input [7:0] d); integer b; begin
        c = c ^ d; for (b = 0; b < 8; b = b + 1) c = c[7] ? {c[6:0], 1'b0} ^ 8'h1D : {c[6:0], 1'b0}; crc_step = c; end endfunction
    task send_byte(input [7:0] b); begin
        @(negedge clk) begin rx_data = b; rx_val = 1; end @(negedge clk) rx_val = 0; repeat (12) @(negedge clk); end endtask
    reg [7:0] pkt [0:15];
    task send(input [6:0] address, input integer count); reg [7:0] c; integer j; begin
        c = 8'hFF; c = crc_step(c, 8'h8F); c = crc_step(c, {1'b0, address}); c = crc_step(c, count[7:0]);
        for (j = 0; j < count; j = j + 1) c = crc_step(c, pkt[j]);
        send_byte(8'h8F); send_byte({1'b0, address}); send_byte(count[7:0]);
        for (j = 0; j < count; j = j + 1) send_byte(pkt[j]);
        send_byte(c); repeat (8) @(negedge clk); end endtask
    task ctrl(input [7:0] cmd); begin
        pkt[0] = cmd; pkt[1] = 8'h50; pkt[2] = 8'h4A; pkt[3] = 8'h4C; pkt[4] = 8'h4B; send(7'h1E, 5); end endtask
    reg [7:0] tx_log [0:65535]; integer tx_count = 0;
    always @(posedge clk) if (tx_val && tx_count < 65536) begin tx_log[tx_count] = tx_data; tx_count = tx_count + 1; end
    // Channel 10 frames (8F 0A 0A + 10 bytes + CRC) in tx_log[from..): count, and the last mode byte.
    task scan(input integer from_i); reg [7:0] c; integer m; begin
        status = 0; last = -1;
        for (k = from_i; k + 13 < tx_count; k = k + 1)
            if (tx_log[k] == 8'h8F && tx_log[k+1] == 8'h0A && tx_log[k+2] == 8'd10) begin
                c = 8'hFF; for (m = 0; m < 13; m = m + 1) c = crc_step(c, tx_log[k+m]);
                if (c == tx_log[k+13]) begin status = status + 1; last = tx_log[k+3]; end
            end
        end endtask
    task wait_mode(input [1:0] m); integer w; begin
        for (w = 0; w < 4000000 && dut.u_chromatic_pocket_link.mode != m; w = w + 1) @(negedge clk); end endtask
    task check_that(input cond, input [511:0] what); begin if (!cond) begin errors = errors + 1;
        $display("FAIL %0s (so=%b sck=%b sck_oe=%b fast=%b mode=%0d status=%0d last=%h)", what, so, sck, sck_oe, fast,
                 dut.u_chromatic_pocket_link.mode, status, last[7:0]); end end endtask
    initial begin
        repeat (10) @(negedge clk); reset = 0; repeat (200) @(negedge clk);
        // Game mode: the pins follow the game exactly, no channel 10 traffic, stock rate.
        game_so = 1; game_clk = 1; repeat (4) @(negedge clk);
        check_that(so === 1 && sck === 1 && sck_oe === 1 && fast === 0, "game pass-through (driven)");
        game_so = 0; game_clk = 0; game_clk_oe = 0; repeat (4) @(negedge clk);
        check_that(so === 0 && sck_oe === 0, "game pass-through (released)");
        game_clk_oe = 1;
        from = tx_count; repeat (500000) @(negedge clk); scan(from);
        check_that(status == 0, "channel 10 sent in game mode");
        // A wrong key is ignored; the keyed entry takes the port (LINK_CLK released: the Pocket clocks).
        pkt[0] = 1; pkt[1] = 8'h50; pkt[2] = 8'h4A; pkt[3] = 8'h4C; pkt[4] = 8'h00; send(7'h1E, 5); repeat (200) @(negedge clk);
        check_that(sck_oe === 1, "wrong key entered media link mode");
        from = tx_count; ctrl(1); wait_mode(2); repeat (200) @(negedge clk);
        check_that(dut.u_chromatic_pocket_link.mode == 2 && sck_oe === 0, "media link mode (2) reached, LINK_CLK released");
        game_so = 1; game_clk = 1; repeat (4) @(negedge clk); check_that(sck_oe === 0, "game signals reach the pins in media mode");
        repeat (900000) @(negedge clk); scan(from); n = status;
        check_that(status >= 1 && last[1:0] == 2'd2 && last[6] == 1'b0, "channel 10 status frames in media mode (mode 2, slow)");
        // Fast UART switch (keyed), then stock SysCtrl: port and rate back, status traffic stops.
        ctrl(3); ctrl(4); repeat (40000) @(negedge clk); check_that(fast === 1, "fast UART after the keyed switch");
        pkt[0] = 0; pkt[1] = 0; send(7'd4, 2); wait_mode(0); repeat (200) @(negedge clk);
        check_that(fast === 0 && sck_oe === 1 && so === 1 && sck === 1, "SysCtrl returns the port and the rate to the game");
        repeat (20000) @(negedge clk); // the one leave report (mode 3) goes out first
        from = tx_count; repeat (500000) @(negedge clk); scan(from);
        check_that(status == 0, "channel 10 sent after SysCtrl");
        // ---- The console as the launcher runs it: the overlay is OPEN (the launcher's screen is the stock overlay), so
        // system_monitor streams every channel back to back (channelsNewDataValid is ~menuDisabled for most of them;
        // the arbiter never idles). The trial: "Pocket link" on the console failed fast with "The fast
        // connection failed": the bridge's switch waited for two byte times of transmitter idle that never come.
        btn_menu = 0; repeat (200) @(negedge clk); btn_menu = 1; repeat (400) @(negedge clk);
        check_that(menuDisabled === 0, "the Menu press opened the overlay");
        from = tx_count; repeat (100000) @(negedge clk);
        check_that(tx_count - from > 100, "the stock stream runs with the overlay open");
        // the longest transmitter-idle run (in gClk cycles) under that stream, as the bridge sees it
        begin : gap
            integer run, worst; run = 0; worst = 0;
            for (i = 0; i < 200000; i = i + 1) begin
                @(negedge clk);
                if (dut.uart_tx_busy_safe) run = 0; else begin run = run + 1; if (run > worst) worst = run; end
            end
            longest_idle = worst;
        end
        ctrl(1); wait_mode(2); repeat (200) @(negedge clk);
        check_that(dut.u_chromatic_pocket_link.mode == 2, "media link mode under the open overlay");
        ctrl(3); ctrl(4);
        for (i = 0; i < 1680000 && fast !== 1; i = i + 1) @(negedge clk); // up to 200 ms (the launcher gives it 500 ms)
        check_that(fast === 1, "fast UART switched with the overlay open (continuous stock stream)");
        switch_cycles = i;
        // the MCU speaks at the new rate: confirmed, and the status that confirms carries the fast bit
        from = tx_count; ctrl(3); repeat (60000) @(negedge clk); scan(from);
        check_that(status >= 1 && last[6] === 1'b1 && last[1:0] == 2'd2, "status confirms the fast rate under the stream");
        pkt[0] = 0; pkt[1] = 0; send(7'd4, 2); wait_mode(0); repeat (200) @(negedge clk);
        check_that(fast === 0 && sck_oe === 1, "SysCtrl returns port and rate under the open overlay");
        if (errors == 0) $display("PASS: combined system_monitor pocket link: game pass-through, wrong key ignored, keyed entry releases LINK_CLK, %0d channel-10 status frames from the stock arbiter (mode 2), keyed fast UART, SysCtrl returns port and rate, no channel-10 traffic in game mode; overlay open: longest TX idle %0d cycles, fast UART switched after %0d cycles, confirmed, SysCtrl exit.", n, longest_idle, switch_cycles);
        $finish;
    end
endmodule
"""


def combined_benches(env):
    """The media benches and the pocket-link bench on the STAGED combined (18.41) sources."""
    import stage_chromatic_fpga_link as link
    tree = link.TREE
    bsp = tree / 'src/rtl/BSP'
    plink = [bsp / 'pocket_link' / n for n in link.rtl_names()]
    LINK_OUT.mkdir(parents=True, exist_ok=True)
    # The sysmon bench names the 18.40 ports only; the combined system_monitor has the link ports
    # too. A simulation-only copy ties them idle (the game's link signals high, clock not driven).
    tb = (SOURCE / 'chromatic_media_sysmon_tb.sv').read_text(encoding='utf-8')
    anchor = '.media_menu(media_menu)\n'
    assert tb.count(anchor) == 1
    tb = tb.replace(anchor, '.media_menu(media_menu),\n'
                    "        .link_game_so_i(1'b1), .link_game_clk_i(1'b1), .link_game_clk_oe_i(1'b0), .link_si_i(1'b1),"
                    " .link_clk_i(1'b1), .link_so_o(), .link_clk_o(), .link_clk_oe_o(), .media_uart_fast() // combined image\n")
    (LINK_OUT / 'sim').mkdir(parents=True, exist_ok=True)
    tb_copy = LINK_OUT / 'sim/chromatic_media_sysmon_tb.sv'
    tb_copy.write_text(tb, encoding='utf-8')
    simulate(env, 'chromatic_media_sysmon_tb',
             [bsp / 'chromatic_media.v', *plink, bsp / 'system_monitor.sv', icarus_copy(bsp / 'system_monitor_arbiter.sv', LINK_OUT),
              bsp / 'uart_packet_wrapper_rx.sv', bsp / 'uart_packet_wrapper_tx.sv', tb_copy],
             ROOT, defines=['MEDIA_MINOR=41'], out=LINK_OUT)
    link_tb = LINK_OUT / 'sim/chromatic_link_sysmon_tb.sv'
    link_tb.write_text(LINK_SYSMON_TB, encoding='utf-8')
    simulate(env, 'chromatic_link_sysmon_tb',
             [bsp / 'chromatic_media.v', *plink, bsp / 'system_monitor.sv', icarus_copy(bsp / 'system_monitor_arbiter.sv', LINK_OUT),
              bsp / 'uart_packet_wrapper_rx.sv', bsp / 'uart_packet_wrapper_tx.sv', link_tb], ROOT, out=LINK_OUT)
    cart_files = [bsp / 'chromatic_media.v', tree / 'src/rtl/EMU/cart.v', SOURCE / 'sim/speedcontrol_model.v',
                  SOURCE / 'chromatic_media_cart_tb.sv']
    simulate(env, 'chromatic_media_cart_tb', cart_files, ROOT, out=LINK_OUT)
    simulate(env, 'chromatic_media_cart_tb', cart_files, ROOT, defines=['DOUBLE_SPEED'], out=LINK_OUT)
    with tempfile.TemporaryDirectory() as work:
        shutil.copy(bsp / 'tlv320regs.hex', work)
        simulate(env, 'chromatic_media_audio_tb',
                 [bsp / 'chromatic_media.v', bsp / 'aud_system_top_8_16.sv', bsp / 'i2c_master_8_16.sv', bsp / 'tlv320_init.v',
                  bsp / 'polling_master_8_16.v', SOURCE / 'chromatic_media_audio_tb.sv'], work, out=LINK_OUT)
    simulate(env, 'chromatic_media_display_tb', [bsp / 'ST7785_panel_master.v', SOURCE / 'chromatic_media_display_tb.sv'],
             ROOT, out=LINK_OUT)
    simulate(env, 'chromatic_pocket_link_tb',
             [*plink, ROOT / 'rtl/pocket_media_link.v', ROOT / 'rtl/chromatic_pocket_link_tb.sv', bsp / 'uart_packet_wrapper_rx.sv'],
             ROOT, out=LINK_OUT)
    print('PASS: combined 18.41 staged sources: sysmon (version 18.41 debug), the bridge inside system_monitor, cart x2, display, '
          'audio and the pocket-link bench pass.')


def uart2_run(env, uart_v, name, **params):
    """One run of rtl/chromatic_uart2_tb.sv: UART2 (the given uart.v + the VHDL port) against the ESP32 model."""
    vvp = LINK_OUT / f'uart2-{name}.vvp'
    subprocess.run([str(BASE / 'bin/iverilog.exe'), '-g2012', '-B', str(BASE / 'lib/ivl'), '-s', 'chromatic_uart2_tb', '-o', str(vvp),
                    *[a for k, v in params.items() for a in ('-P', f'chromatic_uart2_tb.{k}={v}')],
                    str(ROOT / 'rtl/chromatic_uart2_tb.sv'), str(ROOT / 'rtl/uart2_vhdl_port.v'), str(uart_v)], check=True, env=env)
    result = subprocess.run([str(BASE / 'bin/vvp.exe'), '-M', str(BASE / 'lib/ivl'), str(vvp)], capture_output=True, text=True,
                            env=env, timeout=1800, cwd=ROOT)
    lines = result.stdout.splitlines()
    margin = next((l for l in lines if l.startswith('MARGIN')), '')
    res = next((l for l in lines if l.startswith('RESULT')), '')
    if result.returncode or 'FATAL' in result.stdout + result.stderr or not res:
        print(result.stdout[-1500:], result.stderr[-800:])
        sys.exit(f'FAIL: UART2 bench {name}')
    return margin, res


def uart2_benches(env):
    """UART2 ITSELF at the Pocket-link rate: the staged combined uart.v (rate mux, correction mux,
    debouncer bypass) with a line-by-line Verilog port of the stock uart_rx.vhd/uart_tx.vhd, both directions at once,
    against an ESP32 model at its real IDF fractional divider, +-20 ns jitter on every edge, a random gClk phase."""
    import re as _re
    import stage_chromatic_fpga_link as link
    uart_v = link.TREE / 'src/rtl/BSP/uart/uart.v'
    text = uart_v.read_text(encoding='utf-8')
    assert "wire        fast_rate = (BAUD_RATE == 32'd524288);" in text and "fast_rate ? 2'd0 : 2'd3" in text \
        and "fast_rate ? 24'd16 : 24'd72" in text and 'uart_rxd_debounced <= fast_rate ? uart_rxd_shreg[3] :' in text, \
        'the staged uart.v does not carry the 524,288-baud mux'
    pocket_tether = (ROOT / 'shared/pocket_tether.h').read_text(encoding='utf-8')
    assert '#define PJ_TETHER_UART_FAST_BAUD 524288u' in pocket_tether, 'pocket_tether.h fast rate out of step with the FPGA'
    top = (link.TREE / 'src/top.v').read_text(encoding='utf-8')
    assert ".BAUD_RATE(media_uart_fast ? 32'd524288 : 32'd115200)" in top, 'top.v BAUD_RATE mux'
    LINK_OUT.mkdir(parents=True, exist_ok=True)
    margins = {}
    for name, params in (('fast', {}), ('fast-seed2', {'SEED': 99, 'NBYTES': 600}), ('fast-jitter50', {'JITTER_PS': 50000}),
                         ('fast-esp-2pct-fast', {'ERR_PPM': -20000}), ('fast-esp-2pct-slow', {'ERR_PPM': 20000})):
        margin, res = uart2_run(env, uart_v, name, BAUD=524288, ESP_BAUD=524288, **params)
        margins[name] = margin
        print(f'  {name}: {margin}')
    # The stock rate through the same patched uart.v is the stock path (divider 72, correction 3, the 4-sample filter).
    margin, res = uart2_run(env, uart_v, 'stock-115200', BAUD=115200, ESP_BAUD=115200)
    print(f'  stock-115200: {margin}')
    # The first proposal (divider 8 with the stock correction, filter kept) at 1,048,576: must fail, as the review found.
    old = LINK_OUT / 'uart-first-proposal.v'
    stock = (STAGED / 'src/rtl/BSP/uart/uart.v').read_text(encoding='utf-8')
    anchor = "    wire [23:0] divider_value = 'd72;"
    assert stock.count(anchor) == 1
    old.write_text(stock.replace(anchor, "    wire [23:0] divider_value = (BAUD_RATE == 32'd1048576) ? 24'd8 : 24'd72;"), encoding='utf-8')
    margin, res = uart2_run(env, old, 'first-proposal-1048576', BAUD=1048576, ESP_BAUD=1048576, EXPECT_FAIL=1)
    print(f'  first proposal (divider 8 + correction, 958,698 baud vs the ESP32 at 1,048,576): {res.split("fpga_rx=")[1]}')
    nominal = margins['fast']
    cyc = [float(x) for x in _re.findall(r'/([0-9.]+)cyc', nominal)]
    assert len(cyc) == 4 and min(cyc) >= 3.0, ('UART2 sampling margin under 3 gClk cycles at 524,288 baud', nominal)
    print('PASS: UART2 at 524,288 baud (gClk/16, correction off, filter bypassed in fast mode only), both directions at once against the '
          f'ESP32 at its IDF divider (2441/16 of 80 MHz = 524,375 baud), +-20 ns jitter: no byte wrong over 300+600 bytes; worst '
          f'sampling margins {nominal[7:]}; also with +-50 ns jitter and the ESP32 clock 2% fast or slow; stock 115,200 unchanged; '
          'the first proposal (8.75 cycles per bit) fails, as reviewed.')


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env['PATH'] = os.pathsep.join([str(BASE / 'bin'), 'C:\\msys64\\ucrt64\\bin', env['PATH']])  # vvp's DLLs
    stage = ROOT / 'scripts/stage_chromatic_fpga_media.py'
    if not STAGED.exists() or subprocess.run([sys.executable, str(stage), '--check'], capture_output=True).returncode:
        print(f'NOTE: the staged tree at {STAGED} is missing or stale: re-staging it now. Any Gowin outputs there '
              '(build/evt1_x2.fs, build-receipt.json) are removed by this; rebuild before any console use.')
        subprocess.run([sys.executable, str(stage)], check=True)
    minor = media_minor()
    simulate(env, 'chromatic_media_tb', [SOURCE / 'chromatic_media.v', SOURCE / 'chromatic_media_tb.sv'], ROOT)
    simulate(env, 'chromatic_media_sysmon_tb',
             [BSP / 'chromatic_media.v', BSP / 'system_monitor.sv', icarus_copy(BSP / 'system_monitor_arbiter.sv'),
              BSP / 'uart_packet_wrapper_rx.sv', BSP / 'uart_packet_wrapper_tx.sv', SOURCE / 'chromatic_media_sysmon_tb.sv'],
             ROOT, defines=[f'MEDIA_MINOR={minor}'])
    cart_files = [BSP / 'chromatic_media.v', STAGED / 'src/rtl/EMU/cart.v', SOURCE / 'sim/speedcontrol_model.v',
                  SOURCE / 'chromatic_media_cart_tb.sv']
    simulate(env, 'chromatic_media_cart_tb', cart_files, ROOT)                                   # normal speed
    simulate(env, 'chromatic_media_cart_tb', cart_files, ROOT, defines=['DOUBLE_SPEED'])         # double-speed 8 MHz sequencer
    # The staged gb.v must carry the exact stall condition the CPU model above reproduces.
    gb = (STAGED / 'src/rtl/EMU/CORE/gb.v').read_text(encoding='utf-8')
    assert "assign hold_cpu_stop = cpu_hold & cpu_rd_n & cpu_wr_n & (TSTATEo == 3'd4) & ~WRITE_CYCLE;" in gb, 'gb.v stall condition'
    assert 'wire cpu_clken = ~hdma_cpu_stop & ~hold_cpu_stop & ce_cpu;' in gb, 'gb.v cpu_clken gate'
    simulate(env, 'chromatic_media_display_tb', [BSP / 'ST7785_panel_master.v', SOURCE / 'chromatic_media_display_tb.sv'], ROOT)
    with tempfile.TemporaryDirectory() as work:
        shutil.copy(BSP / 'tlv320regs.hex', work)  # tlv320_init.v $readmemh's it from the working directory
        simulate(env, 'chromatic_media_audio_tb',
                 [BSP / 'chromatic_media.v', BSP / 'aud_system_top_8_16.sv', BSP / 'i2c_master_8_16.sv', BSP / 'tlv320_init.v',
                  BSP / 'polling_master_8_16.v', SOURCE / 'chromatic_media_audio_tb.sv'], work)
    print(f'PASS: all five Chromatic FPGA media simulations (staged stock+media source, version 18.{minor} debug).')
    check_media_unchanged()
    check_combined_tree()
    check_link_image()
    combined_benches(env)
    uart2_benches(env)
    check_media_unchanged()  # and again after everything above ran
    print('PASS: combined sound + Pocket link staging 18.41 checks (the 18.40 sound image untouched).')


if __name__ == '__main__':
    main()

# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Stages the stock+media Chromatic FPGA source: ModRetro's stock tree plus the sound hooks.

Copies the pinned read-only upstream (upstream/chromatic-fpga, tag v18.14, never edited) into
staging/chromatic-fpga-media/esp32t and applies the reviewed hooks of
the media design as exact text patches (each anchor must match exactly
once, or the script stops):

  top.v              1. chromatic_i2s_rx + chromatic_audio_mux between the game core's left/right
                        and aud_system_top_8_16 (volume, mute, headphones stay stock)
                     2. menu_gated: OR in media_overlay (one term)
                     3. two new ports on the system_monitor instance
  system_monitor.sv  4. media_audio/media_overlay output ports
                     5. chromatic_media_ctrl on the UART decode (address 0x1D; stock SysCtrl clears)
                     6. version register: debug bit set, minor 40 (launcher_logic.h PJ_FPGA_MEDIA_MINOR)
                     7. media_menu (0x1D bit 2): while set, the physical Menu button no longer toggles the
                        overlay (it is still reported in the Buttons message, bit 8); the MCU's own poke
                        of the menu line (address 9 bit 8) still toggles. Clear: stock behaviour, exactly.
                        media_menu is also an output port: top.v feeds it to the game core (CPU hold) and
                        to the audio mux (game sound silenced while our screen is up).
  CORE/gb.v          8. (src/rtl/EMU/CORE/gb.v, ModRetro's core fork, the file the project builds; the
                        src/Gameboy_MiSTer submodule copy is not in the file list)
                        cpu_hold input / cpu_held output: the CPU's clock enable is gated exactly as the
                        colour-mode HDMA stall gates it (only with no read or write in flight); the PPU, timers,
                        sound and DMA keep running. The first sound image paused the WHOLE core
                        (speedcontrol pause) and the stock display path, which is clocked by the core's
                        LCD timing, repeated the panel's pixel ring down the screen (the scrambled
                        start-up menu of the first sound image; chromatic/fpga-media/chromatic_media_display_tb.sv).
                        speedcontrol's pause is stock again.
  emu_system_top.v   9. media_pause -> u_gb.cpu_hold; chromatic_cpu_hold(cpu_held) -> cart.v hold
  cart.v            10. hold input: once the CPU is held, CART_WR stays released and the data bus is never
                        driven, whatever the stalled CPU's bus signals say; chip select and reads are
                        stock (HDMA, DMA and the idle bus keep reading as on the stock image)
  evt1_x2.sdc       11. QSPI timing: the ESP32's overlay write (data on the falling edge of QSPI_CLK,
                        sampled on the rising edge) gets input delays, so the capture paths are analysed
                        and placement-independent; QSPI_CS (the slave's asynchronous reset, 75 ns away
                        from any clock edge by protocol) is a false path; report_timing commands put the
                        QSPI paths in the timing report. Not the cause of the scrambled menu (that was 8),
                        but the one part of that interface the stock constraints left to chance.
  evt1_x2.gprj      12. src/rtl/BSP/chromatic_media.v added to the file list
  build_options.tcl 13. report-only gen_* options Gowin 1.9.12.04 lacks are skipped (as the stock staging)

The Gameboy_MiSTer submodule (pinned 44f43d57) comes from the stock staging copy when the upstream
checkout has not fetched it. Nothing is built: the Gowin step needs the owner's licence and is a
separate, supervised job. A receipt with hashes goes next to the tree. `--check` only verifies that
the staged tree is what this script would produce. `--stock-timing` applies ONLY the evt1_x2.sdc
patch (11) to staging/chromatic-fpga-stock/esp32t, the otherwise unmodified stock tree, so a
stock build with the same constraints can be compared. No hardware.
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / 'upstream' / 'chromatic-fpga'
PINNED = 'a01aa375147943cbcd4a41f41585220d55e0c28a'  # upstream.lock.json chromatic_fpga
MISTER_PIN = '44f43d5797cf6e67480c156b47974d6f1ca9ce4a'
STOCK_STAGING = ROOT / 'staging' / 'chromatic-fpga-stock' / 'esp32t'
OUT = ROOT / 'staging' / 'chromatic-fpga-media'
MEDIA_V = ROOT / 'chromatic' / 'fpga-media' / 'chromatic_media.v'
LAUNCHER_LOGIC = ROOT / 'chromatic' / 'launcher' / 'shared' / 'launcher_logic.h'
SKIP = {'build', 'impl/pnr', 'impl/gwsynthesis', 'impl/temp', 'impl/evt1_x2.tr.html'}
SDC = 'src/board/evt1_x2/evt1_x2.sdc'
SDC_MARK = '# Chromatic media staging: QSPI overlay write timing'


def media_minor():
    text = LAUNCHER_LOGIC.read_text(encoding='utf-8')
    major = int(re.search(r'#define PJ_FPGA_MEDIA_MAJOR (\d+)u', text).group(1))
    minor = int(re.search(r'#define PJ_FPGA_MEDIA_MINOR (\d+)u', text).group(1))
    assert major == 18 and 0 < minor < 64, (major, minor)
    return minor


def sdc_patch():
    """The timing patch (11), shared with --stock-timing. Anchors on the stock file's last line."""
    return (SDC,
            "set_max_delay -from [get_clocks {hclk}] -to [get_ports {CART_D[*]}] 14\n",
            "set_max_delay -from [get_clocks {hclk}] -to [get_ports {CART_D[*]}] 14\n"
            f"{SDC_MARK} (scripts/stage_chromatic_fpga_media.py, 2026-11-08)\n"
            "# The ESP32 writes the overlay as a QSPI master (mode 0, 40 MHz, IO_MUX pins): the four data\n"
            "# lines change on the falling edge of QSPI_CLK and qspi_slave.v samples them on the rising edge.\n"
            "# Budget assumed at the FPGA pins: data valid at most 5 ns after the falling edge and held until\n"
            "# at least 2 ns before the next falling edge (ESP32 output skew plus board), so the capture paths\n"
            "# are analysed and no longer depend on where the placer puts the slave's registers.\n"
            "# QSPI_CS is the slave's asynchronous reset: the ESP32 asserts it 3 bit times (75 ns) before the\n"
            "# first clock and releases it 75 ns after the last (cs_ena_pretrans/posttrans = 3 in stock main.c\n"
            "# and in the launcher), far from any clock edge: a false path by protocol.\n"
            "set_input_delay -clock sclk -clock_fall -max 5 [get_ports {QSPI_MOSI QSPI_MISO QSPI_WP QSPI_HD}]\n"
            "set_input_delay -clock sclk -clock_fall -min -2 [get_ports {QSPI_MOSI QSPI_MISO QSPI_WP QSPI_HD}]\n"
            "set_false_path -from [get_ports {QSPI_CS}]\n"
            "report_timing -setup -from [get_ports {QSPI_MOSI QSPI_MISO QSPI_WP QSPI_HD}] -max_paths 24 -max_common_paths 24\n"
            "report_timing -hold -from [get_ports {QSPI_MOSI QSPI_MISO QSPI_WP QSPI_HD}] -max_paths 24 -max_common_paths 24\n"
            "report_timing -setup -from_clock [get_clocks {sclk}] -to_clock [get_clocks {sclk}] -max_paths 24 -max_common_paths 24\n"
            "report_timing -hold -from_clock [get_clocks {sclk}] -to_clock [get_clocks {sclk}] -max_paths 24 -max_common_paths 24\n")


def patches():
    """(file, old, new) with '\\n' line ends; apply() converts to the file's own (CRLF throughout)."""
    minor = media_minor()
    return [
        ('src/top.v',
         "    wire [15:0] left, right;\n    wire [7:0]  volume;\n",
         "    wire [15:0] left, right;            // the game core's sound (stock name kept for the core instance)\n"
         "    // Chromatic media (chromatic_media.v, docs/CHROMATIC-AUDIO-PLAN.md): the ESP32's I2S stream on the\n"
         "    // pins stock wires but never reads (I2S_WS = bit clock, I2S_DIN = word select, I2S_DOUT = data),\n"
         "    // selected into the codec path while media_audio is set; game sound otherwise, exactly as stock.\n"
         "    wire        media_audio, media_overlay, media_menu, media_frame;\n"
         "    wire [15:0] media_left, media_right, aud_left, aud_right;\n"
         "    chromatic_i2s_rx u_media_i2s (\n"
         "        .clk(gClk), .reset_n(lock_o), .i2s_bclk(I2S_WS), .i2s_ws(I2S_DIN), .i2s_sd(I2S_DOUT),\n"
         "        .left(media_left), .right(media_right), .frame(media_frame)\n"
         "    );\n"
         "    chromatic_audio_mux u_media_mux (\n"
         "        .clk(gClk), .reset_n(lock_o), .media_audio(media_audio), .media_menu(media_menu),\n"
         "        .game_left(left), .game_right(right), .media_left(media_left), .media_right(media_right),\n"
         "        .media_frame(media_frame), .left(aud_left), .right(aud_right)\n"
         "    );\n"
         "    wire [7:0]  volume;\n"),
        ('src/top.v',
         "        .reset_n(lock_o),\n        .left(left),\n        .right(right),\n\n        .AUD_BCLK(AUD_BCLK),\n",
         "        .reset_n(lock_o),\n        .left(aud_left),   // media mux output (game sound unless media_audio)\n"
         "        .right(aud_right),\n\n        .AUD_BCLK(AUD_BCLK),\n"),
        ('src/top.v',
         "    wire menu_gated = qMenuInit&(CART_DET_sr[6:3]==4'b1111) ? BTN_MENU_filtered : 1'b1;\n",
         "    wire menu_gated = qMenuInit&((CART_DET_sr[6:3]==4'b1111)|media_overlay) ? BTN_MENU_filtered : 1'b1; // media_overlay: overlay without a cartridge\n"),
        ('src/top.v',
         "        .VERSION_DET (VERSION_DET),\n        .POWERED_BY_LIPO (powered_by_lipo)\n    );\n",
         "        .VERSION_DET (VERSION_DET),\n        .POWERED_BY_LIPO (powered_by_lipo),\n"
         "        .media_audio (media_audio),\n        .media_overlay (media_overlay),\n"
         "        .media_menu (media_menu)\n    );\n"),
        ('src/top.v',
         "        .MENU_CLOSED(menuDisabled),\n",
         "        .MENU_CLOSED(menuDisabled),\n"
         "        .media_pause(media_menu), // Chromatic media: the game CPU is held while our overlay is up (gb.v cpu_hold)\n"),
        ('src/rtl/EMU/CORE/gb.v',  # ModRetro's own core fork: the gb.v evt1_x2.gprj builds (src/Gameboy_MiSTer is not)
         "    output speed,   //GBC\n    output DMA_on,\n",
         "    output speed,   //GBC\n    output DMA_on,\n"
         "    // Chromatic media (chromatic_media.v): stall the CPU, as the GBC HDMA stall does, while our screen is up.\n"
         "    // Everything else (PPU and its LCD timing, timers, sound, DMA) keeps running.\n"
         "    input  cpu_hold,\n    output cpu_held,\n"),
        ('src/rtl/EMU/CORE/gb.v',
         "wire cpu_clken = ~hdma_cpu_stop & ce_cpu;\n",
         "// Chromatic media: hold the CPU at an M-cycle boundary. GBse (T2Write=2) drives RD_n/WR_n low in T2+T3\n"
         "// only, so rd_n & wr_n alone would also let the stall land in T1 of a write cycle, where WRITE_CYCLE\n"
         "// (cart.v's wr) is already up while T80's DO still holds the previous byte, and cart.v's free-running\n"
         "// strobe sequencer would write stale data every 16 hclk; TSTATEo == 4 exists only in M1 (opcode fetch,\n"
         "// 4 T-states; its read ended in T3), so the bus then looks exactly like stock's HALT idle. The CPU\n"
         "// stays at that edge (TSTATEo, rd_n, wr_n frozen), so cpu_held holds until cpu_hold drops.\n"
         "wire hold_cpu_stop;\n"
         "assign cpu_held = hold_cpu_stop;\n"
         "wire cpu_clken = ~hdma_cpu_stop & ~hold_cpu_stop & ce_cpu;\n"),
        ('src/rtl/EMU/CORE/gb.v',
         "wire WRITE_CYCLE;\n",
         "wire WRITE_CYCLE;\n"
         "assign hold_cpu_stop = cpu_hold & cpu_rd_n & cpu_wr_n & (TSTATEo == 3'd4) & ~WRITE_CYCLE; // Chromatic media, see cpu_clken\n"),
        ('src/rtl/EMU/emu_system_top.v',
         "    input               MENU_CLOSED,\n",
         "    input               MENU_CLOSED,\n"
         "    input               media_pause, // Chromatic media (chromatic_media.v): hold the game CPU while our overlay is up\n"),
        ('src/rtl/EMU/emu_system_top.v',
         "    wire hdma_active;\n    cart u_cart\n    (\n       .hclk            (hclk           ),\n",
         "    wire hdma_active;\n"
         "    // Chromatic media: the game CPU is stalled (gb.v cpu_hold, the GBC HDMA stall's own mechanism) while our\n"
         "    // screen is up; the PPU, timers and sound keep running, so the core's LCD timing, which clocks the whole\n"
         "    // stock display path (vid_system_top, the overlay line fetch, the panel), never stops. speedcontrol is\n"
         "    // stock: its pause is NOT used (it stopped the clock enables and scrambled the overlay). Once the CPU has\n"
         "    // been held 32 hclk (every strobe of its last access finished) cart.v refuses write strobes and never\n"
         "    // drives the data bus; reads are stock. Dropped the cycle media_pause clears. Nothing touches gbreset.\n"
         "    wire media_cpu_held, media_hold;\n"
         "    chromatic_cpu_hold u_media_hold (.clk(hclk), .reset_n(reset_n), .cpu_held(media_cpu_held), .hold(media_hold));\n"
         "    cart u_cart\n    (\n       .hclk            (hclk           ),\n       .hold            (media_hold     ),\n"),
        ('src/rtl/EMU/emu_system_top.v',
         "        .speed(cpu_speed),   //GBC\n",
         "        .speed(cpu_speed),   //GBC\n"
         "        .cpu_hold(media_pause), // Chromatic media: CPU stall while our screen is up\n"
         "        .cpu_held(media_cpu_held),\n"),
        ('src/rtl/EMU/cart.v',
         "    input               rd,\n",
         "    input               rd,\n"
         "    input               hold, // Chromatic media: the game CPU is held; WR stays released, data never driven\n"),
        ('src/rtl/EMU/cart.v',
         "                endcase\n            end\n        end\n    end\n    \nendmodule\n",
         "                endcase\n            end\n"
         "            // Chromatic media: while our screen holds the game CPU (stalled at T4 of an opcode fetch), no\n"
         "            // cartridge write strobe and no data drive, whatever the stalled CPU's bus signals say, and\n"
         "            // chip select high as in stock's HALT idle (in GBC double speed the sequencer would otherwise\n"
         "            // re-drive nCS of a frozen A000-DFFF address every 2 hclk). An HDMA or DMA read of the cartridge\n"
         "            // (DMA_on) keeps its chip select, so those transfers read as on the stock image.\n"
         "            if (hold) begin\n"
         "                CART_WR       <= 1'd1;\n"
         "                CART_DATA_DIR <= 1'd0;\n"
         "                if (~DMA_on) CART_CS <= 1'd1;\n"
         "            end\n"
         "        end\n    end\n    \nendmodule\n"),
        ('src/rtl/BSP/system_monitor.sv',
         "    input               VERSION_DET,\n    output              POWERED_BY_LIPO\n);\n",
         "    input               VERSION_DET,\n    output              POWERED_BY_LIPO,\n"
         "    // Chromatic media control register (chromatic_media.v): MCU address 0x1D, cleared by SysCtrl (4)\n"
         "    output              media_audio,\n    output              media_overlay,\n"
         "    output              media_menu   // bit 2: Menu button to the MCU; top.v holds the game CPU\n);\n"),
        ('src/rtl/BSP/system_monitor.sv',
         "    reg [13:0] version = {\n        1'd0,  // 1 bit reserved\n        1'd0,  // 1 bit debug,\n"
         "        6'd14, // 6 bits minor version\n        6'd18  // 6 bits major version\n    };\n",
         "    reg [13:0] version = {\n        1'd0,  // 1 bit reserved\n"
         "        1'd1,  // 1 bit debug: set, this is not an official ModRetro release (Chromatic media audio build)\n"
         f"        6'd{minor}, // 6 bits minor version ({minor}: stock 18.14 + chromatic_media.v sound hooks; launcher_logic.h PJ_FPGA_MEDIA_MINOR)\n"
         "        6'd18  // 6 bits major version\n    };\n"),
        ('src/rtl/BSP/system_monitor.sv',
         "        .rx_address(rx_address),\n        .rx_data(rx_data),\n        .rx_data_val(rx_data_val)\n    );\n\nendmodule\n",
         "        .rx_address(rx_address),\n        .rx_data(rx_data),\n        .rx_data_val(rx_data_val)\n    );\n\n"
         "    // Chromatic media: address 0x1D sets media_audio/media_overlay; stock SysCtrl (address 4),\n"
         "    // which stock firmware always sends at start, clears them, as does reset.\n"
         "    // Bit 2 (media_menu) stays inside this module: see the menu button block above.\n"
         "    chromatic_media_ctrl u_chromatic_media_ctrl (\n"
         "        .clk(clk),\n        .reset_n(~reset),\n        .rx_data_val(rx_data_val),\n        .rx_address(rx_address),\n"
         "        .rx_data(rx_data[63:0]),\n        .media_audio(media_audio),\n        .media_overlay(media_overlay),\n"
         "        .media_menu(media_menu)\n    );\n\nendmodule\n"),
        ('src/rtl/BSP/system_monitor.sv',
         "    reg menuDown = 1'b0;\n",
         "    reg menuDown = 1'b0;\n"
         "    // Chromatic media (chromatic_media.v): media_menu (0x1D bit 2) hands the physical Menu button to the\n"
         "    // MCU (Buttons bit 8 below) instead of toggling the overlay; menuDownMcu remembers that a press came\n"
         "    // from the MCU's own poke (address 9 bit 8, top.v ANDs it into BTN_MENU), which always toggles.\n"
         "    reg  menuDownMcu = 1'b0;\n"),
        ('src/rtl/BSP/system_monitor.sv',
         "            if(btnMenu_sr[15:0] == 16'h8000) begin\n"
         "               menuDown <= 1'b1;\n"
         "            end\n"
         "            if(btnMenu_sr[15:0] == 16'h7FFF && menuDown) begin\n"
         "               menuDisabled <= ~menuDisabled;\n"
         "               menuDown     <= 1'b0;\n"
         "            end\n",
         "            if(btnMenu_sr[15:0] == 16'h8000) begin\n"
         "               menuDown <= 1'b1;\n"
         "               menuDownMcu <= MCU_buttons[8]; // Chromatic media: the MCU's own poke, not the physical button\n"
         "            end\n"
         "            if(btnMenu_sr[15:0] == 16'h7FFF && menuDown) begin\n"
         "               if (~media_menu | menuDownMcu) menuDisabled <= ~menuDisabled; // media_menu: physical Menu only reported\n"
         "               menuDown     <= 1'b0;\n"
         "            end\n"),
        sdc_patch(),
        ('evt1_x2.gprj',
         '        <File path="src/rtl/BSP/aud_system_top_8_16.sv" type="file.verilog" enable="1"/>\n',
         '        <File path="src/rtl/BSP/aud_system_top_8_16.sv" type="file.verilog" enable="1"/>\n'
         '        <File path="src/rtl/BSP/chromatic_media.v" type="file.verilog" enable="1"/>\n'),
        ('build_options.tcl',
         "    set value [ide_option $key]\n    puts \"IDE option: -$option $value\"\n    set_option -$option $value\n}\n",
         "    set value [ide_option $key]\n    puts \"IDE option: -$option $value\"\n"
         "    # Staged copy only: Gowin 1.9.12.04 lacks some report-only options that 1.9.12.03\n"
         "    # (ModRetro's version) has. Skip those report generators; anything else still fails.\n"
         "    if {[catch {set_option -$option $value} message]} {\n"
         "        if {[string match gen_* $option] && $value eq \"0\"} {\n"
         "            puts \"Skipped report-only option not in this Gowin version: -$option $value\"\n"
         "        } else {\n            error $message\n        }\n    }\n}\n"),
    ]


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(*args):
    return subprocess.run(['git', *args], capture_output=True, text=True, check=True).stdout.strip()


def copy_tree(src, dst):
    for path in src.rglob('*'):
        rel = path.relative_to(src).as_posix()
        if any(rel == s or rel.startswith(s + '/') for s in SKIP) or '/.git' in '/' + rel:
            continue
        target = dst / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def apply(path, old, new):
    """One exact-text patch in the file's own line ends (every stock file is CRLF). Returns the
    receipt entry; stops unless the anchor occurs exactly once."""
    text = path.read_bytes().decode('utf-8')
    eol = '\r\n' if '\r\n' in text else '\n'
    old_f, new_f = old.replace('\n', eol), new.replace('\n', eol)
    if text.count(old_f) != 1:
        raise SystemExit(f'anchor not found exactly once in {path}: {old.splitlines()[0][:60]!r} ({text.count(old_f)} matches)')
    before = hashlib.sha256(text.encode()).hexdigest()
    path.write_bytes(text.replace(old_f, new_f).encode('utf-8'))
    return {'anchor': old.splitlines()[0].strip(), 'before': before, 'after': sha256(path)}


def build(out):
    head = git('-C', str(UPSTREAM), 'rev-parse', 'HEAD')
    if head != PINNED:
        raise SystemExit(f'upstream/chromatic-fpga is at {head[:12]}, not the pinned {PINNED[:12]}')
    if git('-C', str(UPSTREAM), 'status', '--porcelain'):
        raise SystemExit('upstream/chromatic-fpga has local changes; it must stay the pinned read-only tree')
    src = UPSTREAM / 'esp32t'
    if out.exists():
        def writable(function, path, _):  # Gowin leaves its outputs read-only; a re-stage replaces them
            os.chmod(path, stat.S_IWRITE)
            function(path)
        built = [p for p in ('build/evt1_x2.fs', 'build/build-receipt.json', 'impl/pnr/evt1_x2.fs') if (out / p).exists()]
        if built:
            print(f'NOTE: re-staging {out} removes its Gowin outputs ({", ".join(built)}); rebuild before any console use',
                  file=sys.stderr)
        for child in out.iterdir():  # the folder itself may be some shell's working directory: empty it, keep it
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, onerror=writable)
            else:
                writable(os.unlink, child, None)
    out.mkdir(parents=True, exist_ok=True)
    copy_tree(src, out)
    mister = out / 'src' / 'Gameboy_MiSTer'
    mister_from = 'upstream'
    if not any(mister.glob('rtl/*')):
        stock = STOCK_STAGING / 'src' / 'Gameboy_MiSTer'
        if not any(stock.glob('rtl/*')):
            raise SystemExit('Gameboy_MiSTer submodule missing in upstream and in the stock staging copy')
        pin = subprocess.run(['git', '-C', str(stock), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
        if pin != MISTER_PIN:
            raise SystemExit(f'stock staging Gameboy_MiSTer is {pin[:12]}, expected {MISTER_PIN[:12]}')
        if mister.exists():
            shutil.rmtree(mister)
        copy_tree(stock, mister)
        mister_from = f'staging/chromatic-fpga-stock (submodule {MISTER_PIN[:12]})'
    applied = []
    for rel, old, new in patches():
        applied.append({'file': rel, **apply(out / rel, old, new)})
    media = out / 'src' / 'rtl' / 'BSP' / 'chromatic_media.v'
    media.write_bytes(MEDIA_V.read_bytes())
    (out / 'NEVER-INSTALL-UNREVIEWED.txt').write_text(
        'Staged stock+media Chromatic FPGA source. Not built here; never written to a console until\n'
        'the Gowin build (0 setup/0 hold, QSPI paths reported with positive slack), the temporary\n'
        'SRAM-only trial (Chromatic Manager: Try FPGA image) has shown the start-up menu and Jellyfin\n'
        "picture and sound right, and the rehearsed JTAG restore of the owner's original image has\n"
        'passed, all with the owner present (docs/CHROMATIC-AUDIO-PLAN.md).\n', encoding='utf-8')
    receipt = {
        'upstream_commit': head, 'upstream_tag': 'v18.14', 'gameboy_mister': mister_from,
        'chromatic_media_v_sha256': sha256(MEDIA_V), 'version_register': {'major': 18, 'minor': media_minor(), 'debug': 1},
        'game_hold': 'gb.v cpu_hold (CPU clock-enable stall with no access in flight); speedcontrol pause is stock, '
                     'the core\'s LCD timing never stops',
        'qspi_timing': 'evt1_x2.sdc: set_input_delay -clock_fall max 5 / min -2 on QSPI data, QSPI_CS false path, '
                       'report_timing for the QSPI paths',
        'patches': applied, 'process_options_unchanged': ['JTAG false', 'RECONFIG_N false', 'Multi_Boot false'],
        'built': False, 'device': 'GW5A-EV25UG256CC1/I0 (set_device in build.tcl, unchanged)',
    }
    (out / 'stage-receipt.json').write_text(json.dumps(receipt, indent=1), encoding='utf-8')
    return receipt


def stock_timing():
    """The SDC patch alone on the stock staging tree (idempotent): the comparison build."""
    path = STOCK_STAGING / SDC
    if not path.is_file():
        raise SystemExit(f'no stock staging at {STOCK_STAGING}')
    text = path.read_bytes().decode('utf-8')
    upstream = (UPSTREAM / 'esp32t' / SDC).read_bytes().decode('utf-8')
    if SDC_MARK in text:
        print(f'PASS: {path} already carries the QSPI timing patch')
        return
    if text != upstream:
        raise SystemExit(f'{path} differs from the pinned upstream SDC in some other way; not touching it')
    _, old, new = sdc_patch()
    entry = apply(path, old, new)
    (STOCK_STAGING / 'stock-timing-patch.json').write_text(json.dumps({
        'file': SDC, 'upstream_sdc_sha256': hashlib.sha256(upstream.encode()).hexdigest(), **entry,
        'note': 'Only this timing patch differs from the pinned stock tree (stage_chromatic_fpga_media.py --stock-timing).'},
        indent=1), encoding='utf-8')
    print(json.dumps({'patched': str(path), **entry}, indent=1))


def tree_digest(root):
    out = {}
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root).as_posix()
        if any(rel == s or rel.startswith(s + '/') for s in SKIP) or rel.startswith('impl/') or rel.startswith('build/'):
            continue  # Gowin outputs of a build, not part of the staged source
        if path.suffix in ('.log', '.err'):
            continue  # a build's console log next to the tree (a running build must never trigger a re-stage)
        if path.is_file() and path.name != 'stage-receipt.json':
            out[rel] = sha256(path)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--check', action='store_true', help='verify the staged tree matches a fresh staging (no changes)')
    parser.add_argument('--stock-timing', action='store_true',
                        help='apply only the evt1_x2.sdc QSPI timing patch to staging/chromatic-fpga-stock (comparison build)')
    args = parser.parse_args()
    if args.stock_timing:
        stock_timing()
        return
    target = OUT / 'esp32t'
    if args.check:
        if not target.exists():
            raise SystemExit('nothing staged yet')
        fresh = OUT / 'esp32t.check'
        try:
            build(fresh)
            same = tree_digest(fresh) == tree_digest(target)
        finally:
            shutil.rmtree(fresh, ignore_errors=True)
        if not same:
            raise SystemExit('staged tree differs from what this script produces: re-run without --check')
        print(f'PASS: {target} matches a fresh staging of upstream {PINNED[:12]} + sound hooks')
        return
    receipt = build(target)
    print(json.dumps({'staged': str(target), 'patches': len(receipt['patches']), 'gameboy_mister': receipt['gameboy_mister'],
                      'version': receipt['version_register'], 'built': False}, indent=1))


if __name__ == '__main__':
    sys.exit(main())

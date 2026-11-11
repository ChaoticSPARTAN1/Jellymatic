# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Proposal checker for the Chromatic FPGA pocket-link patch (docs/Design-notes.md).

Reads the STAGED stock+media tree (staging/chromatic-fpga-media/esp32t, the source of the
image on the console) READ-ONLY, applies the pocket-link hooks below in memory, requires every
anchor to match exactly once, and writes the patched copies, the new RTL files and a unified
diff to staging/chromatic-fpga-pocket-link-proposal/ for review. It never edits the staged
tree, never builds, never touches hardware. The same (file, old, new) list went into
scripts/stage_chromatic_fpga_link.py, followed by a Gowin build, the Icarus benches and a write
session with the person present at the console (Chromatic Manager).

Hooks:
  system_monitor.sv  14. link pin ports (game side in, muxed pins out), NUM_CH 10 -> 11
                     15. TX channel 10 (address 0x0A): request-once from the bridge, 10 bytes by position
                     16. Shiny Relay's uart_tx_busy one-cycle gap fix (GPL-3.0, system_monitor.sv 1.1.5):
                         a channel requested while BUSY still reads 0 lost its header once in 16 packets
                     17. chromatic_pocket_link instance on the stock UART decode (0x1E ctrl, 0x1F data)
  emu_system_top.v   18. the game core's link signals come out; the pins are driven from the mux inputs
  top.v              19. six wires between u_emu_system_top and u_system_monitor; LINK_IN/LINK_CLK read
  evt1_x2.gprj       20. the five new RTL files under src/rtl/BSP/pocket_link/
  (SDC)              21. proposed, not in this list: LINK_IN/LINK_CLK are asynchronous inputs synchronised
                         inside gb_link_byte; review whether the stock SDC already covers them
"""
import difflib
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGED = ROOT / 'staging' / 'chromatic-fpga-media' / 'esp32t'
OUT = ROOT / 'staging' / 'chromatic-fpga-pocket-link-proposal'
RTL = ['chromatic_pocket_link.v', 'byte_fifo.v', 'gb_link_byte.v', 'gb_link_stream.v', 'link_mode_guard.v']


def patches():
    return [
        ('src/rtl/BSP/system_monitor.sv',
         "    output              media_menu   // bit 2: Menu button to the MCU; top.v holds the game CPU\n);\n",
         "    output              media_menu,  // bit 2: Menu button to the MCU; top.v holds the game CPU\n"
         "    // Chromatic pocket link (pocket_link/chromatic_pocket_link.v): the Game Boy link port is owned by\n"
         "    // the bridge only in media link mode (MCU address 0x1E, keyed); otherwise these pass the game\n"
         "    // core's signals straight through. SysCtrl (4), reset and a lost heartbeat return the port.\n"
         "    input               link_game_so_i,\n    input               link_game_clk_i,\n    input               link_game_clk_oe_i,\n"
         "    input               link_si_i,\n    input               link_clk_i,\n"
         "    output              link_so_o,\n    output              link_clk_o,\n    output              link_clk_oe_o,\n"
         "    output              media_uart_fast  // 1: UART2 runs at the fast rate (media link only); top.v muxes the divider\n);\n"),
        ('src/rtl/BSP/system_monitor.sv',
         "    localparam NUM_CH = 10;\n",
         "    localparam NUM_CH = 11; // stock 10 + channel 10 (address 0x0A): Chromatic pocket link status\n"),
        ('src/rtl/BSP/system_monitor.sv',
         "    wire [NUM_CH-1:0] channelsNewDataValid =\n    {\n        request_gpd,                                  // Game Palette Data\n",
         "    wire       plink_req;   // Chromatic pocket link: request-once, payload stable until plink_taken\n"
         "    wire [7:0] plink_byte;\n"
         "    wire       plink_taken = write_done && (tx_channel == 4'd10);\n"
         "    wire [NUM_CH-1:0] channelsNewDataValid =\n    {\n"
         "        plink_req,                                    // Chromatic pocket link status (channel 10)\n"
         "        request_gpd,                                  // Game Palette Data\n"),
        ('src/rtl/BSP/system_monitor.sv',
         "                              (tx_channel == 9) ? 8'd8 : // BG Palette Data\n                              8'd1;\n",
         "                              (tx_channel == 9) ? 8'd8 : // BG Palette Data\n"
         "                              (tx_channel == 10) ? 8'd10 : // Chromatic pocket link status\n"
         "                              8'd1;\n"),
        ('src/rtl/BSP/system_monitor.sv',
         "            default:         tx_senddata = 8'd0;\n        endcase\n    end\n",
         "            default:         tx_senddata = 8'd0;\n        endcase\n"
         "        if (tx_channel == 4'd10) tx_senddata = plink_byte; // Chromatic pocket link: bytes by position\n    end\n"),
        ('src/rtl/BSP/system_monitor.sv',
         "        .channelsNewDataValid(channelsNewDataValid),\n        .uart_tx_busy(uart_tx_busy),\n        .tx_address(tx_address),\n",
         "        .channelsNewDataValid(channelsNewDataValid),\n        .uart_tx_busy(uart_tx_busy_safe),\n        .tx_address(tx_address),\n"),
        ('src/rtl/BSP/system_monitor.sv',
         "    uart_packet_wrapper_tx u_uart_packet_wrapper_tx\n    (\n        .clk(clk),\n        .reset(reset),\n        .uart_tx_busy(uart_tx_busy),\n",
         "    uart_packet_wrapper_tx u_uart_packet_wrapper_tx\n    (\n        .clk(clk),\n        .reset(reset),\n        .uart_tx_busy(uart_tx_busy_safe),\n"),
        ('src/rtl/BSP/system_monitor.sv',
         "    wire uartDisabled;\n",
         "    wire uartDisabled;\n"
         "    // From Shiny Relay 1.1.5 system_monitor.sv (GPL-3.0): UART2 reports BUSY = tx_busy | DATA_SEND, but\n"
         "    // tx_busy rises one cycle after a byte is accepted; a byte offered in that cycle is lost. The packet\n"
         "    // wrapper paces data bytes, but a header goes out the moment a channel is requested, so one packet\n"
         "    // in 16 lost its address byte. With a channel requested as often as the pocket link's, cover the gap.\n"
         "    reg  uart_tx_val_q = 1'b0;\n    always @(posedge clk) uart_tx_val_q <= uart_tx_val;\n"
         "    wire uart_tx_busy_safe = uart_tx_busy | uart_tx_val_q;\n"),
        ('src/rtl/BSP/system_monitor.sv',
         "        .media_menu(media_menu)\n    );\n\nendmodule\n",
         "        .media_menu(media_menu)\n    );\n\n"
         "    // Chromatic pocket link: Game Boy link port bridge for the Analogue Pocket tether\n"
         "    // (docs/CHROMATIC-POCKET-TETHER.md). gClk domain, stock UART decode in, TX channel 10 out.\n"
         "    chromatic_pocket_link #(.CLK_HZ(8388608)) u_chromatic_pocket_link (\n"
         "        .clk(clk), .reset(reset),\n"
         "        .rx_data_val(rx_data_val), .rx_address(rx_address), .rx_data(rx_data),\n"
         "        .uart_rx_data_i(uart_rx_data), .uart_rx_val_i(uart_rx_val), .uart_tx_busy_i(uart_tx_busy_safe),\n"
         "        .uart_fast_o(media_uart_fast),\n"
         "        .up_req(plink_req), .up_taken(plink_taken), .tx_bytepos(tx_bytepos), .up_byte(plink_byte),\n"
         "        .game_so_i(link_game_so_i), .game_clk_i(link_game_clk_i), .game_clk_oe_i(link_game_clk_oe_i),\n"
         "        .link_si_i(link_si_i), .link_clk_i(link_clk_i),\n"
         "        .link_so_o(link_so_o), .link_clk_o(link_clk_o), .link_clk_oe(link_clk_oe_o),\n"
         "        .mode_o(), .faults_o()\n    );\n\nendmodule\n"),
        ('src/rtl/EMU/emu_system_top.v',
         "    inout               LINK_CLK,\n    output              LINK_CLK_DIR_LV,\n    input               LINK_IN,\n    output              LINK_OUT,\n",
         "    inout               LINK_CLK,\n    output              LINK_CLK_DIR_LV,\n    input               LINK_IN,\n    output              LINK_OUT,\n"
         "    // Chromatic pocket link: the game core's link signals go out to the mux in system_monitor; the\n"
         "    // pins are driven from what comes back (the game's own signals unless media link mode is on)\n"
         "    output              link_game_so_o,\n    output              link_game_clk_o,\n    output              link_game_clk_oe_o,\n"
         "    input               link_mux_so_i,\n    input               link_mux_clk_i,\n    input               link_mux_clk_oe_i,\n"),
        ('src/rtl/EMU/emu_system_top.v',
         "    assign LINK_CLK_DIR_LV = sc_int_clock2_r1; \n\n    assign LINK_CLK = sc_int_clock2_r1 ? serial_clk_out_r1 : 1'bZ;\n",
         "    assign link_game_so_o     = serial_data_out_r1; // Chromatic pocket link: stock values, muxed outside\n"
         "    assign link_game_clk_o    = serial_clk_out_r1;\n"
         "    assign link_game_clk_oe_o = sc_int_clock2_r1;\n"
         "    assign LINK_CLK_DIR_LV = link_mux_clk_oe_i; \n\n    assign LINK_CLK = link_mux_clk_oe_i ? link_mux_clk_i : 1'bZ;\n"),
        ('src/rtl/EMU/emu_system_top.v',
         "    assign LINK_OUT = serial_data_out_r1;\n",
         "    assign LINK_OUT = link_mux_so_i; // Chromatic pocket link mux (the game's serial_data_out_r1 in game mode)\n"),
        ('src/top.v',
         "    emu_system_top u_emu_system_top(\n",
         "    // Chromatic pocket link: game core <-> system_monitor link mux (docs/CHROMATIC-POCKET-TETHER.md)\n"
         "    wire link_game_so, link_game_clk, link_game_clk_oe, link_mux_so, link_mux_clk, link_mux_clk_oe;\n"
         "    wire media_uart_fast; // 1 only in media link mode: UART2 runs at 524,288 baud; stock SysCtrl/leave/reset clear it\n"
         "    emu_system_top u_emu_system_top(\n"),
        ('src/top.v',
         "        .LINK_CLK(LINK_CLK),\n        .LINK_CLK_DIR_LV (LINK_CLK_DIR_LV), // control direction of clock through core\n        .LINK_IN(LINK_IN),\n        .LINK_OUT(LINK_OUT),\n",
         "        .LINK_CLK(LINK_CLK),\n        .LINK_CLK_DIR_LV (LINK_CLK_DIR_LV), // control direction of clock through core\n        .LINK_IN(LINK_IN),\n        .LINK_OUT(LINK_OUT),\n"
         "        .link_game_so_o(link_game_so), .link_game_clk_o(link_game_clk), .link_game_clk_oe_o(link_game_clk_oe),\n"
         "        .link_mux_so_i(link_mux_so), .link_mux_clk_i(link_mux_clk), .link_mux_clk_oe_i(link_mux_clk_oe),\n"),
        ('src/top.v',
         "        .media_menu (media_menu)\n    );\n",
         "        .media_menu (media_menu),\n"
         "        .link_game_so_i (link_game_so), .link_game_clk_i (link_game_clk), .link_game_clk_oe_i (link_game_clk_oe),\n"
         "        .link_si_i (LINK_IN), .link_clk_i (LINK_CLK),\n"
         "        .link_so_o (link_mux_so), .link_clk_o (link_mux_clk), .link_clk_oe_o (link_mux_clk_oe),\n"
         "        .media_uart_fast (media_uart_fast)\n    );\n"),
        ('src/top.v',
         "        .BAUD_RATE(32'd115200), //input 32\n",
         "        // Chromatic pocket link: the media bit raises UART2 to 524,288 baud (gClk/16 exactly) for\n"
         "        // streaming; media_uart_fast is only ever 1 in keyed media link mode and falls with it,\n"
         "        // so stock firmware always meets 115,200 (docs/CHROMATIC-POCKET-TETHER.md).\n"
         "        .BAUD_RATE(media_uart_fast ? 32'd524288 : 32'd115200), //input 32\n"),
        ('src/rtl/BSP/uart/uart.v',
         "    wire  [1:0] div_correct = 'd3;\n\n    wire [23:0] divider_value = 'd72;// = CLK_FREQ/(15*BAUD_RATE);\n",
         "    // Chromatic pocket link: the stock UART2 runs divider 72 with div_correct 3 (bit counts 01, 10\n"
         "    // and 11 last one cycle longer: 72.75 gClk per bit = 115,307 baud), whatever BAUD_RATE says.\n"
         "    // The media link selects exactly gClk/16 = 524,288 baud: divider 16 AND the correction off (with\n"
         "    // the correction a divider of 16 would be 16.75 cycles, 500,812 baud; the first proposal's\n"
         "    // divider 8 with the correction was 8.75 cycles, 958,698 baud instead of the 1,048,576 meant).\n"
         "    // The 4-sample RX debouncer stays: at 16 cycles per bit its 1-5 cycles of edge skew leave the\n"
         "    // sampling point inside the bit with margin (rtl/chromatic_uart2_tb.sv measures it). A 2-way\n"
         "    // mux on the BAUD_RATE input: every other rate keeps the exact stock 'd72 / 'd3.\n"
         "    wire        fast_rate = (BAUD_RATE == 32'd524288);\n"
         "    wire  [1:0] div_correct = fast_rate ? 2'd0 : 2'd3;\n\n"
         "    wire [23:0] divider_value = fast_rate ? 24'd16 : 24'd72;// = CLK_FREQ/(15*BAUD_RATE);\n"),
        ('src/rtl/BSP/uart/uart.v',
         "            uart_rxd_debounced <= uart_rxd_shreg[0]|uart_rxd_shreg[1]|uart_rxd_shreg[2]|uart_rxd_shreg[3];\n",
         "            // Chromatic pocket link (19): at the fast rate the 4-sample OR is bypassed. It delays a falling\n"
         "            // edge by 4-5 cycles; with the receiver's own start latency that put the sample 15 of 16\n"
         "            // cycles into the bit (rtl/chromatic_uart2_tb.sv). Fast: a plain 2-flop synchroniser\n"
         "            // (uart_rxd_shreg[3] is the first flop). The ESP32 line is point to point, push-pull; the\n"
         "            // stock rate keeps the stock filter exactly.\n"
         "            uart_rxd_debounced <= fast_rate ? uart_rxd_shreg[3] :\n"
         "                                  (uart_rxd_shreg[0]|uart_rxd_shreg[1]|uart_rxd_shreg[2]|uart_rxd_shreg[3]);\n"),
        ('evt1_x2.gprj',
         '        <File path="src/rtl/BSP/chromatic_media.v" type="file.verilog" enable="1"/>\n',
         '        <File path="src/rtl/BSP/chromatic_media.v" type="file.verilog" enable="1"/>\n'
         + ''.join(f'        <File path="src/rtl/BSP/pocket_link/{name}" type="file.verilog" enable="1"/>\n' for name in RTL)),
    ]


def main():
    if not STAGED.exists():
        print('FAIL: staged stock+media tree missing (run stage_chromatic_fpga_media.py first)'); return 1
    texts, originals, crlf = {}, {}, {}
    for rel, old, new in patches():
        if rel not in texts:
            raw = (STAGED / rel).read_bytes().decode('utf-8')
            crlf[rel] = '\r\n' in raw
            texts[rel] = originals[rel] = raw.replace('\r\n', '\n')
        count = texts[rel].count(old)
        if count != 1:
            print(f'FAIL: anchor matches {count} times in {rel}:\n{old}'); return 1
        texts[rel] = texts[rel].replace(old, new)
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / 'src/rtl/BSP/pocket_link').mkdir(parents=True)
    diff, receipt = [], {'staged_tree': str(STAGED), 'inputs': {}, 'outputs': {}, 'rtl': {}}
    for rel, text in texts.items():
        out = OUT / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        data = (text.replace('\n', '\r\n') if crlf[rel] else text).encode('utf-8')
        out.write_bytes(data)
        receipt['inputs'][rel] = hashlib.sha256((STAGED / rel).read_bytes()).hexdigest()
        receipt['outputs'][rel] = hashlib.sha256(data).hexdigest()
        diff += difflib.unified_diff(originals[rel].splitlines(True), text.splitlines(True), f'a/{rel}', f'b/{rel}')
    for name in RTL:
        src = ROOT / 'rtl' / name
        shutil.copy(src, OUT / 'src/rtl/BSP/pocket_link' / name)
        receipt['rtl'][name] = hashlib.sha256(src.read_bytes()).hexdigest()
    (OUT / 'proposal.diff').write_text(''.join(diff), encoding='utf-8')
    (OUT / 'NEVER-INSTALL-PROPOSAL-ONLY.txt').write_text(
        'Review copy made by scripts/check_pocket_link_patch.py. Not a buildable tree, not staged, not built, '
        'never written to a console. See docs/CHROMATIC-POCKET-TETHER.md.\n', encoding='utf-8')
    (OUT / 'proposal-receipt.json').write_text(json.dumps(receipt, indent=1), encoding='utf-8')
    print(f'PASS: {len(patches())} pocket-link hooks match the staged stock+media tree exactly once each; '
          f'review copy and diff in {OUT.relative_to(ROOT)} (staged tree untouched, nothing built)')
    return 0


if __name__ == '__main__':
    sys.exit(main())

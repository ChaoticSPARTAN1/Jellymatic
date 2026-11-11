// SPDX-License-Identifier: GPL-3.0-or-later
// Chromatic media mode: ESP32 audio into the stock audio path, and the control bits.
// Additive to the stock FPGA (ModRetro oss-chromatic-console-fpga): stock modules and
// stock MCU message addresses keep their meaning. See docs/CHROMATIC-MEDIA-PLAN.md.
//
// ESP32 is I2S master on pins that stock wires to the FPGA but never uses:
//   I2S_WS (ESP32 IO25) = bit clock, I2S_DIN (IO26) = word select, I2S_DOUT (IO27) = data.
// Standard I2S: data MSB one bit clock after the word-select edge, WS low = left.
// 16-bit samples, 32 kHz => 1.024 MHz bit clock, sampled in gClk (8.39 MHz).

// Media control register: MCU message address 0x1D (unused by stock).
//   bit 0 media_audio   : speaker/headphones play the ESP32 stream instead of the game
//   bit 1 media_overlay : overlay may open without a cartridge (wired in top.v)
//   bit 2 media_menu    : "our screen owns the overlay". The physical Menu button no longer
//                         toggles (closes) the overlay; it is only reported to the MCU (Buttons
//                         message bit 8), so the launcher's own screens and the Jellyfin app can
//                         act on it. The MCU's own poke of the menu line (address 9 bit 8) still
//                         toggles, so the launcher can always close the overlay for games. The
//                         game CPU is stalled (gb.v cpu_hold, the same clock-enable stall the GBC
//                         HDMA uses: it lands only with no bus access in flight) while the PPU,
//                         timers, sound and DMA keep running, so the core's LCD timing, which clocks
//                         the whole stock display path (vid_system_top, the overlay line fetch,
//                         ST7785_panel_master), never stops; the first sound image paused the whole
//                         core and the panel repeated its pixel ring down the screen (see
//                         chromatic_media_display_tb.sv). chromatic_cpu_hold -> cart.v: no cartridge
//                         write strobe and no data drive once the CPU is held; the game's sound is
//                         silenced in the mux below while the bit is set.
// Cleared by FPGA reset and by any stock SysCtrl message (address 4), which stock
// firmware always sends at start, so stock can never inherit media mode.
module chromatic_media_ctrl (
    input  wire        clk,
    input  wire        reset_n,
    input  wire        rx_data_val,
    input  wire [6:0]  rx_address,
    input  wire [63:0] rx_data,
    output reg         media_audio,
    output reg         media_overlay,
    output reg         media_menu
);
    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            media_audio <= 1'b0;
            media_overlay <= 1'b0;
            media_menu <= 1'b0;
        end else if (rx_data_val) begin
            if (rx_address == 7'h1D) begin
                media_audio <= rx_data[0];
                media_overlay <= rx_data[1];
                media_menu <= rx_data[2];
            end else if (rx_address == 7'd4) begin
                media_audio <= 1'b0;
                media_overlay <= 1'b0;
                media_menu <= 1'b0;
            end
        end
    end
endmodule

module chromatic_i2s_rx (
    input  wire        clk,          // gClk
    input  wire        reset_n,
    input  wire        i2s_bclk,     // asynchronous inputs from the ESP32
    input  wire        i2s_ws,
    input  wire        i2s_sd,
    output reg  [15:0] left,
    output reg  [15:0] right,
    output reg         frame         // one-cycle pulse when a stereo pair completes
);
    // Two-flop synchronisers; edges are detected on the synchronised bit clock.
    reg [2:0] bclk_s;
    reg [1:0] ws_s, sd_s;
    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            bclk_s <= 3'b000; ws_s <= 2'b00; sd_s <= 2'b00;
        end else begin
            bclk_s <= {bclk_s[1:0], i2s_bclk};
            ws_s <= {ws_s[0], i2s_ws};
            sd_s <= {sd_s[0], i2s_sd};
        end
    end
    wire rise = bclk_s[1] & ~bclk_s[2];

    // I2S: word select changes one bit clock before the next word's MSB, so the edge on
    // which the change is first seen still carries the previous word's last bit. Every
    // edge therefore shifts first (while fewer than 16 bits are held), then, if word
    // select changed, closes the word. 16-bit slots end exactly there; wider slots
    // (ESP32's 32-bit slots) were complete after 16 bits and their padding is ignored.
    reg        ws_prev;
    reg [4:0]  bits;      // bits taken in the current word, saturating at 16
    reg [15:0] shift;
    reg        have_left;
    reg [15:0] left_pending;  // the left word waits here so left and right change together at `frame`
    wire [15:0] next_shift = (bits < 5'd16) ? {shift[14:0], sd_s[1]} : shift;
    wire [4:0]  next_bits  = (bits < 5'd16) ? bits + 5'd1 : bits;
    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            ws_prev <= 1'b0; bits <= 5'd0; shift <= 16'd0; have_left <= 1'b0; left_pending <= 16'd0;
            left <= 16'd0; right <= 16'd0; frame <= 1'b0;
        end else begin
            frame <= 1'b0;
            if (rise) begin
                if (ws_s[1] != ws_prev) begin
                    if (next_bits == 5'd16) begin   // a whole word, not a partial first one
                        if (ws_prev == 1'b0) begin
                            left_pending <= next_shift;
                            have_left <= 1'b1;
                        end else if (have_left) begin
                            left <= left_pending;   // the pair appears at once: no half-updated mono mix
                            right <= next_shift;
                            frame <= 1'b1;
                            have_left <= 1'b0;
                        end
                    end
                    ws_prev <= ws_s[1];
                    bits <= 5'd0;
                    shift <= 16'd0;
                end else begin
                    shift <= next_shift;
                    bits <= next_bits;
                end
            end
        end
    end
endmodule

// Chooses what the speaker plays. In media mode the ESP32 stream, otherwise the game,
// exactly as stock. If the stream stops for ~100 ms, media mode plays silence. While our
// screen owns the overlay (media_menu) without media sound, the paused game is silenced
// too: its last sample would otherwise sit on the codec as a DC level.
module chromatic_audio_mux (
    input  wire        clk,          // gClk, 8.388 MHz
    input  wire        reset_n,
    input  wire        media_audio,
    input  wire        media_menu,
    input  wire [15:0] game_left,
    input  wire [15:0] game_right,
    input  wire [15:0] media_left,
    input  wire [15:0] media_right,
    input  wire        media_frame,
    output wire [15:0] left,
    output wire [15:0] right
);
    localparam [19:0] STALE = 20'd838860;  // 100 ms at 8.388 MHz
    reg [19:0] age;
    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) age <= STALE;
        else if (media_frame) age <= 20'd0;
        else if (age != STALE) age <= age + 20'd1;
    end
    wire live = age != STALE;
    assign left  = media_audio ? (live ? media_left  : 16'd0) : (media_menu ? 16'd0 : game_left);
    assign right = media_audio ? (live ? media_right : 16'd0) : (media_menu ? 16'd0 : game_right);
endmodule

// Tells cart.v when the game CPU is really held. gb.v raises cpu_held while cpu_hold is set and
// the CPU sits at T4 of an opcode fetch with rd_n and wr_n high (TSTATEo == 4 & ~WRITE_CYCLE &
// cpu_rd_n & cpu_wr_n): GBse drives RD_n/WR_n low in T2+T3 only, so "no strobe active" alone
// could also freeze T1 of a write cycle, where cart.v's whole-cycle wr is already up and T80's
// DO still holds the previous byte; T4 exists only in M1, whose read ended in T3, so the frozen
// bus is stock's HALT idle (cart.v resyncs its sequencer on TSTATEo == 4 every ce: RD low, CS
// high, WR high, no strobe in flight). With its clock enable gated there the CPU keeps that
// state, so cpu_held stays up until cpu_hold drops. The 32-hclk wait before `hold` is margin
// only: cart.v's sequencer (WR low at step 8, high at step 14 of 16) has long finished the
// previous cycle's write by then; the gate itself (WR released, data never driven, CS high
// unless a DMA/HDMA cartridge read is on) is belt-and-braces. Released the cycle cpu_held drops
// (media_pause cleared): the CPU takes T1 of the next M-cycle on its next clock enable, the
// sequencer is in its resynced phase, no reset anywhere. The core's ce never stops.
module chromatic_cpu_hold (
    input  wire clk,          // hclk
    input  wire reset_n,
    input  wire cpu_held,
    output reg  hold
);
    reg [5:0] quiet;
    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            quiet <= 6'd0;
            hold <= 1'b0;
        end else begin
            if (!cpu_held) quiet <= 6'd0;
            else if (quiet != 6'd63) quiet <= quiet + 6'd1;
            hold <= cpu_held & (quiet >= 6'd32);
        end
    end
endmodule

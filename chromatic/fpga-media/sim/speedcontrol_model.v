// SPDX-License-Identifier: GPL-3.0-only
// Origin: a translation into Verilog of speedcontrol.vhd from ModRetro's Chromatic FPGA design
// (https://github.com/ModRetro/oss-chromatic-console-fpga, v18.14, esp32t/src/rtl/EMU/CORE/speedcontrol.vhd),
// which comes from the MiSTer core (Gameboy_MiSTer, rtl/speedcontrol.vhd) by its authors. ModRetro
// publishes it under its repository's GPL-3.0 (LICENSE); this derivative keeps that licence. Translated by
// ChaoticSPARTAN1 for Jellymatic, 2026-11. ADDITIONAL-TERMS.md does NOT apply to this file.
// Simulation-only Verilog translation of the stock src/rtl/EMU/CORE/speedcontrol.vhd
// (MiSTer lineage): Icarus has no VHDL, so the
// cartridge-bus bench (chromatic_media_cart_tb.sv) needs this model of how the core's clock
// enables stop and restart when `pause` is asserted. Same states, same falling-edge process,
// same counts: NORMAL issues ce every 4 clk_sys (clkdiv 2 bits) and ce_2x every 2; PAUSED is
// entered only when clkdiv = 3 and cart_act = 0 (no cartridge access in flight); leaving it
// takes 16 cycles (unpause_cnt 0..15) after pause drops; no reset is involved. Fast forward
// and RAMACCESS are kept for completeness; the bench never asserts speedup. The built design
// uses the VHDL original, never this file.
`timescale 1ns/1ps
module speedcontrol (
    input  wire clk_sys,
    input  wire pause,
    input  wire speedup,
    input  wire cart_act,
    input  wire DMA_on,
    output reg  ce = 1'b0,
    output reg  ce_2x = 1'b0,
    output reg  refresh = 1'b0,
    output reg  ff_on = 1'b0
);
    localparam NORMAL = 0, PAUSED = 1, FASTFORWARDSTART = 2, FASTFORWARD = 3, FASTFORWARDEND = 4, RAMACCESS = 5;
    reg [1:0] clkdiv = 2'd0;
    reg cart_act_1 = 1'b0;
    integer unpause_cnt = 0, fastforward_cnt = 0, refreshcnt = 0, sdram_busy = 0;
    integer state = NORMAL;

    always @(negedge clk_sys) begin
        ce <= 1'b0;
        ce_2x <= 1'b0;
        refresh <= 1'b0;
        cart_act_1 <= cart_act;
        if (refreshcnt > 0) refreshcnt <= refreshcnt - 1;
        case (state)
            NORMAL: begin
                if (pause && clkdiv == 2'b11 && !cart_act) begin
                    state <= PAUSED;
                    unpause_cnt <= 0;
                end else if (speedup && !pause && !DMA_on && clkdiv == 2'b00) begin
                    state <= FASTFORWARDSTART;
                    fastforward_cnt <= 0;
                end else begin
                    clkdiv <= clkdiv + 2'd1;
                    if (clkdiv == 2'b00) ce <= 1'b1;
                    if (clkdiv[0] == 1'b0) ce_2x <= 1'b1;
                end
            end
            PAUSED: begin
                if (unpause_cnt == 0) refresh <= 1'b1;
                if (!pause) begin
                    if (unpause_cnt == 15) state <= NORMAL;
                    else unpause_cnt <= unpause_cnt + 1;
                end
            end
            FASTFORWARDSTART: begin
                if (fastforward_cnt == 15) begin state <= FASTFORWARD; ff_on <= 1'b1; end
                else fastforward_cnt <= fastforward_cnt + 1;
            end
            FASTFORWARD: begin
                if (pause || !speedup || DMA_on) begin
                    state <= FASTFORWARDEND;
                    fastforward_cnt <= 0;
                    if (clkdiv[0]) clkdiv <= 2'b10;
                end else if (cart_act && !cart_act_1) begin
                    state <= RAMACCESS; sdram_busy <= 1;
                end else if (!cart_act && refreshcnt == 0) begin
                    refreshcnt <= 127; refresh <= 1'b1; state <= RAMACCESS; sdram_busy <= 1;
                end else begin
                    clkdiv[0] <= ~clkdiv[0];
                    if (clkdiv[0] == 1'b0) ce <= 1'b1;
                    ce_2x <= 1'b1;
                end
            end
            FASTFORWARDEND: begin
                if (fastforward_cnt == 15) begin state <= NORMAL; ff_on <= 1'b0; end
                else fastforward_cnt <= fastforward_cnt + 1;
            end
            RAMACCESS: begin
                if (sdram_busy > 0) sdram_busy <= sdram_busy - 1;
                else state <= FASTFORWARD;
            end
        endcase
    end
endmodule

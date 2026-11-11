// SPDX-License-Identifier: GPL-3.0-or-later
// Additional terms: ADDITIONAL-TERMS.md
// The cartridge bus while our screen holds the game (staged design, docs/Design-notes.md):
// the STAGED src/rtl/EMU/cart.v (the only module that drives the cartridge strobes), the new
// chromatic_cpu_hold (chromatic_media.v), a Verilog model of the stock speedcontrol
// (sim/speedcontrol_model.v) and a T-state-accurate model of the CPU as the staged gb.v sees it
// (the GB CPU is VHDL, which Icarus cannot run): machine cycles of 4 T-states (cart.v: "with ~16 MHz
// we have 16 cycles per low speed cycle", one clock enable per T-state), GBse (T2Write = 2) registers
// RD_n/WR_n low after the T1 and T2 edges only (low in T2+T3, GBse.vhd), Write (gb.v WRITE_CYCLE,
// cart.v's wr) is up for the whole write M-cycle, T80 loads DO at the edge ending T1 (T80.vhd), and
// the CPU samples DI at the edge ending T3. The hold is gb.v's: cpu_clken = ce & ~(cpu_hold & rd_n &
// wr_n & TSTATEo == 4 & ~WRITE_CYCLE), so the stall lands only at T4 of a cycle that is not a write
// (T4 of a write cycle is excluded because cart.v's strobe is still in flight there).
// A second cart instance with hold = 0 is the stock reference. Proven here, with the hold asked at
// 2000 random phases (the probe that found the T1 hazard of the first version, which stalled on
// rd_n & wr_n alone: 338 stale-data strobes, 169 doubled writes, 30 spurious strobes, 52 bad CRAM
// reads in the same 2000 trials):
//   1. with media_pause clear the staged cart is identical to the reference on every hclk (stock);
//   2. every stall lands at T4 of a non-write cycle (never in a T1 or in a write cycle); hold follows 32 hclk later;
//      every write cycle gets exactly one WR strobe with the cycle's own data at the cycle's own
//      address, before and after any hold; no strobe outside a write cycle; every CRAM read has
//      chip select low for the whole access; the clock enable KEEPS RUNNING while held (the core's
//      PPU and the LCD timing of the whole display path depend on it);
//   3. while held: 0 WR strobes, data never driven, CART_CS high (also in colour-mode double speed, where
//      the sequencer would otherwise re-drive nCS of a frozen A000-DFFF address every 2 hclk), the
//      CPU model issues no machine cycle, even with the stalled core's wr forced to 1; a DMA/HDMA
//      cartridge read (DMA_on) keeps its chip select;
//   4. a hold requested during a long cartridge access is not granted until it ends;
//   5. media_pause cleared: hold drops the next cycle, the CPU resumes on its next clock enable, no
//      reset is involved, and the staged cart is identical to the reference again.
// Compile with -DDOUBLE_SPEED for the colour-mode double-speed (8 MHz) bus sequencer. No hardware; Icarus only.
`timescale 1ns/1ps
module chromatic_media_cart_tb;
    reg hclk = 0, pclk = 0;
    always #30 hclk = ~hclk;   // ~16.7 MHz
    always #15 pclk = ~pclk;   // ~33 MHz
    reg reset_n = 0, gbreset = 1;
`ifdef DOUBLE_SPEED
    localparam SPEED = 1'b1;
`else
    localparam SPEED = 1'b0;
`endif

    // ---- the clock enables: stock speedcontrol (Verilog model), never paused in the new design ----
    reg media_pause = 0;
    wire ce, ce_2x;
    speedcontrol u_speed (.clk_sys(hclk), .pause(1'b0), .speedup(1'b0), .cart_act(1'b0), .DMA_on(1'b0),
                          .ce(ce), .ce_2x(ce_2x), .refresh(), .ff_on());
    wire ce_cpu = SPEED ? ce_2x : ce;

    // ---- the CPU as gb.v sees it ----
    reg [2:0] tstate = 3'd4, tstates = 3'd4;
    reg [15:0] a = 16'h0150;
    reg [7:0] DO = 8'h00;        // T80 DO register
    reg Write = 0, NoRead = 0;   // MCode outputs for the current M-cycle (stable all cycle)
    reg rd_n = 1, wr_n = 1;      // GBse registered strobes
    reg [7:0] cyc_data = 8'h00;  // what this write cycle must put on the bus
    integer kind = 1;            // 0 internal, 1 ROM read (M1 or operand), 2 CRAM read, 3 MBC write, 4 CRAM write
    integer operands = 0;        // memory cycles left in this instruction
    reg force_wr = 0;            // case 3: pretend the stalled core says "write"
    reg long_access = 0;         // case 4: one very long cartridge read (rd_n held low)
    reg dma_on = 0, dma_cs = 0;  // case 3b: a DMA/HDMA cartridge read while held
    wire hold_stop = media_pause & rd_n & wr_n & (tstate == 3'd4) & ~Write;  // gb.v hold_cpu_stop
    wire cpu_clken = ce_cpu & ~hold_stop;
    wire cpu_held = hold_stop;
    integer cycle = 0, seed = 7, r;
    always @(posedge hclk) if (cpu_clken && !long_access) begin
        rd_n <= 1'b1; wr_n <= 1'b1;
        if ((tstate == 3'd1 || tstate == 3'd2) && !NoRead && !Write) rd_n <= 1'b0;
        if ((tstate == 3'd1 || tstate == 3'd2) && Write) wr_n <= 1'b0;
        if (tstate == 3'd1) DO <= cyc_data;
        if (tstate == tstates) begin
            tstate <= 3'd1; cycle <= cycle + 1;
            if (operands == 0) begin                      // next instruction: M1, an opcode fetch from ROM
                kind = 1; tstates <= 3'd4; a <= 16'h0150 + cycle[11:0]; Write <= 0; NoRead <= 0;
                operands = $urandom(seed) % 3;
            end else begin                                // an operand cycle
                operands = operands - 1; tstates <= 3'd4;
                r = $urandom(seed) % 10;
                kind = (r < 2) ? 0 : (r < 4) ? 1 : (r < 6) ? 2 : (r < 8) ? 3 : 4;
                case (kind)
                    0: begin a <= 16'hC000 + cycle[7:0]; Write <= 0; NoRead <= 1; end
                    1: begin a <= 16'h0400 + cycle[11:0]; Write <= 0; NoRead <= 0; end
                    2: begin a <= 16'hA000 + cycle[11:0]; Write <= 0; NoRead <= 0; end
                    3: begin a <= 16'h2000 + cycle[7:0]; Write <= 1; NoRead <= 1; end
                    4: begin a <= 16'hA100 + cycle[11:0]; Write <= 1; NoRead <= 1; end
                endcase
            end
            cyc_data <= 8'h10 + cycle[7:0];
        end else tstate <= tstate + 3'd1;
    end
    wire cart_sel = ~a[15] | (a[15:13] == 3'b101);
    wire nCS = dma_on ? ~dma_cs : ~(a[15:13] == 3'b101);
    wire cart_wr = (cart_sel & Write) | force_wr;        // gb.v: cart_wr = cart_sel & WRITE_CYCLE
    wire cart_rd = (cart_sel & ~rd_n) | long_access;

    // ---- the hold gate (new, chromatic_media.v) ----
    wire hold;
    chromatic_cpu_hold u_hold (.clk(hclk), .reset_n(reset_n), .cpu_held(cpu_held), .hold(hold));

    // ---- staged cart with the gate, and the reference with hold tied low ----
    wire [15:0] A_d, A_r; wire CLK_d, CLK_r, CS_d, CS_r, RD_d, RD_r, WR_d, WR_r, DIR_d, DIR_r;
    wire [7:0] DO_d, DO_r, DIN_d, DIN_r;
    cart dut (.hclk(hclk), .pclk(pclk), .ce(ce), .ce_2x(ce_2x), .gbreset(gbreset), .cpu_speed(SPEED), .cpu_halt(1'b1),
              .cpu_stop(1'b0), .DMA_on(dma_on), .hdma_active(1'b0), .wr(cart_wr), .rd(cart_rd), .a(a), .CART_DOUT(DO), .nCS(nCS),
              .TSTATEo(tstate), .CART_A(A_d), .CART_CLK(CLK_d), .CART_CS(CS_d), .CART_D_IN(8'h5a), .CART_D_OUT(DO_d),
              .CART_RD(RD_d), .CART_WR(WR_d), .CART_DATA_DIR_E(DIR_d), .CART_DIN_r1(DIN_d), .hold(hold));
    cart stock (.hclk(hclk), .pclk(pclk), .ce(ce), .ce_2x(ce_2x), .gbreset(gbreset), .cpu_speed(SPEED), .cpu_halt(1'b1),
              .cpu_stop(1'b0), .DMA_on(dma_on), .hdma_active(1'b0), .wr(cart_wr), .rd(cart_rd), .a(a), .CART_DOUT(DO), .nCS(nCS),
              .TSTATEo(tstate), .CART_A(A_r), .CART_CLK(CLK_r), .CART_CS(CS_r), .CART_D_IN(8'h5a), .CART_D_OUT(DO_r),
              .CART_RD(RD_r), .CART_WR(WR_r), .CART_DATA_DIR_E(DIR_r), .CART_DIN_r1(DIN_r), .hold(1'b0));

    // ---- observers ----
    reg WR_d_1 = 1;
    reg cycle_seen = 0;   // the current M-cycle was observed from its start
    integer cs_low_run = 0;
    integer stale = 0, spurious = 0, lost = 0, dup = 0, bad_reads = 0, strobes_in_cycle = 0, write_cycles = 0, read_cycles = 0;
    integer wrong_addr = 0;
    reg observing = 0;
    reg [15:0] last_bad_a; reg [7:0] last_bad_d, last_exp_d;
    always @(posedge hclk) begin
        WR_d_1 <= WR_d;
        cs_low_run <= CS_d ? 0 : cs_low_run + 1;
        if (observing) begin
            if (!WR_d_1 && WR_d) begin  // a write strobe ends: the cartridge latches CART_A / CART_D_OUT now
                if (!Write || !cart_sel) begin spurious = spurious + 1; last_bad_a = A_d; last_bad_d = DO_d; end
                else begin
                    strobes_in_cycle = strobes_in_cycle + 1;
                    if (A_d !== a) wrong_addr = wrong_addr + 1;
                    if (DO_d !== cyc_data) begin stale = stale + 1; last_bad_a = A_d; last_bad_d = DO_d; last_exp_d = cyc_data; end
                end
            end
            if (cpu_clken && tstate == 3'd3 && kind == 2) begin   // the CPU samples DI at the edge ending T3
                read_cycles = read_cycles + 1;
                if (cs_low_run < 3) bad_reads = bad_reads + 1;
            end
            if (cpu_clken && tstate == tstates) begin               // M-cycle ends: account its write strobes
                if (Write && cart_sel && cycle_seen) begin            // only cycles observed from their start
                    write_cycles = write_cycles + 1;
                    if (strobes_in_cycle == 0) begin lost = lost + 1; $display("lost strobe: t=%0t kind=%0d held=%b hold=%b pause=%b a=%h", $time, kind, cpu_held, hold, media_pause, a); end
                    else if (strobes_in_cycle > 1) dup = dup + 1;
                end
                strobes_in_cycle = 0; cycle_seen = 1;
            end
        end else cycle_seen = 0;
    end
    // window counters for the held state and stock comparison
    integer wr_falls_d = 0, wr_falls_r = 0, ce_pulses = 0, cycles_issued = 0, differ = 0, identical = 0, dir_driven_d = 0,
            cs_low_d = 0, a_changes_d = 0, cycle_1 = 0;
    reg window = 0, compare = 0; reg WR_r_1 = 1; reg [15:0] A_d_1 = 0;
    always @(posedge hclk) begin
        WR_r_1 <= WR_r; A_d_1 <= A_d; cycle_1 <= cycle;
        if (window) begin
            if (WR_d_1 && !WR_d) wr_falls_d = wr_falls_d + 1;
            if (WR_r_1 && !WR_r) wr_falls_r = wr_falls_r + 1;
            if (!DIR_d) dir_driven_d = dir_driven_d + 1;
            if (!CS_d) cs_low_d = cs_low_d + 1;
            if (A_d !== A_d_1) a_changes_d = a_changes_d + 1;
            if (ce_cpu) ce_pulses = ce_pulses + 1;
            if (cycle != cycle_1) cycles_issued = cycles_issued + 1;
        end
        if (compare) begin
            if ({A_d, CLK_d, CS_d, RD_d, WR_d, DIR_d, DO_d} !== {A_r, CLK_r, CS_r, RD_r, WR_r, DIR_r, DO_r}) differ = differ + 1;
            else identical = identical + 1;
        end
    end
    task clear_counts; begin
        wr_falls_d = 0; wr_falls_r = 0; ce_pulses = 0; cycles_issued = 0; differ = 0; identical = 0; dir_driven_d = 0;
        cs_low_d = 0; a_changes_d = 0;
    end endtask

    integer errors = 0, trial, w, nhold, t0, t_held;
    integer freeze_t4m1 = 0, freeze_other = 0;
    initial begin
        repeat (8) @(negedge hclk); reset_n = 1; gbreset = 0;
        repeat (3000) @(negedge hclk);
        // 1. Stock: identical strobes, every write cycle one good strobe, CRAM reads with CS low.
        clear_counts; window = 1; compare = 1; observing = 1;
        repeat (20000) @(negedge hclk);
        window = 0; compare = 0; observing = 0;
        if (differ != 0 || wr_falls_d == 0 || wr_falls_d != wr_falls_r || hold !== 1'b0)
            begin errors = errors + 1; $display("FAIL stock: differ=%0d WR d/r=%0d/%0d hold=%b", differ, wr_falls_d, wr_falls_r, hold); end
        if (write_cycles == 0 || lost || dup || stale || spurious || wrong_addr || read_cycles == 0 || bad_reads)
            begin errors = errors + 1; $display("FAIL stock strobes: writes=%0d lost=%0d dup=%0d stale=%0d spurious=%0d addr=%0d cram=%0d bad=%0d",
                                                write_cycles, lost, dup, stale, spurious, wrong_addr, read_cycles, bad_reads); end
        write_cycles = 0; lost = 0; dup = 0; stale = 0; spurious = 0; wrong_addr = 0; read_cycles = 0; bad_reads = 0; strobes_in_cycle = 0;
        cycle_seen = 0;   // the cycle in progress at this boundary is not accounted

        // 2. 2000 holds at random phases.
        observing = 1;
        for (trial = 0; trial < 2000; trial = trial + 1) begin
            w = $urandom(seed) % 97;
            repeat (40 + w) @(negedge hclk);
            media_pause = 1;
            @(posedge cpu_held);
            if (tstate == 3'd4 && !Write && rd_n && wr_n) freeze_t4m1 = freeze_t4m1 + 1; else freeze_other = freeze_other + 1;
            nhold = 100 + ($urandom(seed) % 300);
            repeat (nhold) @(negedge hclk);
            media_pause = 0;
            repeat (200) @(negedge hclk);
        end
        observing = 0;
        if (freeze_other != 0) begin errors = errors + 1; $display("FAIL: %0d stalls not at T4 of a non-write cycle", freeze_other); end
        if (lost || dup || stale || spurious || wrong_addr || bad_reads || write_cycles < 1000 || read_cycles < 500)
            begin errors = errors + 1; $display("FAIL holds: writes=%0d lost=%0d dup=%0d stale=%0d spurious=%0d addr=%0d cram=%0d bad=%0d (last A=%h D=%h exp %h)",
                                                write_cycles, lost, dup, stale, spurious, wrong_addr, read_cycles, bad_reads, last_bad_a, last_bad_d, last_exp_d); end

        // 3. One long hold, looked at closely: hold 32 hclk after the stall; ce runs; nothing on the bus.
        wait (Write && tstate == 3'd2); @(negedge hclk);
        media_pause = 1; t0 = $time;
        wait (cpu_held); t_held = $time;
        wait (hold);
        if (($time - t_held) < 60 * 32 || ($time - t_held) > 60 * 36) begin errors = errors + 1; $display("FAIL hold %0d ns after the stall, expected 32 hclk", $time - t_held); end
        if (($time - t0) > 60 * 400) begin errors = errors + 1; $display("FAIL hold entry took %0d ns", $time - t0); end
        repeat (4) @(negedge hclk);
        clear_counts; window = 1;
        repeat (200000) @(negedge hclk);           // 12 ms held
        force_wr = 1; repeat (2000) @(negedge hclk); force_wr = 0; repeat (100) @(negedge hclk);
        window = 0;
        if (ce_pulses < 200000 / 10) begin errors = errors + 1; $display("FAIL held: the clock enable stopped (%0d pulses)", ce_pulses); end
        if (cycles_issued != 0) begin errors = errors + 1; $display("FAIL held: the stalled CPU issued %0d machine cycles", cycles_issued); end
        if (wr_falls_d != 0 || dir_driven_d != 0 || a_changes_d != 0 || cs_low_d != 0)
            begin errors = errors + 1; $display("FAIL held: WR=%0d data driven=%0d A changes=%0d CS low=%0d", wr_falls_d, dir_driven_d, a_changes_d, cs_low_d); end
        // Stalled at T4 the stock sequencer itself never reaches its WR step (it resyncs on T4 every clock
        // enable), so even the reference strobes nothing with wr forced: the gate is belt-and-braces.
        if (wr_falls_r != 0) begin errors = errors + 1; $display("FAIL held: the reference sequencer strobed WR %0d times at T4", wr_falls_r); end
        // 3b. a DMA/HDMA read of cartridge RAM while held: the gate does not force chip select then. In colour-mode
        // double speed the sequencer drives nCS at its step 1 and the read gets its chip select; at normal
        // speed the stock sequencer, resynced at T4 every clock enable, never reaches its step 4 (stock's
        // own behaviour during a T4 stall, HDMA included), so chip select stays high either way.
        dma_on = 1; dma_cs = 1; clear_counts; window = 1; repeat (200) @(negedge hclk); window = 0; dma_on = 0; dma_cs = 0;
        if (SPEED && cs_low_d == 0) begin errors = errors + 1; $display("FAIL held: a DMA cartridge read lost its chip select"); end
        if (!SPEED && cs_low_d != 0) begin errors = errors + 1; $display("FAIL held: chip select driven low at normal speed (%0d)", cs_low_d); end
        repeat (40) @(negedge hclk);
        if (CS_d !== 1'b1) begin errors = errors + 1; $display("FAIL held: chip select not high again after the DMA read"); end

        // 5. Hand-over: hold drops, the CPU resumes on its next clock enable, no reset, identical again.
        media_pause = 0;
        @(negedge hclk); @(negedge hclk);
        if (hold !== 1'b0) begin errors = errors + 1; $display("FAIL release: hold still set"); end
        t0 = $time; wait (cpu_clken);
        if (($time - t0) > 60 * 6) begin errors = errors + 1; $display("FAIL release: first CPU clock enable %0d ns after release", $time - t0); end
        if (gbreset !== 1'b0) begin errors = errors + 1; $display("FAIL release: reset"); end
        repeat (64) @(negedge hclk);
        clear_counts; window = 1; compare = 1;
        repeat (4000) @(negedge hclk);
        window = 0; compare = 0;
        if (differ != 0 || wr_falls_d == 0 || wr_falls_d != wr_falls_r)
            begin errors = errors + 1; $display("FAIL resumed: differ=%0d WR d/r=%0d/%0d", differ, wr_falls_d, wr_falls_r); end

        // 4. Hold requested during a long cartridge read: not granted until it ends; nothing cut short.
        wait (kind == 1 && tstate == 3'd1); @(negedge hclk);
        long_access = 1; rd_n = 0; media_pause = 1;
        clear_counts; window = 1;
        repeat (600) @(negedge hclk);
        if (hold !== 1'b0 || cpu_held !== 1'b0 || ce_pulses == 0) begin errors = errors + 1; $display("FAIL long access: hold=%b held=%b ce=%0d", hold, cpu_held, ce_pulses); end
        long_access = 0;
        wait (hold); window = 0;
        if (WR_d !== 1'b1 || DIR_d !== 1'b1 || CS_d !== 1'b1) begin errors = errors + 1; $display("FAIL after the long access: bus not released"); end
        media_pause = 0; repeat (200) @(negedge hclk);

        if (errors == 0)
            $display("PASS: staged cart.v with the CPU hold (%s): stock identical to reference over 20000 hclk; 2000 holds at random phases all stalled at T4 of a non-write cycle, %0d write cycles each exactly one strobe with its own data and address, 0 spurious strobes, %0d CRAM reads all with chip select; held 12 ms with 0 WR, data never driven, CS high, address constant, 0 machine cycles and the clock enable running (%0d pulses), even with a forced write; DMA read keeps CS; a long access delays the stall; release: hold off next cycle, CPU back on its next enable, no reset, identical again.",
                     SPEED ? "double speed" : "normal speed", write_cycles, read_cycles, ce_pulses);
        else $display("FAIL: %0d errors", errors);
        $finish;
    end
endmodule

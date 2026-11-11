// SPDX-License-Identifier: GPL-3.0-or-later
// Additional terms: ADDITIONAL-TERMS.md
// Why the first sound image showed the start-up menu scrambled (console run): the
// stock display path is clocked by the GAME CORE's LCD timing. ST7785_panel_master (stock, the only
// driver of the panel) writes the core's pixels into a 1024-pixel ring (6.4 lines of 160) and scans
// the panel from its own free-running counters; the ring read pointer is reset ONLY by the core's
// vsync, and the panel frame is re-aligned ONLY by that vsync. mm_burst_read_to_stream (the overlay
// line fetch) and vid_system_top likewise run on gb_lcd_mode / gb_lcd_vsync / gb_lcd_clkena.
// The first sound image paused the whole core (speedcontrol pause: no clock enable) while our screen
// was up, so vsync, hsync and the pixel stream stopped: the panel kept scanning and re-read the ring
// for ever. 1024 = 6 x 160 + 64, so every 6.4 panel rows the same 6.4 lines come round again shifted
// by 64 pixels: the last lines drawn before the pause ("Start with", the header) repeated down the
// screen in a sheared diagonal, exactly what the owner saw. The menu was right for the frames before
// the launcher sent the media bits.
//
// This bench drives the STAGED stock ST7785_panel_master with a model of the core's LCD timing whose
// pixels carry their own (line, x); it checks the panel is right while the timing runs, shows the
// sheared repetition when the timing freezes (the old pause), and the recovery when it runs again:
// the fixed design never stops the core's clock enables (gb.v cpu_hold stalls the CPU only; the PPU,
// and so this timing, keep running). Internal probes read what the panel outputs. Icarus only.
`timescale 1ns/1ps
module chromatic_media_display_tb;
    reg hClk = 0; always #30 hClk = ~hClk;     // ~16.7 MHz
    reg gClk = 0; always #60 gClk = ~gClk;     // ~8.4 MHz (the panel's clock)
    reg nRST = 0;

    // ---- the core's LCD timing, as video.v gives it: a line of 456 dots (a dot = 4 hClk here),
    //      mode[1] high for modes 2+3 (dots 0..251), 160 single-cycle pixel enables in mode 3,
    //      144 lines then 10 blank lines, vsync high during line 0. `run` low = the old pause:
    //      no clock enable, so every signal freezes where it is.
    reg run = 1;
    integer line = 150, dot = 0;
    reg hVsync = 0, hHsync = 0, hValid = 0;
    reg [17:0] pix = 0;
    localparam DOT_HCLK = 4, DOTS = 456, PIX0 = 92;
    integer sub = 0;
    always @(posedge hClk) begin
        hValid <= 1'b0;
        if (run) begin
            if (sub == DOT_HCLK - 1) begin
                sub <= 0;
                if (dot == DOTS - 1) begin
                    dot <= 0;
                    line <= (line == 153) ? 0 : line + 1;
                end else dot <= dot + 1;
            end else sub <= sub + 1;
            if (sub == 0 && line < 144 && dot >= PIX0 && dot < PIX0 + 160) begin
                hValid <= 1'b1;
                pix <= {2'b00, line[7:0], dot[7:0] - PIX0[7:0]};
            end
            hHsync <= (line < 144) && (dot < 252);
            hVsync <= (line == 0);
        end
    end

    // ---- the staged stock panel master ----
    wire LCD_DE, LCD_HSYNC, LCD_VSYNC, LCD_GENLOCK; wire [5:0] LCD_DB;
    ST7785_panel_master dut (.appear_off(1'b0), .gClk(gClk), .nRST(nRST), .hClk(hClk), .hColorPixel(pix), .lcd_on(1'b1),
                             .hVsync(hVsync), .hHsync(hHsync), .hValid(hValid), .LCD_EN(1'b1), .LCD_DE(LCD_DE),
                             .LCD_HSYNC(LCD_HSYNC), .LCD_VSYNC(LCD_VSYNC), .LCD_GENLOCK(LCD_GENLOCK), .LCD_DB(LCD_DB));

    // ---- what the panel shows: the pixel the master puts on LCD_DB (phase 2 = its blue bits) for
    //      each of the first 160 columns of each visible panel row ----
    reg [17:0] panel [0:143][0:159];
    integer col = 0, prow = -1, rows_seen = 0;
    reg capture = 0;
    always @(posedge gClk) begin
        if (dut.hoffset >= 41 && dut.lcd_on_aligned && dut.phase == 2 && dut.LCD_DEI && dut.V_PixelCount >= 4) begin
            if (dut.V_PixelCount - 4 != prow) begin prow = dut.V_PixelCount - 4; col = 0; if (capture) rows_seen = rows_seen + 1; end
            if (capture && prow < 144 && col < 160) panel[prow][col] = dut.color_pixelp;
            col = col + 1;
        end
    end
    task wait_frames(input integer n); integer i; begin
        for (i = 0; i < n; i = i + 1) begin @(posedge LCD_VSYNC); end
    end endtask
    task capture_frame; begin
        @(posedge LCD_VSYNC); capture = 1; rows_seen = 0; @(posedge LCD_VSYNC); capture = 0;
    end endtask

    integer errors = 0, r, c, right, wrong, same, total, distinct;
    reg [7:0] lines_shown [0:255];
    integer s, s2, r2, c2;
    initial begin
        repeat (20) @(negedge gClk); nRST = 1;
        // 1. Running: after the panel has aligned to the core's vsync, every panel row is its source line.
        wait_frames(4);
        capture_frame;
        right = 0; wrong = 0;
        for (r = 0; r < 144; r = r + 1) for (c = 0; c < 160; c = c + 1)
            if (panel[r][c] === {2'b00, r[7:0], c[7:0]}) right = right + 1; else wrong = wrong + 1;
        if (wrong != 0 || rows_seen < 144) begin errors = errors + 1; $display("FAIL running: %0d right %0d wrong rows=%0d (e.g. row 10 col 5 = %h)", right, wrong, rows_seen, panel[10][5]); end

        // 2. The old pause: the core's timing freezes in the middle of a frame (line 20, as if the
        //    launcher's media bits arrived while the header was being drawn). The panel keeps scanning.
        wait (line == 20 && dot == 300); run = 0;
        wait_frames(3);                    // three panel frames with no core vsync
        capture_frame;
        right = 0; wrong = 0; same = 0; total = 0; distinct = 0;
        for (r = 0; r < 256; r = r + 1) lines_shown[r] = 0;
        for (r = 0; r < 144; r = r + 1) for (c = 0; c < 160; c = c + 1) begin
            if (panel[r][c] === {2'b00, r[7:0], c[7:0]}) right = right + 1; else wrong = wrong + 1;
            lines_shown[panel[r][c][15:8]] = 1;
            // the ring: the pixel 1024 stream positions later is the same pixel (6.4 rows down, 64 columns on)
            s = 160 * r + c; s2 = s + 1024; r2 = s2 / 160; c2 = s2 % 160;
            if (r2 < 144) begin total = total + 1; if (panel[r][c] === panel[r2][c2]) same = same + 1; end
        end
        for (r = 0; r < 256; r = r + 1) if (lines_shown[r]) distinct = distinct + 1;
        if (rows_seen < 144) begin errors = errors + 1; $display("FAIL paused: the panel stopped scanning (%0d rows)", rows_seen); end
        if (wrong < 144 * 160 * 9 / 10) begin errors = errors + 1; $display("FAIL paused: picture still mostly right (%0d right %0d wrong)", right, wrong); end
        if (same != total) begin errors = errors + 1; $display("FAIL paused: not the 1024-pixel ring repeat (%0d of %0d)", same, total); end
        if (distinct > 8) begin errors = errors + 1; $display("FAIL paused: %0d distinct source lines on screen, expected the ring's last 6-7", distinct); end
        $display("paused core: %0d of %0d pixels right; %0d distinct source lines repeated down the screen; ring period 1024 holds for %0d/%0d pixels (64-pixel shear every 6.4 rows): panel row 0 shows line %0d x %0d, row 7 shows line %0d x %0d, row 14 shows line %0d x %0d",
                 right, right + wrong, distinct, same, total, panel[0][0][15:8], panel[0][0][7:0], panel[7][0][15:8], panel[7][0][7:0], panel[14][0][15:8], panel[14][0][7:0]);

        // 3. The timing runs again (the fixed design never stops it): right again within two frames.
        run = 1;
        wait_frames(3);
        capture_frame;
        right = 0; wrong = 0;
        for (r = 0; r < 144; r = r + 1) for (c = 0; c < 160; c = c + 1)
            if (panel[r][c] === {2'b00, r[7:0], c[7:0]}) right = right + 1; else wrong = wrong + 1;
        if (wrong != 0) begin errors = errors + 1; $display("FAIL resumed: %0d right %0d wrong", right, wrong); end

        if (errors == 0)
            $display("PASS: stock panel master: right while the core's LCD timing runs; with the core's clock enables stopped (the first sound image's pause) the panel keeps scanning and repeats its 1024-pixel ring, the last %0d lines sheared 64 pixels every 6.4 rows over the whole screen; right again once the timing runs.", distinct);
        else $display("FAIL: %0d errors", errors);
        $finish;
    end
endmodule

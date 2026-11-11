// SPDX-License-Identifier: GPL-3.0-or-later
// Additional terms: ADDITIONAL-TERMS.md
// The staged stock+media system_monitor (staging/chromatic-fpga-media, patched by
// scripts/stage_chromatic_fpga_media.py) driven over its real UART byte interface with the
// same frames the launcher encodes (launcher_logic.h pj_fpga_encode: 8F addr 02 hi lo crc):
//   - address 0x1D sets media_audio / media_overlay; a bad CRC changes nothing;
//   - stock addresses keep their meaning (9 = MCU buttons, 4 = SysCtrl) and SysCtrl clears media;
//   - with the overlay open the version message carries the debug bit and minor `MEDIA_MINOR,
//     major 18, so Chromatic Manager / the launcher can tell this FPGA from stock and ModRetro's
//     updater still recognises the console; the buttons message still reports menu open;
//   - media_menu (0x1D bit 2): a physical Menu press no longer closes the overlay but is reported
//     in the Buttons message (bit 8); the MCU's own poke (address 9 bit 8, ANDed into the menu
//     line as top.v does) still toggles; with the bit clear or after SysCtrl, stock behaviour.
// No hardware; Icarus only.
`timescale 1ns/1ps
module chromatic_media_sysmon_tb;
    reg clk = 0, reset = 1;
    always #59.6 clk = ~clk;                  // gClk 8.388 MHz

    reg btn_menu = 1;                         // pressed = 0
    reg hammer = 0;                           // every other button at once (pressed = 1), for the hold tests
    reg [7:0] rx_data = 0; reg rx_val = 0;
    wire [7:0] tx_data; wire tx_val;
    wire menuDisabled, media_audio, media_overlay, media_menu;
    wire [8:0] mcu_buttons; wire [15:0] system_control;
    system_monitor dut (
        .appear_off(1'b0), .clk(clk), .reset(reset),
        .BTN_A(hammer), .BTN_B(hammer), .BTN_DPAD_DOWN(hammer), .BTN_DPAD_LEFT(hammer), .BTN_DPAD_RIGHT(hammer), .BTN_DPAD_UP(hammer),
        .BTN_MENU(btn_menu & ~mcu_buttons[8]), .BTN_SEL(hammer), .BTN_START(hammer), // top.v: BTN_MENU_filtered & ~MCU_buttons[8]
        .menuDisabled(menuDisabled), .ADC_SEL(), .hAdcReq_ext(), .LCD_INIT_DONE(1'b1), .LCD_PWM(), .LCD_BACKLIGHT_INIT(),
        .hAdcReady_r1(1'b0), .hAdcValue_r1(14'd2000), .hButtons(9'd0), .MCU_buttons(mcu_buttons),
        .hVolume(7'd32), .pmic_sys_status(8'd0), .temperature(8'd25), .hHeadphones(1'b0),
        .gSecondEna(1'b0), .gHalfSecondEna(1'b0), .low_battery(), .LED_Green(), .LED_Red(), .LED_Yellow(), .LED_White(),
        .system_control(system_control), .debug_system(), .paletteBGIn(), .paletteOBJ0In(), .paletteOBJ1In(),
        .gbc_color_temp(), .gbc_mode(1'b0), .gpd(64'd0),
        .uart_rx_data(rx_data), .uart_rx_val(rx_val), .uart_tx_busy(1'b0), .uart_tx_data(tx_data), .uart_tx_val(tx_val),
        .VERSION_DET(1'b1), .POWERED_BY_LIPO(), .media_audio(media_audio), .media_overlay(media_overlay), .media_menu(media_menu)
    );

    integer errors = 0;
    function [7:0] crc8(input [39:0] bytes, input integer count); // CRC-8 SAE J1850 over `count` bytes, MSB-first in `bytes`
        integer i, b; reg [7:0] c, d;
        begin
            c = 8'hFF;
            for (i = 0; i < count; i = i + 1) begin
                d = bytes[39 - 8*i -: 8];
                c = c ^ d;
                for (b = 0; b < 8; b = b + 1) c = c[7] ? {c[6:0], 1'b0} ^ 8'h1D : {c[6:0], 1'b0};
            end
            crc8 = c;
        end
    endfunction

    task send_byte(input [7:0] b);
        begin
            @(negedge clk) begin rx_data = b; rx_val = 1; end
            @(negedge clk) rx_val = 0;
            repeat (12) @(negedge clk);          // the receiver's CRC needs 8 idle clocks per byte
        end
    endtask
    task send_message(input [6:0] address, input [15:0] value, input corrupt);
        reg [7:0] c;
        begin
            c = crc8({8'h8F, {1'b0, address}, 8'd2, value[15:8], value[7:0]}, 5);
            send_byte(8'h8F); send_byte({1'b0, address}); send_byte(8'd2);
            send_byte(value[15:8]); send_byte(value[7:0]); send_byte(corrupt ? c ^ 8'h01 : c);
            repeat (4) @(negedge clk);
        end
    endtask
    task expect_media(input a, input o, input [127:0] what);
        begin
            if (media_audio !== a || media_overlay !== o) begin
                errors = errors + 1;
                $display("FAIL %0s: media_audio=%b media_overlay=%b want %b %b", what, media_audio, media_overlay, a, o);
            end
        end
    endtask

    // Everything the FPGA sends: frames are found afterwards.
    reg [7:0] tx_log [0:8191]; integer tx_count = 0;
    always @(posedge clk) if (tx_val && tx_count < 8192) begin tx_log[tx_count] = tx_data; tx_count = tx_count + 1; end

    integer i, version_frames = 0, button_frames = 0, bad_version = 0;
    reg [15:0] v;

    // Buttons frames in tx_log[from .. tx_count): how many reported the Menu button down (bit 8).
    function integer menu_reports(input integer from);
        integer k; reg [15:0] b;
        begin
            menu_reports = 0;
            for (k = from; k + 5 < tx_count; k = k + 1)
                if (tx_log[k] == 8'h8F && tx_log[k+1] == 8'd2 && tx_log[k+2] == 8'd2 &&
                    crc8({tx_log[k], tx_log[k+1], tx_log[k+2], tx_log[k+3], tx_log[k+4]}, 5) == tx_log[k+5]) begin
                    b = {tx_log[k+3], tx_log[k+4]};
                    if (b[8]) menu_reports = menu_reports + 1;
                end
        end
    endfunction
    // One physical Menu press and release, long enough for several Buttons frames.
    task physical_menu(output integer reports);
        integer from;
        begin
            from = tx_count;
            btn_menu = 0; repeat (3000) @(negedge clk); btn_menu = 1; repeat (200) @(negedge clk);
            reports = menu_reports(from);
        end
    endtask
    task expect_menu(input closed, input [255:0] what);
        begin
            if (menuDisabled !== closed) begin errors = errors + 1; $display("FAIL %0s: menuDisabled=%b want %b", what, menuDisabled, closed); end
        end
    endtask
    integer reports, scan_from;
    initial begin
        repeat (10) @(negedge clk); reset = 0;
        repeat (20) @(negedge clk);
        expect_media(0, 0, "after reset");

        send_message(7'h1D, 16'h0003, 0); expect_media(1, 1, "0x1D = 3");
        send_message(7'h1D, 16'h0001, 0); expect_media(1, 0, "0x1D = 1");
        send_message(7'd9, 16'h0108, 0);  expect_media(1, 0, "buttons poke leaves media");
        if (mcu_buttons !== 9'h108) begin errors = errors + 1; $display("FAIL stock MCU_buttons: %h", mcu_buttons); end
        send_message(7'd9, 16'h0000, 0);  // the MCU releases its poke (bit 8 is ANDed into the menu line, as in top.v)
        repeat (40) @(negedge clk); expect_menu(0, "the MCU's poke (press, release) opened the overlay, as the launcher does");
        send_message(7'h1D, 16'h0002, 1); expect_media(1, 0, "bad CRC ignored");
        send_message(7'd4, 16'h0000, 0);  expect_media(0, 0, "SysCtrl clears media");
        if (system_control !== 16'd0) begin errors = errors + 1; $display("FAIL system_control"); end
        send_message(7'h1D, 16'h0003, 0); expect_media(1, 1, "set again");
        send_message(7'd4, 16'h0001, 0);  expect_media(0, 0, "SysCtrl (mute set) clears media");
        if (system_control !== 16'd1) begin errors = errors + 1; $display("FAIL system_control mute bit"); end

        // Stock: a short physical press closes the overlay, the next reopens it (status channels stream again).
        btn_menu = 0; repeat (40) @(negedge clk); btn_menu = 1; repeat (40) @(negedge clk);
        if (menuDisabled !== 1'b1) begin errors = errors + 1; $display("FAIL menu did not close"); end
        btn_menu = 0; repeat (40) @(negedge clk); btn_menu = 1; repeat (40) @(negedge clk);
        if (menuDisabled !== 1'b0) begin errors = errors + 1; $display("FAIL menu did not open"); end
        scan_from = tx_count;  // frames from here on were sent with the overlay open
        repeat (60000) @(negedge clk);
        for (i = scan_from; i + 5 < tx_count; i = i + 1) begin
            if (tx_log[i] == 8'h8F && tx_log[i+2] == 8'd2 &&
                crc8({tx_log[i], tx_log[i+1], tx_log[i+2], tx_log[i+3], tx_log[i+4]}, 5) == tx_log[i+5]) begin
                v = {tx_log[i+3], tx_log[i+4]};
                if (tx_log[i+1] == 8'd6) begin
                    version_frames = version_frames + 1;
                    if (v !== {4'b0001, 6'd`MEDIA_MINOR, 6'd18}) bad_version = bad_version + 1; // {00, reserved, debug, minor, major}
                end
                if (tx_log[i+1] == 8'd2) begin
                    button_frames = button_frames + 1;
                    if (v[9] !== 1'b0) begin errors = errors + 1; $display("FAIL buttons message says menu closed"); end
                end
            end
        end
        if (version_frames == 0 || bad_version != 0) begin
            errors = errors + 1;
            $display("FAIL version frames=%0d bad=%0d (want debug bit, minor %0d, major 18)", version_frames, bad_version, `MEDIA_MINOR);
        end
        if (button_frames == 0) begin errors = errors + 1; $display("FAIL no buttons message"); end
        send_message(7'h1D, 16'h0001, 0); expect_media(1, 0, "set with the overlay open");

        // Menu passthrough. Stock first: a physical press closes the open overlay, another reopens it.
        physical_menu(reports); expect_menu(1, "stock: Menu closes the overlay");
        physical_menu(reports); expect_menu(0, "stock: Menu reopens the overlay");
        // media_menu set (with audio, as the launcher sends it): the physical button is reported, not acted on.
        send_message(7'h1D, 16'h0005, 0); expect_media(1, 0, "0x1D = 5 (audio + menu)");
        physical_menu(reports); expect_menu(0, "media_menu: physical Menu leaves the overlay open");
        if (reports == 0) begin errors = errors + 1; $display("FAIL media_menu: Buttons frames did not report the Menu button"); end
        physical_menu(reports); expect_menu(0, "media_menu: second press still ignored");
        if (media_menu !== 1'b1) begin errors = errors + 1; $display("FAIL media_menu port not set"); end
        // Buttons hammered with our screen up: every button, the all-buttons reset combo (Menu+A+B+Start+Select
        // held), Menu alone, Menu with others, released and pressed again, 40 rounds. The overlay never closes
        // (menuDisabled stays 0, so MENU_CLOSED = 0: in stock emu_system_top.v every button bit into the core is
        // ANDed with MENU_CLOSED and the reset combo is gated by MENU_CLOSED too) and the media bits stay.
        for (i = 0; i < 40; i = i + 1) begin
            hammer = 1; btn_menu = 0; repeat (500) @(negedge clk);   // reset combo held
            btn_menu = 1; repeat (300) @(negedge clk);               // Menu released under other buttons
            hammer = 0; repeat (300) @(negedge clk);
            btn_menu = 0; repeat (400) @(negedge clk); hammer = 1; repeat (200) @(negedge clk); btn_menu = 1; hammer = 0;
            repeat (300) @(negedge clk);
            if (menuDisabled !== 1'b0) begin errors = errors + 1; $display("FAIL hammer round %0d closed the overlay", i); end
        end
        expect_media(1, 0, "media bits after hammering");
        if (media_menu !== 1'b1) begin errors = errors + 1; $display("FAIL media_menu lost while hammering"); end
        // The MCU's own poke (address 9 bit 8 pressed, then released) still closes and reopens it.
        send_message(7'd9, 16'h0100, 0); repeat (200) @(negedge clk);
        send_message(7'd9, 16'h0000, 0); repeat (200) @(negedge clk); expect_menu(1, "media_menu: MCU poke closes");
        send_message(7'd9, 16'h0100, 0); repeat (200) @(negedge clk);
        send_message(7'd9, 16'h0000, 0); repeat (200) @(negedge clk); expect_menu(0, "media_menu: MCU poke reopens");
        if (mcu_buttons !== 9'd0) begin errors = errors + 1; $display("FAIL MCU_buttons after poke release"); end
        // Bit cleared by 0x1D = 1 (audio only): stock behaviour again.
        send_message(7'h1D, 16'h0001, 0);
        physical_menu(reports); expect_menu(1, "menu bit clear: Menu closes the overlay");
        physical_menu(reports); expect_menu(0, "menu bit clear: Menu reopens");
        // Set again, then stock SysCtrl (what stock sends at start) clears it: stock behaviour.
        send_message(7'h1D, 16'h0004, 0); expect_media(0, 0, "0x1D = 4 (menu only)");
        physical_menu(reports); expect_menu(0, "menu only: physical Menu ignored");
        send_message(7'd4, 16'h0000, 0);
        physical_menu(reports); expect_menu(1, "after SysCtrl: Menu closes the overlay");
        physical_menu(reports); expect_menu(0, "after SysCtrl: Menu reopens");
        if (errors == 0)
            $display("PASS: staged system_monitor: 0x1D sets media bits, stock 9/4 unchanged, SysCtrl clears, bad CRC ignored, version %0d.%0d debug=1 in %0d frames, %0d buttons frames; media_menu: physical Menu reported not acted on, overlay stays open through 40 rounds of every button and the reset combo, MCU poke still toggles, stock again when clear.",
                     18, `MEDIA_MINOR, version_frames, button_frames);
        else $display("FAIL: %0d errors", errors);
        $finish;
    end
endmodule

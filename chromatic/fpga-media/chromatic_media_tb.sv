// SPDX-License-Identifier: GPL-3.0-or-later
// Additional terms: ADDITIONAL-TERMS.md
// Simulation of chromatic_media.v: ESP32-style I2S at 16- and 32-bit slots on a clock
// unrelated to gClk, the audio mux and its stale-stream silence, and the control
// register including the stock SysCtrl clear. No hardware.
`timescale 1ns/1ps
module chromatic_media_tb;
    reg clk = 0, reset_n = 0;
    always #59.6 clk = ~clk;                  // gClk 8.388 MHz

    reg bclk = 0, ws = 0, sd = 0;
    wire [15:0] left, right;
    wire frame;
    chromatic_i2s_rx rx(.clk(clk), .reset_n(reset_n), .i2s_bclk(bclk), .i2s_ws(ws), .i2s_sd(sd),
                        .left(left), .right(right), .frame(frame));

    reg media_audio = 0;
    reg [15:0] game_l = 16'h1234, game_r = 16'h5678;
    wire [15:0] out_l, out_r;
    chromatic_audio_mux mux(.clk(clk), .reset_n(reset_n), .media_audio(media_audio), .media_menu(1'b0),
                            .game_left(game_l), .game_right(game_r), .media_left(left), .media_right(right),
                            .media_frame(frame), .left(out_l), .right(out_r));

    reg rx_val = 0; reg [6:0] rx_addr = 0; reg [63:0] rx_data = 0;
    wire m_audio, m_overlay, m_menu;
    chromatic_media_ctrl ctrl(.clk(clk), .reset_n(reset_n), .rx_data_val(rx_val), .rx_address(rx_addr),
                              .rx_data(rx_data), .media_audio(m_audio), .media_overlay(m_overlay), .media_menu(m_menu));

    integer errors = 0, frames = 0;
    reg [15:0] sent_l [0:63];
    reg [15:0] sent_r [0:63];
    integer got = 0;
    always @(posedge clk) if (frame) begin
        if (got >= 0 && got < 64 && (left !== sent_l[got] || right !== sent_r[got])) begin
            errors = errors + 1;
            $display("mismatch frame %0d: got %h %h want %h %h", got, left, right, sent_l[got], sent_r[got]);
        end
        got = got + 1;
    end

    real half = 488.3;                        // 1.024 MHz bit clock
    task send_word(input [15:0] value, input integer slot, input new_ws);
        integer i;
        begin
            for (i = 0; i < slot; i = i + 1) begin
                // Falling edge: drive data (MSB first, padded) and change WS one bit early.
                #(half) bclk = 0;
                if (i == slot - 1) ws = new_ws;
                sd = (i < 16) ? value[15 - i] : 1'b0;
                #(half) bclk = 1;
            end
        end
    endtask

    task run_stream(input integer slot, input integer count);
        integer f;
        begin
            got = 0;
            // ESP32 starts with WS low; the first left word may be partial and is ignored.
            ws = 0;
            send_word(16'h0000, slot, 1'b1);   // a sync pair; its frame completes only when
            send_word(16'h0000, slot, 1'b0);   // the next word starts, so it is counted as -1
            got = -1;
            for (f = 0; f < count; f = f + 1) begin
                sent_l[f] = $random; sent_r[f] = $random;
                send_word(sent_l[f], slot, 1'b1);   // left, then WS goes high for right
                send_word(sent_r[f], slot, 1'b0);
            end
            send_word(16'h0, slot, 1'b1);           // closes the last right word
            #2000;
            if (got != count) begin
                errors = errors + 1;
                $display("slot %0d: %0d frames for %0d sent", slot, got, count);
            end
        end
    endtask

    initial begin
        #500 reset_n = 1;
        run_stream(16, 32);
        run_stream(32, 32);
        half = 487.1;                         // a slightly different, drifting rate
        run_stream(32, 16);

        // Mux: game when off; stream when on and live; silence once the stream stops.
        media_audio = 0; #1000;
        if (out_l !== 16'h1234 || out_r !== 16'h5678) begin errors = errors + 1; $display("game passthrough"); end
        media_audio = 1; #1000;
        if (out_l !== left || out_r !== right) begin errors = errors + 1; $display("media not selected"); end
        #101_000_000;                          // > 100 ms without a frame
        if (out_l !== 16'd0 || out_r !== 16'd0) begin errors = errors + 1; $display("stale stream not silenced"); end
        media_audio = 0; #1000;
        if (out_l !== 16'h1234) begin errors = errors + 1; $display("game after media"); end

        // Control: address 0x1D sets, stock SysCtrl (4) clears, other addresses ignored.
        @(negedge clk) begin rx_val = 1; rx_addr = 7'h1D; rx_data = 64'h3; end
        @(negedge clk) rx_val = 0;
        @(negedge clk) if (!m_audio || !m_overlay || m_menu) begin errors = errors + 1; $display("ctrl set"); end
        @(negedge clk) begin rx_val = 1; rx_addr = 7'h1D; rx_data = 64'h7; end
        @(negedge clk) rx_val = 0;
        @(negedge clk) if (!m_audio || !m_overlay || !m_menu) begin errors = errors + 1; $display("ctrl set bit 2"); end
        @(negedge clk) begin rx_val = 1; rx_addr = 7'd9; rx_data = 64'h100; end
        @(negedge clk) rx_val = 0;
        @(negedge clk) if (!m_audio || !m_menu) begin errors = errors + 1; $display("poke cleared media"); end
        @(negedge clk) begin rx_val = 1; rx_addr = 7'd4; rx_data = 64'h0; end
        @(negedge clk) rx_val = 0;
        @(negedge clk) if (m_audio || m_overlay || m_menu) begin errors = errors + 1; $display("SysCtrl did not clear"); end
        @(negedge clk) begin rx_val = 1; rx_addr = 7'h1D; rx_data = 64'h4; end
        @(negedge clk) rx_val = 0;
        @(negedge clk) if (m_audio || m_overlay || !m_menu) begin errors = errors + 1; $display("menu alone"); end
        @(negedge clk) begin rx_val = 1; rx_addr = 7'h1D; rx_data = 64'h0; end
        @(negedge clk) rx_val = 0;
        @(negedge clk) if (m_menu) begin errors = errors + 1; $display("menu bit not cleared by 0x1D=0"); end

        if (errors == 0) $display("PASS: I2S 16/32-bit slots with drift, mux passthrough/select/stale silence, media control (bits 0-2) and stock clear.");
        else $display("FAIL: %0d errors", errors);
        $finish;
    end
endmodule

// SPDX-License-Identifier: GPL-3.0-or-later
// Additional terms: ADDITIONAL-TERMS.md
// End to end sound on the staged stock+media FPGA source, codec side: a model of the ESP32's
// I2S output (jellyfin_esp32.c: master, 16 kHz, 16-bit stereo Philips, the sample in both
// slots) into chromatic_i2s_rx, through chromatic_audio_mux, into the UNMODIFIED stock
// aud_system_top_8_16 serializer exactly as top.v now wires it; a second stock
// aud_system_top_8_16 fed by the game directly is the reference.
//   1. media_audio = 0 with the I2S stream running: the codec lines (AUD_WCLK, AUD_DIN) of the
//      media path are identical to stock's on every gClk cycle (bit-identical audio for games).
//   2. media_audio = 1: the words on the codec lines are the sent samples, sample-and-held at
//      the codec's 131 kHz frame rate, through stock's own negation, speaker mono mix, software
//      mute and headphone stereo (checked under each), every sample in order, none lost.
//   3. The stream stops for > 100 ms: silence; it resumes: sound again.
//   4. media_audio back to 0: game sound, identical to stock again.
// volume and hHeadphones come from the codec over I2C in hardware (no slave here), so the
// bench forces them in both instances alike. No hardware; Icarus only.
`timescale 1ns/1ps
module chromatic_media_audio_tb;
    reg gClk = 0, hClk = 0, reset_n = 0;
    always #59.6 gClk = ~gClk;                // 8.388 MHz
    always #7.4 hClk = ~hClk;

    // The game: a slowly changing pattern.
    reg [15:0] game_l = 16'h1000, game_r = 16'hF000;
    always @(posedge gClk) begin game_l <= game_l + 16'd3; game_r <= game_r - 16'd5; end

    // ESP32 I2S model: 16 kHz, 32 bit clocks per frame (16 per slot), WS low = left, data MSB one
    // bit clock after the WS edge, sample duplicated in both slots.
    reg bclk = 0, ws = 0, sd = 0, i2s_on = 0;
    reg [15:0] sent [0:4095]; integer sent_n = 0;
    reg [15:0] cur = 0, prev = 0;
    real half = 976.5625;                     // 512 kHz bit clock
    // One frame = 32 bit clocks. Philips: WS changes on the falling edge together with the LAST bit
    // of the word that is ending, and the new word's MSB follows on the next clock, so position 0
    // carries the previous frame's right LSB (with WS going low for left) and position 16 carries
    // the left LSB (with WS going high for right).
    task i2s_frame(input [15:0] s, input [15:0] previous);
        integer i; reg [31:0] word;
        begin
            word = {s, s};
            for (i = 0; i < 32; i = i + 1) begin
                #(half) bclk = 0;
                if (i == 0) ws = 0; else if (i == 16) ws = 1;
                sd = (i == 0) ? previous[0] : word[32 - i];
                #(half) bclk = 1;
            end
        end
    endtask
    always begin
        if (i2s_on) begin
            cur = $random; if (cur == prev) cur = cur + 16'd1;
            if (sent_n < 4096) begin sent[sent_n] = cur; sent_n = sent_n + 1; end
            i2s_frame(cur, prev);
            prev = cur;
        end else begin
            #(half * 64);
        end
    end

    wire [15:0] media_l, media_r; wire media_frame;
    chromatic_i2s_rx rx (.clk(gClk), .reset_n(reset_n), .i2s_bclk(bclk), .i2s_ws(ws), .i2s_sd(sd),
                         .left(media_l), .right(media_r), .frame(media_frame));
    reg media_audio = 0, media_menu = 0;
    wire [15:0] aud_l, aud_r;
    chromatic_audio_mux mux (.clk(gClk), .reset_n(reset_n), .media_audio(media_audio), .media_menu(media_menu),
                             .game_left(game_l), .game_right(game_r), .media_left(media_l), .media_right(media_r),
                             .media_frame(media_frame), .left(aud_l), .right(aud_r));

    reg mute = 0;
    wire scl_s, sda_s, scl_m, sda_m; pullup(scl_s); pullup(sda_s); pullup(scl_m); pullup(sda_m);
    wire s_bclk, s_din, s_mclk, s_rst, s_wclk; wire [7:0] s_vol, s_pmic, s_temp; wire s_hp;
    wire m_bclk, m_din, m_mclk, m_rst, m_wclk; wire [7:0] m_vol, m_pmic, m_temp; wire m_hp;
    aud_system_top_8_16 u_stock (.appear_off(1'b0), .gClk(gClk), .hClk(hClk), .reset_n(reset_n), .left(game_l), .right(game_r),
        .software_mute(mute), .volume(s_vol), .hHeadphones(s_hp), .pmic_sys_status(s_pmic), .temperature(s_temp),
        .AUD_BCLK(s_bclk), .AUD_DIN(s_din), .AUD_DOUT(1'b0), .AUD_MCLK(s_mclk), .AUD_RESET(s_rst), .AUD_WCLK(s_wclk),
        .SCL(scl_s), .SDA(sda_s), .VERSION_DET(1'b1), .POWER_ON_FPGA(1'b1));
    aud_system_top_8_16 u_media (.appear_off(1'b0), .gClk(gClk), .hClk(hClk), .reset_n(reset_n), .left(aud_l), .right(aud_r),
        .software_mute(mute), .volume(m_vol), .hHeadphones(m_hp), .pmic_sys_status(m_pmic), .temperature(m_temp),
        .AUD_BCLK(m_bclk), .AUD_DIN(m_din), .AUD_DOUT(1'b0), .AUD_MCLK(m_mclk), .AUD_RESET(m_rst), .AUD_WCLK(m_wclk),
        .SCL(scl_m), .SDA(sda_m), .VERSION_DET(1'b1), .POWER_ON_FPGA(1'b1));

    // Codec frames of the media path: 32 bits MSB first, data updated when audio_phase was 0.
    reg [31:0] shift = 0; integer bit_n = 0; reg prev_wclk = 0;
    reg [15:0] word_a, word_b; reg word_new = 0;
    always @(posedge gClk) begin
        word_new <= 0;
        if (u_media.audio_phase == 1'b1) begin          // data just updated on this edge's predecessor: stable now
            if (m_wclk && !prev_wclk) bit_n = 0;
            shift = {shift[30:0], m_din};
            bit_n = bit_n + 1;
            if (bit_n == 32) begin word_a <= shift[31:16]; word_b <= shift[15:0]; word_new <= 1; end
            prev_wclk = m_wclk;
        end
    end

    integer errors = 0, identical = 0, differ = 0;
    integer compare_stock = 0;
    always @(posedge gClk) if (compare_stock) begin
        if (m_din === s_din && m_wclk === s_wclk && m_bclk === s_bclk) identical = identical + 1;
        else differ = differ + 1;
    end

    // Expected codec words for the I2S sample `s` under stock's own rules.
    function [15:0] neg(input [15:0] x); neg = -x; endfunction
    function [15:0] mono(input [15:0] l, input [15:0] r); reg [16:0] sum; begin sum = neg(l) + neg(r); mono = sum[16:1]; end endfunction

    // Phase-2 tracking: every distinct word B must be the next sent sample, in order.
    integer expect_i = 0, matched = 0, hold_max = 0, hold = 0; reg [15:0] last_b = 16'hxxxx; integer track = 0, want_zero = 0, first = 0;
    reg hp = 0;
    // sent_n-2: the last complete sample may still be on its way through the mux and serializer
    // (one codec frame); sent_n-1 is in flight. The first word after a start may be either.
    task start_tracking; begin last_b = word_b; expect_i = sent_n - 2; matched = 0; first = 1; track = 1; end endtask
    always @(posedge gClk) if (word_new && track) begin
        if (want_zero) begin
            if (word_a !== 16'd0 || word_b !== 16'd0) begin errors = errors + 1; if (errors < 10) $display("FAIL not silent/muted: %h %h", word_a, word_b); end
        end else if (word_b !== last_b) begin
            hold = 0; last_b = word_b;
            // the next sent sample (or, for the first word after a start, the one after it)
            if (expect_i < sent_n && word_b === (hp ? neg(sent[expect_i]) : mono(sent[expect_i], sent[expect_i]))) begin
                if (word_a !== (hp ? neg(sent[expect_i]) : 16'd0)) begin errors = errors + 1; if (errors < 10) $display("FAIL word A %h for sample %0d", word_a, expect_i); end
                expect_i = expect_i + 1; matched = matched + 1; first = 0;
            end else if (expect_i + 1 < sent_n && word_b === (hp ? neg(sent[expect_i+1]) : mono(sent[expect_i+1], sent[expect_i+1]))) begin
                if (!first) begin errors = errors + 1; if (errors < 10) $display("FAIL sample %0d skipped", expect_i); end
                expect_i = expect_i + 2; first = 0;
            end else if (first && expect_i + 2 < sent_n && word_b === (hp ? neg(sent[expect_i+2]) : mono(sent[expect_i+2], sent[expect_i+2]))) begin
                expect_i = expect_i + 3; first = 0;
            end else begin
                errors = errors + 1; if (errors < 10) $display("FAIL codec word %h not sample %0d (%h)", word_b, expect_i, sent[expect_i]);
            end
        end else begin
            hold = hold + 1; if (hold > hold_max) hold_max = hold;
        end
    end

    initial begin
        force u_stock.volume = 8'h20; force u_media.volume = 8'h20;     // the wheel, read over I2C in hardware
        force u_stock.hHeadphones = 1'b0; force u_media.hHeadphones = 1'b0;
        #500 reset_n = 1;
        i2s_on = 1;
        // 1. Game sound with the I2S stream running: media path identical to stock.
        #200_000; compare_stock = 1; #30_000_000; compare_stock = 0;
        if (differ != 0 || identical < 100000) begin errors = errors + 1; $display("FAIL phase 1: %0d identical, %0d differ", identical, differ); end
        // 1b. Our screen up, no media sound (media_menu alone): the paused game is silenced, then stock again.
        media_menu = 1; #200_000; want_zero = 1; track = 1; #2_000_000; track = 0; want_zero = 0;
        media_menu = 0; #200_000; identical = 0; differ = 0; compare_stock = 1; #10_000_000; compare_stock = 0;
        if (differ != 0) begin errors = errors + 1; $display("FAIL phase 1b: %0d differ after media_menu cleared", differ); end
        // 2. Media sound: speaker (mono mix), every sample in order.
        media_audio = 1; #300_000;                 // the mux is live after the first pair
        start_tracking;
        #(half * 64 * 200);                         // 200 samples (12.5 ms)
        track = 0;
        if (matched < 190) begin errors = errors + 1; $display("FAIL phase 2 speaker: %0d samples matched", matched); end
        if (hold_max < 6 || hold_max > 10) begin errors = errors + 1; $display("FAIL sample-and-hold: a sample stayed %0d extra frames", hold_max); end
        // 2b. Software mute (stock system_control[0]) silences media sound.
        mute = 1; #200_000; want_zero = 1; track = 1; #2_000_000; track = 0; want_zero = 0; mute = 0; #200_000;
        // 2c. Headphones: stereo, the sample in both words.
        force u_media.hHeadphones = 1'b1; force u_stock.hHeadphones = 1'b1; hp = 1;
        #(half * 64 * 3); start_tracking;
        #(half * 64 * 100); track = 0;
        if (matched < 95) begin errors = errors + 1; $display("FAIL phase 2 headphones: %0d matched", matched); end
        // 2d. Volume above 0x76 mutes (stock rule), media too.
        force u_media.volume = 8'h77; force u_stock.volume = 8'h77; #200_000; want_zero = 1; track = 1; #2_000_000; track = 0; want_zero = 0;
        force u_media.volume = 8'h20; force u_stock.volume = 8'h20; force u_media.hHeadphones = 1'b0; force u_stock.hHeadphones = 1'b0; hp = 0;
        // 3. The stream stops: silence after 100 ms; resumes: sound.
        i2s_on = 0; #120_000_000; want_zero = 1; track = 1; #2_000_000; track = 0; want_zero = 0;
        i2s_on = 1; #(half * 64 * 4); start_tracking; #(half * 64 * 50); track = 0;
        if (matched < 45) begin errors = errors + 1; $display("FAIL after resume: %0d matched", matched); end
        // 4. Back to the game: identical to stock again.
        media_audio = 0; #200_000; identical = 0; differ = 0; compare_stock = 1; #10_000_000; compare_stock = 0;
        if (differ != 0) begin errors = errors + 1; $display("FAIL phase 4: %0d differ", differ); end
        if (errors == 0)
            $display("PASS: staged audio path: game sound bit-identical to stock with I2S running (%0d cycles), game silenced while our screen is up (media_menu) and identical again after, media sound sample-exact at the codec through stock negation/mono/mute/headphones/volume rules, hold %0d frames, silence after a 100 ms stall, sound on resume, identical to stock again.", identical, hold_max + 1);
        else $display("FAIL: %0d errors", errors);
        $finish;
    end
endmodule

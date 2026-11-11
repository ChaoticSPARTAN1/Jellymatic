// SPDX-License-Identifier: GPL-3.0-or-later
// Chromatic FPGA: the Pocket link bridge for the stock+media image (docs/CHROMATIC-POCKET-TETHER.md).
// Phase 2: 64-byte downstream packets, switchable fast UART, link clock 256 or 512 kHz, filtered link inputs.
//
// Sits in the gClk (8.388 MHz) domain next to system_monitor. Control comes through the stock
// MCU<->FPGA UART decode (uart_packet_wrapper_rx: 0x8F, address, count, data, CRC-8 J1850);
// downstream data has its own parser on the raw UART bytes, because the stock decoder keeps
// only the last 10 data bytes of a packet. The bridge owns the Game Boy link port pins while
// the launcher's "Pocket link" mode is on. Power-up, FPGA reset, a lost heartbeat and stock's
// own SysCtrl message (address 4, sent by stock firmware at start) all hand the pins back to
// the game core and return the UART to the stock rate, so stock games never see any of it.
//
// MCU -> FPGA (new addresses, unused by stock and by Shiny Relay):
//   0x1E PLINK_CTRL, 5 data bytes: [cmd][key byte 3..0], key 0x504a4c4b (launcher_logic.h)
//        cmd 1 enter media link mode, 2 leave, 3 heartbeat (the guard expires after 3 s without
//        one), 4 switch UART2 to the fast rate (keyed; only in media mode; takes effect once the
//        FPGA's own transmitter has been idle for a few byte times). The key is ignored by 2, 3.
//   0x1F PLINK_DATA, 1..64 data bytes: downstream link bytes for the Pocket, in order. The MCU
//        pads a short tail with 0xFF (the link idle byte, which the Pocket ignores). Only
//        accepted in media mode; a packet that arrives while the previous one is still being
//        queued, or that does not fit the 1 KiB link TX queue, is dropped (fault bit 2), which
//        the MCU's credit rule (status packets below) prevents.
// FPGA -> MCU: one system_monitor TX channel (address 0x0A), 10 data bytes, the stock
//   launcher parser's maximum (PJ_FPGA_MAX_PAYLOAD):
//        [0] {1'b0, fast, faults[3:0], mode[1:0]}  mode 2 = media link; fast = UART at 524,288, confirmed
//        [1] n, 0..6 valid upstream bytes          faults: 0 link byte timeout, 1 RX overflow,
//        [2] TX queue level high byte, [3] low byte          2 downstream drop, 3 heartbeat expired
//        [4..9] upstream bytes from the Pocket, FF padding after n
//   Requested whenever upstream bytes wait, and at least every STATUS_CYCLES while in media
//   mode, so the MCU has fresh credit (queue level) and mode information. The request-once
//   pattern is Shiny Relay's (system_monitor.sv link_ev_req, GPL-3.0): the payload stays
//   stable until the arbiter has sent the packet (taken_i).
//
// Fast UART. uart_fast_o selects UART2's fast setting in uart.v (divider 16, correction off, RX
// filter bypassed): exactly gClk / 16 = 524,288 baud (rtl/chromatic_uart2_tb.sv simulates UART2
// itself). The switch happens at the first byte boundary after the request: the cycle in which
// uart_tx_busy_i is low (the transmitter has finished its stop bit and accepted nothing in the
// last cycle, hook 16), so no byte is ever cut in the middle. It does NOT wait for a long idle
// gap: while the launcher's overlay is open (which it always is in Pocket link mode) system_monitor
// streams every channel back to back (channelsNewDataValid = ~menuDisabled) and the transmitter
// is never idle for more than the packet wrapper's 16-cycle pacing. The first build waited for two
// byte times of idle (FAST_GUARD_CYCLES 1460) and so never switched on the console: the launcher
// timed out with "The fast connection failed" (2026-11-09 trial). The stock packet in flight at the
// switch is finished at the new rate, so the MCU, still at 115,200 for a few ms, loses it (CRC) and
// resynchronises on 0x8F. uart_fast_o falls with media mode (leave, SysCtrl, expiry, reset), so
// stock firmware always finds 115,200.
// Confirmation (review 2026-11-09): after switching, the FPGA waits for one valid packet (CRC
// checked by the stock decoder) at the new rate. Until it has one it reports fast=0 and sends no
// status at the new rate; with one, the status that confirms the rate is sent (the MCU has
// switched: it was the MCU that spoke). None within FAST_CONFIRM_CYCLES (250 ms, shorter than
// the MCU's 500 ms): the FPGA goes back to 115,200 by itself and stays in media mode, so the
// MCU's own timeout (back to 115,200, leave twice) finds it there. Either way both ends finish at
// 115,200; nothing flaps.
//
// Link byte engine: gb_link_byte, Game Boy timing, PERIPHERAL only. The Pocket is clock
// master (256 kHz at open, raised by it after HELLO). No pin or clock of the stock game link changes.
//
// Input conditioning (GLITCH_SAMPLES, default 3; 0 or 1 = none, the earlier behaviour). On the
// console the bridge received fewer bytes than the Pocket sent at 256 kHz and no frame validated
// (2026-11-09): one extra or missing clock edge is a bit slip that the byte engine does not heal
// before the next quiet gap. So LINK_CLK and LINK_IN each pass through the same small filter in
// front of the (shared, unchanged) engine: the pin is sampled on gClk into a GLITCH_SAMPLES-deep
// history and the filtered level only changes when every sample in the history agrees.
//   - A pulse that is caught by fewer than GLITCH_SAMPLES samples never reaches the engine. With
//     3 samples at 8.388608 MHz (119.2 ns) that is every pulse shorter than 238.4 ns, at any
//     phase. Pulses of 238.4 to 357.6 ns pass or not depending on their phase against gClk;
//     357.6 ns and longer always pass.
//   - A real level passes when it is held for 3 samples: 256 kHz (half period 1953 ns, 16.4
//     samples) and 512 kHz (977 ns, 8.2 samples) pass with room to spare.
//   - Clock and data take the same path (same depth, same clock), so the engine sees the
//     relation between them that is on the cable, 3 gClk periods (358 ns) later.
//   - The history's first flop is the only one that sees the asynchronous pin. The filtered
//     level is combinational from the history (plus a hold flop) and is used only by the
//     engine's own two synchroniser flops and the hold flop, so whatever the first flop does
//     it has a whole gClk period to settle before the next flop, and two more flops follow.
//   - Cost: the answer bit on LINK_OUT now changes 5 to 6 gClk periods (596 to 715 ns) after the
//     falling clock edge at the pin instead of 2 to 3 (238 to 358 ns). The Pocket samples at
//     its rising edge, one half period after the fall: 1953 ns at 256 kHz, 977 ns at 512 kHz
//     (about 260 ns left before the Pocket's own synchroniser and the cable). A 1 MHz link clock
//     (half period 488 ns) does NOT work with the filter on; the launcher advertises 512 kHz.
//     With GLITCH_SAMPLES=0 the engine's two-flop synchroniser alone is used, as before
//     (good to about 1 MHz, no glitch rejection).
module chromatic_pocket_link #(
    parameter integer CLK_HZ=8388608,
    parameter integer LINK_FIFO_BYTES=1024,
    parameter integer STATUS_CYCLES=CLK_HZ/20,    // 50 ms
    parameter integer HEARTBEAT_CYCLES=CLK_HZ*3,  // 3 s without a heartbeat: back to the game
    parameter integer FAST_CONFIRM_CYCLES=CLK_HZ/4, // 250 ms for one valid packet at the new rate, else back to 115,200
    parameter integer GLITCH_SAMPLES=3            // equal gClk samples a link input level needs (see the header); 0 or 1 = no filter
)(
    input  wire        clk,
    input  wire        reset,          // active high (~lock_o), as system_monitor
    // stock UART decode, as uart_packet_wrapper_rx presents it (control messages)
    input  wire        rx_data_val,
    input  wire [6:0]  rx_address,
    input  wire [79:0] rx_data,
    // raw UART bytes (UART2 RX_DATA/RX_DATA_VAL) for the wide data packets, and TX idle
    input  wire [7:0]  uart_rx_data_i,
    input  wire        uart_rx_val_i,
    input  wire        uart_tx_busy_i,
    output reg         uart_fast_o,
    // one system_monitor TX channel
    output wire        up_req,         // request-once pulse for channelsNewDataValid
    input  wire        up_taken,       // write_done && tx_channel == this channel
    input  wire [7:0]  tx_bytepos,
    output reg  [7:0]  up_byte,
    // the game core's link pins (emu_system_top registered signals)
    input  wire        game_so_i,
    input  wire        game_clk_i,
    input  wire        game_clk_oe_i,  // sc_int_clock2_r1: 1 = the game drives LINK_CLK
    // physical port
    input  wire        link_si_i,      // LINK_IN
    input  wire        link_clk_i,     // LINK_CLK as an input
    output wire        link_so_o,      // LINK_OUT (always driven, as stock)
    output wire        link_clk_o,     // LINK_CLK value when driven
    output wire        link_clk_oe,    // LINK_CLK_DIR_LV (1 = output); 0 in media mode: the Pocket clocks
    output wire [1:0]  mode_o,
    output wire [3:0]  faults_o
);
    localparam [31:0] KEY=32'h504a4c4b;
    localparam integer LW=$clog2(LINK_FIFO_BYTES+1);
    localparam integer DATA_BYTES=64;
    wire tx_ready;
    wire [LW-1:0] tx_level,rx_level;

    // ---- control messages (stock decode) ----
    wire ctrl=rx_data_val&&rx_address==7'h1e;
    wire [7:0] cmd=rx_data[39:32];
    wire keyed=rx_data[31:0]==KEY;
    wire enter_req=ctrl&&cmd==8'd1&&keyed;
    wire leave_req=(ctrl&&cmd==8'd2)||(rx_data_val&&rx_address==7'd4); // stock SysCtrl leaves too
    wire heartbeat=ctrl&&cmd==8'd3;
    wire fast_req=ctrl&&cmd==8'd4&&keyed;

    wire enabled,expired;
    wire [1:0] mode;
    wire media_so,media_so_oe,media_clk,media_clk_oe;
    reg  [3:0] faults;
    assign mode_o=mode;
    assign faults_o=faults;
    link_mode_guard #(.HEARTBEAT_CYCLES(HEARTBEAT_CYCLES),.GAP_CYCLES(CLK_HZ/12000)) guard(
        .clk(clk),.rst(reset),.request_i(enter_req),.key_i(KEY),.disable_i(leave_req),
        .heartbeat_i(heartbeat),
        .game_so_i(game_so_i),.game_so_oe_i(1'b1),.game_clk_i(game_clk_i),.game_clk_oe_i(game_clk_oe_i),
        .media_so_i(media_so),.media_so_oe_i(media_so_oe),.media_clk_i(media_clk),.media_clk_oe_i(media_clk_oe),
        .link_so_o(link_so_o),.link_so_oe(),.link_clk_o(link_clk_o),.link_clk_oe(link_clk_oe),
        .media_enable_o(enabled),.mode_o(mode),.expired_o(expired));

    // ---- fast UART switch, confirmed by the MCU speaking at the new rate ----
    reg fast_pending,fast_confirmed;
    reg [21:0] confirm_timer; // up to 4,194,303 cycles (0.5 s); FAST_CONFIRM_CYCLES must fit
    always @(posedge clk) begin
        if(reset||!enabled) begin uart_fast_o<=1'b0; fast_pending<=1'b0; fast_confirmed<=1'b0; confirm_timer<=22'd0; end
        else begin
            if(fast_req&&!uart_fast_o) fast_pending<=1'b1;
            if(fast_pending&&!uart_tx_busy_i) begin // a byte boundary: the transmitter is idle this cycle (see the header)
                uart_fast_o<=1'b1; fast_pending<=1'b0; fast_confirmed<=1'b0; confirm_timer<=FAST_CONFIRM_CYCLES;
            end else if(uart_fast_o&&!fast_confirmed) begin
                if(rx_data_val) fast_confirmed<=1'b1;                // a CRC-valid packet at the new rate
                else if(confirm_timer==0) uart_fast_o<=1'b0;         // nobody spoke at it: back to 115,200
                else confirm_timer<=confirm_timer-22'd1;
            end
        end
    end
    wire fast_shown=uart_fast_o&&fast_confirmed; // the status bit, and what triggers the confirming status

    // ---- downstream: own parser on the raw UART bytes, 1..64 data bytes per 0x1F packet ----
    localparam [2:0] W_IDLE=3'd0,W_ADDR=3'd1,W_COUNT=3'd2,W_DATA=3'd3,W_CRC=3'd4;
    reg [2:0]  wstate;
    reg [7:0]  waddr,wcount,wcrc;
    reg [3:0]  wcrc_bits;
    reg [6:0]  widx;           // bytes stored so far (0..64)
    reg        wide;           // this packet is a 0x1F data packet of acceptable length
    reg [7:0]  wbuf[0:DATA_BYTES-1];
    reg        draining;       // pushing wbuf into the link queue
    reg [6:0]  drain_at,drain_end;
    reg        down_drop;
    wire down_valid=draining&&drain_at!=drain_end;
    // CRC-8 J1850 over 0x8F, address, count, data: the stock wrapper's serial form (8 cycles per
    // byte; a byte is at least 80 cycles even at the fast rate).
    always @(posedge clk) begin
        if(wstate==W_IDLE&&!uart_rx_val_i) begin wcrc<=8'hff; wcrc_bits<=4'd0; end
        else if(uart_rx_val_i) begin wcrc<=wcrc^uart_rx_data_i; wcrc_bits<=4'd8; end
        else if(wcrc_bits!=0) begin
            wcrc_bits<=wcrc_bits-4'd1;
            wcrc<=wcrc[7]?{wcrc[6:0],1'b0}^8'h1d:{wcrc[6:0],1'b0};
        end
    end
    always @(posedge clk) begin
        down_drop<=1'b0;
        if(reset) begin wstate<=W_IDLE; wide<=1'b0; widx<=7'd0; draining<=1'b0; drain_at<=7'd0; drain_end<=7'd0; end
        else begin
            if(!enabled) begin draining<=1'b0; drain_at<=7'd0; drain_end<=7'd0; end
            else if(down_valid&&tx_ready) begin
                drain_at<=drain_at+7'd1;
                if(drain_at+7'd1==drain_end) draining<=1'b0;
            end
            if(uart_rx_val_i) case(wstate)
                W_IDLE: if(uart_rx_data_i==8'h8f) wstate<=W_ADDR;
                W_ADDR: begin waddr<=uart_rx_data_i; wstate<=W_COUNT; end
                W_COUNT: begin
                    wcount<=uart_rx_data_i; widx<=7'd0;
                    wide<=(waddr==8'h1f)&&(uart_rx_data_i!=8'd0)&&(uart_rx_data_i<=DATA_BYTES[7:0]);
                    wstate<=(uart_rx_data_i==8'd0)?W_CRC:W_DATA;
                end
                W_DATA: begin
                    if(wide) begin wbuf[widx[5:0]]<=uart_rx_data_i; widx<=widx+7'd1; end
                    if(wcount==8'd1) wstate<=W_CRC; else wcount<=wcount-8'd1;
                end
                default: begin // W_CRC
                    wstate<=W_IDLE;
                    if(wide&&enabled&&wcrc==uart_rx_data_i) begin
                        if(draining) down_drop<=1'b1;        // the previous packet is still queueing: data lost
                        else if({{(32-LW){1'b0}},tx_level}+{25'd0,widx}>LINK_FIFO_BYTES) down_drop<=1'b1; // would overfill
                        else begin draining<=1'b1; drain_at<=7'd0; drain_end<=widx; end
                    end
                    wide<=1'b0;
                end
            endcase
            if(!enabled) begin wide<=1'b0; end
        end
    end

    // ---- link input conditioning: the same consensus filter on the clock and the data input ----
    wire link_si_f,link_clk_f;
    generate if(GLITCH_SAMPLES>=2) begin : conditioned
        reg [GLITCH_SAMPLES-1:0] clk_h,si_h; // [0] is the first flop on the pin
        reg clk_q,si_q;                      // the level held while the history disagrees
        assign link_clk_f=(&clk_h)?1'b1:(~|clk_h)?1'b0:clk_q;
        assign link_si_f =(&si_h) ?1'b1:(~|si_h) ?1'b0:si_q;
        always @(posedge clk) begin
            if(reset) begin // the idle level
                clk_h<={GLITCH_SAMPLES{1'b1}}; si_h<={GLITCH_SAMPLES{1'b1}}; clk_q<=1'b1; si_q<=1'b1;
            end else begin
                clk_h<={clk_h[GLITCH_SAMPLES-2:0],link_clk_i}; si_h<={si_h[GLITCH_SAMPLES-2:0],link_si_i};
                clk_q<=link_clk_f; si_q<=link_si_f;
            end
        end
    end else begin : direct
        assign link_clk_f=link_clk_i;
        assign link_si_f=link_si_i;
    end endgenerate

    wire [7:0] rx_data_link;
    wire rx_valid,timeout,overflow;
    reg  rx_pop;
    gb_link_stream #(.HALF_CYCLES(CLK_HZ/512000),.QUIET_CYCLES(CLK_HZ/1000),
        .FIFO_DEPTH(LINK_FIFO_BYTES),.DROP_IDLE_RX(1)) link(
        .clk(clk),.rst(reset),.enable_i(enabled),.master_i(1'b0),.resync_i(1'b0),.half_cycles_i(12'd0),
        .tx_data_i(wbuf[drain_at[5:0]]),.tx_valid_i(down_valid),.tx_ready_o(tx_ready),
        .rx_data_o(rx_data_link),.rx_valid_o(rx_valid),.rx_ready_i(rx_pop),
        .tx_level_o(tx_level),.rx_level_o(rx_level),
        .link_si_i(link_si_f),.link_clk_i(link_clk_f),.link_so_o(media_so),.link_so_oe(media_so_oe),
        .link_clk_o(media_clk),.link_clk_oe(media_clk_oe),.busy_o(),.timeout_o(timeout),.overflow_o(overflow));

    always @(posedge clk) begin
        if(reset||enter_req) faults<=4'd0;
        else faults<=faults|{expired,down_drop,overflow,timeout};
    end

    // ---- upstream: status + up to 6 Pocket bytes, request-once ----
    reg [47:0] up_data;
    reg [2:0]  up_count;
    reg [7:0]  up_status;
    reg [15:0] up_level;
    reg        have_packet,req_sent,filling,was_enabled,status_due,was_fast;
    reg [31:0] status_timer;
    // A status packet is due on the timer while in media mode, once more when the mode ends
    // (heartbeat expiry, leave, SysCtrl) so the MCU learns of the exit promptly, and right
    // after the UART rate changed, so the MCU can confirm the new rate.
    // Nothing is sent while a new rate waits for its confirmation: the MCU may not have switched yet.
    wire probation=uart_fast_o&&!fast_confirmed;
    wire want=((rx_valid&&enabled)||status_due)&&!probation;
    assign up_req=have_packet&&!req_sent;
    always @(posedge clk) begin
        rx_pop<=1'b0;
        if(reset) begin
            have_packet<=1'b0; req_sent<=1'b0; filling<=1'b0; up_count<=3'd0; up_data<=48'd0;
            status_timer<=STATUS_CYCLES; up_status<=8'd0; up_level<=16'd0; was_enabled<=1'b0; status_due<=1'b0; was_fast<=1'b0;
        end else begin
            was_enabled<=enabled; was_fast<=fast_shown;
            if(enabled&&status_timer!=0) status_timer<=status_timer-1;
            // a rate change is reported once confirmed (the MCU is at the new rate) or once undone
            if((enabled&&status_timer==0)||(was_enabled&&!enabled)||(was_fast!=fast_shown)) status_due<=1'b1;
            if(!have_packet&&!filling&&want) begin
                filling<=1'b1; up_count<=3'd0; up_data<={48{1'b1}};
            end else if(filling) begin
                if(rx_valid&&!rx_pop&&up_count<3'd6) begin
                    up_data<={up_data[39:0],rx_data_link}; up_count<=up_count+3'd1; rx_pop<=1'b1;
                end else if(!rx_pop&&(!rx_valid||up_count==3'd6)) begin
                    // place the taken bytes at the front, FF padding after them
                    case(up_count)
                        3'd0: up_data<={48{1'b1}};
                        3'd1: up_data<={up_data[7:0],{40{1'b1}}};
                        3'd2: up_data<={up_data[15:0],{32{1'b1}}};
                        3'd3: up_data<={up_data[23:0],{24{1'b1}}};
                        3'd4: up_data<={up_data[31:0],{16{1'b1}}};
                        3'd5: up_data<={up_data[39:0],{8{1'b1}}};
                        default: ;
                    endcase
                    up_status<={1'b0,fast_shown,faults,mode}; up_level<=tx_level;
                    filling<=1'b0; have_packet<=1'b1; status_timer<=STATUS_CYCLES; status_due<=1'b0;
                end
            end
            if(have_packet&&!req_sent) req_sent<=1'b1;
            if(up_taken&&req_sent) begin req_sent<=1'b0; have_packet<=1'b0; end
            if(!enabled) status_timer<=STATUS_CYCLES; // no periodic status traffic in game mode
        end
    end
    always @* begin
        case(tx_bytepos)
            8'd0: up_byte=up_status;
            8'd1: up_byte={5'd0,up_count};
            8'd2: up_byte=up_level[15:8];
            8'd3: up_byte=up_level[7:0];
            8'd4: up_byte=up_data[47:40];
            8'd5: up_byte=up_data[39:32];
            8'd6: up_byte=up_data[31:24];
            8'd7: up_byte=up_data[23:16];
            8'd8: up_byte=up_data[15:8];
            8'd9: up_byte=up_data[7:0];
            default: up_byte=8'hff;
        endcase
    end
    wire [LW-1:0] unused_rx_level=rx_level;
endmodule

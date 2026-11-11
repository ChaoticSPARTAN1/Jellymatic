// SPDX-License-Identifier: GPL-3.0-or-later
// Additional terms: ADDITIONAL-TERMS.md
// Explicit replacement for 33-bit link_lite in a separately built media core.
// Same bus/physical port shape; DIFFERENT wire protocol and register signature.
// Existing SDK of_net code must NOT use this peripheral.
// Word offsets: 0 ctrl/status, 1 TX byte, 2 RX byte (pop on read), 3 clock half-period
// in clk cycles (read/write, clamped to MIN_HALF..SLOW_HALF; reset restores the safe
// rate HALF), 4 TX queued, 5 RX queued, 6 faults W1C, 7 slowest half-period this core
// accepts (read only; cores before slow rates read 0 and clamp word 3
// to HALF), 31 signature 0x4b4c4a50 ("PJLK").
// CTRL W: enable[0], reset[1], master[2], idle polling[3]. R: enable[0],master[2],
// polling[3],TX ready[4],RX valid[5],busy[6]. Faults: timeout,overflow,TX drop,RX empty.
//
// The byte engine and both queues are the unchanged shared modules (gb_link_byte,
// byte_fifo), joined exactly as gb_link_stream joins them. The engine's half-period
// input is 12 bits, so a master clock slower than FAST_MAX cycles per half (about
// 11 kHz at 90 MHz) is produced by pocket_link_slow_master below: the same master
// transfer with a wider counter. Both give the same 2 ms clock-high guard after
// enable, and changing between them restarts that guard.
module pocket_media_link #(
    parameter integer CLK_HZ=100000000,
    parameter integer SCK_HZ=256000,
    parameter integer FIFO_BYTES=1024,
    parameter integer SLOW_HZ=8000,     // slowest selectable master clock
    parameter integer FAST_MAX=4095,    // longest half-period given to gb_link_byte
    parameter integer SI_FILTER=4       // 0 off; N: slow-rate receive debounce of half>>N cycles, see below
)(
    input wire clk,input wire reset_n,
    input wire reg_wr,input wire reg_rd,input wire [4:0] reg_addr,input wire [31:0] reg_wdata,
    output reg [31:0] reg_rdata,
    input wire link_si_i,output wire link_so_o,output wire link_so_oe,
    input wire link_sck_i,output wire link_sck_o,output wire link_sck_oe,
    input wire link_sd_i,output wire link_sd_o,output wire link_sd_oe,
    output wire irq
);
    localparam integer HALF=CLK_HZ/(SCK_HZ*2),LW=$clog2(FIFO_BYTES+1);
    // Peers synchronize SCK with two flops, so never exceed 4 MHz or an 8-cycle half.
    localparam integer MIN_HALF=(CLK_HZ/8000000>8)?CLK_HZ/8000000:8;
    localparam integer SLOW_HALF=CLK_HZ/(SLOW_HZ*2),QUIET=CLK_HZ/1000;
    localparam integer HQ=16;
    reg enable_q,master_q,poll_q,role_q;
    reg [HQ-1:0] half_q;
    reg [3:0] faults;
    reg [31:0] rx_read_snapshot;
    wire reset=!reset_n;
    wire soft_reset=reg_wr&&reg_addr==0&&reg_wdata[1];
    wire tx_write=reg_wr&&reg_addr==1;
    wire rx_read=reg_rd&&reg_addr==2;
    wire [LW-1:0] tx_level,rx_level;
    wire poll=enable_q&&master_q&&poll_q&&(tx_level<2);
    assign link_sd_o=1'b0;
    assign link_sd_oe=1'b0;

    // ---- queues and engine, joined as in gb_link_stream ----
    wire clear=!enable_q||soft_reset||(role_q!=master_q);
    wire active=enable_q&&!reset&&!clear;
    // A new half-period takes effect between bytes, never inside one: no byte starts while
    // a change is pending, so neither a rate change nor a change of engine cuts a byte.
    reg [HQ-1:0] half_run;
    wire rate_pending=half_run!=half_q;
    wire use_slow=master_q&&(half_run>FAST_MAX);
    wire [7:0] tx_byte,rx_data;
    wire tx_valid,tx_fifo_ready,rx_fifo_valid,rx_fifo_ready;
    wire [7:0] f_rx_byte,s_rx_byte;
    wire f_tx_ready,f_rx_valid,f_so,f_so_oe,f_clk,f_clk_oe,f_busy,timeout,overflow;
    wire s_tx_ready,s_rx_valid,s_so,s_clk,s_busy;
    wire s_enable=active&&use_slow;
    wire [7:0] rx_byte=use_slow?s_rx_byte:f_rx_byte;
    wire eng_rx_valid=use_slow?s_rx_valid:f_rx_valid;
    wire eng_tx_ready=!rate_pending&&(use_slow?s_tx_ready:f_tx_ready);
    wire busy=use_slow?s_busy:f_busy;
    wire idle_rx=(rx_byte==8'hff);
    wire eng_rx_ready=idle_rx||rx_fifo_ready;
    wire tx_ready=active&&tx_fifo_ready;
    wire rx_valid=active&&rx_fifo_valid;
    assign irq=enable_q&&(rx_valid||(|faults));
    assign link_so_o=use_slow?s_so:f_so;
    assign link_so_oe=f_so_oe||s_enable;
    assign link_sck_o=use_slow?s_clk:f_clk;
    assign link_sck_oe=f_clk_oe||s_enable;

    // Debounce of the received data line while this side is the master, at rates SLOWER
    // than the safe rate only: there the half-period has margin to spare, and a pulse
    // shorter than the window (half>>SI_FILTER cycles, at most 31) never reaches the
    // engine. The safe rate and faster take the line directly, exactly as before, because
    // the window would be spent out of their sampling margin (measured in
    // pocket_chromatic_hello_tb: it turned a tolerated 1.5 us slow edge into bad replies).
    wire si;
    generate if(SI_FILTER!=0)begin:si_filter
        reg [1:0] meta;reg held;reg [4:0] run;
        wire [HQ-1:0] part=half_run>>SI_FILTER;
        wire [4:0] limit=(part>31)?5'd31:part[4:0];
        always @(posedge clk)begin
            meta<={meta[0],link_si_i};
            if(reset||!active)begin meta<=2'b11;held<=1;run<=0;end
            else if(meta[1]==held)run<=0;
            else if(run>=limit)begin held<=meta[1];run<=0;end
            else run<=run+1'b1;
        end
        assign si=(master_q&&half_run>HALF)?held:link_si_i;
    end else begin:si_direct
        assign si=link_si_i;
    end endgenerate

    byte_fifo #(.DEPTH(FIFO_BYTES)) tx_fifo(
        .clk(clk),.rst(reset),.clear_i(clear),.in_data(tx_write?reg_wdata[7:0]:8'hff),
        .in_valid((tx_write||poll)&&active),.in_ready(tx_fifo_ready),
        .out_data(tx_byte),.out_valid(tx_valid),.out_ready(eng_tx_ready),.level_o(tx_level));
    byte_fifo #(.DEPTH(FIFO_BYTES)) rx_fifo(
        .clk(clk),.rst(reset),.clear_i(clear),.in_data(rx_byte),.in_valid(eng_rx_valid&&!idle_rx),
        .in_ready(rx_fifo_ready),.out_data(rx_data),.out_valid(rx_fifo_valid),
        .out_ready(rx_read),.level_o(rx_level));
    gb_link_byte #(.HALF_CYCLES(HALF),.QUIET_CYCLES(QUIET),.RUNTIME_HALF(1)) wire_byte(
        .clk(clk),.rst(reset),.enable_i(enable_q&&!use_slow),.master_i(master_q),.resync_i(soft_reset),
        .half_cycles_i(half_run[11:0]),
        .tx_data_i(tx_byte),.tx_valid_i(tx_valid&&!rate_pending&&!use_slow),.tx_ready_o(f_tx_ready),
        .rx_data_o(f_rx_byte),.rx_valid_o(f_rx_valid),.rx_ready_i(eng_rx_ready),
        .link_si_i(si),.link_clk_i(link_sck_i),.link_so_o(f_so),
        .link_so_oe(f_so_oe),.link_clk_o(f_clk),.link_clk_oe(f_clk_oe),
        .busy_o(f_busy),.timeout_o(timeout),.overflow_o(overflow));
    pocket_link_slow_master #(.QUIET_CYCLES(QUIET),.HW(HQ)) slow_byte(
        .clk(clk),.enable_i(s_enable),.half_cycles_i(half_run),
        .tx_data_i(tx_byte),.tx_valid_i(tx_valid&&!rate_pending&&use_slow),.tx_ready_o(s_tx_ready),
        .rx_data_o(s_rx_byte),.rx_valid_o(s_rx_valid),.rx_ready_i(eng_rx_ready),
        .link_si_i(si),.link_so_o(s_so),.link_clk_o(s_clk),.busy_o(s_busy));

    wire [3:0] events={rx_read&&!rx_valid,tx_write&&!tx_ready,overflow,timeout};
    wire [3:0] clear_faults=(reg_wr&&reg_addr==6)?reg_wdata[3:0]:4'b0;
    always @(posedge clk)begin
        role_q<=master_q;
        if(reset||soft_reset)begin
            enable_q<=0;master_q<=0;poll_q<=0;faults<=0;rx_read_snapshot<=32'hffffffff;half_q<=HALF;half_run<=HALF;
        end
        else begin
            if(!busy)half_run<=half_q;
            // axi_periph_slave samples rdata two cycles after its read pulse.
            // Preserve the popped head through that registered response path.
            if(rx_read)rx_read_snapshot<=rx_valid?{24'b0,rx_data}:32'hffffffff;
            faults<=(faults&~clear_faults)|events;
            if(reg_wr&&reg_addr==0)begin
                enable_q<=reg_wdata[0];master_q<=reg_wdata[2];poll_q<=reg_wdata[3];
            end
            if(reg_wr&&reg_addr==3)
                half_q<=reg_wdata<MIN_HALF?MIN_HALF:reg_wdata>SLOW_HALF?SLOW_HALF:reg_wdata[HQ-1:0];
        end
    end
    always @* begin
        reg_rdata=0;
        case(reg_addr)
            0:reg_rdata={25'b0,busy,rx_valid,tx_ready,poll_q,master_q,1'b0,enable_q};
            2:reg_rdata=rx_read?(rx_valid?{24'b0,rx_data}:32'hffffffff):rx_read_snapshot;
            3:reg_rdata=half_q;
            4:reg_rdata=tx_level;
            5:reg_rdata=rx_level;
            6:reg_rdata={28'b0,faults};
            7:reg_rdata=SLOW_HALF;
            31:reg_rdata=32'h4b4c4a50;
            default:;
        endcase
    end
    wire unused_sd=link_sd_i;
`ifndef SYNTHESIS
    initial begin
        if(SLOW_HALF>=(1<<HQ)||SLOW_HALF<HALF||FAST_MAX>4095||FAST_MAX<HALF||QUIET<4*SLOW_HALF)
            $fatal(1,"Pocket link rate parameters are outside the engine/quiet contract");
    end
`endif
endmodule

// Master-only byte transfer for clocks too slow for gb_link_byte's 12-bit counter.
// Same wire behaviour as that module's master branch: idle-high clock, MSB first, data
// changes on the falling edge and is sampled on the rising edge, the last data bit is
// held through idle, and no byte starts until the clock has been high for two quiet
// intervals after enable.
module pocket_link_slow_master #(
    parameter integer QUIET_CYCLES=90000,
    parameter integer HW=16
)(
    input wire clk,input wire enable_i,input wire [HW-1:0] half_cycles_i,
    input wire [7:0] tx_data_i,input wire tx_valid_i,output wire tx_ready_o,
    output reg [7:0] rx_data_o,output reg rx_valid_o,input wire rx_ready_i,
    input wire link_si_i,output reg link_so_o,output reg link_clk_o,output reg busy_o
);
    localparam integer QW=$clog2(2*QUIET_CYCLES+1);
    reg [1:0] si_sync;
    reg [HW-1:0] half_count;
    reg [QW-1:0] guard_count;
    reg [2:0] bit_count;
    reg [7:0] tx_shift,rx_shift;
    wire rx_space=!rx_valid_o||rx_ready_i;
    assign tx_ready_o=enable_i&&!busy_o&&guard_count==0&&rx_space;
    always @(posedge clk)begin
        si_sync<={si_sync[0],link_si_i};
        if(!enable_i)begin
            si_sync<=2'b11;link_so_o<=1;link_clk_o<=1;busy_o<=0;rx_valid_o<=0;rx_data_o<=0;
            half_count<=0;guard_count<=2*QUIET_CYCLES;bit_count<=0;tx_shift<=8'hff;rx_shift<=0;
        end else begin
            if(rx_valid_o&&rx_ready_i)rx_valid_o<=0;
            if(guard_count!=0)guard_count<=guard_count-1'b1;
            if(!busy_o)begin
                link_clk_o<=1;
                if(tx_valid_i&&tx_ready_o)begin
                    tx_shift<=tx_data_i;rx_shift<=0;bit_count<=0;
                    half_count<=half_cycles_i-1'b1;busy_o<=1;
                end
            end else if(half_count!=0)half_count<=half_count-1'b1;
            else begin
                half_count<=half_cycles_i-1'b1;
                if(link_clk_o)begin
                    link_clk_o<=0;link_so_o<=tx_shift[7];
                end else begin
                    link_clk_o<=1;
                    rx_shift<={rx_shift[6:0],si_sync[1]};
                    tx_shift<={tx_shift[6:0],1'b1};
                    if(bit_count==7)begin
                        busy_o<=0;rx_data_o<={rx_shift[6:0],si_sync[1]};rx_valid_o<=1;
                    end else bit_count<=bit_count+1'b1;
                end
            end
        end
    end
endmodule

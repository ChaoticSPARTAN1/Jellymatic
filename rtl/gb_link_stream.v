// Buffered byte endpoint. Board integration must gate enable_i with explicit
// media mode and mux stock Game Boy pins outside this module by default.
module gb_link_stream #(
    parameter integer HALF_CYCLES=49,
    parameter integer QUIET_CYCLES=1024,
    parameter integer FIFO_DEPTH=1024,
    parameter integer DROP_IDLE_RX=0,
    parameter integer RUNTIME_HALF=0
)(
    input wire clk,input wire rst,input wire enable_i,input wire master_i,input wire resync_i,
    input wire [11:0] half_cycles_i,
    input wire [7:0] tx_data_i,input wire tx_valid_i,output wire tx_ready_o,
    output wire [7:0] rx_data_o,output wire rx_valid_o,input wire rx_ready_i,
    output wire [$clog2(FIFO_DEPTH+1)-1:0] tx_level_o,rx_level_o,
    input wire link_si_i,input wire link_clk_i,
    output wire link_so_o,link_so_oe,link_clk_o,link_clk_oe,
    output wire busy_o,timeout_o,overflow_o
);
    wire [7:0] tx_byte,rx_byte;
    wire tx_valid,tx_ready,rx_valid,rx_ready,tx_fifo_ready,rx_fifo_valid,rx_fifo_ready;
    wire idle_rx=DROP_IDLE_RX&&(rx_byte==8'hff);
    assign rx_ready=idle_rx||rx_fifo_ready;
    reg role_q;
    always @(posedge clk)role_q<=master_i;
    wire clear_i=!enable_i||resync_i||(role_q!=master_i);
    wire active=enable_i&&!rst&&!clear_i;
    assign tx_ready_o=active&&tx_fifo_ready;
    assign rx_valid_o=active&&rx_fifo_valid;
    byte_fifo #(.DEPTH(FIFO_DEPTH)) tx_fifo(
        .clk(clk),.rst(rst),.clear_i(clear_i),.in_data(tx_data_i),
        .in_valid(tx_valid_i&&active),.in_ready(tx_fifo_ready),
        .out_data(tx_byte),.out_valid(tx_valid),.out_ready(tx_ready),.level_o(tx_level_o));
    byte_fifo #(.DEPTH(FIFO_DEPTH)) rx_fifo(
        .clk(clk),.rst(rst),.clear_i(clear_i),.in_data(rx_byte),.in_valid(rx_valid&&!idle_rx),
        .in_ready(rx_fifo_ready),.out_data(rx_data_o),.out_valid(rx_fifo_valid),
        .out_ready(rx_ready_i),.level_o(rx_level_o));
    gb_link_byte #(.HALF_CYCLES(HALF_CYCLES),.QUIET_CYCLES(QUIET_CYCLES),.RUNTIME_HALF(RUNTIME_HALF)) wire_byte(
        .clk(clk),.rst(rst),.enable_i(enable_i),.master_i(master_i),.resync_i(resync_i),
        .half_cycles_i(half_cycles_i),
        .tx_data_i(tx_byte),.tx_valid_i(tx_valid),.tx_ready_o(tx_ready),
        .rx_data_o(rx_byte),.rx_valid_o(rx_valid),.rx_ready_i(rx_ready),
        .link_si_i(link_si_i),.link_clk_i(link_clk_i),.link_so_o(link_so_o),
        .link_so_oe(link_so_oe),.link_clk_o(link_clk_o),.link_clk_oe(link_clk_oe),
        .busy_o(busy_o),.timeout_o(timeout_o),.overflow_o(overflow_o));
endmodule

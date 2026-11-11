// SPDX-License-Identifier: GPL-3.0-or-later
// Additional terms: ADDITIONAL-TERMS.md
// Composition for an explicit media mode. This is NOT yet wired into a Gowin
// board top, system-monitor UART commands, power sequencing or a firmware image.
module chromatic_link_bridge #(
    parameter integer CLK_HZ=25000000,
    parameter integer FIFO_WORDS=1024,
    parameter integer LINK_FIFO_BYTES=1024
)(
    input wire clk_i,input wire reset_i,
    input wire request_i,input wire [31:0] key_i,input wire disable_i,input wire heartbeat_i,
    input wire qwrite_clk_i,input wire qcs_i,input wire qcommand_i,
    input wire [31:0] qaddress_i,input wire [9:0] qlength_i,
    input wire qvalid_i,input wire [15:0] qdata_i,
    output wire legacy_allowed_o,output wire [31:0] consumed_bytes_o,
    output wire [7:0] upstream_data_o,output wire upstream_valid_o,input wire upstream_ready_i,
    input wire game_so_i,input wire game_so_oe_i,input wire game_clk_i,input wire game_clk_oe_i,
    input wire link_si_i,input wire link_clk_i,
    output wire link_so_o,output wire link_so_oe,output wire link_clk_o,output wire link_clk_oe,
    output wire [1:0] mode_o,output reg [3:0] fault_status_o
);
    wire enabled,expired,qfault,timeout,overflow;
    wire media_so,media_so_oe,media_clk,media_clk_oe;
    link_mode_guard #(.HEARTBEAT_CYCLES(CLK_HZ*3),.GAP_CYCLES(CLK_HZ/12000)) guard(
        .clk(clk_i),.rst(reset_i),.request_i(request_i),.key_i(key_i),
        // Latch faults before disabling: media disable asynchronously flushes
        // ingress fault state, so feeding it straight back creates a reset loop.
        .disable_i(disable_i||((|fault_status_o)&&(mode_o!=0))),.heartbeat_i(heartbeat_i),
        .game_so_i(game_so_i),.game_so_oe_i(game_so_oe_i),.game_clk_i(game_clk_i),.game_clk_oe_i(game_clk_oe_i),
        .media_so_i(media_so),.media_so_oe_i(media_so_oe),.media_clk_i(media_clk),.media_clk_oe_i(media_clk_oe),
        .link_so_o(link_so_o),.link_so_oe(link_so_oe),.link_clk_o(link_clk_o),.link_clk_oe(link_clk_oe),
        .media_enable_o(enabled),.mode_o(mode_o),.expired_o(expired));
    wire [7:0] tx_data,rx_data;
    wire tx_valid,tx_ready,rx_valid,rx_ready;
    chromatic_media_ingress #(.FIFO_WORDS(FIFO_WORDS)) ingress(
        .clk_i(clk_i),.reset_i(reset_i),.media_enable_i(enabled),.qwrite_clk_i(qwrite_clk_i),
        .qcs_i(qcs_i),.qcommand_i(qcommand_i),.qaddress_i(qaddress_i),.qlength_i(qlength_i),
        .qvalid_i(qvalid_i),.qdata_i(qdata_i),.legacy_allowed_o(legacy_allowed_o),
        .data_o(tx_data),.valid_o(tx_valid),.ready_i(tx_ready),
        .consumed_bytes_o(consumed_bytes_o),.fault_o(qfault));
    // Raw FF is the physical polling idle. A payload FF is escaped as 7D DF.
    assign upstream_data_o=rx_data;
    assign upstream_valid_o=enabled&&rx_valid&&(rx_data!=8'hff);
    assign rx_ready=(rx_data==8'hff)||upstream_ready_i;
    gb_link_stream #(.HALF_CYCLES(CLK_HZ/512000),.QUIET_CYCLES(CLK_HZ/1000),
        .FIFO_DEPTH(LINK_FIFO_BYTES),.DROP_IDLE_RX(1)) link(
        .clk(clk_i),.rst(reset_i),.enable_i(enabled),.master_i(1'b0),.resync_i(1'b0),
        .tx_data_i(tx_data),.tx_valid_i(tx_valid),.tx_ready_o(tx_ready),
        .rx_data_o(rx_data),.rx_valid_o(rx_valid),.rx_ready_i(rx_ready),.tx_level_o(),.rx_level_o(),
        .link_si_i(link_si_i),.link_clk_i(link_clk_i),.link_so_o(media_so),.link_so_oe(media_so_oe),
        .link_clk_o(media_clk),.link_clk_oe(media_clk_oe),.busy_o(),.timeout_o(timeout),.overflow_o(overflow));
    always @(posedge clk_i)begin
        if(reset_i||(request_i&&key_i==32'h504a4c4b&&mode_o==0))fault_status_o<=0;
        else fault_status_o<=fault_status_o|{expired,overflow,timeout,qfault};
    end
endmodule

// SPDX-License-Identifier: GPL-3.0-or-later
// Additional terms: ADDITIONAL-TERMS.md
// Route already-decoded STOCK QSPI writes into a bounded media FIFO. The stock
// decoder's valid/data are sampled on the FALLING QSPI edge, as its PSRAM FIFO
// already does. Supply a board-qualified buffered clock at qwrite_clk_i.
//
// Media write address: 0x504a0000, command=1, EVEN byte count 2..1024, length=N-1.
// Escape/pad data at the wire layer (0xff idle is legal padding). No QSPI reads,
// flash/cart access, or pin ownership here. Media disabled after reset by caller.
//
// CRITICAL integration: gate BOTH legacy FIFO writes AND end-of-CS PSRAM burst
// requests with legacy_allowed_o. Gating data alone leaves a spurious burst.
// Stock physical PSRAM addresses are 23 bits; all higher banks are isolated.
module chromatic_media_ingress #(parameter integer FIFO_WORDS=1024)(
    input wire clk_i,input wire reset_i,input wire media_enable_i,
    input wire qwrite_clk_i,input wire qcs_i,input wire qcommand_i,
    input wire [31:0] qaddress_i,input wire [9:0] qlength_i,
    input wire qvalid_i,input wire [15:0] qdata_i,
    output wire legacy_allowed_o,
    output wire [7:0] data_o,output wire valid_o,input wire ready_i,
    output reg [31:0] consumed_bytes_o,output wire fault_o
);
    assign legacy_allowed_o=(qaddress_i[31:23]==0);
    wire fifo_reset=reset_i||!media_enable_i;
    wire media_address=(qaddress_i==32'h504a0000);
    wire media_bank=(qaddress_i[31:16]==16'h504a);
    reg [9:0] words;
    reg qfault;
    (* async_reg="true" *) reg [1:0] enabled_q;
    (* async_reg="true" *) reg [1:0] fault_sync;
    always @(posedge qwrite_clk_i or posedge fifo_reset)
        if(fifo_reset)enabled_q<=0;else enabled_q<={enabled_q[0],1'b1};
    // CS resets only the per-transaction count, never the sticky fault.
    always @(posedge qwrite_clk_i or posedge qcs_i or posedge fifo_reset)
        if(qcs_i||fifo_reset)words<=0;
        else if(qvalid_i&&media_bank&&words!=1023)words<=words+1'b1;
    wire legal=media_address&&qcommand_i&&qlength_i[0]&&words<({1'b0,qlength_i}+1'b1)/2;
    wire write_ready;
    wire attempt=qvalid_i&&!qcs_i&&media_bank&&enabled_q[1]&&!fifo_reset;
    wire write_valid=attempt&&legal&&!qfault;
    always @(posedge qwrite_clk_i or posedge fifo_reset)
        if(fifo_reset)qfault<=0;
        else if(attempt&&(!legal||!write_ready))qfault<=1;
    always @(posedge clk_i or posedge fifo_reset)
        if(fifo_reset)fault_sync<=0;else fault_sync<={fault_sync[0],qfault};
    assign fault_o=fault_sync[1];
    wire [15:0] word_data;
    wire word_valid,word_ready;
    reg high_byte;
    assign data_o=high_byte?word_data[15:8]:word_data[7:0];
    assign valid_o=word_valid&&!fifo_reset&&!fault_o;
    assign word_ready=high_byte&&ready_i&&valid_o;
    async_word_fifo #(.DEPTH(FIFO_WORDS)) queue(
        .reset_i(fifo_reset),.write_clk_i(qwrite_clk_i),.write_data_i(qdata_i),
        .write_valid_i(write_valid),.write_ready_o(write_ready),
        .read_clk_i(clk_i),.read_data_o(word_data),.read_valid_o(word_valid),.read_ready_i(word_ready));
    always @(posedge clk_i or posedge fifo_reset)
        if(fifo_reset)begin high_byte<=0;consumed_bytes_o<=0;end
        else if(valid_o&&ready_i)begin high_byte<=~high_byte;consumed_bytes_o<=consumed_bytes_o+1'b1;end
endmodule

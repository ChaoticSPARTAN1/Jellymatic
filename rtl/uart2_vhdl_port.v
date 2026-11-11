// SPDX-License-Identifier: MIT AND GPL-3.0-or-later
// Additional terms: ADDITIONAL-TERMS.md
// Derived from "Simple UART for FPGA" by Jakub Cabal (https://github.com/jakubcabal/uart_for_fpga),
// MIT License: see LICENSES/MIT-uart_for_fpga.txt. The port and its additions: GPL-3.0-or-later.
// SIMULATION ONLY. A line-by-line Verilog port of the stock Chromatic FPGA's UART_RX and UART_TX
// (ModRetro's esp32t/src/rtl/BSP/uart/uart_rx.vhd and uart_tx.vhd, from Jakub Cabal's UART),
// so the stock UART2 wrapper (uart.v, Verilog, used unchanged from the staged tree) can be run in
// Icarus, which has no VHDL. Every process keeps its VHDL structure: the divider with DIV_CORRECT
// (bit counts 01/10/11 lengthen the bit by one cycle), the registered clock enables, the bit counter,
// the shift register, the control registers latched while idle, parity, the output register and the
// state machines (RX: registered state, combinational outputs; TX: outputs registered with the state).
// Never synthesised: the Gowin build keeps the original VHDL. Checked by scripts/test_chromatic_fpga_media.py
// (chromatic_uart2_tb.sv), which also runs the stock 115,200 setting through it.
`timescale 1ns/1ps
module UART_RX #(parameter P_PARITY_BIT="none") (
    input  wire        CLK,
    input  wire        RST,
    input  wire        UART_RXD,
    input  wire [7:0]  PARITY_BIT,
    input  wire [7:0]  STOP_BIT,
    input  wire [7:0]  DATA_BITS,
    input  wire [23:0] DIVIDER_VALUE,
    input  wire [1:0]  DIV_CORRECT,
    output wire [15:0] DATA_OUT,
    output reg         DATA_VLD,
    output reg         FRAME_ERROR
);
    reg rx_clk_en=1'b0,rx_hf_clk_en=1'b0;
    reg [23:0] rx_ticks=24'd0;
    reg [15:0] rx_data=16'd0;
    reg [7:0]  parity_bit_reg=8'd0,stop_bit_reg=8'd0,data_bits_reg=8'd0;
    reg [23:0] divider_value_reg=24'd0;
    reg [1:0]  div_correct_reg=2'd0;
    reg [3:0]  rx_bit_count=4'd0;
    reg parity_temp=1'b0,rx_parity_error=1'b0;
    reg rx_output_reg_en,rx_bit_count_en,rx_data_shreg_en,rx_clk_divider_en,rx_parity_check_en,rx_busy;
    localparam [2:0] IDLE=3'd0,STARTBIT=3'd1,DATABITS=3'd2,PARITYBIT=3'd3,STOPBIT=3'd4,STOPBIT15=3'd5,STOPBIT2=3'd6;
    reg [2:0] rx_pstate=IDLE,rx_nstate;
    initial begin DATA_VLD=1'b0; FRAME_ERROR=1'b0; end
    // uart_rx_clk_divider: the four VHDL branches differ only in the count compared at the end of a bit
    wire corr=((rx_bit_count[1:0]==2'b01)&&div_correct_reg[1])||((rx_bit_count[1:0]==2'b10)&&(div_correct_reg==2'b11))||
              ((rx_bit_count[1:0]==2'b11)&&(div_correct_reg!=2'b00));
    wire [23:0] end_count=corr?divider_value_reg:(divider_value_reg-24'd1);
    always @(posedge CLK) begin
        if(rx_clk_divider_en) begin
            if(rx_ticks==end_count) begin rx_ticks<=24'd0; rx_clk_en<=1'b0; rx_hf_clk_en<=1'b1; end
            else if(rx_ticks==(divider_value_reg>>1)) begin rx_ticks<=rx_ticks+24'd1; rx_clk_en<=1'b1; rx_hf_clk_en<=1'b1; end
            else begin rx_ticks<=rx_ticks+24'd1; rx_clk_en<=1'b0; rx_hf_clk_en<=1'b0; end
        end else begin rx_ticks<=24'd0; rx_clk_en<=1'b0; rx_hf_clk_en<=1'b0; end
    end
    // uart_rx_bit_counter
    always @(posedge CLK) begin
        if(RST) rx_bit_count<=4'd0;
        else if(rx_bit_count_en&&rx_clk_en) rx_bit_count<=rx_bit_count+4'd1; // 1111 wraps to 0000, as the VHDL
        else if(!rx_bit_count_en) rx_bit_count<=4'd0;
    end
    // uart_rx_data_shift_reg
    always @(posedge CLK) begin
        if(RST) rx_data<=16'd0;
        else if(rx_clk_en&&rx_data_shreg_en) begin
            if(data_bits_reg==8'd5) rx_data<={11'd0,UART_RXD,rx_data[4:1]};
            else if(data_bits_reg==8'd6) rx_data<={10'd0,UART_RXD,rx_data[5:1]};
            else if(data_bits_reg==8'd7) rx_data<={9'd0,UART_RXD,rx_data[6:1]};
            else if(data_bits_reg==8'd8) rx_data<={8'd0,UART_RXD,rx_data[7:1]};
            else rx_data<={UART_RXD,rx_data[15:1]};
        end
    end
    assign DATA_OUT=rx_data;
    // uart_rx_ctrl_reg
    always @(posedge CLK) begin
        if(RST) begin parity_bit_reg<=8'd0; stop_bit_reg<=8'd0; data_bits_reg<=8'd0; divider_value_reg<=24'd0; div_correct_reg<=2'd0; end
        else if(!rx_busy) begin
            parity_bit_reg<=PARITY_BIT; stop_bit_reg<=STOP_BIT; data_bits_reg<=DATA_BITS;
            divider_value_reg<=DIVIDER_VALUE; div_correct_reg<=DIV_CORRECT;
        end
    end
    // parity_gen
    always @(posedge CLK) begin
        if(RST) parity_temp<=1'b0;
        else if(parity_bit_reg==8'd0) parity_temp<=1'b0;
        else if(parity_bit_reg==8'd1) begin
            if(rx_pstate==STARTBIT) parity_temp<=1'b1;
            else if(rx_bit_count_en&&rx_clk_en) parity_temp<=parity_temp^UART_RXD;
        end else if(parity_bit_reg==8'd2) begin
            if(rx_pstate==STARTBIT) parity_temp<=1'b0;
            else if(rx_bit_count_en&&rx_clk_en) parity_temp<=parity_temp^UART_RXD;
        end else if(parity_bit_reg==8'd3) parity_temp<=1'b1;
        else if(parity_bit_reg==8'd4) parity_temp<=1'b0;
    end
    // uart_rx_parity_check_reg
    always @(posedge CLK) begin
        if(RST) rx_parity_error<=1'b0;
        else if(rx_parity_check_en) rx_parity_error<=(parity_bit_reg==8'd0)?1'b0:(parity_temp^UART_RXD);
    end
    // uart_rx_output_reg
    always @(posedge CLK) begin
        if(RST) begin DATA_VLD<=1'b0; FRAME_ERROR<=1'b0; end
        else if(rx_output_reg_en) begin DATA_VLD<=!rx_parity_error&&UART_RXD; FRAME_ERROR<=!UART_RXD; end
        else begin DATA_VLD<=1'b0; FRAME_ERROR<=1'b0; end
    end
    always @(posedge CLK) if(RST) rx_pstate<=IDLE; else rx_pstate<=rx_nstate;
    always @* begin
        rx_output_reg_en=1'b0; rx_bit_count_en=1'b0; rx_data_shreg_en=1'b0; rx_clk_divider_en=1'b0;
        rx_parity_check_en=1'b0; rx_busy=1'b1; rx_nstate=rx_pstate;
        case(rx_pstate)
            IDLE: begin rx_busy=1'b0; rx_nstate=UART_RXD?IDLE:STARTBIT; end
            STARTBIT: begin rx_clk_divider_en=1'b1; rx_nstate=rx_clk_en?DATABITS:STARTBIT; end
            DATABITS: begin
                rx_bit_count_en=1'b1; rx_data_shreg_en=1'b1; rx_clk_divider_en=1'b1; rx_nstate=DATABITS;
                if(rx_clk_en) begin
                    if((data_bits_reg==8'd5&&rx_bit_count==4'd4)||(data_bits_reg==8'd6&&rx_bit_count==4'd5)||
                       (data_bits_reg==8'd7&&rx_bit_count==4'd6)||(data_bits_reg==8'd8&&rx_bit_count==4'd7)||
                       (data_bits_reg==8'd16&&rx_bit_count==4'd15))
                        rx_nstate=(parity_bit_reg==8'd0)?STOPBIT:PARITYBIT;
                end
            end
            PARITYBIT: begin rx_clk_divider_en=1'b1; rx_parity_check_en=1'b1; rx_nstate=rx_clk_en?STOPBIT:PARITYBIT; end
            STOPBIT: begin
                rx_clk_divider_en=1'b1;
                if(rx_clk_en) begin
                    if(stop_bit_reg==8'd1) rx_nstate=STOPBIT15;
                    else if(stop_bit_reg==8'd2) rx_nstate=STOPBIT2;
                    else begin rx_nstate=IDLE; rx_output_reg_en=1'b1; end
                end
            end
            STOPBIT15: begin rx_clk_divider_en=1'b1; if(rx_hf_clk_en) begin rx_nstate=IDLE; rx_output_reg_en=1'b1; end end
            STOPBIT2: begin rx_clk_divider_en=1'b1; if(rx_clk_en) begin rx_nstate=IDLE; rx_output_reg_en=1'b1; end end
            default: begin rx_nstate=IDLE; end
        endcase
    end
endmodule

module UART_TX (
    input  wire        CLK,
    input  wire        RST,
    output reg         UART_TXD,
    input  wire [7:0]  PARITY_BIT,
    input  wire [7:0]  STOP_BIT,
    input  wire [7:0]  DATA_BITS,
    input  wire [23:0] DIVIDER_VALUE,
    input  wire [1:0]  DIV_CORRECT,
    input  wire [15:0] DATA_IN,
    input  wire        DATA_SEND,
    output wire        BUSY
);
    reg tx_clk_en=1'b0,tx_hf_clk_en=1'b0,tx_clk_divider_en=1'b0;
    reg [23:0] tx_ticks=24'd0;
    reg [15:0] tx_data=16'd0;
    reg [7:0]  parity_bit_reg=8'd0,stop_bit_reg=8'd0,data_bits_reg=8'd8;
    reg [23:0] divider_value_reg=24'd32;
    reg [1:0]  div_correct_reg=2'd0;
    reg [3:0]  tx_bit_count=4'd0;
    reg tx_bit_count_en=1'b0,tx_busy=1'b0,parity_temp=1'b0;
    reg [1:0]  tx_data_out_sel=2'd0;
    localparam [2:0] IDLE=3'd0,TXSYNC=3'd1,STARTBIT=3'd2,DATABITS=3'd3,PARITYBIT=3'd4,STOPBIT=3'd5,STOPBIT15=3'd6,STOPBIT2=3'd7;
    reg [2:0] tx_pstate=IDLE;
    initial UART_TXD=1'b1;
    assign BUSY=tx_busy|DATA_SEND;
    // uart_tx_clk_divider
    wire corr=((tx_bit_count[1:0]==2'b01)&&div_correct_reg[1])||((tx_bit_count[1:0]==2'b10)&&(div_correct_reg==2'b11))||
              ((tx_bit_count[1:0]==2'b11)&&(div_correct_reg!=2'b00));
    wire [23:0] end_count=corr?divider_value_reg:(divider_value_reg-24'd1);
    always @(posedge CLK) begin
        if(tx_clk_divider_en) begin
            if(tx_ticks==end_count) begin tx_ticks<=24'd0; tx_clk_en<=1'b0; tx_hf_clk_en<=1'b0; end
            else if(tx_ticks==24'd1) begin tx_ticks<=tx_ticks+24'd1; tx_clk_en<=1'b1; tx_hf_clk_en<=1'b1; end
            else if(tx_ticks==(divider_value_reg>>1)+24'd1) begin tx_ticks<=tx_ticks+24'd1; tx_clk_en<=1'b0; tx_hf_clk_en<=1'b1; end
            else begin tx_ticks<=tx_ticks+24'd1; tx_clk_en<=1'b0; tx_hf_clk_en<=1'b0; end
        end else begin tx_ticks<=24'd0; tx_clk_en<=1'b0; tx_hf_clk_en<=1'b0; end
    end
    // uart_tx_input_data_reg
    always @(posedge CLK) begin
        if(RST) tx_data<=16'd0;
        else if(DATA_SEND&&!tx_busy) tx_data<=DATA_IN;
    end
    // uart_tx_ctrl_reg
    always @(posedge CLK) begin
        if(RST) begin parity_bit_reg<=8'd0; stop_bit_reg<=8'd0; data_bits_reg<=8'd8; divider_value_reg<=24'd32; div_correct_reg<=2'd0; end
        else if(tx_pstate==TXSYNC||tx_pstate==IDLE) begin
            parity_bit_reg<=PARITY_BIT; stop_bit_reg<=STOP_BIT; data_bits_reg<=DATA_BITS;
            divider_value_reg<=DIVIDER_VALUE; div_correct_reg<=DIV_CORRECT;
        end
    end
    // uart_tx_bit_counter
    always @(posedge CLK) begin
        if(RST) tx_bit_count<=4'd0;
        else if(tx_bit_count_en&&tx_clk_en) tx_bit_count<=tx_bit_count+4'd1;
        else if(!tx_bit_count_en) tx_bit_count<=4'd0;
    end
    // uart_tx_output_data_reg
    always @(posedge CLK) begin
        if(RST) UART_TXD<=1'b1;
        else case(tx_data_out_sel)
            2'b01: UART_TXD<=1'b0;
            2'b10: UART_TXD<=tx_data[tx_bit_count];
            2'b11: UART_TXD<=parity_temp;
            default: UART_TXD<=1'b1;
        endcase
    end
    // parity_gen
    always @(posedge CLK) begin
        if(RST) parity_temp<=1'b0;
        else if(parity_bit_reg==8'd0) parity_temp<=1'b0;
        else if(parity_bit_reg==8'd1) begin
            if(tx_pstate==STARTBIT) parity_temp<=1'b1;
            else if(tx_bit_count_en&&tx_clk_en) parity_temp<=parity_temp^tx_data[tx_bit_count];
        end else if(parity_bit_reg==8'd2) begin
            if(tx_pstate==STARTBIT) parity_temp<=1'b0;
            else if(tx_bit_count_en&&tx_clk_en) parity_temp<=parity_temp^tx_data[tx_bit_count];
        end else if(parity_bit_reg==8'd3) parity_temp<=1'b1;
        else if(parity_bit_reg==8'd4) parity_temp<=1'b0;
    end
    // the state machine: outputs registered with the state, as the VHDL clocked process
    always @(posedge CLK) begin
        if(RST) tx_pstate<=IDLE;
        else case(tx_pstate)
            IDLE: begin
                tx_busy<=1'b0; tx_data_out_sel<=2'b00; tx_bit_count_en<=1'b0; tx_clk_divider_en<=1'b0;
                tx_pstate<=DATA_SEND?TXSYNC:IDLE;
            end
            TXSYNC: begin
                tx_busy<=1'b1; tx_data_out_sel<=2'b00; tx_bit_count_en<=1'b0; tx_clk_divider_en<=1'b1;
                tx_pstate<=tx_clk_en?STARTBIT:TXSYNC;
            end
            STARTBIT: begin
                tx_busy<=1'b1; tx_data_out_sel<=2'b01; tx_bit_count_en<=1'b0; tx_clk_divider_en<=1'b1;
                tx_pstate<=tx_clk_en?DATABITS:STARTBIT;
            end
            DATABITS: begin
                tx_busy<=1'b1; tx_data_out_sel<=2'b10; tx_bit_count_en<=1'b1; tx_clk_divider_en<=1'b1;
                if(tx_clk_en&&((data_bits_reg==8'd5&&tx_bit_count==4'd4)||(data_bits_reg==8'd6&&tx_bit_count==4'd5)||
                   (data_bits_reg==8'd7&&tx_bit_count==4'd6)||(data_bits_reg==8'd8&&tx_bit_count==4'd7)||
                   (data_bits_reg==8'd16&&tx_bit_count==4'd15)))
                    tx_pstate<=(parity_bit_reg==8'd0)?STOPBIT:PARITYBIT;
                else tx_pstate<=DATABITS;
            end
            PARITYBIT: begin
                tx_busy<=1'b1; tx_data_out_sel<=2'b11; tx_bit_count_en<=1'b0; tx_clk_divider_en<=1'b1;
                tx_pstate<=tx_clk_en?STOPBIT:PARITYBIT;
            end
            STOPBIT: begin
                tx_data_out_sel<=2'b00; tx_bit_count_en<=1'b0; tx_clk_divider_en<=1'b1;
                if(stop_bit_reg==8'd0) begin
                    tx_busy<=1'b0;
                    if(DATA_SEND) tx_pstate<=TXSYNC; else if(tx_clk_en) tx_pstate<=IDLE; else tx_pstate<=STOPBIT;
                end else if(stop_bit_reg==8'd1) begin
                    tx_busy<=1'b1; tx_pstate<=tx_clk_en?STOPBIT15:STOPBIT;
                end else if(stop_bit_reg==8'd2) begin
                    tx_pstate<=tx_clk_en?STOPBIT2:STOPBIT;
                end
            end
            STOPBIT15: begin
                tx_data_out_sel<=2'b00; tx_bit_count_en<=1'b0; tx_clk_divider_en<=1'b1; tx_busy<=1'b0;
                if(DATA_SEND) tx_pstate<=TXSYNC; else if(tx_hf_clk_en) tx_pstate<=IDLE; else tx_pstate<=STOPBIT15;
            end
            STOPBIT2: begin
                tx_data_out_sel<=2'b00; tx_bit_count_en<=1'b0; tx_clk_divider_en<=1'b1; tx_busy<=1'b0;
                if(DATA_SEND) tx_pstate<=TXSYNC; else if(tx_clk_en) tx_pstate<=IDLE; else tx_pstate<=STOPBIT2;
            end
            default: begin
                tx_busy<=1'b1; tx_data_out_sel<=2'b00; tx_bit_count_en<=1'b0; tx_clk_divider_en<=1'b0; tx_pstate<=IDLE;
            end
        endcase
    end
endmodule

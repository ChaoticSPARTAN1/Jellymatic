//
// File name          :uart.v
// Module name        :uart.v
// Created by         :GaoYun Semi
// Author             :(Winson)
// Created On         :2020-11-05 14:26 GuangZhou
// Last Modified      :
// Update Count       :2020-11-05 14:26
// Description        :
//----------------------------------------------------------------------
//===========================================
module UART2 
#(    parameter CLK_FREQ    = 30'd60000000 // set system clock frequency in Hz
      //parameter BAUD_RATE   = 4000000   // baud rate value
     // parameter P_PARITY_BIT  = "none"   // legal values: "none", "even", "odd", "mark", "space"
)
(
    input              CLK        , // clock
    input              RST        , // reset
        // UART INTERFACE
    output             UART_TXD   ,
    input              UART_RXD   ,
    output             UART_RTS   , // when UART_RTS = 0, UART This Device Ready to receive.
    input              UART_CTS   , // when UART_CTS = 0, UART Opposite Device Ready to receive.
        // UART Control Reg
    input  [31:0]      BAUD_RATE ,
    input  [7:0]       PARITY_BIT,
    input  [7:0]       STOP_BIT  ,
    input  [7:0]       DATA_BITS ,
        // USER DATA INPUT INTERFACE
    input   [15:0]     TX_DATA    , //
    input              TX_DATA_VAL, // when TX_DATA_VAL = 1, data on TX_DATA will be transmit, DATA_SEND can set to 1 only when BUSY = 0
    output             TX_BUSY    , // when BUSY = 1 transiever is busy, you must not set DATA_SEND to 1
        // USER FIFO CONTROL INTERFACE
    output  [15:0]     RX_DATA    , //
    output             RX_DATA_VAL  //
);


    reg  [3:0] uart_rxd_shreg     ;
    reg        uart_rxd_debounced ;
    reg  [3:0] uart_cts_shreg     ;
    reg        uart_cts_debounced ;
    wire       uart_tx_busy       ;
    wire [31:0]div_out ;
    wire       div_cmpl;
    // Chromatic pocket link: the stock UART2 runs divider 72 with div_correct 3 (bit counts 01, 10
    // and 11 last one cycle longer: 72.75 gClk per bit = 115,307 baud), whatever BAUD_RATE says.
    // The media link selects exactly gClk/16 = 524,288 baud: divider 16 AND the correction off (with
    // the correction a divider of 16 would be 16.75 cycles, 500,812 baud; the first proposal's
    // divider 8 with the correction was 8.75 cycles, 958,698 baud instead of the 1,048,576 meant).
    // The 4-sample RX debouncer stays: at 16 cycles per bit its 1-5 cycles of edge skew leave the
    // sampling point inside the bit with margin (rtl/chromatic_uart2_tb.sv measures it). A 2-way
    // mux on the BAUD_RATE input: every other rate keeps the exact stock 'd72 / 'd3.
    wire        fast_rate = (BAUD_RATE == 32'd524288);
    wire  [1:0] div_correct = fast_rate ? 2'd0 : 2'd3;

    wire [23:0] divider_value = fast_rate ? 24'd16 : 24'd72;// = CLK_FREQ/(15*BAUD_RATE);
    // -------------------------------------------------------------------------
    // UART DIVIDER VALUE
    // -------------------------------------------------------------------------
    //always @(posedge CLK or posedge RST) begin
    //    if (RST) begin
    //        divider_value <= 24'd0;
    //    end
    //    else begin
    //        //divider_value <= CLK_FREQ/(15*BAUD_RATE);
    //        divider_value <= CLK_FREQ/(BAUD_RATE);
    //    end
    //end
    // -------------------------------------------------------------------------
    // UART RXD DEBAUNCER
    // -------------------------------------------------------------------------
    always @(posedge CLK or posedge RST) begin
        if (RST) begin
            uart_rxd_shreg <= 4'hF;
            uart_rxd_debounced <= 1'b1;
        end
        else begin
            uart_rxd_shreg <= {UART_RXD, uart_rxd_shreg[3:1]};
            // Chromatic pocket link (19): at the fast rate the 4-sample OR is bypassed. It delays a falling
            // edge by 4-5 cycles; with the receiver's own start latency that put the sample 15 of 16
            // cycles into the bit (rtl/chromatic_uart2_tb.sv). Fast: a plain 2-flop synchroniser
            // (uart_rxd_shreg[3] is the first flop). The ESP32 line is point to point, push-pull; the
            // stock rate keeps the stock filter exactly.
            uart_rxd_debounced <= fast_rate ? uart_rxd_shreg[3] :
                                  (uart_rxd_shreg[0]|uart_rxd_shreg[1]|uart_rxd_shreg[2]|uart_rxd_shreg[3]);
        end
    end

    always @(posedge CLK or posedge RST) begin
        if (RST) begin
            uart_cts_shreg <= 4'hF;
            uart_cts_debounced <= 1'b1;
        end
        else begin
            uart_cts_shreg <= {UART_CTS, uart_cts_shreg[3:1]};
            uart_cts_debounced <= uart_cts_shreg[0]|uart_cts_shreg[1]|uart_cts_shreg[2]|uart_cts_shreg[3];
        end
    end

    assign UART_RTS  = 1'b0;
    assign TX_BUSY   = uart_tx_busy | UART_CTS;

    // -------------------------------------------------------------------------
    // UART TRANSMITTER
    // -------------------------------------------------------------------------
    UART_TX 
    //#( .P_PARITY_BIT (P_PARITY_BIT)) 
        uart_tx_inst (
            .CLK         (CLK         )
           ,.RST         (RST         )
            // UART INTERFACE
           //,.UART_CLK_EN (uart_clk_en )
           ,.UART_TXD    (UART_TXD    )
            // UART PARAMETER
           //,.UART_PARITY_BIT (UART_PARITY_BIT  )
           //,.UART_DATA_BIT   (UART_DATA_BIT    )
           ,.PARITY_BIT  (PARITY_BIT)
           ,.STOP_BIT    (STOP_BIT  )//0: 1 stop bit  1: 1.5 stop bit 2: 2 stop bit
           ,.DATA_BITS   (DATA_BITS )//5 6 7 8 16
           ,.DIVIDER_VALUE(divider_value )//16'd37
           ,.DIV_CORRECT(div_correct  )//16'd37

            // USER DATA INPUT INTERFACE
           ,.DATA_IN     (TX_DATA     )
           ,.DATA_SEND   (TX_DATA_VAL )
           ,.BUSY        (uart_tx_busy)
    );

    // -------------------------------------------------------------------------
    // UART RECEIVER
    // ------------------------------------------------------------------------- 
    UART_RX 
    #( .P_PARITY_BIT ("odd")) 
        uart_rx_inst (
            .CLK         (CLK                )
           ,.RST         (RST                )
           // UART INTERFACE
           //,.UART_CLK_EN (uart_clk_en        )
           ,.UART_RXD    (uart_rxd_debounced )
            // UART PARAMETER
           //,.UART_PARITY_BIT (UART_PARITY_BIT  )
           //,.UART_DATA_BIT (UART_DATA_BIT  )
           ,.PARITY_BIT  (PARITY_BIT )
           ,.STOP_BIT    (STOP_BIT   )//0: 1 stop bit  1: 1.5 stop bit 2: 2 stop bit
           ,.DATA_BITS   (DATA_BITS  )//5 6 7 8 16
           ,.DIVIDER_VALUE(divider_value)//
           ,.DIV_CORRECT(div_correct  )//16'd37
            // USER DATA INPUT INTERFACE
           ,.DATA_OUT    (RX_DATA    )
           ,.DATA_VLD    (RX_DATA_VAL)
           ,.FRAME_ERROR (           )
    );


endmodule

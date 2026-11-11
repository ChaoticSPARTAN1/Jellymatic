// SPDX-License-Identifier: GPL-3.0-or-later
// Additional terms: ADDITIONAL-TERMS.md
// The Chromatic's MCU<->FPGA UART2 itself, at the rate the Pocket link uses, against an ESP32 model.
// DUT: the STAGED uart.v (the stock wrapper with its 4-sample RX debouncer, plus the pocket-link rate
// mux), with UART_RX/UART_TX from rtl/uart2_vhdl_port.v (a line-by-line port of the stock VHDL:
// DIV_CORRECT, registered enables, state machines). gClk is 8,388,608 Hz with a random phase.
// The ESP32 end is modelled as its UART hardware runs (IDF 5.3 uart_ll_set_baudrate on ESP32):
// clk_div = (80 MHz << 4) / baud, bit periods of div_int or div_int+1 APB cycles (12.5 ns) spreading
// div_frag/16, start detected on an APB edge, bits sampled at the middle of its own bit periods.
// Every line edge gets +-JITTER ns of uniform jitter, both ways. ERR stretches the ESP32's clock
// (a rate error sweep). Traffic: 0x55 bytes (every bit an edge, so the eye is measured, not hidden
// by equal neighbours), then random bytes, back to back and with gaps, both directions at once.
// Prints MARGIN lines (worst distance from a sample instant to the edges bounding its bit, in ns and
// gClk cycles) and RESULT; $fatal on any byte wrong in either direction. Run by
// scripts/test_chromatic_fpga_media.py. No hardware.
`timescale 1ns/1ps
module chromatic_uart2_tb;
    parameter integer BAUD=524288;        // the BAUD_RATE input of UART2 (top.v's mux)
    parameter integer ESP_BAUD=524288;    // what the launcher asks of the ESP32 (uart_set_baudrate)
    parameter integer ERR_PPM=0;          // ESP32 clock error, parts per million (+: slower ESP32)
    parameter integer JITTER_PS=20000;    // +- edge jitter
    parameter integer NBYTES=300;
    parameter integer SEED=7;
    parameter integer EXPECT_FAIL=0;      // 1: the run must see errors (the old 8-cycle hook)
    localparam real GCLK_NS=1.0e9/8388608.0;
    localparam real APB_NS=12.5*(1.0+ERR_PPM*1.0e-6);
    localparam integer CLKDIV=(80000000*16)/ESP_BAUD;   // integer division, as IDF
    integer seed=SEED;
    reg clk=0;
    real phase;
    initial begin phase=($urandom(seed)%1000)*GCLK_NS/1000.0; #(phase); forever #(GCLK_NS/2.0) clk=~clk; end
    reg rst=1; initial begin #2000; rst=0; end

    reg  esp_txd=1;          // ESP32 TX -> FPGA UART_RXD
    wire fpga_txd;           // FPGA UART_TXD -> ESP32 RX
    reg  [15:0] tx_data=0; reg tx_val=0; wire tx_busy;
    wire [15:0] rx_data; wire rx_val;
    UART2 #(.CLK_FREQ(30'd8388608)) dut(.CLK(clk),.RST(rst),.UART_TXD(fpga_txd),.UART_RXD(esp_txd),.UART_RTS(),.UART_CTS(1'b0),
        .BAUD_RATE(BAUD),.PARITY_BIT(8'd0),.STOP_BIT(8'd0),.DATA_BITS(8'd8),
        .TX_DATA(tx_data),.TX_DATA_VAL(tx_val),.TX_BUSY(tx_busy),.RX_DATA(rx_data),.RX_DATA_VAL(rx_val));

    // (signed: $urandom is unsigned, and an unsigned subtraction would wrap to a huge delay)
    function real jit; input integer dummy; integer r; begin r=$urandom(seed)%2001; jit=((r-1000)/1000.0)*JITTER_PS/1000.0; end endfunction
    // ---- ESP32 bit clock: div_int or div_int+1 APB cycles, the fraction accumulated ----
    integer acc_tx=0,acc_rx=0;
    function real esp_bit_tx; input integer d; begin acc_tx=acc_tx+(CLKDIV&15);
        if(acc_tx>=16) begin acc_tx=acc_tx-16; esp_bit_tx=((CLKDIV>>4)+1)*APB_NS; end else esp_bit_tx=(CLKDIV>>4)*APB_NS; end endfunction
    function real esp_bit_rx; input integer d; begin acc_rx=acc_rx+(CLKDIV&15);
        if(acc_rx>=16) begin acc_rx=acc_rx-16; esp_bit_rx=((CLKDIV>>4)+1)*APB_NS; end else esp_bit_rx=(CLKDIV>>4)*APB_NS; end endfunction

    // ---- traffic: what each side sent, in order ----
    reg [7:0] to_fpga[0:NBYTES-1], to_esp[0:NBYTES-1];
    integer i,got_fpga=0,got_esp=0,err_fpga=0,err_esp=0,measured=1;
    initial for(i=0;i<NBYTES;i=i+1) begin
        to_fpga[i]=i<NBYTES/3?8'h55:$urandom(seed); to_esp[i]=i<NBYTES/3?8'h55:$urandom(seed); end

    // ESP32 TX: start, 8 data LSB first, stop; jittered edges; back to back or a random gap
    task esp_send(input [7:0] b); integer k; reg v; real t; begin
        for(k=0;k<10;k=k+1) begin
            v=k==0?1'b0:k==9?1'b1:b[k-1];
            esp_txd <= #(JITTER_PS/1000.0+jit(0)) v;
            t=esp_bit_tx(0); #(t);
        end
    end endtask
    initial begin
        @(negedge rst); #(5000);
        for(i=0;i<NBYTES;i=i+1) begin
            esp_send(to_fpga[i]);
            if(i>=NBYTES/3&&($urandom(seed)%4)==0) #(($urandom(seed)%3)*esp_bit_tx(0));
        end
    end
    // FPGA TX: as system_monitor does, a one-cycle TX_DATA_VAL whenever the transmitter is not busy (back to back)
    integer j;
    initial begin
        @(negedge rst); #(7000);
        for(j=0;j<NBYTES;j=j+1) begin
            @(posedge clk); while(tx_busy) @(posedge clk);
            #1; tx_data={8'd0,to_esp[j]}; tx_val=1; @(posedge clk); #1; tx_val=0;
            @(posedge clk); // BUSY rises one cycle late: the arbiter (with Shiny Relay's fix, hook 16) looks again only after it
            if(j>=NBYTES/3&&($urandom(seed)%4)==0) repeat($urandom(seed)%40) @(posedge clk);
        end
    end
    // FPGA RX: every byte checked
    always @(posedge clk) if(rx_val) begin
        if(got_fpga<NBYTES&&rx_data[7:0]!==to_fpga[got_fpga]) err_fpga=err_fpga+1;
        got_fpga=got_fpga+1;
    end

    // ---- eye margins on the 0x55 bytes (every bit bounded by two real edges) ----
    // FPGA receive: sample instants are the clock edges with rx_clk_en (the shift register / stop
    // decision); the line it samples is the debounced one inside uart.v.
    real deb_edge=0, rx_sample=-1, rx_left=1e9, rx_right=1e9;
    always @(dut.uart_rxd_debounced) begin
        if(measured&&rx_sample>=0) begin if($realtime-rx_sample<rx_right) rx_right=$realtime-rx_sample; rx_sample=-1; end
        deb_edge=$realtime;
    end
    always @(posedge clk) if(dut.uart_rx_inst.rx_clk_en&&(dut.uart_rx_inst.rx_pstate==3'd2||dut.uart_rx_inst.rx_pstate==3'd1)&&got_fpga<NBYTES/3-1) begin
        if($realtime-deb_edge<rx_left) rx_left=$realtime-deb_edge;
        rx_sample=$realtime;
    end
    // ESP32 receive model: start found on an APB edge after the falling edge, bits at mid-period
    real tx_edge=0, esp_sample=-1, tx_left=1e9, tx_right=1e9;
    always @(fpga_txd) begin
        if(esp_sample>=0) begin if($realtime-esp_sample<tx_right) tx_right=$realtime-esp_sample; esp_sample=-1; end
        tx_edge=$realtime;
    end
    reg [7:0] eb; integer k2; real per,acc_t;
    initial begin
        @(negedge rst);
        forever begin
            @(negedge fpga_txd);
            #((APB_NS-0.001)*($urandom(seed)%1000)/1000.0+jit(0)+JITTER_PS/1000.0); // the next APB edge, jittered
            acc_t=0; eb=0;
            for(k2=0;k2<10;k2=k2+1) begin
                per=esp_bit_rx(0);
                #(per/2.0);
                if(got_esp<NBYTES/3-1) begin
                    if($realtime-tx_edge<tx_left) tx_left=$realtime-tx_edge;
                    esp_sample=$realtime;
                end
                if(k2==0&&fpga_txd!==1'b0) err_esp=err_esp+1;       // start bit
                else if(k2>=1&&k2<=8) eb[k2-1]=fpga_txd;
                else if(k2==9&&fpga_txd!==1'b1) err_esp=err_esp+1;  // stop bit
                if(k2<9) #(per/2.0);                                // after the stop sample it hunts for the next start at once
            end
            if(got_esp<NBYTES&&eb!==to_esp[got_esp]) err_esp=err_esp+1;
            got_esp=got_esp+1;
        end
    end

    initial begin
        #(NBYTES*12.0*1.0e9/BAUD*1.6+2.0e6);
        $display("MARGIN fpga_rx left=%0.1fns/%0.2fcyc right=%0.1fns/%0.2fcyc  esp_rx left=%0.1fns/%0.2fcyc right=%0.1fns/%0.2fcyc",
                 rx_left,rx_left/GCLK_NS,rx_right,rx_right/GCLK_NS,tx_left,tx_left/GCLK_NS,tx_right,tx_right/GCLK_NS);
        $display("RESULT baud=%0d esp_baud=%0d clkdiv=%0d err_ppm=%0d jitter_ps=%0d fpga_rx=%0d/%0d errors=%0d esp_rx=%0d/%0d errors=%0d",
                 BAUD,ESP_BAUD,CLKDIV,ERR_PPM,JITTER_PS,got_fpga,NBYTES,err_fpga,got_esp,NBYTES,err_esp);
        if(EXPECT_FAIL) begin
            if(err_fpga==0&&err_esp==0&&got_fpga==NBYTES&&got_esp==NBYTES) $fatal(1,"expected errors at this setting, saw none");
        end else if(err_fpga||err_esp||got_fpga!=NBYTES||got_esp!=NBYTES) $fatal(1,"UART2 bytes wrong or lost");
        $finish;
    end
endmodule

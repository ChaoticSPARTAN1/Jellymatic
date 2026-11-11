// SPDX-License-Identifier: GPL-3.0-or-later
// Additional terms: ADDITIONAL-TERMS.md
// The link input filter of the Chromatic bridge (chromatic_pocket_link, GLITCH_SAMPLES), tested
// against the Pocket's real link block (pocket_media_link at 90 MHz, as the core builds it) with
// disturbances put on the two wires that arrive at the Chromatic: LINK_CLK and LINK_IN.
// One run = one bridge setting, one link rate, one kind of disturbance. Ordered bytes go both
// ways at once while the Pocket polls continuously; the run is "intact" only when both ends
// received exactly the bytes that were sent, in order, with none extra, and the bridge shows
// no fault bit. A single extra or lost clock edge is a bit slip and fails that at once.
// The two clocks are unrelated (one link bit is 32.6 gClk periods), so every glitch position
// against gClk is visited within a run; +phase_ps moves the start as well.
// No hardware; run by scripts/test_chromatic_link_filter.py.
//
// Parameter GLITCH: the bridge's GLITCH_SAMPLES (3 = the built image, 0 = no filter).
// Plusargs:
//   +khz=N       link clock asked of the Pocket (default 256); reg 3 = 90000/(2N) as the app
//   +n=N         bytes each way (default 96)
//   +gw_ps=N     glitch width
//   +gmode=N     sum of: 1 bounce after every clock fall (back high for gw, goff after the edge)
//                        2 bounce after every clock rise (back low for gw, goff after the edge)
//                        4 lone high pulse in the middle of every low clock phase
//                        8 lone low pulse in the middle of every high clock phase (also between bytes)
//                       16 data inverted for gw from every clock rise (where the bridge samples)
//                       32 data inverted for gw in the middle of every low clock phase
//   +goff_ps=N   bounce start after the edge (default 60000)
//   +phase_ps=N  gClk start offset
//   +expect=1 intact required, 0 damage required (the control: the same glitch without the filter),
//           2 report only
`timescale 1ns/1ps
module chromatic_pocket_link_glitch_tb #(parameter integer GLITCH=3);
    integer khz=256,n=96,gw_ps=0,gmode=0,goff_ps=60000,phase_ps=0,expect_ok=1;
    real GP=119.2093; // gClk period
    reg pclk=0,gclk=0;
    always #5.5555 pclk=~pclk;     // 90 MHz
    initial begin
        if(!$value$plusargs("khz=%d",khz)) khz=256;
        if(!$value$plusargs("n=%d",n)) n=96;
        if(!$value$plusargs("gw_ps=%d",gw_ps)) gw_ps=0;
        if(!$value$plusargs("gmode=%d",gmode)) gmode=0;
        if(!$value$plusargs("goff_ps=%d",goff_ps)) goff_ps=60000;
        if(!$value$plusargs("phase_ps=%d",phase_ps)) phase_ps=0;
        if(!$value$plusargs("expect=%d",expect_ok)) expect_ok=1;
        #(phase_ps/1000.0);
        forever #59.60465 gclk=~gclk; // 8.388608 MHz
    end

    // ---- Pocket ----
    reg preset_n=0,wr=0,rd=0; reg [4:0] addr=0; reg [31:0] wdata=0; wire [31:0] rdata;
    wire pso,poe,psck,psckoe,irq;
    // ---- Chromatic ----
    reg reset=1,uart_rx_val=0,uart_tx_busy=0,up_taken=0; reg [7:0] uart_rx_data=0,tx_bytepos=0;
    wire [6:0] rx_address; wire [79:0] rx_data; wire rx_data_val,uart_fast,up_req;
    wire [7:0] up_byte; wire link_so,link_clk,link_clk_oe; wire [1:0] mode; wire [3:0] faults;

    // ---- the two wires into the Chromatic, with the disturbances ----
    wire raw_clk=psckoe?psck:1'b1;
    wire raw_dat=poe?pso:1'b1;
    reg g1=0,g2=0,g4=0,g8=0,d16=0,d32=0,gl_on=0;
    wire clk_pin=raw_clk^g1^g2^g4^g8;
    wire dat_pin=raw_dat^d16^d32;
    integer glitches=0;
    real gw,goff,half_ns,mid;
    always @(negedge raw_clk) if(gl_on&&gmode[0]) begin #(goff) g1=1; #(gw) g1=0; glitches=glitches+1; end
    always @(posedge raw_clk) if(gl_on&&gmode[1]) begin #(goff) g2=1; #(gw) g2=0; glitches=glitches+1; end
    always @(negedge raw_clk) if(gl_on&&gmode[2]) begin #(mid) g4=1; #(gw) g4=0; glitches=glitches+1; end
    always @(posedge raw_clk) if(gl_on&&gmode[3]) begin #(mid) g8=1; #(gw) g8=0; glitches=glitches+1; end
    always @(posedge raw_clk) if(gl_on&&gmode[4]) begin d16=1; #(gw) d16=0; glitches=glitches+1; end
    always @(negedge raw_clk) if(gl_on&&gmode[5]) begin #(mid) d32=1; #(gw) d32=0; glitches=glitches+1; end

    pocket_media_link #(.CLK_HZ(90000000),.SCK_HZ(256000)) pocket(
        .clk(pclk),.reset_n(preset_n),.reg_wr(wr),.reg_rd(rd),.reg_addr(addr),.reg_wdata(wdata),.reg_rdata(rdata),
        .link_si_i(link_so),.link_so_o(pso),.link_so_oe(poe),.link_sck_i(link_clk_oe?link_clk:1'b1),
        .link_sck_o(psck),.link_sck_oe(psckoe),.link_sd_i(1'b0),.link_sd_o(),.link_sd_oe(),.irq(irq));
    uart_packet_wrapper_rx stock_rx(.clk(gclk),.reset(reset),.uart_rx_data(uart_rx_data),.uart_rx_val(uart_rx_val),
        .uartDisabled(1'b0),.rx_address(rx_address),.rx_data(rx_data),.rx_data_val(rx_data_val));
    chromatic_pocket_link #(.GLITCH_SAMPLES(GLITCH)) dut(
        .clk(gclk),.reset(reset),.rx_data_val(rx_data_val),.rx_address(rx_address),.rx_data(rx_data),
        .uart_rx_data_i(uart_rx_data),.uart_rx_val_i(uart_rx_val),.uart_tx_busy_i(uart_tx_busy),.uart_fast_o(uart_fast),
        .up_req(up_req),.up_taken(up_taken),.tx_bytepos(tx_bytepos),.up_byte(up_byte),
        .game_so_i(1'b1),.game_clk_i(1'b1),.game_clk_oe_i(1'b0),
        .link_si_i(dat_pin),.link_clk_i(clk_pin),
        .link_so_o(link_so),.link_clk_o(link_clk),.link_clk_oe(link_clk_oe),.mode_o(mode),.faults_o(faults));

    // ---- MCU side: the stock packet form; bytes spaced as at the fast rate to keep runs short
    //      (the bridge's parsers take a byte whenever one is presented) ----
    real byte_ns=1e9/524288.0*10.0;
    function [7:0] crc8(input [7:0] crc,input [7:0] d); integer i; begin
        crc=crc^d; for(i=0;i<8;i=i+1) crc=crc[7]?{crc[6:0],1'b0}^8'h1d:{crc[6:0],1'b0}; crc8=crc; end endfunction
    task uart_byte(input [7:0] b); begin
        @(negedge gclk); uart_rx_data=b; uart_rx_val=1; @(negedge gclk); uart_rx_val=0; #(byte_ns-119.2); end endtask
    reg [7:0] pkt[0:63];
    task send_packet(input [6:0] address,input integer count); reg [7:0] c; integer i; begin
        c=8'hff; c=crc8(c,8'h8f); c=crc8(c,{1'b0,address}); c=crc8(c,count[7:0]);
        for(i=0;i<count;i=i+1) c=crc8(c,pkt[i]);
        uart_byte(8'h8f); uart_byte({1'b0,address}); uart_byte(count[7:0]);
        for(i=0;i<count;i=i+1) uart_byte(pkt[i]); uart_byte(c); end endtask
    task ctrl(input [7:0] cmd); begin
        pkt[0]=cmd; pkt[1]=8'h50; pkt[2]=8'h4a; pkt[3]=8'h4c; pkt[4]=8'h4b; send_packet(7'h1e,5); end endtask

    // the stock arbiter: takes the status packet some byte times after the request
    reg arb_flag=0; integer up_bytes=0; reg [7:0] up_seq[0:4095];
    always @(posedge gclk) if(up_req) begin
        if(arb_flag) $fatal(1,"second request while the packet is pending"); arb_flag=1; end
    reg [7:0] sampled[0:9]; integer sp;
    initial forever begin
        wait(arb_flag);
        #(byte_ns*6); uart_tx_busy=1; #(byte_ns*3);
        for(sp=0;sp<10;sp=sp+1) begin tx_bytepos=sp; @(negedge gclk); sampled[sp]=up_byte; #(byte_ns-119.2); end
        #(byte_ns); uart_tx_busy=0;
        @(negedge gclk); up_taken=1; @(negedge gclk); up_taken=0; arb_flag=0;
        if(sampled[1]>6) $fatal(1,"status packet claims %0d bytes",sampled[1]);
        for(sp=0;sp<sampled[1];sp=sp+1) if(up_bytes<4096) begin up_seq[up_bytes]=sampled[4+sp]; up_bytes=up_bytes+1; end
    end

    // ---- Pocket register access: one thread owns the bus ----
    task write_reg(input [4:0] a,input [31:0] d); begin
        @(negedge pclk); addr=a; wdata=d; wr=1; @(negedge pclk); wr=0; end endtask
    task read_reg(input [4:0] a,output [31:0] v); begin
        @(negedge pclk); addr=a; rd=1; @(negedge pclk); rd=0; @(negedge pclk); #1; v=rdata; end endtask
    function [7:0] down_byte(input integer i); down_byte=(i*37+11)%255; endfunction // never FF, the idle byte
    function [7:0] up_value(input integer i); up_value=(i*91+5)%255; endfunction
    reg [7:0] rx_seq[0:4095]; integer rx_count=0,up_sent=0; reg serve=0; reg [31:0] st,rv;
    initial forever begin
        wait(serve);
        read_reg(0,st);
        if(st[5]) begin read_reg(2,rv); if(rx_count<4096) begin rx_seq[rx_count]=rv[7:0]; rx_count=rx_count+1; end end
        else if(st[4]&&up_sent<n) begin write_reg(1,{24'd0,up_value(up_sent)}); up_sent=up_sent+1; end
    end

    // ---- when the answer bit moves on LINK_OUT, measured from the clock fall at the Chromatic's pin (clean runs) ----
    real t_fall=0,lat,lat_min=1e9,lat_max=0;
    always @(negedge clk_pin) t_fall=$realtime;
    always @(link_so) if(gl_on&&gmode==0&&mode==2) begin
        lat=$realtime-t_fall; if(lat<lat_min) lat_min=lat; if(lat>lat_max) lat_max=lat; end

    integer i,k,sent,chunk,bad_down,bad_up,extra_down,extra_up,ok,half,lo,hi; reg timed_out=0;
    initial begin
        #2; gw=gw_ps/1000.0; goff=goff_ps/1000.0;
        half=90000/(2*khz); half_ns=half*11.111; mid=(half_ns-gw)/2.0;
        if(gmode!=0&&(gw<=0||goff+gw>=half_ns-20||mid<=0)) $fatal(1,"glitch does not fit the clock phase");
        #1000; reset=0; preset_n=1;
        ctrl(1); wait(mode==2); #2000;
        // the app's open: reset, rate, enable with the clock idle high for 3 ms, then polling
        write_reg(0,2); write_reg(3,half); write_reg(0,5); #3000000;
        if(dut.link.wire_byte.armed!==1) $fatal(1,"the bridge did not arm in the quiet gap");
        write_reg(0,13); gl_on=1; serve=1;
        fork
            begin : mcu // downstream, with the MCU's credit rule
                sent=0;
                while(sent<n) begin
                    chunk=(n-sent>64)?64:(n-sent);
                    for(k=0;k<chunk;k=k+1) pkt[k]=down_byte(sent+k);
                    send_packet(7'h1f,chunk); sent=sent+chunk;
                    #2000; while(dut.tx_level>0||dut.draining) #1000;
                end
            end
            begin : limit #(60000000+n*40000.0*256/khz); timed_out=1; end
            begin wait((rx_count>=n&&up_bytes>=n&&sent>=n)||timed_out); disable limit; disable mcu; end
        join
        #3000000; // idle polling goes on (and so do the glitches): nothing more may arrive
        gl_on=0; serve=0; #100000;
        bad_down=0; bad_up=0;
        for(i=0;i<n&&i<rx_count;i=i+1) if(rx_seq[i]!==down_byte(i)) bad_down=bad_down+1;
        for(i=0;i<n&&i<up_bytes;i=i+1) if(up_seq[i]!==up_value(i)) bad_up=bad_up+1;
        extra_down=rx_count-n; extra_up=up_bytes-n;
        ok=(rx_count==n&&up_bytes==n&&bad_down==0&&bad_up==0&&faults===4'd0&&!timed_out);
        $display("RESULT intact=%0d filter=%0d khz=%0d half=%0d gmode=%0d gw_ps=%0d goff_ps=%0d phase_ps=%0d down=%0d/%0d down_wrong=%0d up=%0d/%0d up_wrong=%0d faults=%b timeout=%0d glitches=%0d so_ns=%0.0f..%0.0f",
            ok,GLITCH,khz,half,gmode,gw_ps,goff_ps,phase_ps,rx_count,n,bad_down,up_bytes,n,bad_up,faults,timed_out,glitches,
            gmode==0?lat_min:0.0,gmode==0?lat_max:0.0);
        if(gmode==0&&ok) begin
            // LINK_OUT moves (2 engine flops + the register) after the filter's GLITCH samples: a bound, not a typical value
            lo=(GLITCH>=2)?GLITCH+2:2; hi=lo+1;
            if(lat_min<lo*GP-1.0||lat_max>hi*GP+1.0)
                $fatal(1,"LINK_OUT moved %0.0f..%0.0f ns after the clock fall, outside %0d..%0d gClk periods",lat_min,lat_max,lo,hi);
            $display("INFO: LINK_OUT moves %0.0f to %0.0f ns after the falling clock edge (%0d to %0d gClk periods); the Pocket samples %0.0f ns after it at %0d kHz",
                lat_min,lat_max,lo,hi,half_ns,khz);
        end
        if(gmode!=0&&glitches<n*8) $fatal(1,"only %0d glitches were applied",glitches);
        if(expect_ok==1&&!ok) $fatal(1,"FAIL: the stream was damaged");
        if(expect_ok==0&&ok) $fatal(1,"FAIL: the control run was not damaged, so this glitch proves nothing");
        $display("PASS: %0s", expect_ok==1?"both directions intact":expect_ok==0?"damaged, as the control expects":"reported only");
        $finish;
    end
endmodule

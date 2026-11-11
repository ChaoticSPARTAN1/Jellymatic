// SPDX-License-Identifier: GPL-3.0-or-later
// Additional terms: ADDITIONAL-TERMS.md
// Two FPGAs on one cable: the Chromatic pocket-link bridge (gClk 8.388 MHz) fed by the STOCK
// uart_packet_wrapper_rx plus the raw UART bytes (115200, then 524,288 baud), and the
// Pocket's pocket_media_link MMIO peripheral (25 MHz model clock) on an unrelated clock,
// as master at ~260 kHz and ~520 kHz. The bridge runs with its default link input filter
// (GLITCH_SAMPLES 3), as in the built image. A 1 MHz link needs the filter off; that case,
// glitches and the 90 MHz Pocket block are in rtl/chromatic_pocket_link_glitch_tb.sv
// (scripts/test_chromatic_link_filter.py).
// Checks the wire contract, the game-mode pass-through, the arming gap the Pocket must give,
// 64-byte downstream packets in order and the padded tail, upstream status packets through
// a model of the system_monitor arbiter, the queue-full drop fault, the keyed fast-UART
// switch with TX-idle guard and its status bit, downstream throughput at the fast rate on a
// 512 kHz link, SysCtrl exit (pins and UART rate back) and heartbeat expiry.
// No hardware; run by scripts/test_pocket_tether.py.
`timescale 1ns/1ps
module chromatic_pocket_link_tb;
    // ---- clocks ----
    reg gclk=0; always #59.6 gclk=~gclk;       // 8.388 MHz
    reg pclk=0; always #20 pclk=~pclk;         // Pocket 25 MHz model
    reg reset=1;
    // ---- MCU UART into the stock wrapper and the bridge's own parser ----
    reg [7:0] uart_rx_data=0; reg uart_rx_val=0; reg uart_tx_busy=0;
    wire [6:0] rx_address; wire [79:0] rx_data; wire rx_data_val; wire uart_fast;
    uart_packet_wrapper_rx stock_rx(.clk(gclk),.reset(reset),.uart_rx_data(uart_rx_data),.uart_rx_val(uart_rx_val),
        .uartDisabled(1'b0),.rx_address(rx_address),.rx_data(rx_data),.rx_data_val(rx_data_val));
    // ---- the bridge ----
    wire up_req; reg up_taken=0; reg [7:0] tx_bytepos=0; wire [7:0] up_byte;
    reg game_so=1,game_clk=1,game_clk_oe=0;
    wire link_so,link_clk,link_clk_oe; wire [1:0] mode; wire [3:0] faults;
    wire pso,poe,psck,psckoe;
    chromatic_pocket_link #(.LINK_FIFO_BYTES(64),.HEARTBEAT_CYCLES(8388608/4),.FAST_CONFIRM_CYCLES(8388608/10)) dut(
        .clk(gclk),.reset(reset),.rx_data_val(rx_data_val),.rx_address(rx_address),.rx_data(rx_data),
        .uart_rx_data_i(uart_rx_data),.uart_rx_val_i(uart_rx_val),.uart_tx_busy_i(uart_tx_busy),.uart_fast_o(uart_fast),
        .up_req(up_req),.up_taken(up_taken),.tx_bytepos(tx_bytepos),.up_byte(up_byte),
        .game_so_i(game_so),.game_clk_i(game_clk),.game_clk_oe_i(game_clk_oe),
        .link_si_i(poe?pso:1'b1),.link_clk_i(psckoe?psck:1'b1),
        .link_so_o(link_so),.link_clk_o(link_clk),.link_clk_oe(link_clk_oe),.mode_o(mode),.faults_o(faults));
    // ---- the Pocket ----
    reg preset_n=0,wr=0,rd=0; reg [4:0] addr=0; reg [31:0] wdata=0; wire [31:0] rdata; wire irq;
    pocket_media_link #(.CLK_HZ(25000000),.FIFO_BYTES(1024)) pocket(
        .clk(pclk),.reset_n(preset_n),.reg_wr(wr),.reg_rd(rd),.reg_addr(addr),.reg_wdata(wdata),.reg_rdata(rdata),
        .link_si_i(link_so),.link_so_o(pso),.link_so_oe(poe),.link_sck_i(link_clk_oe?link_clk:1'b1),
        .link_sck_o(psck),.link_sck_oe(psckoe),.link_sd_i(1'b0),.link_sd_o(),.link_sd_oe(),.irq(irq));

    // ---- helpers ----
    real baud=115200.0;
    function real byte_ns; input real b; byte_ns=1e9/b*10.0; endfunction
    function [7:0] crc8(input [7:0] crc,input [7:0] d); integer i; begin
        crc=crc^d; for(i=0;i<8;i=i+1) crc=crc[7]?{crc[6:0],1'b0}^8'h1d:{crc[6:0],1'b0}; crc8=crc; end endfunction
    task uart_byte(input [7:0] b); begin
        @(negedge gclk); uart_rx_data=b; uart_rx_val=1; @(negedge gclk); uart_rx_val=0; #(byte_ns(baud)-119.2); end endtask
    reg [7:0] pkt[0:63]; integer pk;
    task send_packet(input [6:0] address,input integer n); reg [7:0] c; integer i; begin
        c=8'hff; c=crc8(c,8'h8f); c=crc8(c,{1'b0,address}); c=crc8(c,n[7:0]);
        for(i=0;i<n;i=i+1) c=crc8(c,pkt[i]);
        uart_byte(8'h8f); uart_byte({1'b0,address}); uart_byte(n[7:0]);
        for(i=0;i<n;i=i+1) uart_byte(pkt[i]);
        uart_byte(c); end endtask
    task ctrl(input [7:0] cmd); begin
        pkt[0]=cmd; pkt[1]=8'h50; pkt[2]=8'h4a; pkt[3]=8'h4c; pkt[4]=8'h4b; send_packet(7'h1e,5); end endtask
    task write_reg(input [4:0] a,input [31:0] d); begin @(negedge pclk); addr=a; wdata=d; wr=1; @(negedge pclk); wr=0; end endtask
    task read_reg(input [4:0] a,output [31:0] v); begin
        @(negedge pclk); addr=a; rd=1; @(negedge pclk); rd=0; @(negedge pclk); #1; v=rdata; end endtask
    task pocket_write(input [7:0] b); reg [31:0] s; begin
        read_reg(0,s); while(!s[4]) read_reg(0,s); write_reg(1,b); end endtask

    // ---- arbiter + uart_packet_wrapper_tx model: 4 overhead bytes + 10 data bytes per packet,
    //      at the UART's current rate; drives uart_tx_busy while a packet goes out ----
    reg arb_flag=0; integer status_packets=0,up_bytes=0; reg [7:0] up_seq[0:255]; reg [7:0] last_status=0,last_n=0; reg [15:0] last_level=0;
    always @(posedge gclk) if(up_req) begin if(arb_flag) $fatal(1,"second request while the packet is pending"); arb_flag=1; end
    reg [7:0] sampled[0:9]; integer sp;
    initial forever begin
        wait(arb_flag);
        #(byte_ns(baud)*6);                     // other channels (buttons etc.) between ours
        uart_tx_busy=1;
        #(byte_ns(baud)*3);                     // header, address, count
        for(sp=0;sp<10;sp=sp+1) begin tx_bytepos=sp; @(negedge gclk); sampled[sp]=up_byte; #(byte_ns(baud)-119.2); end
        #(byte_ns(baud));                       // CRC
        uart_tx_busy=0;
        @(negedge gclk); up_taken=1; @(negedge gclk); up_taken=0; arb_flag=0;
        last_status=sampled[0]; last_n=sampled[1]; last_level={sampled[2],sampled[3]}; status_packets=status_packets+1;
        if(last_n>6) $fatal(1,"status packet claims %0d bytes",last_n);
        for(sp=0;sp<last_n;sp=sp+1) begin up_seq[up_bytes]=sampled[4+sp]; up_bytes=up_bytes+1; end
        for(sp=last_n;sp<6;sp=sp+1) if(sampled[4+sp]!==8'hff) $fatal(1,"upstream padding is not FF");
    end

    // ---- Pocket receive thread ----
    integer rx_count=0; reg [7:0] rx_seq[0:4095]; reg rx_run=0; reg [31:0] v;
    always @(posedge pclk) if(rx_run&&!wr&&!rd) begin
        addr=0; #1; if(rdata[5]) begin
            @(negedge pclk); addr=2; rd=1; @(negedge pclk); rd=0; @(negedge pclk); #1;
            rx_seq[rx_count]=rdata[7:0]; rx_count=rx_count+1; end
    end

    integer i,k; real t0,t1; integer expect_count,base; reg [3:0] faults_before;
    task burst(input integer packets,input integer khz_label); begin
        // `packets` x 64 ordered bytes at the current UART rate; the Pocket drains at its clock
        faults_before=faults; base=rx_count; t0=$realtime;
        for(k=0;k<packets;k=k+1) begin
            for(i=0;i<64;i=i+1) pkt[i]=(k*64+i)%255; send_packet(7'h1f,64); // never FF: the link idle byte the Pocket drops
            // credit model: never more than the 64-byte queue + one staged packet in flight
            while(dut.tx_level>0) #1000;
        end
        wait(rx_count>=base+packets*64); t1=$realtime;
        for(i=0;i<packets*64;i=i+1) if(rx_seq[base+i]!==((i)%255)) $fatal(1,"burst byte %0d is %h",i,rx_seq[base+i]);
        $display("INFO: %0d bytes in %0.2f ms = %0.0f bytes/s downstream at %0.0f baud, 64-byte packets, link %0d kHz (queue 64 in this bench)",
            packets*64,(t1-t0)/1e6,(packets*64)/((t1-t0)/1e9),baud,khz_label);
        if(faults!==faults_before) $fatal(1,"faults %b after burst",faults); // sticky bits from step 8 stay; nothing new may appear
    end endtask
    initial begin
        #1000; reset=0; preset_n=1;
        // 1. Game mode by default: pins follow the game core exactly.
        game_so=0; game_clk=0; game_clk_oe=1; #500;
        if(link_so!==0||link_clk!==0||link_clk_oe!==1||mode!==0) $fatal(1,"game pass-through wrong (driven)");
        game_so=1; game_clk=1; game_clk_oe=0; #500;
        if(link_so!==1||link_clk_oe!==0) $fatal(1,"game pass-through wrong (input)");
        // Data packets in game mode are ignored (both the small and the wide form).
        for(i=0;i<8;i=i+1) pkt[i]=8'h11; send_packet(7'h1f,8); for(i=0;i<64;i=i+1) pkt[i]=8'h22; send_packet(7'h1f,64); #2000;
        if(dut.tx_level!==0||mode!==0||dut.draining!==0) $fatal(1,"data accepted outside media mode");
        // The fast switch is refused outside media mode.
        ctrl(4); #20000; if(uart_fast!==0) $fatal(1,"fast UART outside media mode");
        // 2. The Pocket starts first and polls continuously (the wrong order).
        write_reg(0,13); #3000000;
        // 3. Enter media link mode; a wrong key is ignored.
        pkt[0]=1; pkt[1]=0; pkt[2]=0; pkt[3]=0; pkt[4]=0; send_packet(7'h1e,5); #2000;
        if(mode!==0) $fatal(1,"wrong key entered media mode");
        ctrl(1); wait(mode==2); #2000;
        if(link_clk_oe!==0) $fatal(1,"media mode drives LINK_CLK");
        // 4. Under continuous polling the peripheral never arms...
        #4000000; if(dut.link.wire_byte.armed!==0) $fatal(1,"armed without a quiet gap");
        // ...until the Pocket pauses polling for a few ms (tether.h breathing gap).
        write_reg(0,5); #3000000; write_reg(0,13);
        if(dut.link.wire_byte.armed!==1) $fatal(1,"peripheral did not arm in the gap");
        // 5. Downstream at 115200: 96 ordered bytes as 64 + 32, then a 5-byte tail (count 5, no padding
        //    needed on the wire; the MCU may still pad with FF, which the Pocket ignores).
        rx_run=1; t0=$realtime;
        for(i=0;i<64;i=i+1) pkt[i]=8'd1+i; send_packet(7'h1f,64);
        while(dut.tx_level>0) #1000;
        for(i=0;i<32;i=i+1) pkt[i]=8'd65+i; send_packet(7'h1f,32);
        while(dut.tx_level>0) #1000;
        pkt[0]=8'hA1; pkt[1]=8'hA2; pkt[2]=8'hA3; pkt[3]=8'hA4; pkt[4]=8'hA5; pkt[5]=8'hff; pkt[6]=8'hff; pkt[7]=8'hff;
        send_packet(7'h1f,8);
        ctrl(3); // a heartbeat in the stream is harmless
        expect_count=101; wait(rx_count>=expect_count); t1=$realtime; #200000;
        if(rx_count!==expect_count) $fatal(1,"Pocket received %0d bytes, expected %0d",rx_count,expect_count);
        for(i=0;i<96;i=i+1) if(rx_seq[i]!==8'd1+i) $fatal(1,"downstream byte %0d is %h",i,rx_seq[i]);
        for(i=0;i<5;i=i+1) if(rx_seq[96+i]!==8'hA1+i) $fatal(1,"tail byte %0d is %h",i,rx_seq[96+i]);
        $display("INFO: downstream %0d bytes in %0.3f ms = %0.0f bytes/s at 115200 baud, 64-byte packets",
            expect_count,(t1-t0)/1e6,expect_count/((t1-t0)/1e9));
        if(faults!==0) $fatal(1,"faults %b after clean downstream",faults);
        // A packet with a bad CRC is dropped silently (no fault, nothing queued).
        for(i=0;i<16;i=i+1) pkt[i]=8'h33; pkt[0]=8'h34; // break the CRC by changing a byte after computing it
        begin : badcrc
            reg [7:0] c; c=8'hff; c=crc8(c,8'h8f); c=crc8(c,8'h1f); c=crc8(c,8'd16); for(i=0;i<16;i=i+1) c=crc8(c,8'h33);
            uart_byte(8'h8f); uart_byte(8'h1f); uart_byte(8'd16); for(i=0;i<16;i=i+1) uart_byte(pkt[i]); uart_byte(c);
        end
        #300000; if(rx_count!==expect_count||faults!==0) $fatal(1,"bad-CRC packet was not dropped cleanly");
        // 6. Upstream: 20 Pocket bytes through status packets.
        for(i=0;i<20;i=i+1) pocket_write(8'h20+i);
        wait(up_bytes>=20); #100;
        for(i=0;i<20;i=i+1) if(up_seq[i]!==8'h20+i) $fatal(1,"upstream byte %0d is %h",i,up_seq[i]);
        if(last_status[1:0]!==2'd2||last_status[5:2]!==0||last_status[6]!==0) $fatal(1,"status byte %h",last_status);
        // 7. Idle: a status packet at least every ~60 ms with no data.
        k=status_packets; #30000000; ctrl(3); #30000000; if(status_packets<=k) $fatal(1,"no periodic status packet");
        if(mode!==2) $fatal(1,"heartbeats did not hold media mode in idle");
        if(last_n!==0||last_level!==0) $fatal(1,"idle status shows data n=%0d level=%0d",last_n,last_level);
        // 8. Queue-full drop: Pocket stops polling; 64 + 8 bytes overfill the 64-byte queue.
        ctrl(3); write_reg(0,5); rx_run=0; #200000;
        for(i=0;i<64;i=i+1) pkt[i]=8'h5a; send_packet(7'h1f,64);
        #100000; for(i=0;i<8;i=i+1) pkt[i]=8'h5b; send_packet(7'h1f,8);
        #2000; if(faults[2]!==1) $fatal(1,"queue-full drop not flagged: faults=%b level=%0d",faults,dut.tx_level);
        wait(last_status[4]==1); // the fault reaches the MCU in a status packet
        if(last_level>64) $fatal(1,"reported level %0d exceeds the queue",last_level);
        // 9. Fast UART: the keyed switch waits for TX idle, then the status packet reports it at the new rate.
        ctrl(3); write_reg(0,13); rx_run=1; wait(rx_count>=expect_count+64); // drain the 64 queued bytes first
        expect_count=rx_count;
        uart_tx_busy=1; ctrl(4); #400000; if(uart_fast!==0) $fatal(1,"switched while TX busy"); uart_tx_busy=0;
        #(1460*119.2+2000); if(uart_fast!==1) $fatal(1,"fast UART not selected after TX idle");
        // Until the MCU speaks at the new rate the FPGA sends nothing and reports fast=0 (no status
        // at a rate the MCU may not have yet).
        k=status_packets; #2000000; if(status_packets!==k) $fatal(1,"status sent before the MCU confirmed the new rate");
        if(dut.fast_confirmed!==0) $fatal(1,"fast confirmed without a packet");
        baud=524288.0; // the MCU switches after the FPGA
        ctrl(3); wait(last_status[6]==1); // its heartbeat at the new rate confirms; the status that says so follows
        if(dut.fast_confirmed!==1) $fatal(1,"fast not confirmed by a valid packet");
        if(last_status[1:0]!==2'd2) $fatal(1,"fast status lost the mode");
        // 10. Throughput at the fast rate, link at ~520 kHz (Pocket half-period register), twice.
        if(dut.GLITCH_SAMPLES!==3) $fatal(1,"the default link input filter is not 3 samples");
        write_reg(3,24); #100000; burst(16,520); ctrl(3);
        burst(16,520); ctrl(3);
        // upstream still works at the fast rate and the faster link
        k=up_bytes; for(i=0;i<6;i=i+1) pocket_write(8'h40+i); wait(up_bytes>=k+6); #100;
        for(i=0;i<6;i=i+1) if(up_seq[k+i]!==8'h40+i) $fatal(1,"fast upstream byte %0d is %h",i,up_seq[k+i]);
        write_reg(3,48); #100000; // back to 256 kHz for the exit checks
        // 11. Stock SysCtrl (address 4) leaves media mode; pins and UART rate return.
        pkt[0]=0; pkt[1]=0; send_packet(7'd4,2); wait(mode==0); #2000;
        if(uart_fast!==0) $fatal(1,"UART still fast after SysCtrl");
        baud=115200.0;
        game_clk_oe=1; game_clk=0; #500; if(link_clk_oe!==1||link_clk!==0) $fatal(1,"pins not returned after SysCtrl");
        game_clk_oe=0; game_clk=1;
        wait(last_status[1:0]!=2'd2); // the exit was reported
        // 11b. Nobody speaks at the new rate (the MCU never switched, or its bytes do not arrive): the FPGA returns to
        //      115,200 by itself within FAST_CONFIRM_CYCLES, stays in media mode, sends nothing meanwhile, and says so
        //      at 115,200; the MCU's leave then finds it at 115,200.
        ctrl(1); wait(mode==2); #2000; ctrl(4); #(1460*119.2+400000); if(uart_fast!==1) $fatal(1,"probation: not switched");
        k=status_packets; wait(uart_fast==0); if(mode!==2) $fatal(1,"probation timeout left media mode");
        if(status_packets!==k) $fatal(1,"status sent during an unconfirmed rate");
        wait(status_packets>k); if(last_status[6]!==0||last_status[1:0]!==2'd2) $fatal(1,"revert not reported at 115,200");
        ctrl(2); wait(mode==0); if(uart_fast!==0) $fatal(1,"rate not stock after leave");
        // 12. Re-enter clears the sticky faults; heartbeat expiry (shortened to 250 ms here) exits.
        write_reg(0,13); #200000; write_reg(0,5); #3000000; write_reg(0,13);
        ctrl(1); wait(mode==2); #2000; if(faults!==0) $fatal(1,"faults not cleared on enter: %b",faults);
        ctrl(3); #150000000; if(mode!==2) $fatal(1,"heartbeat did not hold media mode");
        #300000000; if(mode!==0) $fatal(1,"missing heartbeat did not return to the game");
        if(faults[3]!==1) $fatal(1,"expiry fault not latched");
        $display("PASS: pocket-link bridge v2: game pass-through, keyed entry, arming gap, 64-byte downstream packets with tail, bad CRC dropped, 26 upstream bytes in %0d status packets, periodic status, queue-full drop fault, keyed fast UART (524,288 baud) with TX-idle guard, confirmed by the MCU's first packet at the new rate (no status before it), self-revert to 115,200 when nobody speaks at it, bursts on a 520 kHz link with the link input filter on (3 samples), SysCtrl exit restores pins and rate, heartbeat expiry",status_packets);
        $finish;
    end
    initial begin #1500000000; $fatal(1,"Bounded pocket-link test timed out"); end
endmodule

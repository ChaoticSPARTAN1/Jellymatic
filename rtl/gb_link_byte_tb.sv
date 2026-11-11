// SPDX-License-Identifier: GPL-3.0-or-later
// Additional terms: ADDITIONAL-TERMS.md
`timescale 1ns/1ps
module gb_link_byte_tb;
    reg mc=0,sc=0;
    always #20 mc=~mc;
    always #23 sc=~sc;
    reg rst=1,en=0,connect=1,mresync=0,sresync=0;
    reg [7:0] mt=0,st=0;
    reg mv=0,sv=0,mrr=1,srr=1;
    wire mtr,str,mrv,srv,ms,ss,msoe,ssoe,mck,mcke,sck,scke,mbusy,sbusy,sto,soflow;
    wire [7:0] mr,sr;
    wire cable_clock=mcke?mck:1'b1;
    gb_link_byte #(.HALF_CYCLES(49),.QUIET_CYCLES(256)) master(
        .clk(mc),.rst(rst),.enable_i(en),.master_i(1'b1),.resync_i(mresync),
        .tx_data_i(mt),.tx_valid_i(mv),.tx_ready_o(mtr),.rx_data_o(mr),.rx_valid_o(mrv),.rx_ready_i(mrr),
        .link_si_i(connect&&ssoe?ss:1'b1),.link_clk_i(cable_clock),.link_so_o(ms),.link_so_oe(msoe),
        .link_clk_o(mck),.link_clk_oe(mcke),.busy_o(mbusy),.timeout_o(),.overflow_o());
    gb_link_byte #(.HALF_CYCLES(49),.QUIET_CYCLES(256)) slave(
        .clk(sc),.rst(rst),.enable_i(en),.master_i(1'b0),.resync_i(sresync),
        .tx_data_i(st),.tx_valid_i(sv),.tx_ready_o(str),.rx_data_o(sr),.rx_valid_o(srv),.rx_ready_i(srr),
        .link_si_i(connect&&msoe?ms:1'b1),.link_clk_i(connect?cable_clock:1'b1),.link_so_o(ss),.link_so_oe(ssoe),
        .link_clk_o(sck),.link_clk_oe(scke),.busy_o(sbusy),.timeout_o(sto),.overflow_o(soflow));
    reg [7:0] me[0:511],se[0:511];
    integer mw=0,sw=0,mn=0,sn=0,timeouts=0,overflows=0,cycles=0,i,target_m,target_s;
    reg check_bytes=1;
    reg [7:0] expected_mwire,expected_swire;
    integer wire_bit=0;
    always @(posedge mbusy)wire_bit=0;
    always @(negedge cable_clock)if(check_bytes&&mbusy)begin
        // Independent edge/bit-order contract, beyond two matching endpoints.
        #200;
        if(wire_bit>7||ms!==expected_mwire[7-wire_bit]||ss!==expected_swire[7-wire_bit])
            $fatal(1,"Physical MSB-first/falling-edge contract mismatch");
        wire_bit=wire_bit+1;
    end
    always @(posedge mc)begin
        cycles=cycles+1;
        if(cycles>450000)$fatal(1,"Bounded link unit timeout");
        if(check_bytes&&!rst&&mrv&&mrr)begin
            if(mn>=mw||mr!==me[mn])$fatal(1,"Master byte mismatch at %0d got=%h expected=%h",mn,mr,me[mn]);
            mn=mn+1;
        end
        if(!en&&(mcke||msoe))$fatal(1,"Disabled master is driving cable");
    end
    always @(posedge sc)begin
        if(scke)$fatal(1,"Peripheral tried to drive cable clock");
        if(sto)timeouts=timeouts+1;
        if(soflow)overflows=overflows+1;
        if(check_bytes&&!rst&&srv&&srr)begin
            if(sn>=sw||sr!==se[sn])$fatal(1,"Peripheral byte mismatch at %0d got=%h expected=%h",sn,sr,se[sn]);
            sn=sn+1;
        end
        if(!en&&ssoe)$fatal(1,"Disabled peripheral is driving cable");
    end
    task push_master(input [7:0] value);
        begin
            @(negedge mc);mt=value;expected_mwire=value;mv=1;
            @(posedge mc);while(!mtr)@(posedge mc);
            @(negedge mc);mv=0;
        end
    endtask
    task push_slave(input [7:0] value);
        begin
            @(negedge sc);st=value;expected_swire=value;sv=1;
            @(posedge sc);while(!str)@(posedge sc);
            @(negedge sc);sv=0;
        end
    endtask
    task transfer(input [7:0] a,input [7:0] b);
        begin
            me[mw]=b;mw=mw+1;se[sw]=a;sw=sw+1;
            push_slave(b);push_master(a);
            wait(mn==mw&&sn==sw);
        end
    endtask
    initial begin
        repeat(6)@(negedge mc);rst=0;en=1;
        for(i=0;i<256;i=i+1)transfer(i[7:0],i[7:0]^8'ha5);
        // Polling an empty peripheral yields the reserved idle byte, not old data.
        me[mw]=8'hff;mw=mw+1;se[sw]=8'h63;sw=sw+1;expected_swire=8'hff;
        push_master(8'h63);wait(mn==mw&&sn==sw);
        // A pending receive byte stops the master at a complete byte boundary.
        @(negedge mc);mrr=0;
        me[mw]=8'h36;mw=mw+1;se[sw]=8'h17;sw=sw+1;
        push_slave(8'h36);push_master(8'h17);wait(mrv);
        repeat(30)@(negedge mc);
        if(mtr||!mck||mbusy)$fatal(1,"Master did not apply receive backpressure");
        @(negedge mc);mrr=1;wait(mn==mw&&sn==sw);
        transfer(8'h80,8'h01);
        // Peripheral cannot pause a remote clock: signal overflow, preserve old byte.
        @(negedge sc);srr=0;
        me[mw]=8'h18;mw=mw+1;se[sw]=8'h29;sw=sw+1;
        push_slave(8'h18);push_master(8'h29);wait(srv&&mn==mw);
        me[mw]=8'h38;mw=mw+1;
        push_slave(8'h38);push_master(8'h49);wait(mn==mw);
        repeat(12)@(negedge sc);
        if(overflows!=1||sr!==8'h29)$fatal(1,"Overflow lost the older byte or was not reported");
        @(negedge sc);srr=1;wait(sn==sw);
        // Remove the cable part way through a byte, observe timeout and re-arm.
        check_bytes=0;mrr=0;srr=0;
        push_slave(8'h3c);push_master(8'hc3);
        repeat(4)@(negedge cable_clock);
        connect=0;
        wait(timeouts==1);
        if(sbusy||srv)$fatal(1,"Incomplete byte was delivered after cable loss");
        @(negedge mc);en=0;mv=0;sv=0;
        repeat(8)@(negedge mc);connect=1;mrr=1;srr=1;check_bytes=1;en=1;
        transfer(8'h81,8'h7e);
        // Explicit resync aborts/flushes without leaving a driven low clock.
        check_bytes=0;mrr=0;srr=0;
        push_slave(8'h5a);push_master(8'ha5);@(negedge cable_clock);
        @(negedge mc);mresync=1;sresync=1;
        repeat(4)@(negedge mc);
        if(mcke||msoe||ssoe)$fatal(1,"Resync did not release cable outputs");
        mresync=0;sresync=0;mrr=1;srr=1;check_bytes=1;
        transfer(8'h55,8'haa);
        @(negedge mc);en=0;
        repeat(8)@(negedge mc);
        if(mcke||msoe||ssoe)$fatal(1,"Cable not released at end");
        $display("PASS: link byte engine; all 256 values in both directions, asynchronous clocks, backpressure, overflow, cable-loss timeout, resync and output release; master=%0d slave=%0d",mn,sn);
        $finish;
    end
endmodule

// SPDX-License-Identifier: GPL-3.0-or-later
// Additional terms: ADDITIONAL-TERMS.md
`timescale 1ns/1ps
module gb_link_stream_tb;
    reg mc=0,sc=0;
    always #20 mc=~mc;
    always #23 sc=~sc;
    reg rst=1,en=0;
    reg [7:0] st=0;
    reg mv=0,sv=0,mready=0;
    wire mtr,str,mrv,srv,ms,ss,msoe,ssoe,mck,mcke,sck,scke,mbusy,sbusy,mto,sto,mov,sov;
    wire [7:0] mr,sr;
    wire [5:0] mtl,mrl,stl,srl;
    wire cable_clock=mcke?mck:1'b1;
    gb_link_stream #(.FIFO_DEPTH(32),.HALF_CYCLES(49),.QUIET_CYCLES(256)) master(
        .clk(mc),.rst(rst),.enable_i(en),.master_i(1'b1),.resync_i(1'b0),
        .tx_data_i(8'hff),.tx_valid_i(mv),.tx_ready_o(mtr),.rx_data_o(mr),.rx_valid_o(mrv),.rx_ready_i(mready),
        .tx_level_o(mtl),.rx_level_o(mrl),
        .link_si_i(ssoe?ss:1'b1),.link_clk_i(cable_clock),.link_so_o(ms),.link_so_oe(msoe),
        .link_clk_o(mck),.link_clk_oe(mcke),.busy_o(mbusy),.timeout_o(mto),.overflow_o(mov));
    gb_link_stream #(.FIFO_DEPTH(32),.HALF_CYCLES(49),.QUIET_CYCLES(256)) slave(
        .clk(sc),.rst(rst),.enable_i(en),.master_i(1'b0),.resync_i(1'b0),
        .tx_data_i(st),.tx_valid_i(sv),.tx_ready_o(str),.rx_data_o(sr),.rx_valid_o(srv),.rx_ready_i(1'b1),
        .tx_level_o(stl),.rx_level_o(srl),
        .link_si_i(msoe?ms:1'b1),.link_clk_i(cable_clock),.link_so_o(ss),.link_so_oe(ssoe),
        .link_clk_o(sck),.link_clk_oe(scke),.busy_o(sbusy),.timeout_o(sto),.overflow_o(sov));
    reg [7:0] bytes[0:4095];
    reg [1023:0] input_path,output_path;
    integer length,sent=0,clock_requests=0,received=0,slave_received=0,cycles=0,fd;
    integer wire_start=0,full_cycles=0;
    integer stress=1;
    real rate;
    always @(posedge mc)begin
        cycles=cycles+1;
        if(cycles>5000000)$fatal(1,"Buffered cable simulation timeout");
        if(!rst&&en)begin
            if(mv&&mtr)clock_requests=clock_requests+1;
            if(mrl==32)full_cycles=full_cycles+1;
            if(mrv&&mready)begin
                if(received>=length||mr!==bytes[received])$fatal(1,"Wire stream mismatch at byte %0d got=%h expected=%h",received,mr,bytes[received]);
                $fwrite(fd,"%c",mr);received=received+1;
            end
            if(mov||mto)$fatal(1,"Master reported loss in a valid stream");
        end
    end
    always @(posedge sc)begin
        if(!rst&&en)begin
            if(sv&&str)sent=sent+1;
            if(srv)begin
                if(sr!==8'hff)$fatal(1,"Reverse clock/request byte corrupted");
                slave_received=slave_received+1;
            end
            if(scke||sto||sov)$fatal(1,"Peripheral contention/timeout/overflow in a valid stream");
        end
    end
    always @(negedge sc)begin
        if(!rst&&en)begin sv=(sent<length);if(sent<length)st=bytes[sent];end
        else sv=0;
    end
    always @(negedge mc)begin
        if(!rst&&en)begin
            // Deliberate long consumer stalls fill the RX RAM and pause the
            // clock at byte boundaries. The peripheral must retain its place.
            mready=!stress||(cycles%60000>=35000);
            mv=(clock_requests<length)&&(stl>=16||clock_requests>0);
            if(mv&&wire_start==0)wire_start=cycles;
        end else begin mv=0;mready=0;end
    end
    initial begin
        if(!$value$plusargs("INPUT=%s",input_path)||!$value$plusargs("OUTPUT=%s",output_path)||
           !$value$plusargs("LENGTH=%d",length)||length<512||length>4096)$fatal(1,"Invalid wire test arguments");
        if(!$value$plusargs("STALL=%d",stress))stress=1;
        $readmemh(input_path,bytes,0,length-1);
        fd=$fopen(output_path,"wb");if(!fd)$fatal(1,"Cannot save received stream");
        repeat(8)@(negedge mc);rst=0;en=1;
        wait(received==length);
        wait(slave_received==length);
        @(negedge mc);en=0;
        repeat(8)@(negedge mc);
        $fclose(fd);
        if(mcke||msoe||scke||ssoe)$fatal(1,"Link outputs not released at stream end");
        if(stress&&full_cycles==0)$fatal(1,"Consumer stalls did not fill the receive FIFO");
        rate=$itor(length)*25000000.0/$itor(cycles-wire_start);
        $display("PASS: buffered asynchronous wire stream; bytes=%0d simulated_bytes_per_second=%0f full_fifo_cycles=%0d reverse_bytes=%0d",received,rate,full_cycles,slave_received);
        $finish;
    end
endmodule

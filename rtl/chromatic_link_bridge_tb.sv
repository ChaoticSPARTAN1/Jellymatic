// SPDX-License-Identifier: GPL-3.0-or-later
// Additional terms: ADDITIONAL-TERMS.md
`timescale 1ns/1ps
module chromatic_link_bridge_tb;
    reg clk=0;always #20 clk=~clk;
    reg pclk=0;always #23 pclk=~pclk;
    reg reset=0,request=0,disable_req=0,hb=0;
    reg qclk=0,qcs=1;reg [3:0] qp=0;
    wire qvalid,qcommand;wire [15:0] qdata;wire [31:0] qaddr;wire [9:0] qlen;
    wire legacy,so,so_oe,sck,sck_oe;wire [1:0] mode;wire [3:0] faults;
    wire [31:0] consumed;wire [7:0] up_data;wire up_valid;
    wire mso,mso_oe,msck,msck_oe,mready,mvalid;wire [7:0] mdata;
    reg master_enable=0,mreceive=1,allow_fault=0;integer sent_control=0,up_count=0,n=0,count=0,cycles=0;
    reg [7:0] memory[0:4095];reg [1023:0] input_path,output_path;integer file_out;
    QSPI_Slave decoder(.QSPI_CLK(qclk),.QSPI_CS(qcs),.QSPI_MOSI(qp[0]),
        .QSPI_MISO(qp[1]),.QSPI_WP(qp[2]),.QSPI_HD(qp[3]),.qMenuInit(),
        .qDataValid(qvalid),.qData(qdata),.qAddress(qaddr),.qLength(qlen),.qCommand(qcommand));
    chromatic_link_bridge #(.FIFO_WORDS(64),.LINK_FIFO_BYTES(64)) bridge(
        .clk_i(clk),.reset_i(reset),.request_i(request),.key_i(32'h504a4c4b),
        .disable_i(disable_req),.heartbeat_i(hb),.qwrite_clk_i(~qclk),.qcs_i(qcs),
        .qcommand_i(qcommand),.qaddress_i(qaddr),.qlength_i(qlen),.qvalid_i(qvalid),.qdata_i(qdata),
        .legacy_allowed_o(legacy),.consumed_bytes_o(consumed),
        .upstream_data_o(up_data),.upstream_valid_o(up_valid),.upstream_ready_i(1'b1),
        .game_so_i(1'b1),.game_so_oe_i(1'b0),.game_clk_i(1'b1),.game_clk_oe_i(1'b0),
        .link_si_i(mso_oe?mso:1'b1),.link_clk_i(msck_oe?msck:1'b1),
        .link_so_o(so),.link_so_oe(so_oe),.link_clk_o(sck),.link_clk_oe(sck_oe),
        .mode_o(mode),.fault_status_o(faults));
    // Independent Pocket-clock model. Equivalent 256k-ish clock for 21.74 MHz.
    gb_link_stream #(.HALF_CYCLES(43),.QUIET_CYCLES(1024),.FIFO_DEPTH(32)) master(
        .clk(pclk),.rst(reset),.enable_i(master_enable),.master_i(1'b1),.resync_i(1'b0),
        .tx_data_i(sent_control<128?sent_control[7:0]:8'hff),.tx_valid_i(master_enable),.tx_ready_o(mready),
        .rx_data_o(mdata),.rx_valid_o(mvalid),.rx_ready_i(mreceive),.tx_level_o(),.rx_level_o(),
        .link_si_i(so_oe?so:1'b1),.link_clk_i(1'b1),.link_so_o(mso),.link_so_oe(mso_oe),
        .link_clk_o(msck),.link_clk_oe(msck_oe),.busy_o(),.timeout_o(),.overflow_o());
    always @(posedge pclk)begin
        if(master_enable&&mready&&sent_control<128)sent_control<=sent_control+1;
        if(mvalid&&mreceive&&mdata!=8'hff)begin
            if(count>=n||mdata!==memory[count])$fatal(1,"QSPI->link mismatch at %0d",count);
            $fwrite(file_out,"%c",mdata);count=count+1;
        end
    end
    always @(posedge clk)begin
        cycles<=cycles+1;
        hb<=(cycles%50000==0);
        if(up_valid)begin
            if(up_count>=128||up_data!==up_count[7:0])$fatal(1,"Upstream control mismatch");
            up_count=up_count+1;
        end
        if(!reset&&!allow_fault&&faults!==4'b0)$fatal(1,"Bridge fault %b raw=%b%b%b%b fifo=%b words=%d length=%d enabled=%b",
            faults,bridge.expired,bridge.overflow,bridge.timeout,bridge.qfault,
            bridge.ingress.write_ready,bridge.ingress.words,qlen,bridge.enabled);
    end
    task nibble(input [3:0] value);
        begin qp=value;#12.5;qclk=1;#12.5;qclk=0;#1;end
    endtask
    task burst(input integer start,input integer length,input bit command);
        integer i;reg [9:0] size;reg [31:0] address;reg [7:0] data;
        begin
            size=length-1;address=32'h504a0000;qcs=0;nibble({3'b0,command});
            for(i=9;i>=0;i=i-1)nibble({3'b0,size[i]});
            for(i=31;i>=0;i=i-1)nibble({3'b0,address[i]});
            repeat(3)nibble(0);
            for(i=0;i<length;i=i+1)begin
                data=start+i<n?memory[start+i]:8'hff;
                nibble(data[7:4]);nibble(data[3:0]);
            end
            qcs=1;#50;
        end
    endtask
    integer offset,size;
    initial begin
        if(!$value$plusargs("INPUT=%s",input_path)||!$value$plusargs("OUTPUT=%s",output_path)
            ||!$value$plusargs("LENGTH=%d",n)||n<4||n>4096)$fatal(1,"Missing bounded input");
        $readmemh(input_path,memory,0,n-1);file_out=$fopen(output_path,"wb");
        if(!file_out)$fatal(1,"Cannot open private output");
        #1;reset=1;#1;qcs=0;#2;qcs=1;repeat(8)@(negedge clk);reset=0;
        if(so_oe||sck_oe)$fatal(1,"Media pins active at default game mode");
        request=1;@(negedge clk);request=0;wait(mode==2);
        // Give peripheral time to arm before enabling the master clock.
        repeat(27000)@(negedge clk);master_enable=1;
        fork
            begin
                offset=0;
                while(offset<n)begin
                    // Credits are the cumulative sink counter. This models
                    // control feedback, not a tested UART driver.
                    while(offset-consumed>64)@(negedge clk);
                    size=n-offset>=64?64:n-offset;
                    if(size%2)size=size+1;
                    burst(offset,size,1);offset=offset+size;
                end
            end
            begin
                // Pause Pocket consumption long enough to fill queues. The
                // master must stop at byte boundaries and credits must stall.
                wait(count>=200);mreceive=0;repeat(70000)@(negedge pclk);mreceive=1;
            end
        join
        wait(count==n&&up_count==128);master_enable=0;disable_req=1;
        wait(mode==0);repeat(10)@(negedge clk);
        if(so_oe||sck_oe)$fatal(1,"Link ownership did not return to game default");
        // A real ingress fault must latch, leave media mode, and stay visible
        // after the asynchronous FIFO flush clears the original raw fault.
        allow_fault=1;disable_req=0;request=1;@(negedge clk);request=0;wait(mode==2);
        burst(0,2,0);wait(faults[0]);wait(mode==0);repeat(10)@(negedge clk);
        if(faults!==4'b1||so_oe||sck_oe)$fatal(1,"Ingress fault did not latch/return to game");
        // Only a fresh explicit keyed request clears it and re-enters media.
        request=1;@(negedge clk);request=0;wait(mode==2);repeat(5)@(negedge clk);
        if(faults!==4'b0)$fatal(1,"Explicit restart did not clear sticky fault");
        disable_req=1;wait(mode==0);
        $fclose(file_out);
        $display("PASS: stock QSPI pins -> credit-limited asynchronous FIFO -> buffered cable -> Pocket receiver; bytes=%0d upstream_controls=%0d, consumer pause, sticky-fault game return/restart and explicit game-mode return passed",count,up_count);
        $finish;
    end
    initial begin #250000000;$fatal(1,"Bounded bridge test timed out");end
endmodule

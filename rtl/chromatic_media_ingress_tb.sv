// SPDX-License-Identifier: GPL-3.0-or-later
// Additional terms: ADDITIONAL-TERMS.md
`timescale 1ns/1ps
// Exercise the actual unchanged stock QSPI decoder, not a hand-fed parallel bus.
module chromatic_media_ingress_tb;
    reg clk=0;always #20 clk=~clk;
    reg qclk=0,qcs=1;reg [3:0] qp=0;
    reg reset=1,enable=0,ready=1;
    wire qvalid,qcommand;wire [15:0] qdata;wire [31:0] qaddr;wire [9:0] qlen;
    wire legacy,valid,fault;wire [7:0] data;wire [31:0] consumed;
    QSPI_Slave decoder(.QSPI_CLK(qclk),.QSPI_CS(qcs),.QSPI_MOSI(qp[0]),
        .QSPI_MISO(qp[1]),.QSPI_WP(qp[2]),.QSPI_HD(qp[3]),.qMenuInit(),
        .qDataValid(qvalid),.qData(qdata),.qAddress(qaddr),.qLength(qlen),.qCommand(qcommand));
    chromatic_media_ingress #(.FIFO_WORDS(32)) dut(
        .clk_i(clk),.reset_i(reset),.media_enable_i(enable),.qwrite_clk_i(~qclk),
        .qcs_i(qcs),.qcommand_i(qcommand),.qaddress_i(qaddr),.qlength_i(qlen),
        .qvalid_i(qvalid),.qdata_i(qdata),.legacy_allowed_o(legacy),
        .data_o(data),.valid_o(valid),.ready_i(ready),.consumed_bytes_o(consumed),.fault_o(fault));
    reg [7:0] expected[0:8191];integer sent=0,received=0,legacy_words=0;
    reg compare=1;
    always @(posedge clk)if(valid&&ready)begin
        if(compare)begin
            if(received>=sent||data!==expected[received])$fatal(1,"QSPI CDC byte mismatch at %0d",received);
            received=received+1;
        end
    end
    always @(negedge qclk)if(qvalid&&legacy)legacy_words=legacy_words+1;
    task clock_nibble(input [3:0] nibble);
        begin qp=nibble;#12.5;qclk=1;#12.5;qclk=0;#1;end
    endtask
    task transaction(input [31:0] address,input bit command,input integer count,input integer base,input bit expect_media);
        integer i;reg [9:0] length;reg [7:0] byte_data;
        begin
            length=count-1;qcs=0;
            clock_nibble({3'b0,command});
            for(i=9;i>=0;i=i-1)clock_nibble({3'b0,length[i]});
            for(i=31;i>=0;i=i-1)clock_nibble({3'b0,address[i]});
            repeat(3)clock_nibble(0);
            for(i=0;i<count;i=i+1)begin
                byte_data=(base+i)&255;
                if(expect_media)begin expected[sent]=byte_data;sent=sent+1;end
                clock_nibble(byte_data[7:4]);clock_nibble(byte_data[3:0]);
            end
            qcs=1;#101;
        end
    endtask
    task await_bytes;
        integer cycles;
        begin
            cycles=0;
            while(received<sent&&cycles<20000)begin @(negedge clk);cycles=cycles+1;end
            if(received!=sent)$fatal(1,"QSPI bytes missing %0d/%0d",received,sent);
            if(fault)$fatal(1,"Unexpected ingress fault");
        end
    endtask
    task media_reset;
        begin enable=0;repeat(5)@(negedge clk);enable=1;repeat(5)@(negedge clk);end
    endtask
    integer i,before_legacy;
    initial begin
        // CS transition initializes the stock decoder with no QSPI clock running.
        #2;qcs=0;#2;qcs=1;repeat(5)@(negedge clk);reset=0;
        transaction(32'h504a0000,1,32,0,0);repeat(10)@(negedge clk);
        if(valid||fault||legacy_words)$fatal(1,"Disabled media reached a consumer");
        transaction(32'h00010000,1,32,0,0);
        if(legacy_words!=16)$fatal(1,"Valid stock framebuffer write changed");
        before_legacy=legacy_words;
        media_reset();
        // Burst producer outruns the consumer, with gaps allowing FIFO drain.
        for(i=0;i<80;i=i+1)begin
            transaction(32'h504a0000,1,32,i*32,1);
            if(i%3==0)repeat(50)@(negedge clk);
            await_bytes();
        end
        if(consumed!=2560||legacy_words!=before_legacy)$fatal(1,"Media aliased PSRAM or bad credit count");
        // Hold the output over arbitrary clock stops and consumer stalls.
        ready=0;transaction(32'h504a0000,1,32,0,1);repeat(100)@(negedge clk);
        ready=1;await_bytes();
        // Invalid command/address/odd lengths refuse media, never legacy memory.
        transaction(32'h504a0000,0,32,0,0);repeat(8)@(negedge clk);
        if(!fault||valid)$fatal(1,"Invalid media command accepted");
        media_reset();transaction(32'h504a0002,1,32,0,0);repeat(8)@(negedge clk);
        if(!fault||valid)$fatal(1,"Unknown media address accepted");
        media_reset();transaction(32'h504a0000,1,31,0,0);repeat(8)@(negedge clk);
        if(!fault||valid)$fatal(1,"Odd media length accepted");
        // Overflow sticks and suppresses the rest until explicit mode reset.
        media_reset();compare=0;ready=0;
        transaction(32'h504a0000,1,256,0,0);repeat(8)@(negedge clk);
        if(!fault||valid)$fatal(1,"Uncredited overflow was not stopped");
        media_reset();compare=1;ready=1;sent=0;received=0;
        // No old queued data may survive mode changes, even with QSPI stopped.
        repeat(50)@(negedge clk);if(valid||consumed)$fatal(1,"Old-session bytes leaked");
        transaction(32'h504a0000,1,32,173,1);await_bytes();
        if(legacy_words!=before_legacy)$fatal(1,"Reserved write reached legacy memory");
        $display("PASS: stock QSPI decoder -> asynchronous media FIFO -> ordered bytes; stock writes isolated, stalls/clock stops, invalid commands/addresses/odd lengths/overflow refused, mode reset flushes.");
        $finish;
    end
    initial begin #10000000;$fatal(1,"Bounded QSPI test timed out");end
endmodule

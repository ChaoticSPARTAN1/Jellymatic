// SPDX-License-Identifier: GPL-3.0-or-later
// Additional terms: ADDITIONAL-TERMS.md
`timescale 1ns/1ps
module byte_fifo_tb;
    reg clk=0;always #5 clk=~clk;
    reg rst=1,clear=0,valid=0,ready=0;
    reg [7:0] data=0;
    wire input_ready,output_valid;
    wire [7:0] output_data;
    wire [4:0] level;
    byte_fifo #(.DEPTH(16)) dut(.clk(clk),.rst(rst),.clear_i(clear),
        .in_data(data),.in_valid(valid),.in_ready(input_ready),
        .out_data(output_data),.out_valid(output_valid),.out_ready(ready),.level_o(level));
    reg [7:0] expected[0:10000];
    reg [7:0] held;
    reg stalled=0;
    reg [31:0] random_state=32'h40c3f17b;
    integer count=0,wr=0,rd=0,i,pushes=0,pops=0,full_cycles=0;
    always @(posedge clk)begin
        if(rst||clear)begin count=0;wr=0;rd=0;stalled=0;end
        else begin
            if(level!==count)$fatal(1,"FIFO occupancy mismatch");
            if(stalled&&(!output_valid||held!==output_data))$fatal(1,"FIFO changed a stalled output");
            if(output_valid&&ready)begin
                if(!count||output_data!==expected[rd])$fatal(1,"FIFO data/order mismatch");
                rd=rd+1;count=count-1;pops=pops+1;
            end
            if(valid&&input_ready)begin expected[wr]=data;wr=wr+1;count=count+1;pushes=pushes+1;end
            stalled=output_valid&&!ready;held=output_data;
            if(level==16)full_cycles=full_cycles+1;
        end
    end
    initial begin
        repeat(4)@(negedge clk);rst=0;
        for(i=0;i<6000;i=i+1)begin
            @(negedge clk);
            random_state=random_state^(random_state<<13);
            random_state=random_state^(random_state>>17);
            random_state=random_state^(random_state<<5);
            clear=(i==1800||i==3900);
            ready=(i%257>50)&&(random_state[3:0]>3);
            if(!valid||input_ready||clear)begin valid=(random_state[7:4]>1);data=data+1'b1;end
        end
        @(negedge clk);valid=0;ready=1;clear=0;
        wait(count==0);repeat(4)@(negedge clk);
        if(pushes<2000||pops<2000||full_cycles<10)$fatal(1,"FIFO stress did not exercise its bounds");
        $display("PASS: RAM FIFO stress; pushes=%0d pops=%0d full_cycles=%0d, simultaneous transfers, wraparound, stalls and clear",pushes,pops,full_cycles);
        $finish;
    end
    initial begin #1000000;$fatal(1,"FIFO simulation timeout");end
endmodule

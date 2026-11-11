// Synchronous RAM FIFO with a registered, stable ready/valid output.
// Capacity includes the output register. Memory is not reset or exposed empty.
module byte_fifo #(parameter integer DEPTH=1024)(
    input wire clk,input wire rst,input wire clear_i,
    input wire [7:0] in_data,input wire in_valid,output wire in_ready,
    output reg [7:0] out_data,output reg out_valid,input wire out_ready,
    output wire [$clog2(DEPTH+1)-1:0] level_o
);
    localparam integer AW=$clog2(DEPTH),CW=$clog2(DEPTH+1);
    reg [7:0] memory [0:DEPTH-1];
    reg [AW-1:0] write_ptr,read_ptr;
    reg [CW-1:0] queued;
    assign level_o=queued+out_valid;
    wire pop=out_valid&&out_ready;
    assign in_ready=!rst&&!clear_i&&((level_o<DEPTH)||pop);
    wire push=in_valid&&in_ready;
    wire load=(!out_valid||out_ready)&&(queued!=0);
    always @(posedge clk) begin
        if(rst||clear_i) begin
            write_ptr<=0;read_ptr<=0;queued<=0;out_data<=0;out_valid<=0;
        end else begin
            if(push) begin memory[write_ptr]<=in_data;write_ptr<=write_ptr+1'b1;end
            if(load) begin
                out_data<=memory[read_ptr];read_ptr<=read_ptr+1'b1;out_valid<=1;
            end else if(pop)out_valid<=0;
            case({push,load})
                2'b10:queued<=queued+1'b1;
                2'b01:queued<=queued-1'b1;
                default:;
            endcase
        end
    end
`ifndef SYNTHESIS
    initial if(DEPTH<2 || (DEPTH&(DEPTH-1))!=0)$fatal(1,"FIFO depth must be a power of two");
    always @(posedge clk)if(!rst&&!clear_i&&level_o>DEPTH)$fatal(1,"FIFO overflow");
`endif
endmodule

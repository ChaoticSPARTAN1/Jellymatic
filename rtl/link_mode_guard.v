// Explicit, temporary ownership of link pins. Power-up/reset defaults to the
// unchanged game-side signals. A lost media heartbeat returns ownership to the
// game after a break-before-make interval. This is NOT flash recovery.
module link_mode_guard #(
    parameter integer HEARTBEAT_CYCLES=75000000,
    parameter integer GAP_CYCLES=2048
)(
    input wire clk,input wire rst,
    input wire request_i,input wire [31:0] key_i,input wire disable_i,input wire heartbeat_i,
    input wire game_so_i,input wire game_so_oe_i,input wire game_clk_i,input wire game_clk_oe_i,
    input wire media_so_i,input wire media_so_oe_i,input wire media_clk_i,input wire media_clk_oe_i,
    output wire link_so_o,output wire link_so_oe,output wire link_clk_o,output wire link_clk_oe,
    output wire media_enable_o,output wire [1:0] mode_o,output reg expired_o
);
    localparam [1:0] GAME=0,ENTER=1,MEDIA=2,LEAVE=3;
    localparam integer HW=$clog2(HEARTBEAT_CYCLES+1),GW=$clog2(GAP_CYCLES+1);
    reg [1:0] state;
    reg [HW-1:0] heartbeat;
    reg [GW-1:0] gap;
    assign mode_o=state;
    assign media_enable_o=(state==MEDIA)&&!rst&&!disable_i;
    assign link_so_o=state==MEDIA?media_so_i:game_so_i;
    assign link_clk_o=state==MEDIA?media_clk_i:game_clk_i;
    assign link_so_oe=!rst&&((state==GAME&&game_so_oe_i)||(state==MEDIA&&!disable_i&&media_so_oe_i));
    assign link_clk_oe=!rst&&((state==GAME&&game_clk_oe_i)||(state==MEDIA&&!disable_i&&media_clk_oe_i));
    always @(posedge clk) begin
        expired_o<=0;
        if(rst)begin state<=GAME;heartbeat<=0;gap<=0;end
        else case(state)
            GAME:begin
                if(request_i&&key_i==32'h504a4c4b&&!disable_i)begin state<=ENTER;gap<=GAP_CYCLES-1;end
            end
            ENTER:begin
                if(disable_i)begin state<=LEAVE;gap<=GAP_CYCLES-1;end
                else if(gap==0)begin state<=MEDIA;heartbeat<=HEARTBEAT_CYCLES-1;end
                else gap<=gap-1'b1;
            end
            MEDIA:begin
                if(disable_i)begin state<=LEAVE;gap<=GAP_CYCLES-1;end
                else if(heartbeat_i)heartbeat<=HEARTBEAT_CYCLES-1;
                else if(heartbeat==0)begin state<=LEAVE;gap<=GAP_CYCLES-1;expired_o<=1;end
                else heartbeat<=heartbeat-1'b1;
            end
            default:begin
                if(gap==0)state<=GAME;
                else gap<=gap-1'b1;
            end
        endcase
    end
`ifndef SYNTHESIS
    initial if(HEARTBEAT_CYCLES<2||GAP_CYCLES<2)$fatal(1,"Invalid link mode timeout");
`endif
endmodule

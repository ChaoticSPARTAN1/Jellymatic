// SPDX-License-Identifier: GPL-3.0-or-later
// Additional terms: ADDITIONAL-TERMS.md
`timescale 1ns/1ps
module link_mode_guard_tb;
    reg clk=0;always #5 clk=~clk;
    reg rst=1,request=0,disable_req=0,heartbeat=0;
    reg [31:0] key=0;
    reg gs=0,gc=1,gso=1,gco=0;
    reg ms=1,mc=0,mso=1,mco=1;
    wire so,soe,sck,cke,enabled,expired;
    wire [1:0] mode;
    integer expires=0,i;
    link_mode_guard #(.HEARTBEAT_CYCLES(40),.GAP_CYCLES(4)) dut(
        .clk(clk),.rst(rst),.request_i(request),.key_i(key),.disable_i(disable_req),.heartbeat_i(heartbeat),
        .game_so_i(gs),.game_so_oe_i(gso),.game_clk_i(gc),.game_clk_oe_i(gco),
        .media_so_i(ms),.media_so_oe_i(mso),.media_clk_i(mc),.media_clk_oe_i(mco),
        .link_so_o(so),.link_so_oe(soe),.link_clk_o(sck),.link_clk_oe(cke),
        .media_enable_o(enabled),.mode_o(mode),.expired_o(expired));
    always @(negedge clk)begin
        #1;
        if(expired)expires=expires+1;
        if(!rst&&!disable_req)case(mode)
            0:if(enabled||so!==gs||sck!==gc||soe!==gso||cke!==gco)$fatal(1,"Game link signals altered in default mode");
            2:if(!enabled||so!==ms||sck!==mc||soe!==mso||cke!==mco)$fatal(1,"Media link selection mismatch");
            default:if(enabled||soe||cke)$fatal(1,"Both modes must release pins during handoff");
        endcase
    end
    task enable_media;
        begin
            @(negedge clk);request=1;key=32'h504a4c4b;
            @(negedge clk);request=0;
            wait(enabled);
        end
    endtask
    initial begin
        repeat(4)@(posedge clk);@(negedge clk);rst=0;
        for(i=0;i<16;i=i+1)begin @(posedge clk);gs=~gs;gc=~gc;gco=~gco;end
        @(negedge clk);request=1;key=32'h504a4c4a;
        repeat(8)@(posedge clk);@(negedge clk);request=0;
        if(mode!=0)$fatal(1,"Invalid activation key changed game mode");
        heartbeat=1;repeat(50)@(posedge clk);@(negedge clk);heartbeat=0;
        if(mode!=0)$fatal(1,"Heartbeat alone activated media mode");
        enable_media();
        repeat(100)begin @(negedge clk);heartbeat=1;@(negedge clk);heartbeat=0;end
        if(!enabled||expires)$fatal(1,"Healthy media heartbeat timed out");
        wait(mode==3);#1;if(enabled||soe||cke)$fatal(1,"Expired media still owns pins");
        wait(mode==0);repeat(3)@(posedge clk);
        if(expires!=1)$fatal(1,"Heartbeat loss not reported once");
        enable_media();@(negedge clk);disable_req=1;#1;
        if(enabled||soe||cke)$fatal(1,"Explicit disable_req did not immediately release media pins");
        repeat(3)@(negedge clk);disable_req=0;wait(mode==0);
        enable_media();@(negedge clk);rst=1;#1;
        if(enabled||soe||cke)$fatal(1,"Reset did not release pins");
        repeat(3)@(negedge clk);rst=0;repeat(4)@(negedge clk);
        if(mode!=0)$fatal(1,"Reset did not restore default game mode");
        $display("PASS: game-default mode guard; invalid key/stray heartbeat ignored, handoff gaps, heartbeat expiry, explicit disable_req and reset restore game-side link ownership");
        $finish;
    end
    initial begin #20000;$fatal(1,"Mode guard test timeout");end
endmodule

// Game Boy style 8-bit serial transfer: idle-high clock, MSB first,
// update data on falling edges and sample on rising edges. No board pins here.
module gb_link_byte #(
    parameter integer HALF_CYCLES = 49,
    parameter integer QUIET_CYCLES = 1024,
    parameter integer RUNTIME_HALF = 0 // 1: master half-period comes from half_cycles_i
)(
    input wire clk, input wire rst, input wire enable_i, input wire master_i,
    input wire [11:0] half_cycles_i,
    input wire resync_i,
    input wire [7:0] tx_data_i, input wire tx_valid_i, output wire tx_ready_o,
    output reg [7:0] rx_data_o, output reg rx_valid_o, input wire rx_ready_i,
    input wire link_si_i, input wire link_clk_i,
    output reg link_so_o, output wire link_so_oe,
    output reg link_clk_o, output wire link_clk_oe,
    output reg busy_o, output reg timeout_o, output reg overflow_o
);
    localparam integer HW = 12;
    wire [HW-1:0] half_period = RUNTIME_HALF ? half_cycles_i : HALF_CYCLES;
    localparam integer QW = $clog2(2*QUIET_CYCLES+1);
    reg role_q;
    reg [1:0] si_sync, sck_sync;
    reg sck_last;
    wire rise = sck_sync[1] && !sck_last;
    wire fall = !sck_sync[1] && sck_last;
    reg [HW-1:0] half_count;
    reg [QW-1:0] guard_count, stable_count;
    reg armed;
    reg [2:0] bit_count;
    reg [7:0] tx_shift, rx_shift, pending_data;
    reg pending;
    wire active = enable_i && !rst && !resync_i && (master_i==role_q);
    wire rx_space = !rx_valid_o || rx_ready_i;
    assign link_so_oe = active;
    assign link_clk_oe = active && master_i;
    assign tx_ready_o = active && (master_i ? (!busy_o && guard_count==0 && rx_space) : !pending);

    always @(posedge clk) begin
        si_sync <= {si_sync[0],link_si_i};
        sck_sync <= {sck_sync[0],link_clk_i};
        sck_last <= sck_sync[1];
        timeout_o <= 0;
        overflow_o <= 0;
        if(rst || !enable_i || resync_i || master_i!=role_q) begin
            role_q <= master_i;
            si_sync <= 2'b11;
            sck_sync <= 2'b11;
            sck_last <= 1;
            link_so_o <= 1;
            link_clk_o <= 1;
            busy_o <= 0;
            rx_valid_o <= 0;
            rx_data_o <= 0;
            half_count <= 0;
            guard_count <= 2*QUIET_CYCLES;
            stable_count <= 0;
            armed <= 0;
            bit_count <= 0;
            tx_shift <= 8'hff;
            rx_shift <= 0;
            pending <= 0;
            pending_data <= 8'hff;
        end else begin
            if(rx_valid_o && rx_ready_i)rx_valid_o <= 0;
            if(master_i) begin
                if(guard_count!=0)guard_count <= guard_count-1'b1;
                if(!busy_o) begin
                    link_clk_o <= 1;
                    // Keep the last data bit through idle: the peripheral's
                    // synchronized final rising edge arrives a few cycles late.
                    if(tx_valid_i && tx_ready_o) begin
                        tx_shift <= tx_data_i;
                        rx_shift <= 0;
                        bit_count <= 0;
                        half_count <= half_period-1'b1;
                        busy_o <= 1;
                    end
                end else if(half_count!=0)half_count <= half_count-1'b1;
                else begin
                    half_count <= half_period-1'b1;
                    if(link_clk_o) begin
                        link_clk_o <= 0;
                        link_so_o <= tx_shift[7];
                    end else begin
                        link_clk_o <= 1;
                        rx_shift <= {rx_shift[6:0],si_sync[1]};
                        tx_shift <= {tx_shift[6:0],1'b1};
                        if(bit_count==7) begin
                            busy_o <= 0;
                            rx_data_o <= {rx_shift[6:0],si_sync[1]};
                            rx_valid_o <= 1;
                        end else bit_count <= bit_count+1'b1;
                    end
                end
            end else begin
                link_clk_o <= 1; // Never drive the cable clock in peripheral mode.
                if(tx_valid_i && tx_ready_o) begin
                    pending <= 1;
                    pending_data <= tx_data_i;
                end
                if(rise || fall)stable_count <= 0;
                else if(stable_count<QUIET_CYCLES)stable_count <= stable_count+1'b1;
                if(!armed && sck_sync[1] && stable_count>=QUIET_CYCLES-1)armed <= 1;
                if(busy_o && stable_count>=QUIET_CYCLES-1) begin
                    // A missing cable/clock discards an incomplete byte. Re-arm
                    // only after a quiet high interval, never in mid-byte.
                    busy_o <= 0;
                    armed <= 0;
                    link_so_o <= 1;
                    rx_shift <= 0;
                    bit_count <= 0;
                    timeout_o <= 1;
                end else if(armed && fall) begin
                    if(!busy_o) begin
                        tx_shift <= pending ? pending_data : 8'hff;
                        link_so_o <= pending ? pending_data[7] : 1'b1;
                        if(pending)pending <= 0;
                        rx_shift <= 0;
                        bit_count <= 0;
                        busy_o <= 1;
                    end else link_so_o <= tx_shift[7];
                end else if(busy_o && rise) begin
                    rx_shift <= {rx_shift[6:0],si_sync[1]};
                    tx_shift <= {tx_shift[6:0],1'b1};
                    if(bit_count==7) begin
                        busy_o <= 0;
                        if(rx_space) begin
                            rx_data_o <= {rx_shift[6:0],si_sync[1]};
                            rx_valid_o <= 1;
                        end else overflow_o <= 1; // Preserve the older byte.
                    end else bit_count <= bit_count+1'b1;
                end
            end
        end
    end
`ifndef SYNTHESIS
    initial begin
        if(HALF_CYCLES<8 || QUIET_CYCLES<4*HALF_CYCLES)
            $fatal(1,"Link timing parameters are outside the CDC/quiet contract");
    end
`endif
endmodule

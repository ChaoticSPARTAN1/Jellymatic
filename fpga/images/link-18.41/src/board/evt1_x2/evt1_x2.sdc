//Copyright (C)2014-2026 GOWIN Semiconductor Corporation.
//All rights reserved.
//File Title: Timing Constraints file
//Tool Version: V1.9.9.03 
//Created Time: 2026-07-08 13:22:26
create_clock -name sclk -period 25 -waveform {0 12.5} [get_ports {QSPI_CLK}]
create_clock -name exclk -period 29.802 -waveform {0 14.901} [get_ports {CLK_FPGA}]
create_clock -name ck24 -period 41.667 -waveform {0 20.833} [get_ports {CLK_24MHz}]
create_clock -name usbintsclk -period 8 -waveform {0 4} [get_nets {u_usb_top/u_USB_SoftPHY_Top/usb2_0_softphy/u_usb_20_phy_utmi/u_usb2_0_softphy/u_usb_phy_hs/sclk}] -add
create_generated_clock -name xclk2 -source [get_ports {CLK_FPGA}] -master_clock exclk -divide_by 1 -multiply_by 4 [get_pins {u_Gowin_PLL/PLLA_inst/CLKOUT0}]
create_generated_clock -name pclk -source [get_ports {CLK_FPGA}] -master_clock exclk -divide_by 1 -multiply_by 1 [get_pins {u_Gowin_PLL/PLLA_inst/CLKOUT1}]
create_generated_clock -name gclk -source [get_ports {CLK_FPGA}] -master_clock exclk -divide_by 4 -multiply_by 1 [get_pins {u_Gowin_PLL/PLLA_inst/CLKOUT3}]
create_generated_clock -name xclk -source [get_ports {CLK_FPGA}] -master_clock exclk -divide_by 1 -multiply_by 2 [get_pins {u_Gowin_PLL/PLLA_inst/CLKOUT4}]
create_generated_clock -name hclk -source [get_ports {CLK_FPGA}] -master_clock exclk -divide_by 2 -multiply_by 1 [get_pins {u_Gowin_PLL/PLLA_inst/CLKOUT2}]
// Audio serialization uses gclk with an alternate-cycle enable.
// AUD_BCLK is an output signal; there is no internal audio_bclk clock domain.
create_generated_clock -name PHY_CLKOUT -source [get_ports {CLK_24MHz}] -master_clock ck24 -divide_by 16 -multiply_by 40 [get_pins {u_usb_top/u_Gowin_PLL_USB/PLLA_inst/CLKOUT1}]
create_generated_clock -name fclk_960M -source [get_ports {CLK_24MHz}] -master_clock ck24 -divide_by 1 -multiply_by 40 [get_nets {u_usb_top/fclk_960M}]
// pclk, hclk and gclk share the main PLL reference.
// Preserve their relationships so transfers between them are timed.
// Historical exceptions, disabled in v18.11: these related domains contain
// direct transfers that require setup/hold analysis (including cartridge-to-CPU).
// Keep disabled; any future exception needs a verified crossing/enable contract.
// The original file contained the same three declarations twice.
#set_clock_groups -asynchronous -group [get_clocks {pclk}] -group [get_clocks {hclk}]
#set_clock_groups -asynchronous -group [get_clocks {pclk}] -group [get_clocks {gclk}]
#set_clock_groups -asynchronous -group [get_clocks {hclk}] -group [get_clocks {gclk}]
#set_clock_groups -asynchronous -group [get_clocks {pclk}] -group [get_clocks {hclk}]
#set_clock_groups -asynchronous -group [get_clocks {pclk}] -group [get_clocks {gclk}]
#set_clock_groups -asynchronous -group [get_clocks {hclk}] -group [get_clocks {gclk}]
set_clock_groups -asynchronous -group [get_clocks {PHY_CLKOUT}] -group [get_clocks {fclk_960M}]
set_clock_groups -asynchronous -group [get_clocks {PHY_CLKOUT}] -group [get_clocks {usbintsclk}]
# USB and video use independent oscillators. uvc_capture transfers ownership
# through two-flop synchronizers; each RGB line bank is held until acknowledged.
# Keep all relationships within the main PLL timed (see above).
set_clock_groups -asynchronous -group [get_clocks {PHY_CLKOUT}] -group [get_clocks {hclk}]
set_clock_groups -asynchronous -group [get_clocks {PHY_CLKOUT}] -group [get_clocks {gclk}]
set_clock_groups -asynchronous -group [get_clocks {PHY_CLKOUT}] -group [get_clocks {pclk}]
set_clock_groups -asynchronous -group [get_clocks {PHY_CLKOUT}] -group [get_clocks {xclk}]
set_clock_groups -asynchronous -group [get_clocks {PHY_CLKOUT}] -group [get_clocks {xclk2}]
# Cartridge read contract: data is stable for two pclk periods before CPU
# consumption, so CART_DIN_r1 already captured the same byte on the preceding
# falling edge. This is an assumed bus guarantee, not enforced by these commands.
# Use the launch clock: setup 2 adds one pclk period (29.802 ns), giving
# 44.703 ns nominal falling-pclk to rising-hclk setup time. Hold 1 restores
# the original hold relationship. Limit the exception to cartridge -> CPU;
# DMA destinations and other pclk/hclk transfers retain their normal checks.
# List nested CPU instances explicitly: V1.9.12.03 matched cpu/* only at
# that level in the routed exception report. These are HDL instance names,
# independent of source filenames. Check Regs endpoints in report_exceptions.
set_multicycle_path -setup -start -from [get_regs {u_emu_system_top/u_cart/CART_DIN_r1_*}] -to [get_regs {u_emu_system_top/u_gb/cpu/* u_emu_system_top/u_gb/cpu/u0/* u_emu_system_top/u_gb/cpu/u0/Regs/*}] 2
set_multicycle_path -hold -start -from [get_regs {u_emu_system_top/u_cart/CART_DIN_r1_*}] -to [get_regs {u_emu_system_top/u_gb/cpu/* u_emu_system_top/u_gb/cpu/u0/* u_emu_system_top/u_gb/cpu/u0/Regs/*}] 1
#set_false_path -from [get_pins {BTN_MENU_filtered_s0/F}] -to [get_regs {u_system_monitor/btnMenu_r1_s0}] 
set_max_delay -from [get_ports {CART_D[*]}] -to [get_clocks {hclk}] 13
set_max_delay -from [get_clocks {hclk}] -to [get_ports {CART_A[*]}] 14
set_max_delay -from [get_clocks {hclk}] -to [get_ports {CART_WR}] 14
set_max_delay -from [get_clocks {hclk}] -to [get_ports {CART_RD}] 14
set_max_delay -from [get_clocks {hclk}] -to [get_ports {CART_CS}] 14
set_max_delay -from [get_clocks {hclk}] -to [get_ports {LINK_SD}] 14
set_max_delay -from [get_clocks {hclk}] -to [get_ports {CART_D[*]}] 14
# Chromatic media staging: QSPI overlay write timing (scripts/stage_chromatic_fpga_media.py, 2026-11-08)
# The ESP32 writes the overlay as a QSPI master (mode 0, 40 MHz, IO_MUX pins): the four data
# lines change on the falling edge of QSPI_CLK and qspi_slave.v samples them on the rising edge.
# Budget assumed at the FPGA pins: data valid at most 5 ns after the falling edge and held until
# at least 2 ns before the next falling edge (ESP32 output skew plus board), so the capture paths
# are analysed and no longer depend on where the placer puts the slave's registers.
# QSPI_CS is the slave's asynchronous reset: the ESP32 asserts it 3 bit times (75 ns) before the
# first clock and releases it 75 ns after the last (cs_ena_pretrans/posttrans = 3 in stock main.c
# and in the launcher), far from any clock edge: a false path by protocol.
set_input_delay -clock sclk -clock_fall -max 5 [get_ports {QSPI_MOSI QSPI_MISO QSPI_WP QSPI_HD}]
set_input_delay -clock sclk -clock_fall -min -2 [get_ports {QSPI_MOSI QSPI_MISO QSPI_WP QSPI_HD}]
set_false_path -from [get_ports {QSPI_CS}]
report_timing -setup -from [get_ports {QSPI_MOSI QSPI_MISO QSPI_WP QSPI_HD}] -max_paths 24 -max_common_paths 24
report_timing -hold -from [get_ports {QSPI_MOSI QSPI_MISO QSPI_WP QSPI_HD}] -max_paths 24 -max_common_paths 24
report_timing -setup -from_clock [get_clocks {sclk}] -to_clock [get_clocks {sclk}] -max_paths 24 -max_common_paths 24
report_timing -hold -from_clock [get_clocks {sclk}] -to_clock [get_clocks {sclk}] -max_paths 24 -max_common_paths 24
# Chromatic pocket link staging: asynchronous Game Boy link inputs (scripts/stage_chromatic_fpga_link.py, 2026-11-09)
# LINK_IN (serial data in) and LINK_CLK (the bidirectional serial clock, an input whenever the far
# end clocks) come from the other end of the link cable: another Game Boy or the Analogue Pocket,
# each on its own oscillator, with no phase or frequency relationship to any clock here, and no
# clock travels with the data into this design that an input delay could describe. Both captures
# are asynchronous samples followed by synchronisers, not synchronous captures:
#  - the stock game path registers both once on hclk (emu_system_top.v LINK_IN_r1/LINK_CLK_r1);
#    link.v then shifts LINK_CLK_r1 through two more flops (serial_clk_in_last) and samples the
#    data line only on the edge it detects there, microseconds after LINK_IN settled (the game
#    link runs at 8 kHz, 256 kHz at most in GBC fast mode), and in internal-clock mode it samples
#    at its own divider edge, a half bit after its own clock edge;
#  - the pocket-link bridge two-flop synchronises both on gClk (pocket_link/gb_link_byte.v
#    si_sync/sck_sync) and acts on the second flop only.
# Until this patch the ports had no set_input_delay, so Gowin left them unconstrained; the false
# path states that by design. Outputs (LINK_OUT, LINK_CLK when driven, LINK_CLK_DIR_LV) and
# register-to-register paths behind the first flops stay timed as before.
set_false_path -from [get_ports {LINK_IN LINK_CLK}]
# Review evidence: the worst setup paths of the whole design, and the stock cartridge-bus port limits
# (the set_max_delay lines above), printed in the timing report so a violation is named, not just counted.
report_timing -setup -max_paths 20 -max_common_paths 1
report_timing -hold -max_paths 20 -max_common_paths 1
report_timing -setup -to [get_ports {CART_A[*] CART_D[*] CART_WR CART_RD CART_CS LINK_SD}] -max_paths 10 -max_common_paths 1
report_timing -setup -from [get_ports {CART_D[*]}] -max_paths 10 -max_common_paths 1

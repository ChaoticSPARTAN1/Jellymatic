# Third-party notices

Components by others that are **included in this repository**, with where their licence text is.

| Component | Files here | Licence | Licence text |
|---|---|---|---|
| ModRetro Chromatic FPGA design v18.14 (commit a01aa375), files modified by ChaoticSPARTAN1 for Jellymatic, 2026-11 | `fpga/images/sound-18.40/src/` and `fpga/images/link-18.41/src/`: ModRetro's files `top.v`, `rtl/BSP/system_monitor.sv`, `rtl/EMU/emu_system_top.v`, `rtl/EMU/cart.v`, `board/evt1_x2/evt1_x2.sdc` (both images), `rtl/BSP/uart/uart.v`, and the project files `evt1_x2.gprj`, `build_options.tcl` (18.41); the full list is in [NOTICE](NOTICE) | GPL-3.0 | [LICENSE](LICENSE) (the same GPL-3.0 text) |
| `uart.v` (UART2 wrapper) in ModRetro's design | `fpga/images/link-18.41/src/rtl/BSP/uart/uart.v` (modified) | GPL-3.0, as ModRetro publishes it in its repository | [LICENSE](LICENSE). The file came to ModRetro's design from Gowin's reference design; ModRetro publishes it under its repository's GPL. Kept as ModRetro ships it apart from Jellymatic's marked hook. |
| MiSTer core ("Gameboy_MiSTer") (Till Harbaum and the MiSTer developers), as used in ModRetro's design | `fpga/images/*/src/rtl/EMU/CORE/gb.v` (modified) | GPL-3.0-or-later (file header) | [LICENSE](LICENSE) |
| Shiny Relay 1.1.5 (Shiny Gen), the open firmware for the Chromatic | the `uart_tx_busy` one-cycle gap fix in `fpga/images/link-18.41/src/rtl/BSP/system_monitor.sv` (taken from its `system_monitor.sv`, marked in the file); the request-once status channel pattern of its `link_ev_req` in `rtl/chromatic_pocket_link.v`; the plain-word Wi-Fi join messages in `chromatic/launcher/shared/wifi_scan.h` | GPL-3.0 | [LICENSE](LICENSE) (the same GPL-3.0 text) |
| `speedcontrol.vhd` of ModRetro's design (from the MiSTer core "Gameboy_MiSTer"), translated to Verilog for simulation | `chromatic/fpga-media/sim/speedcontrol_model.v` (translation by ChaoticSPARTAN1, 2026-11) | GPL-3.0, as ModRetro publishes the original (ADDITIONAL-TERMS.md does not apply) | [LICENSE](LICENSE) |
| "Simple UART for FPGA" (Jakub Cabal), as used in ModRetro's design; Verilog port for simulation | `rtl/uart2_vhdl_port.v` | MIT (port: GPL-3.0-or-later) | [LICENSES/MIT-uart_for_fpga.txt](LICENSES/MIT-uart_for_fpga.txt) |
| Espressif ESP-IDF bootloader start file | `chromatic/launcher/bootloader_components/main/bootloader_start.c` (modified) | Apache-2.0 | [LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt) |
| Jellyfin logo (jellyfin/jellyfin-ux, commit 69a2b7b1) | scaled in `chromatic/launcher/shared/launcher_art.h`, `scripts/chromatic_theme.py`; shown in `docs/images/jellymatic-icon.png` and console screenshots | CC BY-SA 4.0 | [LICENSES/CC-BY-SA-4.0-Jellyfin-logo.txt](LICENSES/CC-BY-SA-4.0-Jellyfin-logo.txt) |
| Roboto Medium (Roboto 2.138, Copyright 2011 Google Inc.), rasterized and scaled by `scripts/build_ui_font.py` | `pocket/ui_font.h` | Apache-2.0 | [LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt) |
| Press Start 2P (CodeMan38) | `scripts/chromatic_gui_fonts/heading-PressStart2P-Regular.ttf`, `docs/fonts/`, `remote-pilot/fonts/` | SIL OFL 1.1 | [LICENSES/OFL-1.1-PressStart2P.txt](LICENSES/OFL-1.1-PressStart2P.txt) |
| Silkscreen (The Silkscreen Project Authors) | `scripts/chromatic_gui_fonts/body-Silkscreen-Regular.ttf`, `docs/fonts/`, `remote-pilot/fonts/` | SIL OFL 1.1 | [LICENSES/OFL-1.1-Silkscreen.txt](LICENSES/OFL-1.1-Silkscreen.txt) |

## Not included, needed to build or run

Installed separately by you; each comes with its own licence: ESP-IDF v5.3 and its components, ModRetro's
full FPGA source with its submodule, Gowin EDA (proprietary, needs your own licence), openFPGALoader
(Apache-2.0), Python (PSF), PySide6/Qt (LGPL-3.0), esptool (GPL-2.0-or-later), pyserial (BSD-3-Clause).

## If you distribute built images

This repository ships **no** built firmware. If you publish a built start-up menu or FPGA image:

- **Start-up menu** (ESP32 binaries): include ESP-IDF's notices (its `LICENSE` and `COPYRIGHT.rst`, which
  list FreeRTOS (MIT), lwIP (BSD-3-Clause), Mbed TLS (Apache-2.0), wpa_supplicant (BSD), newlib and the
  Espressif Wi-Fi/PHY libraries (Apache-2.0)), this repository's licence, and the exact source commit.
  Linking with Espressif's binary-only Wi-Fi/PHY libraries and calling the ESP32 ROM is allowed by the
  additional permission in [ADDITIONAL-TERMS.md](ADDITIONAL-TERMS.md), section 4.
- **FPGA images**: they are GPL works. Ship them with the GPL text, this repository's source at the same
  commit and ModRetro's source at the commit in `upstream.lock.json`, plus the notices of the T80 CPU
  ([LICENSES/BSD-style-T80.txt](LICENSES/BSD-style-T80.txt), required for synthesized forms) and the
  SameBoy-derived boot ROM ([LICENSES/MIT-SameBoy-boot-ROM.txt](LICENSES/MIT-SameBoy-boot-ROM.txt)). Both
  are in ModRetro's source, not in this repository, but both are built into the images. The images also
  contain Gowin IP that ModRetro publishes only in encrypted form; check Gowin's terms for redistributing
  bitstreams before you publish one.

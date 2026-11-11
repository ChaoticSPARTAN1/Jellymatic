<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Building from source

[Home](Home.md) › Building from source

**Contents:** [Start-up menu](#start-up-menu) · [Console chip (FPGA) images](#console-chip-fpga-images) ·
[Chromatic Manager](#chromatic-manager) · [Tests](#tests) · [Previews and pictures](#previews-and-pictures)

Everything builds on Windows. Nothing in this page touches a console.

## Start-up menu

Needs: Git, Python 3.12, about 3 GB of disk.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\Get-Upstreams.ps1 -EspIdf   # ESP-IDF v5.3 at the pinned commit
$env:IDF_TOOLS_PATH = "$env:LOCALAPPDATA\Jellymatic\tools\esp32-tools"
upstream\esp-idf\install.ps1 esp32                                            # Espressif's own tool installer
powershell -ExecutionPolicy Bypass -File scripts\Build-ChromaticLauncher.ps1
```

The result goes to `release\launcher\` (ignored by Git), where Chromatic Manager installs it from.
`idf.py flash` is made to fail on purpose: it would overwrite the console's official firmware. Install
only with Chromatic Manager.

Settings: `chromatic\launcher\CMakeLists.txt` (`PROJECT_VER`, `PJ_ENABLE_SETUP`),
`chromatic\launcher\sdkconfig.defaults`, `chromatic\launcher\partitions.csv` (the three official rows
first, unchanged).

## Console chip (FPGA) images

Needs: Gowin EDA **V1.9.12.04** with **your own** Gowin licence (from gowinsemi.com; never commit it),
and ModRetro's FPGA source:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\Get-Upstreams.ps1 -ChromaticFpga
$env:GOWIN_SH = "C:\Gowin\Gowin_V1.9.12.04_x64\IDE\bin\gw_sh.exe"              # where your Gowin is
py -3 scripts\stage_chromatic_fpga_link.py --build                           # 18.41: stage + Gowin build
py -3 scripts\stage_chromatic_fpga_link.py --export my-18.41                 # copy image, receipts, sources
```

The staging scripts copy ModRetro's tree (never edited) to `staging\` and apply exact text patches; every
anchor must match or they stop. `scripts\stage_chromatic_fpga_media.py` does the same for the sound image
18.40 (build it with `gw_sh build.tcl` in `staging\chromatic-fpga-media\esp32t`, then
`py -3 scripts\record_chromatic_fpga_build.py staging\chromatic-fpga-media\esp32t`).

`fpga\images\link-18.41\` and `fpga\images\sound-18.40\` hold the **exact sources and receipts** of the
reviewed images, but not the images themselves. To use an image, put the **official release files**
`evt1_x2.fs` and `evt1_x2.bin` (published separately as release assets) in that folder's `build\`. The
Manager refuses any image whose bytes differ from its receipt (and, for 18.41, from the SHA-256 pins in
`scripts\chromatic_fpga.py`). A build of your own is almost never byte-identical (any other Gowin version,
setting or IP changes the bytes), so it will not match the pins and is refused; that is on purpose, because only the reviewed image
was tried on a console. Building is still useful to check the sources and the timing reports.

The RTL added by Jellymatic is in `rtl\` (Pocket link bridge, link-port byte/stream, FIFOs) and
`chromatic\fpga-media\` (sound hooks), each with Icarus Verilog test benches.

## Chromatic Manager

Runs from source (`Setup-Jellymatic.ps1`, then `Chromatic-Manager-Window.cmd`). To make a portable
Windows program for yourself: `scripts\Build-ChromaticManagerApp.ps1` (PyInstaller; output in `dist\`).
If you **share** such a build, you take on the obligations of the bundled libraries: Qt/PySide6 is
LGPL-3.0 (keep the Qt libraries replaceable, ship their licence and point to their source), esptool is
GPL-2.0-or-later, Python and the other packages have their own licences. That is why this repository
ships source, not a built program.

## Tests

Needs MSYS2 UCRT64 gcc (`C:\msys64\ucrt64\bin\gcc.exe`) for the C host checks, and for the RTL benches
Icarus Verilog in `<tools>\fpga-tools\native\ucrt64` (`bin\iverilog.exe`, `bin\vvp.exe`; `<tools>` is
`%LOCALAPPDATA%\Jellymatic\tools` or `JELLYMATIC_TOOLS`).

```powershell
$py = "$env:LOCALAPPDATA\Jellymatic\venv\Scripts\python.exe"
& $py -B scripts\test_chromatic_manager.py        # Manager logic against a simulated console
& $py -B scripts\test_chromatic_manager_gui.py    # the window, offscreen
& $py -B scripts\test_chromatic_launcher.py       # launcher host checks (+ ELF checks when built)
& $py -B scripts\test_chromatic_setup.py          # on-console Wi-Fi/Jellyfin set-up, power-cut model
& $py -B scripts\test_chromatic_fpga.py           # FPGA jobs against a simulated chip
```

Build the start-up menu first ([above](#start-up-menu)): the Manager tests install that build on a
simulated console, and the launcher test checks the linked program; without it they stop with "No launcher
release found" (the launcher test skips its build checks). The FPGA staging tests need ModRetro's source
(`Get-Upstreams.ps1 -ChromaticFpga`) and staged trees: run `py -3 scripts\stage_chromatic_fpga_media.py`,
`py -3 scripts\check_pocket_link_patch.py` and `py -3 scripts\stage_chromatic_fpga_link.py` first (nothing
is built by these; they only copy and patch).

The tests never open a device. Where they need a stock firmware image they use a **made-up** one
(`compose_chromatic_image.synthetic_stock`, no ModRetro code in it), unless you point
`JELLYMATIC_STOCK_DUMP` at a full read of your own console. Remote Pilot's tests are in `remote-pilot\`
(`python -m unittest test_chromatic`, `node test_chromatic_panel.js`).

## Previews and pictures

- `scripts\render_chromatic_manager_previews.py --out <folder>`: the Manager window in each state, with
  made-up data.
- `scripts\test_chromatic_launcher.py` and `test_chromatic_setup.py` write the console's screens as
  `.ppm`/`.png` previews under `staging\`.
- `scripts\test_chromatic_player.py` writes the Jellyfin app's screens (synthetic test pattern video).
- The console's text font, `pocket\ui_font.h`, is made from Google's Roboto Medium (Apache-2.0) by
  `py -3 scripts\build_ui_font.py --fetch` (needs `fonttools`; the font is pinned by SHA-256 in
  `upstream.lock.json`). `--check` only compares the result with the file in the repository.

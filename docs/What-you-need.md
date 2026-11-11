<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# What you need

[Home](Home.md) › What you need

**Contents:** [For the start-up menu and Jellyfin](#for-the-start-up-menu-and-jellyfin) ·
[For Jellyfin sound and the Pocket link](#for-jellyfin-sound-and-the-pocket-link) ·
[Not needed](#not-needed)

## For the start-up menu and Jellyfin

| You need | Why | Notes |
|---|---|---|
| A **ModRetro Chromatic** with its **official firmware** | The start-up menu sits next to it | Tested with official MCU firmware v4.6 and FPGA v18.14. The Manager reads the console first and **refuses** layouts or firmware it does not know, rather than guess. |
| A **cartridge** in the console | The menu draws over a running cartridge | Any working cartridge or flash cart. Without one, the console simply starts as normal. |
| A **Windows 10 or 11 PC** | Chromatic Manager runs there | macOS/Linux: the code is Python and mostly portable, but only Windows is tested. |
| **Python 3.12 or newer** | Runs Chromatic Manager from source | From [python.org](https://www.python.org/downloads/). `Setup-Jellymatic.ps1` installs the rest. |
| A **USB-C data cable** | PC ↔ console | The console must be switched **on** while the Manager works. |
| A **Jellyfin server** on your network, with **Quick Connect** turned on | The console signs in with its own code | See [Hosting Jellyfin](Hosting-Jellyfin.md). |
| **2.4 GHz Wi-Fi** | The console's radio | A phone hotspot works. Hotel-style sign-in pages are not supported. |
| The **start-up menu image** | What the Manager installs | Download `Jellymatic-start-up-menu-<version>.zip` from the [Releases page](https://github.com/ChaoticSPARTAN1/Jellymatic/releases/latest) and extract it into your Jellymatic folder (it fills `release/launcher/`), or build it ([Building from source](Building-from-source.md)). |
| **openFPGALoader** with **GWU2X** cable support | The first [full backup](Install-with-Chromatic-Manager.md#full-backup-before-any-fpga-change) reads the console chip (FPGA) too, over the console's USB | Not shipped with Jellymatic (Apache-2.0). Ready-made downloads often lack GWU2X support, so it usually has to be **built**: see ModRetro's own instructions in its [Chromatic FPGA source](https://github.com/ModRetro/oss-chromatic-console-fpga#flashing) (README, "Flashing": openFPGALoader must be compiled with GWU2X cable support) and [openFPGALoader's build guide](https://trabucayre.github.io/openFPGALoader/guide/install.html). Put it at `%LOCALAPPDATA%\Jellymatic\tools\openFPGALoader\openFPGALoader.exe` or set `JELLYMATIC_OPENFPGALOADER`. |
| The Gowin **GWU2X USB driver** | The JTAG bridge openFPGALoader talks to | Usually installed by ModRetro's official updater. |

## For Jellyfin sound and the Pocket link

These change the console's **game chip (FPGA)**. They are optional; the start-up menu and silent video
work without them.

| You need | Why |
|---|---|
| A **full backup of your console** (both chips) | The Manager will only change the FPGA when it can put your original image back. It takes this backup by itself the first time ([Install](Install-with-Chromatic-Manager.md#full-backup-before-any-fpga-change)). |
| **openFPGALoader** and the **GWU2X driver** | As above: they read and write the FPGA over the console's USB. |
| The **official release files** of the FPGA images 18.40 (sound) and 18.41 (sound + Pocket link) | The sound and the Pocket link **need the official release files** (`evt1_x2.fs` and `evt1_x2.bin` of each image), published separately as **release assets** of this project: download `Jellymatic-FPGA-images-<version>.zip` from the [Releases page](https://github.com/ChaoticSPARTAN1/Jellymatic/releases/latest) and extract it into your Jellymatic folder, keeping its folders. It puts each pair in its folder's `build\` (`fpga/images/sound-18.40/build/`, `fpga/images/link-18.41/build/`). This repository has the exact sources and receipts, but no binaries. **A build of your own will not match the pins** (it is almost never byte-identical to the reviewed image), and the Manager refuses it on purpose: only the reviewed image was tried on a console ([Building from source](Building-from-source.md#console-chip-fpga-images)). |
| For the Pocket link: an **Analogue Pocket**, **PocketFin**, and a **link cable** | See [Pocket link](Pocket-link.md). |

## Not needed

- No developer cartridge, UART adapter or soldering.
- No account anywhere, and nothing is uploaded. Your Wi-Fi and Jellyfin details stay on your PC
  (`%LOCALAPPDATA%\Jellymatic`) and your console.
- No ModRetro files: Jellymatic never ships their firmware or updater. For official updates you use
  **ModRetro's own update tool**, downloaded from ModRetro ([Recovery](Recovery.md#official-updates-and-restoring-chromatic-os)).

Next: [Before you start](Before-you-start.md)

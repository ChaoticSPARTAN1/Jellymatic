<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# FAQ

[Home](Home.md) › FAQ

**Contents:** [General](#general) · [Safety](#safety) · [Jellyfin](#jellyfin) · [Pocket link](#pocket-link) · [Help](#help)

## General

**Is this made by ModRetro or Jellyfin?** No. Jellymatic is an independent project, not affiliated with
ModRetro or the Jellyfin project.

**Does it cost anything?** No. It is free software (GPL-3.0-or-later). Building the FPGA images needs
Gowin's FPGA tools, which need a (free) licence from Gowin.

**Which consoles?** The ModRetro Chromatic. Tested with official firmware MCU v4.6 and FPGA v18.14 on one
console. The Manager refuses firmware or layouts it does not recognise.

**Do I need the PC after set-up?** No. Wi-Fi and Jellyfin are set up on the console and it talks to your
server directly. The PC is for installing, updating and recovery.

## Safety

**Can it break my console?** Every write is backed up, journalled, read back and approved by you, and the
official firmware stays on the console. The ESP32's ROM rescue mode cannot be blocked by anything in
flash, so the Manager and ModRetro's updater can always reach it. Still: **use it at your own risk**.

**Are my games and saves safe?** Jellymatic never reads or writes cartridges, flash carts, their SD cards or
saves. **Games** starts the untouched official firmware.

**Can I go back to completely official?** Yes: **Remove menu** or **Restore original** in the Manager, or
ModRetro's official update tool ([Recovery](Recovery.md)).

**Does it send anything anywhere?** No. Nothing is uploaded. The console talks only to your Jellyfin
server (and, if you use it, Remote Pilot on your own PC).

## Jellyfin

**Where is my Jellyfin password?** Only where you put it. The console gets its own sign-in with Quick
Connect; the Manager can do the same if you give it this PC's Jellyfin settings
(kept in `%LOCALAPPDATA%\Jellymatic`).

**Why is there no sound?** Sound needs the FPGA sound image 18.40 or 18.41
([Install](Install-with-Chromatic-Manager.md#jellyfin-sound-fpga-1840)).

**Why is the picture small?** The console's screen is 160 × 144; video is up to 160 × 120.

## Pocket link

**What do I need?** An Analogue Pocket with [PocketFin](https://github.com/ChaoticSPARTAN1/PocketFin), a
link cable, and the 18.41 FPGA image on the Chromatic ([Pocket link](Pocket-link.md)).

**Does the Pocket need Wi-Fi?** No. The Chromatic does the network part.

## Help

**Where do I report a bug?** In the repository's issues ([CONTRIBUTING](../CONTRIBUTING.md)). Security
problems privately ([SECURITY](../SECURITY.md)). Support is not guaranteed.

**Can I use an AI assistant?** Yes, with care: [AI prompt examples](AI-prompt-examples.md).

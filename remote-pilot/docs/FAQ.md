# FAQ

[Home](Home.md) › FAQ

**On this page:** [General](#general) · [Safety](#safety) · [Technical](#technical)

## General

**Is this an official ModRetro tool?**
No. It is an independent hobby project, not affiliated with, endorsed by or supported by ModRetro.

**Do I need a console to try it?**
No. The Chromatic preset has simulators for Wi-Fi and USB, and the **Example** preset is a local
demo.

**Can I play games remotely?**
Not yet. The launcher menus work today; games need a future FPGA update
([What it does](What-it-does.md)).

**Does it work with other handhelds?**
The viewer is generic. Other devices can be added as presets with their own adapter
([ADAPTER_API.md](../ADAPTER_API.md)). This repository ships the Chromatic preset and the Example.

**Is it free?**
Yes, under GPL-3.0-or-later, with an attribution term ([Credits and licences](Credits-and-licences.md)).

## Safety

**Can Remote Pilot brick my console?**
Remote Pilot never writes the console's flash itself. It can start Chromatic Manager for firmware
jobs, but only from this PC, after you type an exact phrase. Chromatic Manager makes a backup first
and can restore stock. As with any firmware tool, use it at your own risk.

**Does anything go to the internet?**
No. The viewer runs on your PC and the console link stays on your home network. The setup script
downloads pyserial from PyPI once; that is the only download.

**Can other people on my Wi-Fi see the viewer?**
Not by default (it listens on 127.0.0.1 only). With `--viewer-lan` they need the one-time code
printed on this PC, and the connection is plain HTTP (not encrypted), so use that only on a home
network you trust. See [Security model](Security-model.md).

**Can I keep the Remote Pilot folder in OneDrive?**
No. Keep it out of OneDrive and other cloud-synced folders: `local\chromatic\pairing.json` holds
the key that lets a PC control your console.

**What should I never share?**
Anything in `local\` (especially `local\chromatic\pairing.json`), Wi-Fi names and passwords,
addresses and tokens.

## Technical

**Which Python?** 3.11 or newer. The receiver uses only the standard library; pyserial is needed
for USB only.

**Which browser?** Any current Edge, Chrome or Firefox.

**Why does `localhost` not work?** On purpose: numeric addresses protect against DNS-rebinding
attacks. Use `http://127.0.0.1:38764/`.

**Where are my screenshots?** In `local\captures\` inside the Remote Pilot folder.

**Can my AI assistant use it?** Yes; see [Using it with AI assistants](Using-with-AI-assistants.md).

# What it does

[Home](Home.md) › What it does

**On this page:** [In one sentence](#in-one-sentence) · [What works today](#what-works-today) ·
[What needs more work](#what-needs-more-work) · [What it never does](#what-it-never-does)

## In one sentence

Remote Pilot is a small program on your PC (the *receiver*) plus a web page (the *viewer*) that
shows your Chromatic's screen and lets you press its buttons, read its logs and save screenshots.

## What works today

With the Chromatic **launcher** (the start-up menu from
[Jellymatic](https://github.com/ChaoticSPARTAN1/Jellymatic), version 1.1.2 or newer):

- **Connect** over a USB cable or your home Wi-Fi. Both use a private pairing key.
- **See the screen**: take a screenshot or watch live video (up to 15 pictures a second).
- **Press buttons**: single presses, several at once (chords), or hold a button. A game controller
  plugged into the PC also works.
- **Read logs and status**: the console's log lines, a fixed list of read-only console commands
  (version, uptime, Wi-Fi state...), a timing graph and a saved QA report.
- **Save evidence**: screenshots, capture bundles and reports are saved in `local\captures\` on
  your PC.
- **Drive it from scripts or AI assistants**: `python -m chromatic.cli` does everything the page
  does (see [Using it with AI assistants](Using-with-AI-assistants.md)).
- **Try it without a console**: built-in simulators behave like a real launcher, over Wi-Fi or USB.

## What needs more work

While a **game** is running, the console's own (stock) chip owns the picture, sound and buttons.
Seeing and controlling games needs a future FPGA update, so the viewer marks those features
**Needs FPGA update**. The simulator's *Simulated future device (FPGA game bridge)* mode shows how
they will look.

## What it never does

- It never writes your console's flash memory itself. Firmware changes are done only by
  **Chromatic Manager** (part of Jellymatic), which Remote Pilot can start for you from this PC
  after you type an exact phrase.
- It never touches cartridges or save files.
- It never sends anything to the internet. Everything stays on your PC and your home network.

Next: [Install](Install.md)

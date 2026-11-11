# Troubleshooting

[Home](Home.md) › Troubleshooting

**On this page:** [The page does not open](#the-page-does-not-open) ·
[USB is not found or will not open](#usb-is-not-found-or-will-not-open) ·
[Wi-Fi does not connect](#wi-fi-does-not-connect) · [Buttons do nothing](#buttons-do-nothing) ·
[Game features say Needs FPGA update](#game-features-say-needs-fpga-update) ·
[Getting the console back to normal](#getting-the-console-back-to-normal)

## The page does not open

- Is the black `Start-RemotePilot.cmd` window still open? Start it again if not.
- Use exactly `http://127.0.0.1:38764/`. `localhost` is refused on purpose, and so is the PC's
  network address unless you started it with `--viewer-lan`.
- "Address already in use": another copy is running, or another program uses port 38764. Close it,
  or start with `--viewer-port 38800` and open that port instead.
- "Python was not found": install Python 3.11+ ([Install](Install.md)).

## USB is not found or will not open

- **"USB support (pyserial) is not installed in this Python"**: run `Setup-RemotePilot.ps1`, then
  start with `Start-RemotePilot.cmd` (it uses `local\venv`).
- No port listed: use a USB-C **data** cable, switch the console on, and check that the launcher
  (1.1.2 or newer, from [Jellymatic](https://github.com/ChaoticSPARTAN1/Jellymatic)) is showing.
  A stock console has nothing to talk to.
- **USB paused** (amber band): Chromatic Manager is using the port for a job. Wait until it ends;
  **Use USB again** appears when it is safe.
- The launcher hands over to Games after its countdown. Remote Pilot pauses the countdown only
  while a session is open; if the console already started a game, switch it off and on.
- "Access denied" on the COM port: another program (a serial terminal, the Manager) has it open.

## Wi-Fi does not connect

- Pair first ([Pairing](Pairing-with-a-Chromatic.md)). Wi-Fi needs a key.
- On the console, open the launcher's Jellyfin screen and press **A** so it joins Wi-Fi.
- The PC and console must be on the **same private network** (for example both on your home
  router, not a guest network that isolates devices).
- If the PC has several networks (VPN, virtual adapters), start with
  `Start-RemotePilot.cmd --pc-host <the PC's address on the console's network>`.
- Press **Search again** / **Retry connection** after the console's address changes.

## Buttons do nothing

- Tick **Enable remote controls** and keep the tab in front and focused.
- The badge must say **Device connected**, and the PC link must be on (**Connect to device**).
- While a game runs, buttons need a future FPGA update (see below); the launcher menus work.

## Game features say Needs FPGA update

That is expected. During games the console's unmodified stock chip owns the picture, sound and
buttons. Those features light up only when a future FPGA update offers them. The simulator mode
**Simulated future device (FPGA game bridge)** shows how they will work.

## Getting the console back to normal

Remote Pilot never changes the console's firmware, so closing it is always safe. If the console
itself needs fixing:

- The launcher's **Games** entry always starts the console's own firmware.
- Use **Chromatic Manager** from [Jellymatic](https://github.com/ChaoticSPARTAN1/Jellymatic) to
  check the console, remove the launcher or restore stock from your backup. Follow its own guide,
  with the console in front of you.
- If a Manager job was interrupted, run the Manager again: it finishes or undoes the job first (the
  viewer then asks for the phrase `FINISH CHROMATIC JOB`).

Still stuck? See the [FAQ](FAQ.md) or open an issue ([CONTRIBUTING.md](../CONTRIBUTING.md)),
without private details.

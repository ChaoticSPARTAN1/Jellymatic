# ModRetro Chromatic preset

Select **ModRetro Chromatic** in the header menu. The viewer swaps the generic
dual-screen gamepad drawing for a Chromatic-shaped device (one 160×144 screen in the
upper half, D-pad, A, B, Start, Select, Menu) in the Chromatic Manager's
pixel-art colours, and a **Chromatic tools** panel that lists every feature with
its real availability.

Status: **the PC side is complete and tested against simulators and against the
launcher's own CHR1 handler (its PC harness).** USB and Wi-Fi need the
Chromatic launcher 1.1.2 or newer on the console, from
[Jellymatic](https://github.com/ChaoticSPARTAN1/Jellymatic); older launchers
speak no CHR1 (the read-only log reader still works). Remote Pilot never writes
flash itself; firmware changes go only through the Chromatic Manager.

Main purpose: let a person or an AI test agent connect to the Chromatic and
drive it for QA during development: buttons, screen, logs, status. Playing games
remotely is a later layer that needs an FPGA update.

## Features

"Simulator" means it works end to end against `chromatic/simulator.py` today.

| Feature | Chromatic | Waiting on |
| --- | --- | --- |
| Paired local-network link | CHR1, mutual HMAC, private LAN only (simulator, launcher harness) | Launcher 1.1.2 or newer; it joins Wi-Fi after you press A on its media screen |
| Wired link | CHR1 over USB serial, same actions as Wi-Fi (simulator, launcher harness) | Launcher 1.1.2 or newer |
| Automatic choice | USB when present and allowed, else Wi-Fi (simulator, launcher harness) | as above |
| Pairing | USB pairing: the same 6-digit code on both screens, a physical A press approves; or the key Chromatic Manager provisions (simulator, launcher harness) | as above |
| Find device on the LAN, Retry | /24 handshake scan, cached address, Search again (simulator) | as above |
| Read-only log reader | USB serial text lines, any launcher (simulated serial device) | — |
| Ping, AI-agent tools | `python -m chromatic.cli` wait, status, tail, ping, perf, press, hold, shot --png (simulator, viewer) | — |
| Connect / Disconnect gate | Shared; device actions blocked while disconnected | — |
| Screenshots | 160×144 BMP or PNG of whatever owns the screen (simulator) | Launcher: CHR1 `screen`; games: FPGA update |
| Live video | CHV1 delta/RLE video, 1–15 fps, measured fps (simulator) | Launcher: CHR1 `video`; games: FPGA update |
| Live sound | PCM16 mono 16,384 Hz, game only (simulator FPGA mode) | FPGA update (the launcher is silent) |
| Button press | 20–250 ms presses (simulator) | Launcher: CHR1 `input`; games: FPGA update |
| Chords | Ctrl+click, up to 4 buttons; MENU never chorded (simulator) | as above |
| Hold and release | Mouse hold with 600 ms device lease, refreshed every 200 ms (simulator) | Launcher: CHR1 `hold`; games: FPGA update |
| Game controller | Gamepad API → D-pad/A/B/Start/Select; MENU not mapped (simulator) | as above |
| Remote-control lease | Focused-tab lease for all buttons | — |
| Console commands | Read-only fixed list: help, version, uptime, heap, wifi, phase, reset-reason (simulator) | Launcher: CHR1 `console` |
| Device log retrieval | Serial lines (read only) and CHR1 `log` (simulator) | — |
| Capture bundle | Screen + log + report + `chromatic-capture-*.json` | — |
| Report | QA report: status, availability, timing, activity, log tail | — |
| Timing telemetry | Frame/work graph from CHR1 `perf` (simulator) | — |
| Diagnostics collection | Device log, status, console output, Manager files, last Manager job | — |
| Install / update / recover firmware | Chromatic Manager: status, backup, install, remove, restore-stock; typed confirmation, this PC only, no cancel | Real runs only with the console present |
| Wireless self-update | Launcher-slot-only update, design only (LAUNCHER-SIDE.md 5) | Launcher work |
| Analog sticks, touch screen, SD data transfer | Not applicable: the Chromatic has none of these; cartridges are never written | — |

Game screenshots, video, sound and input need an FPGA update **and** device
firmware that stays on the network during games (LAUNCHER-SIDE.md 4). Until a
device offers them they show "Needs FPGA update"; the simulator's
**Simulated future device (FPGA game bridge)** mode lights them up so the PC
side can be tested.

## Connections

Choose in the panel's **Connection** list:

- **Configured**: the real console, from `local/chromatic/config.json`:
  - `auto` (the default once the config allows the serial port): USB when the
    port is present and the launcher answers, otherwise Wi-Fi;
  - `usb`: CHR1 over USB only;
  - `lan`: Wi-Fi only (the default when only a pairing key exists);
  - `serial`: the read-only log reader (any launcher; nothing is written).
- **Simulated launcher over Wi-Fi**, **Simulated future device (FPGA game
  bridge)**, **Simulated USB serial (read only)**.
- **Simulated USB console (pairing demo)**: a console with no key; press
  **Pair over USB** and the simulator presses A after 3 s. Its key stays in memory.
- **Simulated console on USB and Wi-Fi**: the automatic choice.
- **Off**.

**Connect to device** (the shared button) re-reads `local/chromatic/config.json`
and switches to the configured transport. With no config yet it lists the
serial ports whose USB vendor ID is the Chromatic's (`374E`), without opening
any, and offers **Allow USB on COMx** under the button. Only that click, from
this PC's own browser, writes the private, Git-ignored config and opens the port:

```json
{"mode": "auto", "serialPort": "COM5", "serialOpenAllowed": true, "lanHost": "auto"}
```

You can also write it yourself, with the console in front of you, and add
`"managerProject": "<path to your Jellymatic folder>"` for the Manager tools
(Allow USB keeps fields already there). Use `\\` for each backslash in JSON.
Running the Manager from Remote Pilot stays off until you also add
`"managerRunAllowed": true`; a full template with it switched off:

```json
{"mode": "auto", "serialPort": "COM5", "serialOpenAllowed": true, "lanHost": "auto",
 "managerProject": "C:\\Jellymatic", "managerRunAllowed": false}
```

Keep the Remote Pilot folder out of OneDrive and other cloud-synced folders:
`local/chromatic/pairing.json` holds the key that lets a PC control the console.

USB needs **pyserial** in the Python that runs the receiver. `Setup-RemotePilot.ps1`
creates `local\venv` with it, and `Start-RemotePilot.cmd` uses that venv when it
exists. Without it the panel says "USB support (pyserial) is not installed in
this Python". DTR and RTS stay de-asserted; only CHR1 lines are written, and only
once a launcher 1.1.2+ shows on the port.

**Reaching the launcher.** It runs from power-on until it hands over to stock
(about 5 s of countdown). Remote Pilot opens a USB session the moment the
launcher's start line appears, which pauses the countdown (at most 10 minutes
per power-on). After a hand-over the console runs stock until it is switched
off and on; Remote Pilot never resets it.

Pairing key, in `local/chromatic/pairing.json` (Git-ignored, never shown on the page):
- **Pair over USB** in the panel (or `python -m chromatic.cli pair-usb`): both
  screens show the same code and only an A press on the console approves. That
  key lasts until the console powers off. A key Chromatic Manager provisioned
  is never overwritten silently; it is kept as `pairing-provisioned.json`.
- Pairing capture (serial reader) keeps the key it replaces as
  `pairing-backup-<date-time>.json`.
- Choosing the connection, pairing and the Manager's output work only from this
  PC's own browser, never from the LAN viewer.
- Chromatic Manager's provisioning writes the console's record and this file together.
- Or `python -m chromatic.cli pair --key-file <file>`.

## Firmware patching

The **Firmware patching (Chromatic Manager)** section starts
`scripts/chromatic_manager.py` from `managerProject` with the Manager's own
Python (`managerPython` if set; else the `venv` that `Setup-Jellymatic.ps1` makes in
your per-user Jellymatic folder, `JELLYMATIC_HOME` or `%LOCALAPPDATA%\Jellymatic`; else
an older setup's `private/chromatic-tools/venv`), as a separate process, and shows its
redacted output. Manager journals and backups are looked for in that per-user folder
first, then in `managerProject/private/`. Guards:

- only when `"managerRunAllowed": true` is in `local/chromatic/config.json` (off by default);
- only from this PC's own browser (127.0.0.1), never the LAN viewer;
- Remote Pilot's serial mode must be off (the Manager needs the port);
- the exact phrase must be typed: `READ CHROMATIC`, `BACKUP CHROMATIC`,
  `INSTALL CHROMATIC LAUNCHER`, `REMOVE CHROMATIC LAUNCHER`,
  `RESTORE CHROMATIC STOCK`; if a Manager journal is present the phrase is
  `FINISH CHROMATIC JOB`, because the Manager first finishes or undoes it;
- one job at a time, and no cancel button: interrupting a write is what the
  Manager's journal exists to recover from, not something to trigger;
- writing commands get `--yes` only after the phrase.

All backup, identity, ordering, verification and undo rules are the
Manager's own. Output is kept in `local/chromatic/manager-jobs/`. **Only run it
with the console in front of you and plugged in.**

## Scripts and AI agents

With the viewer running (`python receiver.py serve`), the Chromatic preset
selected and the PC link connected:

```text
python -m chromatic.cli mode usb-sim          # or auto-sim, lan-sim, lan-sim-fpga, configured
python -m chromatic.cli pair-usb              # prints the code; press A on the console
python -m chromatic.cli wait --seconds 30     # until a CHR1 session is up (USB or Wi-Fi)
python -m chromatic.cli status
python -m chromatic.cli press DOWN            # or a chord: press A B
python -m chromatic.cli hold RIGHT --seconds 1
python -m chromatic.cli shot --png            # prints the PNG's full path, for an agent to view
python -m chromatic.cli tail --lines 20       # newest device lines (RAM log in a session)
python -m chromatic.cli ping | perf | log | bundle | report
python -m chromatic.cli console version
```

Every command prints JSON (`status` and `tail` print text unless `--json`).

## Files

- `chromatic/`: protocol, CHV1 video, read-only serial reader, USB (CHR1) and
  Wi-Fi transports, automatic choice, simulators (`simulator.py`, `usb_sim.py`),
  Manager file reader and runner, runtime, HTTP dispatch, CLI.
- `presets/chromatic/`: manifest, `adapter.py`, `panel.js`, `theme.css`,
  PROTOCOL.md, LAUNCHER-SIDE.md, this README.
- Private, ignored: `local/chromatic/` (config, pairing key, cached address,
  Manager job output) and `local/captures/chromatic-*`.
- Tests: `test_chromatic.py`, `test_chromatic_panel.js`, `test_chromatic_usb.py`
  (USB, pairing, automatic choice; it also builds the launcher's PC harness with
  gcc when a Jellymatic checkout is found, see `CHROMATIC_LAUNCHER_SOURCE`, and
  skips that part otherwise), `test_chromatic_handoff.py`.

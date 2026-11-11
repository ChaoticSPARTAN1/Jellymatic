# Using it with AI assistants

[Home](Home.md) › Using it with AI assistants

**On this page:** [Why it works well with assistants](#why-it-works-well-with-assistants) ·
[The commands](#the-commands) · [Example prompts](#example-prompts) · [Safety rules](#safety-rules)

## Why it works well with assistants

Remote Pilot has a command-line tool, `python -m chromatic.cli`, made for AI test agents such as
**Claude** (Claude Code), **Codex** or any assistant that can run commands on your PC. Every
command talks only to the Remote Pilot you started on this PC (127.0.0.1) and prints JSON, so an
assistant can press buttons, take screenshots, read logs and check results by itself. It never
opens the USB port directly and never needs your pairing key.

Start Remote Pilot first (`Start-RemotePilot.cmd`), select **ModRetro Chromatic** and press
**Connect to device**, then let the assistant work. Run commands from the Remote Pilot folder
(use `local\venv\Scripts\python.exe` if `python` is not on your PATH).

## The commands

```text
python -m chromatic.cli status                  # what is connected, launcher version, phase
python -m chromatic.cli mode lan-sim            # simulators: lan-sim, lan-sim-fpga, usb-sim, auto-sim, serial-sim
python -m chromatic.cli mode configured         # back to your real console
python -m chromatic.cli wait --seconds 30       # wait until a session is up
python -m chromatic.cli press DOWN              # one short press
python -m chromatic.cli press A B               # a chord
python -m chromatic.cli hold RIGHT --seconds 1.5
python -m chromatic.cli shot --png              # saves a screenshot and prints its full path
python -m chromatic.cli tail --lines 20         # newest console log lines
python -m chromatic.cli ping | perf | log | bundle | report
python -m chromatic.cli console version         # read-only console commands
```

`python -m chromatic.cli --help` lists everything. Screenshots are saved in `local\captures\`; an
assistant that can read images can open the printed path to see the screen.

## Example prompts

Use these as a starting point. Replace the parts in angle brackets.

**Claude Code / Codex: a quick smoke test with the simulator**

> Remote Pilot is running on this PC at http://127.0.0.1:38764/. In the Remote Pilot folder, use
> `python -m chromatic.cli` only. Switch to `mode lan-sim`, wait for the session, take a PNG
> screenshot and look at it, press DOWN twice and A once, take another screenshot, and tell me what
> changed on screen. Do not use any other way to reach the device.

**Testing a menu on a real console**

> My Chromatic is connected to Remote Pilot (mode configured). Using only `python -m chromatic.cli`,
> navigate the start-up menu: screenshot, press DOWN, screenshot, until you have seen every item.
> Report each item's name and whether the highlight moved correctly. Do not press A on
> <the item you must not open>.

**Collecting a bug report**

> Reproduce <the problem> with `python -m chromatic.cli`, then run `bundle` and `report` and give me
> the saved file names. Summarise the last 20 log lines. Do not paste file contents that show
> addresses or keys.

**Any other assistant (chat-only)**

> I will run commands for you and paste the JSON back. Plan a short test of the Chromatic start-up
> menu using `python -m chromatic.cli press`, `shot --png` and `tail`, one command at a time.

## Safety rules

- **Never paste pairing keys** (`local\chromatic\pairing.json`), `config.json`, Wi-Fi names or
  passwords, server addresses or tokens into a chat with any assistant, and do not ask an assistant
  to read those files.
- Keep the assistant on `chromatic.cli`. It does not need, and should not get, direct access to the
  USB port, `local\`, or your network.
- **Firmware changes** (`manager-run install`, `remove`, `restore-stock`) start Chromatic Manager,
  which writes the console's flash. Only allow them when you are there with the console plugged in;
  they also need the exact typed phrase, and they stay switched off until you add
  `"managerRunAllowed": true` to `local\chromatic\config.json` yourself. Leave that setting out
  (or set it to `false`) while an assistant is working, and tell the assistant it may not run
  `manager-run` at all unless you say so in that message.
- Screenshots and logs can show private details; check them before sharing them anywhere.
- Remote Pilot is a local tool: never let an assistant expose it to the internet (see
  [Security model](Security-model.md)).

Next: [Security model](Security-model.md)

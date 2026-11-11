# Device-side work for Remote Pilot (spec only)

> **Launcher 1.1.2** (and newer) covers items 1 to 3
> below:
> - `PJ-DIAG` lines;
> - one CHR1 handler over USB serial and Wi-Fi TCP 38771;
> - keys from a provisioned record (Chromatic Manager) or USB pairing approved by
>   an A press on the console.
>
> Its spec is the remote-control notes in the Jellymatic project. Remote Pilot
> implements the PC side (`chromatic/usb_transport.py`, `chromatic/auto_transport.py`)
> and tests it against that launcher's PC harness. Items 4 and 5 are unchanged.
> The table below describes launcher 1.1.0, before CHR1.

This is a request to the launcher project
(`chromatic/launcher` in [Jellymatic](https://github.com/ChaoticSPARTAN1/Jellymatic)) and, for
games, to a future FPGA project. Nothing here has been implemented there, and
Remote Pilot's work did not touch that project. Launcher items keep its
existing rules: it runs only between true power-on and the hand-over, every way
out lands on stock, it opens no NVS, and it sends the FPGA only the messages it
already sends. Items that would break a rule say so and are marked as
decisions for the user.

| Feature | Today (launcher 1.1.0) | Launcher work only | Also needs an FPGA update |
| --- | --- | --- | --- |
| Start marker, version, hand-over (USB serial) | Works, read only | Unchanged | — |
| Log retrieval | Two serial markers | Item 1 lines, CHR1 `log` | — |
| Wi-Fi link, pairing | Scans only | Items 2–3 | — |
| Launcher screenshots and live video | No | CHR1 `screen`, `video` | — |
| Launcher buttons: press, chord, hold | No | CHR1 `input`, `hold` | — |
| Timing, read-only console | No | CHR1 `perf`, `console` | — |
| Game screenshots and live video | No | No | Item 4 |
| Game sound | No | No | Item 4 |
| Game buttons | No | No | Item 4 |
| Firmware patching | PC Chromatic Manager (USB) | Item 5 design: launcher slot only | — |

## 1. Serial lines (small, safe, useful now)

Remote Pilot already reads these if the launcher prints them. Plain text, one
line each, no unit data (no MAC, serial, chip ID, SSID or key):

```text
PJ-LAUNCHER-READY 1.1.0          (exists)
PJ-LAUNCHER-SCREEN menu|media|other-firmware
PJ-LAUNCHER-SELECT games|jellyfin|other-firmware
PJ-LAUNCHER-COUNTDOWN stopped
PJ-LAUNCHER-WIFI scan-ok <count>|scan-failed|joined|join-failed
PJ-LAUNCHER-PC waiting|session|denied|closed
PJ-LAUNCHER-TO-STOCK             (exists)
```

## 2. CHR1 listener in the launcher

Implement the device side of [PROTOCOL.md](PROTOCOL.md) with the launcher
capabilities `status,log,screen,video,input,hold,perf,console`. The host
simulator (`chromatic/simulator.py`) is the executable reference and
`test_chromatic.py` checks the exact wire behaviour.

- A FreeRTOS task, started only after Wi-Fi joins, listening on TCP 38771 for
  one RFC 1918 peer on the same subnet. Stop listening and close before
  `esp_restart()` on every hand-over path.
- `status`/`perf`: from `pj_launcher` state and the main loop's own timing.
- `log`: a small RAM ring (for example 8 KiB) of the lines printed to the UART.
- `screen`/`video`: copy `frame[]` under the same lock the redraw uses, so a
  half-drawn frame is never sent. Keep the last frame sent per session for CHV1
  deltas (46 KB of RAM; drop to keyframes only if RAM is short). Add no QSPI
  traffic.
- `input`/`hold`: feed synthetic button values into `pj_launch_buttons()`,
  through the same logic as physical buttons; physical buttons win. `hold`
  expires 600 ms after the last refresh. `MENU` and Games still hand over;
  reply `handover` instead of `queued`, then close and restart.
- `console`: the fixed read-only commands only; no command changes anything.
- HMAC-SHA256 from ESP-IDF's mbedTLS (the project already depends on it through
  `esp_tls` in `esp32_media`); nonces from `esp_fill_random()`.
- Watchdog: the CHR1 task must not block the main loop, which keeps feeding
  the RTC watchdog as now. A hung CHR1 task must not prevent the 9 s fallback.

### Session lease

The 5 s countdown would end a remote session before it starts. Proposed: an
authenticated session pauses the countdown, for at most 10 minutes per
power-on. The 2-minute untouched timeout applies when no session is open.

## 3. Provisioning: Wi-Fi credentials and the pairing key

The launcher deliberately has no NVS mount and no flash write, so storing a
Wi-Fi password and a pairing key is a **decision for the user** and the
launcher session. Options, safest first:

| Option | How | Trade-off |
| --- | --- | --- |
| A. RAM only, per power-on | A "PC pairing" screen generates a key and prints `PJ-PAIR <64 hex>` once on USB serial; Remote Pilot's armed pairing capture stores it in `local/chromatic/pairing.json`. Wi-Fi credentials would need a PC→console path too | Nothing stored on the console. Needs the cable every power-on and a serial write path for Wi-Fi credentials |
| B. Launcher-owned flash sector | A small region inside the launcher's own slot, written only from an explicit on-console "Pair with PC" action, erased by "Forget PC" | Persistent and cable-free. Adds a flash write to the launcher (today it has none); the Manager must keep or erase it knowingly on install/remove/restore |
| C. Read stock's NVS Wi-Fi settings | Reuse the network stock joined | Needs an NVS mount, which the launcher cannot do by design. **Not recommended** |

Whatever is chosen: the key is 32 random bytes, never derived from a short
code; `PJ-PAIR`/`PJ-PROVISION`/`PJ-WIFI` lines are never logged by Remote
Pilot; "Forget PC" is available on the console.

## 4. Game access: what an FPGA update would need

Games run on the stock FPGA; the ESP32 never sees the game picture, sound or
buttons, and after hand-over it runs stock firmware, so the launcher's CHR1
listener is gone. Game features therefore need all of:

1. **FPGA RTL** (Gowin toolchain, not installed) that, on request, copies the
   160×144 game framebuffer to the MCU over the existing QSPI/UART bus or a
   spare channel, taps the audio output (mono PCM16 at 16,384 Hz is enough),
   and ORs remote button bits into the joypad input with a hardware timeout so
   a stalled MCU releases them. Physical buttons must always win.
2. **ESP32 firmware that stays on the network during games.** Stock firmware
   cannot be changed in this project, so this means either a supported hook in
   a future official firmware, or a resident program, which conflicts with the
   dual-boot rule that games run on unmodified stock. **Decision for the user.**
3. The same CHR1 session advertising `game-screen,game-video,game-input,game-audio`
   with `status.target = "game"`. Remote Pilot needs no change for this: the
   simulator's `lan-sim-fpga` mode already exercises it end to end.

Changing the FPGA image also affects recovery: today ROM download mode is
reached through the stock FPGA, which is why the dual-boot work leaves it
alone. Any FPGA change needs its own recovery plan before it is attempted.

## 5. Over-the-air launcher update (design only, not built)

Today every flash write goes through the Chromatic Manager over USB, and Remote
Pilot only starts it (typed confirmation, this PC only, no cancel). A future
wireless path may replace **only the launcher slot**:

- **Scope.** Writes only inside the launcher partition, `0x260000`–`0x328000`.
  Never the bootloader (`0x1000`–`0x8000`), the partition table
  (`0x8000`–`0x9000`), settings (NVS, PHY), the stock firmware slot, the
  unknown preserved data at `0x100000`–`0x255000`, or the FPGA. The writer
  checks every address against that single range before erasing.
- **Who writes.** The running launcher, from a new "Receive update" screen the
  user opens on the console. Never from a remote command alone: the console
  shows the incoming version and size and waits for a physical A press.
- **Transfer.** CHR1 actions `update-begin <size> <sha256>`,
  `update-chunk <offset>` (≤ 4 KiB, MAC-protected like every response),
  `update-end`. The image must be an ESP-IDF app image whose header, chip type
  and size fit the slot, and whose SHA-256 matches; optionally also a
  signature from the user's own release key.
- **Why it is safe enough.** The bootloader picks the launcher only on a true
  power-on and only if its image validates; otherwise it boots stock. So a
  half-written or corrupt launcher slot means "no menu, stock boots", never an
  unbootable console. The hard part is that the launcher runs from that same
  slot and cannot overwrite the code it is executing. Options: (a) split the slot into two halves and write
  the inactive half, with the bootloader choosing the newer valid half (needs
  a bootloader change, which is the riskiest write in the project); or (b) a
  tiny RAM-resident writer that finishes the job and restarts, accepting that a
  power cut mid-write leaves "no menu, stock boots" until the Manager repairs
  it. (b) keeps the bootloader unchanged and is recommended.
- **After writing.** Restart into stock (not power-on), tell the user to power
  cycle; the new launcher must print `PJ-LAUNCHER-READY <new version>`.
  Remote Pilot then shows the version from status.
- **Manager interaction.** The Manager's identity and backup checks still
  apply to USB writes; it must treat a launcher slot with a newer launcher as
  "installed" and keep its undo path working.

## 6. Acceptance on hardware (only with the user present)

1. With launcher 1.1.0 installed, Remote Pilot's serial mode (configured by
   the user) shows the start marker and the hand-over. Check that opening the
   port does not reset the console; if it does, note the driver and keep serial
   mode off while games run.
2. After items 1–3 are built and installed through the supervised Manager
   path: discovery, handshake, wrong-key DENY, status, log, one screenshot
   matching the console, live video at 5 fps, one DOWN press, a held RIGHT
   released by the 600 ms lease when the PC is unplugged from the network,
   MENU handing over, the link closing at hand-over, and the 9 s watchdog
   fallback still working with a PC connected.

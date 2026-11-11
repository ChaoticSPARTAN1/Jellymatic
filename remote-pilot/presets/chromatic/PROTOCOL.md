# CHR1: Remote Pilot ↔ Chromatic protocol

Status: **specification, PC side and host simulator; launcher 1.1.2 and newer
implement it.** Launchers older than 1.1.2 print two serial lines and speak no
CHR1. Launcher 1.1.2 runs one
CHR1 handler over USB serial and over Wi-Fi TCP 38771; its spec is
the remote-control notes in the Jellymatic project, and
its device code is `chromatic/launcher/shared/remote.h` and `chr1.h` there.
Reference code here: `chromatic/protocol.py` (both sides), `chromatic/video.py`
(CHV1 frames), `chromatic/lan_transport.py` and `chromatic/usb_transport.py`
(PC), `chromatic/simulator.py` and `chromatic/usb_sim.py` (device side, used by
the tests). `test_chromatic_usb.py` also runs the PC side against the launcher's
own handler through its PC harness (`check_remote.c serve-usb` / `serve-lan`).

| Transport | Role | What it can carry |
| --- | --- | --- |
| **CHR1 over USB serial** | First choice when the port is present and allowed | Everything CHR1 carries, plus the launcher's text lines outside sessions |
| **CHR1 over Wi-Fi** | When USB is not connected | Status, logs, screenshots, live video, sound, buttons (press, chord, hold), timing, read-only console |
| USB serial log reader | Read only, any launcher | The console's text lines: start marker, hand-over marker, `PJ-DIAG`, boot banner, stock prompt |

**Choosing a transport** (`"mode": "auto"`, the default once the config allows
the serial port): USB when the configured port is present and the launcher
answers on it, otherwise Wi-Fi with the paired key. A Wi-Fi session that is
already up is kept until it ends, because the console allows one session
across both transports and a USB `CHR1 OPEN` would only get `CHR1 BUSY`.

## Who owns the screen

The Chromatic runs two very different things:

- **The launcher** (ESP32, power-on until hand-over) draws its own 160×144
  RGB565 overlay and reads button state from the FPGA. Mirroring and driving it
  need **launcher work only**.
- **Games** run on the unmodified stock FPGA, which owns the game picture,
  sound and buttons; after hand-over the ESP32 runs stock firmware. Game
  screenshots, video, sound and input need **an FPGA update** (Gowin toolchain,
  not installed) **and** device firmware that stays on the network during
  games. Until a device advertises the `game-*` capabilities, Remote Pilot
  shows those features as "Needs FPGA update". The simulator's
  `lan-sim-fpga` mode pretends such a device exists so the PC side is tested.

The same actions (`screen`, `video`, `input`, `hold`) address whatever owns the
screen; `status.target` says which (`launcher` or `game`), and the matching
capability (`screen` or `game-screen`, and so on) must have been offered.

## CHR1 over USB serial (launcher 1.1.2)

115200 8N1 through the stock FPGA's USB serial bridge, with the same port rules
as the read-only reader below (DTR and RTS de-asserted before open and never
toggled; opened only with `"serialOpenAllowed": true`). On top of the session
framing below, USB adds an opening line and pairing lines:

```
PC → console   CHR1 OPEN
console → PC   CHR1 HELLO <device_nonce> <version>   then the handshake below, on this stream
           or  CHR1 NOKEY                             no key yet: offer USB pairing
           or  CHR1 BUSY                              a Wi-Fi session holds the console
           or  CHR1 WAIT                              a handshake just failed; retry after 1 s
           or  (no CHR1 line within 1 s)              the launcher is not running

PC → console   CHR1 PAIR <pc_nonce: 32 hex>           only without a key, outside a session
console → PC   CHR1 PAIRING <device_nonce> <code>     the console shows the code; A allows, B refuses (60 s)
console → PC   PJ-PAIR <key: 64 hex>                  the owner pressed A on the console
           or  CHR1 PAIR-DENIED / CHR1 PAIR-REFUSED <why>
```

`code = uint32_be(SHA-256("CHR1 pair-code <device_nonce> <pc_nonce>")[0..4]) % 1000000`,
six digits; the PC checks it and shows the same code. Only a physical press on
the console answers. The key lives in the console's RAM until power-off; a key
provisioned by Chromatic Manager makes the console refuse pairing.

What Remote Pilot does on USB (`chromatic/usb_transport.py`):

- **Writes only CHR1 lines.** Every write passes an allow-list: `CHR1 OPEN`,
  `CHR1 PAIR <nonce>`, `CHR1 AUTH ...` and `REQ ...`. Nothing else can reach the port.
- **Writes nothing until a CHR1 launcher shows on the port**: `PJ-LAUNCHER-READY`
  with version 1.1.2 or later, or `PJ-DIAG` lines from a launcher already running
  (one `CHR1 OPEN` then). No answer means no CHR1, and the port stays quiet until
  the next launcher start or the user's Retry (one more `OPEN`). After
  `PJ-LAUNCHER-TO-STOCK`, or a `handover` reply, nothing is written: stock
  firmware never receives a byte from Remote Pilot.
- **Opens the session at the start marker**, not at the next poll: the
  launcher's countdown is about 5 s, and a session pauses it (for at most 10
  minutes per power-on). Physical buttons still work on the console.
- **Keeps session bytes out of the log.** During a session the UART carries only
  CHR1, including raw binary bodies; those bytes go to the session reader only.
  A session that ends cleanly (`bye` answered, or `handover`) returns to text at
  once; one that ends mid-reply drops bytes until the line has been quiet for
  0.3 s. `CHR1` and `PJ-PAIR` lines are answers, never log lines.
- **Speed.** 115200 baud moves about 11.5 KB/s: a raw 46,080-byte screenshot
  takes about 4 s, an RLE video keyframe of the launcher (1 to 4 KB) 0.1 to
  0.35 s. A hold whose refresh waits behind a raw screenshot is released by
  the console's 600 ms lease; that is safe, not an error.
  Launchers before 1.3.2 send only about 2.5 KB/s while the Jellyfin app is open
  (a 46 KB screenshot takes about 20 s, and the console then ends the session);
  Remote Pilot measures this, reads the log tail every 5th poll and says "USB is
  slower while Jellyfin plays: screenshots take a few seconds". 1.3.2 sends at
  the line's pace. After a session ends mid-reply, the answer to the next
  `CHR1 OPEN` is kept even though the old reply's last bytes come just before it.
- The Chromatic Manager needs the port: Remote Pilot refuses to start it while
  it holds the USB port.

## USB serial log reader (read only)

115200 8N1 through the stock FPGA's USB serial bridge.

| Line | Meaning |
| --- | --- |
| `PJ-LAUNCHER-READY <x.y.z>` | Launcher started (true power-on only) |
| `PJ-LAUNCHER-TO-STOCK` | Launcher is about to restart into stock firmware |
| `PJ-PAIR <64 hex>` | Proposed: pairing key, printed once on the launcher's pairing screen |
| `rst:0x.. (<REASON>),boot:...` | ESP32 ROM banner; Remote Pilot keeps only the reason name |
| `mcu>` | Stock firmware's console prompt |

- **Never write.** The module has no write call, no break, no flow control.
- **DTR and RTS are de-asserted before opening and never toggled.** The stock
  FPGA drives `ESP32_EN` and `ESP32_IO0` from them; the official updater and the
  Manager use exactly that to reset into download mode. Some USB-serial drivers
  pulse the lines on open anyway. A reset that is not a true power-on boots
  stock, so it cannot brick the console, but it can interrupt a game. **Not yet
  tried on hardware.**
- Opened only with `"mode": "serial"`, `"serialPort": "COMn"` and
  `"serialOpenAllowed": true` in `local/chromatic/config.json`, written by the
  user. Close it before the Manager or the official updater runs (Remote Pilot
  refuses to start the Manager while its serial mode is on).
- Lines are bounded (240 characters, 2,000 kept), stripped of control
  characters and redacted: MAC-shaped values and hex runs of 16+ digits become
  `[redacted]`. Lines starting `PJ-PAIR`, `PJ-PROVISION` or `PJ-WIFI` are never
  logged or shown. While the user has armed **pairing capture**, the next valid
  `PJ-PAIR` key is written to `local/chromatic/pairing.json` and nowhere else.

## CHR1 over the local network

### Roles, addresses, discovery

Like Remote Pilot's reverse pairing, the **console listens** and the PC
connects: TCP **38771**. The PC connects only to RFC 1918 IPv4 addresses
(loopback only for the simulator); the console accepts only RFC 1918 peers on
its own subnet. One client at a time; a second connection is closed.

`"lanHost": "auto"` (the default) scans the PC's own /24 with 32 parallel
probes of 0.4 s, each a full handshake, and caches the answering address in
ignored `local/chromatic/last-device.json`. "Search again" (the shared Retry)
rescans; a lost session triggers at most one rescan every 30 s. No broadcast.

### Pairing

A 32-byte random key. On the PC it lives only in ignored
`local/chromatic/pairing.json` as `{"key": "<64 hex>"}` and is never shown in
the viewer, logs, status or captures. Ways to get it there: pairing capture
from USB serial (above), or `python -m chromatic.cli pair --key-file <file>`.
The PC never derives a key from a short code. How the console stores it is a
launcher decision (LAUNCHER-SIDE.md, "Provisioning").

### Framing

Every message starts with one line of printable ASCII, under 256 bytes with its
`\n`. A response line may be followed by a body of the length it states.
Anything else closes the connection.

### Handshake (mutual proof of the pairing key)

```text
console → PC   CHR1 HELLO <device_nonce:32 hex> <launcher version x.y.z>
PC → console   CHR1 AUTH <pc_nonce:32 hex> <HMAC-SHA256(K, "CHR1 pc-auth <device_nonce> <pc_nonce>"):64 hex>
console → PC   CHR1 OK <HMAC-SHA256(K, "CHR1 device-auth <pc_nonce> <device_nonce>"):64 hex> <caps>
           or  CHR1 DENY            (then close)
```

`caps` is a comma list from
`status,log,screen,video,input,hold,perf,console,game-screen,game-video,game-input,game-audio`.
Nonces are fresh per connection; a PC nonce equal to the device nonce is
rejected. Session key: `S = HMAC-SHA256(K, "CHR1 session <device_nonce> <pc_nonce>")`.

### Requests and responses

```text
PC → console   REQ <seq> <action> <arg> <mac>
console → PC   RES <seq> <code> <length> <mac>\n<body: length bytes>
```

- `seq` starts at 1 and increases by exactly one per request; anything else
  (replay, skip) closes the connection.
- request `mac` = first 32 hex of `HMAC-SHA256(S, "REQ <seq> <action> <arg>")`.
- response `mac` = first 32 hex of `HMAC-SHA256(S, "RES <seq> <code> <length>\n" + body)`.
- `code` is `ok`, `refused`, `busy` or `error`; for anything but `ok` the body
  is a UTF-8 reason of at most 160 bytes.
- One request in flight. The PC gives up after 3 s with no byte of the answer, then
  closes; while bytes keep coming it waits (a body may take up to its size at
  1.5 KB/s). It never resends an `input` or `hold` whose outcome is unknown.
- The console closes a connection with no valid request for 15 s, and
  releases all remote buttons when a connection closes for any reason.

| Action | `arg` | `ok` body | Limit | Capability |
| --- | --- | --- | --- | --- |
| `status` | `-` | JSON, below | 1,024 B | `status` |
| `log` | `1`..`16384` | UTF-8 tail of the device's RAM log | `arg` bytes | `log` |
| `screen` | `-` | Current frame, 160×144 RGB565 little-endian, top row first | exactly 46,080 B | `screen` / `game-screen` |
| `video` | `key` or `delta` | One CHV1 frame (below) | 46,100 B | `video` / `game-video` |
| `input` | `<BUTTONS>:<ms>` | `queued`, or `handover` | 16 B | `input` / `game-input` |
| `hold` | `<BUTTONS>` or `NONE` | `held`, `released` or `handover` | 16 B | `hold` / `game-input` |
| `perf` | `-` | JSON timing sample | 512 B | `perf` |
| `console` | a command from the list below | UTF-8 text | 1,024 B | `console` |
| `audio` | last chunk number received | u32 chunk number + PCM | 8,196 B | `game-audio` |
| `ping` | `-` | `pong` | 16 B | always |
| `bye` | `-` | `bye`, then close | 16 B | always |

**Buttons.** `BUTTONS` is 1–4 distinct names from
`UP DOWN LEFT RIGHT A B START SELECT MENU` joined by `+` (a chord), for example
`A+B`. `MENU` is only ever a single `input`: never chorded, never held, because
on the launcher it hands over to stock. `input` presses the chord for `ms`
(20–250) and releases. `hold` replaces the held set (`NONE` releases); the
device releases by itself **600 ms** after the last `hold`, so the PC refreshes
a hold about every 200 ms and a lost PC or network can never leave a button
stuck. Physical buttons always win. Buttons go through the same logic as
physical presses; when a press hands over to stock (`MENU`, or `A`/`START` on
Games) the reply is `handover` and the console closes the link right after.

**Console** commands are read-only and fixed: `help version uptime heap wifi
phase reset-reason`. Replies contain no unit identifiers.

**Status** (all fields optional except `phase`; unknown fields ignored; no
unit identifiers, MAC addresses, SSIDs or keys):

```json
{"phase": "menu", "target": "launcher", "version": "1.1.0", "screen": "menu",
 "selected": "jellyfin", "countdownS": null, "pcSession": true, "held": "NONE", "uptimeMs": 4210}
```

`phase` is `menu`, `media`, `other-firmware`, `game` (only with game access) or
`handover`.

**Timing** (`perf`): `{"fps", "frameMs", "workMs", "timestampMs", "heapFreeKb"}`,
finite non-negative numbers; Remote Pilot draws frame and work time.

### CHV1 video frames and the bandwidth budget

A raw frame is 160 × 144 × 2 = **46,080 bytes**. At 10 fps that is 461 KB/s
(3.7 Mbit/s), about what an ESP32 can sustain over TCP while also feeding the
FPGA overlay over QSPI, so live video is compressed and pull-based:

```text
header, 20 bytes little-endian:
  "CHV1" | u32 seq | u32 base | u8 kind | u8 0 | u16 rows | u32 payload length
kind 0  raw keyframe: payload = the 46,080-byte frame
kind 1  RLE keyframe: 144 coded rows
kind 2  RLE delta against frame `base`: 18-byte changed-row bitmap, then one coded row per set bit
coded row: runs of (u8 count 1..255, u16 pixel) summing to exactly 160
```

- `seq` increases by one per frame sent on the session. The console keeps only
  the last frame it sent; `video delta` codes against it, `video key` forces a
  keyframe. The encoder falls back to RLE keyframe, then raw, whenever coding
  is not smaller, so a frame is never larger than 46,100 bytes.
- The PC asks for `delta` while its chain is intact and `key` after any
  mismatch, timeout or reconnect. Only one frame is ever in flight, so a slow
  link lowers the frame rate instead of building a queue.
- Measured on the simulator's frames: launcher menu keyframe 3.3 KB, unchanged
  frame 38 B, menu cursor move 1.5 KB; simulated game keyframe 1.4 KB, game
  delta with one moving object under 0.1 KB. Real game graphics with detailed
  backgrounds compress far less; budget for raw keyframes.
- Budget: Remote Pilot offers 1, 2, 5, 10 or 15 fps (default 5) and shows the
  measured rate. Recommended device ceiling: 10 fps for the launcher, 15 fps
  for games, and at most ~150 KB/s of video so buttons stay responsive.
- Screenshots (`screen`) stay uncompressed for exactness and are saved as
  24-bit 160×144 BMPs.

### Sound (game-audio only)

PCM16 little-endian mono at 16,384 Hz in chunks of up to 0.25 s (8,192 bytes),
prefixed by a u32 chunk number. The PC sends the last number it received and
gets the newest chunk after it, or just the number when nothing is new. Late
audio is dropped, never queued. The launcher makes no sound, so this needs the
FPGA update.

### What the PC does with it

| Remote Pilot route | CHR1 |
| --- | --- |
| `GET /api/chromatic/status` | cached `status`, `log 4096` and `perf`, polled once a second off the HTTP thread |
| `GET /api/chromatic/live.bmp` | `video` (or `screen`), decoded and returned, never saved |
| `GET /api/chromatic/live.pcm` | `audio` |
| `POST /api/chromatic/shot` | `screen` → `local/captures/chromatic-screen-*.bmp` |
| `POST /api/chromatic/bundle` | screen + log + report + `chromatic-capture-*.json` manifest |
| `POST /api/chromatic/log`, `report`, `collect` | saved under `local/captures/` |
| `POST /api/chromatic/arm` | browser-tab control lease (1.5 s), required for buttons |
| `POST /api/chromatic/input`, `hold` | `input`, `hold`; one in flight, presses 150 ms apart, holds ≤ ~10/s |
| `POST /api/chromatic/console` | `console` |
| `POST /api/chromatic/manager-run` | not CHR1: starts the Chromatic Manager on the PC (see README) |

`python -m chromatic.cli` wraps these routes for scripts and AI agents.

## Security notes

- Local network only, no internet relay, no broadcast discovery.
- A discovery probe performs a handshake with every host that answers on
  38771. A foreign host learns only an HMAC over nonces; it cannot derive the
  session key, so it cannot read or alter a session.
- Wrong key: `CHR1 DENY`. The console should allow at most one failed
  handshake per second.
- Integrity and authentication, not confidentiality. The payloads are screen
  images, logs and timing with no secrets by design; add encryption before
  carrying anything sensitive.
- Nothing in CHR1 writes flash, changes settings or starts a firmware image.
  Firmware changes go only through the Chromatic Manager on the PC, or, as a
  future design, the launcher-slot-only update in LAUNCHER-SIDE.md.

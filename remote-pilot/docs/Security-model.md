# Security model

[Home](Home.md) › Security model

**On this page:** [This PC only, by default](#this-pc-only-by-default) ·
[Other web sites cannot use it](#other-web-sites-cannot-use-it) ·
[Phones and tablets (optional)](#phones-and-tablets-optional) · [The console link](#the-console-link) ·
[Firmware changes](#firmware-changes) · [Files and secrets](#files-and-secrets) ·
[Never expose it to the internet](#never-expose-it-to-the-internet)

## This PC only, by default

The receiver listens on **127.0.0.1** (loopback), so only programs on this PC can reach the viewer.
Nothing listens on your network unless you start it with `--viewer-lan`.

## Other web sites cannot use it

A web page you visit could try to send requests to `127.0.0.1`. Remote Pilot refuses them:

- the `Host` must be a numeric address with the viewer's port (this blocks "DNS rebinding" tricks),
  and `localhost` names are refused;
- a request from another site (a different `Origin`) is refused;
- every request that changes something must carry the header `X-Remote-Pilot-Viewer: 1`. Browsers
  will not add that header for another site without asking the server first (a CORS
  "preflight"), and Remote Pilot never answers that question, so the request never goes out;
- request bodies are limited to 512 bytes, and pages are served with a strict Content Security
  Policy, `X-Frame-Options: DENY` and `nosniff`.

## Phones and tablets (optional)

`Start-RemotePilot.cmd --viewer-lan` also lets devices on your **private** home network open the
viewer. Then:

- the receiver prints a **one-time 6-digit code** in its own window on this PC; a browser must
  enter it once and then gets a 12-hour cookie (HttpOnly, SameSite=Strict);
- each code works **once**: after a phone or tablet pairs, a new code is printed for the next one;
- a code **expires after 10 minutes**; a new one is printed automatically;
- wrong codes are limited to 5 tries a minute per address, and to **20 wrong codes in total**: then
  the code is replaced, a new one is printed on this PC, and pairing pauses for a minute;
- only private addresses (10.x, 172.16-31.x, 192.168.x) are accepted, and the receiver keeps at most
  64 signed-in browsers (the oldest is signed out first);
- firmware patching, **Allow USB**, the USB hand-off with Chromatic Manager, choosing the
  connection (`mode`), pairing (Pair over USB, pairing capture) and the Manager's job output and
  files work only from this PC's own browser.

**The LAN viewer is plain HTTP, not encrypted.** Anyone on the same network can read what it
sends, including the sign-in cookie. Use it only on a home network you trust, never on shared,
public or guest Wi-Fi.

## The console link

- **Pairing** needs a physical **A** press on the console after both screens show the same code,
  or a key provisioned by Chromatic Manager.
- Each USB or Wi-Fi session starts with a fresh challenge proved with the paired key (HMAC); the
  key itself is never sent.
- Wi-Fi only talks to private addresses. Requests and replies have fixed size limits; a timed-out
  button press is never resent.
- The USB port is only listed, never opened, until you click **Allow USB on COMx** in this PC's
  browser. If Chromatic Manager holds the port for a job, Remote Pilot keeps it closed, even after a
  restart, until the job is finished.

## Firmware changes

Remote Pilot never writes the console's flash. The **Firmware patching** section (and
`chromatic.cli manager-run`) only **starts Chromatic Manager** as a separate program, with an
allow-listed command (`status`, `backup`, `install`, `remove`, `restore-stock`), and only:

- when you switched it on yourself with `"managerRunAllowed": true` in
  `local\chromatic\config.json` (it is **off** until you add that line);
- from this PC's own browser (127.0.0.1), never the LAN viewer;
- after the exact confirmation phrase is typed (for example `INSTALL CHROMATIC LAUNCHER`);
- one job at a time, with no cancel (the Manager's own journal handles interrupted jobs);
- using the Manager folder and Python **you** set in `local\chromatic\config.json`
  (`managerProject`, `managerPython`). Remote Pilot never guesses them and the viewer cannot change
  them. Anyone who can edit that file can choose what program runs, so keep your user account
  private.

## Files and secrets

- The pairing key, settings, captures and logs live in `local\` inside the Remote Pilot folder.
  Git ignores it. Never commit, upload or paste it.
- **Keep the Remote Pilot folder out of OneDrive, Dropbox, Google Drive and other cloud-synced
  folders** (on many PCs Desktop and Documents are synced). `local\chromatic\pairing.json` holds
  the key that lets a PC control your console, and a synced folder copies it to the cloud.
- When pairing capture replaces a key, the old one is kept beside it as
  `pairing-backup-<date-time>.json`. Delete backups you no longer need.
- Status, log lines and the page never show the pairing key. The privacy eye (on by default) hides
  addresses, network names and codes on screen.
- Capture files are served only by bare file name from `local\captures\` (no folders, no `..`,
  only `.bmp`, `.txt` and `.json`).
- Preset adapters (`presets/<id>/adapter.py`) are Python code that runs inside the receiver:
  **only add presets you trust**.

## Never expose it to the internet

Do not forward port 38764 on your router, do not put Remote Pilot behind a public reverse proxy or
tunnel, and do not run `--viewer-lan` on public Wi-Fi. A proxy on the same PC would make every
request look like it came from this PC and skip the pairing code.

To report a security problem, see [SECURITY.md](../SECURITY.md).

Next: [Troubleshooting](Troubleshooting.md)

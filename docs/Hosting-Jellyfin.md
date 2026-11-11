<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Hosting Jellyfin for wireless use

[Home](Home.md) › Hosting Jellyfin

**Contents:** [What the console needs](#what-the-console-needs) · [Steps](#steps) ·
[A separate sign-in per device](#a-separate-sign-in-per-device-recommended) · [Checks](#checks) · [Safety](#safety)

## What the console needs

- A Jellyfin server on the **same local network** as the console's Wi-Fi.
- An address the console can reach, for example `http://your-server:8096` (8096 is Jellyfin's usual port).
- **Quick Connect** turned on, so the console can get its own sign-in without your password.
- The server's FFmpeg (Jellyfin's own) able to transcode: the console asks for small MJPEG video with
  ADPCM sound, which the server makes on the fly.

## Steps

1. Install Jellyfin on a PC, NAS or small computer that stays on: see
   [jellyfin.org/downloads](https://jellyfin.org/downloads/). Add your libraries.
2. Give the server a **fixed address** on your network (in your router's DHCP settings, "address
   reservation"), so `http://your-server:8096` keeps working.
3. Allow Jellyfin through the server's firewall for your **private** network (port 8096, TCP), and UDP 7359
   if you want the console's **Find servers nearby** to work.
4. In Jellyfin: **Dashboard › General › Quick Connect: Enable**. Save.
5. Check that transcoding works: **Dashboard › Playback › Transcoding** (the default settings are fine).
6. On the console: **Jellyfin › Select › Jellyfin server › Find servers nearby**, or enter the address.

## A separate sign-in per device (recommended)

Use **Quick Connect** for the console (and for Chromatic Manager, when you give it Jellyfin settings).
Each gets its **own** sign-in, listed in **Dashboard › Devices**, that you can revoke on its own without
changing your password or signing out your TV or phone. Jellymatic never signs out other devices.

You can also make a separate Jellyfin **user** for the console with access to only some libraries
(Dashboard › Users › Add user, then sign in on the console as that user).

## Checks

| Problem | Try |
|---|---|
| "Find servers nearby" finds nothing | Enter the address by hand; allow UDP 7359; console and server on the same network |
| Quick Connect code is refused | Turn Quick Connect on (Dashboard › General); the code expires after a few minutes |
| Plays, but stutters | Move closer to the router; the console adjusts quality by itself |
| No sound | Install the sound image ([Install](Install-with-Chromatic-Manager.md#jellyfin-sound-fpga-1840)) |

## Safety

- Keep Jellyfin on your **home network**. Jellymatic does not need it to be reachable from the internet.
- If you do expose it, use HTTPS and strong passwords; the console supports `https://` addresses.
- Never post your server address, user names or tokens in issues or screenshots.

Next: [Pocket link](Pocket-link.md)

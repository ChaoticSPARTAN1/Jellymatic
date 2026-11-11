<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Security

## Reporting a vulnerability

Please report security problems **privately**, not in a public issue: use GitHub's
**Security › Report a vulnerability** (private vulnerability reporting) on this repository. Describe the
problem, how to reproduce it, and what it affects (console, PC tool, Remote Pilot, Jellyfin sign-in).
There is no guarantee of a response time; reports are handled as time allows.

## Never post publicly

- Wi-Fi network names and passwords
- Jellyfin server addresses, user names, user IDs, tokens, Quick Connect codes, device IDs
- Remote Pilot pairing keys, the Manager's local API token or discovery file
- Console backups, flash dumps, serial numbers, MAC addresses
- Logs or screenshots that show any of the above (turn on the Manager's eye button before a screenshot)

## What Jellymatic does to protect you

- Your Wi-Fi and Jellyfin details stay in `%LOCALAPPDATA%\Jellymatic` on your PC and on your console;
  nothing is uploaded.
- The console gets its own revocable Jellyfin sign-in (Quick Connect); your password never goes to it, and
  Jellymatic signs out only that console's own sign-in.
- Chromatic Manager's API listens on 127.0.0.1 only, with a random token per run; every write needs your
  Approve in the window.
- Remote Pilot works on private networks only and never shows password fields while connected.

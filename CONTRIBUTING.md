<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Contributing to Jellymatic

Pull requests, bug fixes and bug reports are welcome. This is a hobby project: there is no guarantee of a
response or a fix, but every report is read when time allows.

## Reporting a bug

Open an issue with:

1. What you did, what you expected, what happened.
2. Versions: start-up menu (shown on its credits screen and in Chromatic Manager), Chromatic Manager
   commit, FPGA image (official, 18.40 or 18.41), Windows version.
3. The Manager's result headline and, if useful, the relevant log lines **after removing** every address,
   network name, user name, token and device ID. Turn on the Manager's eye button (private details hidden)
   before taking screenshots.

Never post Wi-Fi details, server addresses, Jellyfin tokens, backups or full logs. For security problems
use [SECURITY.md](SECURITY.md) instead of an issue.

## Pull requests

- Keep the safety rules: no change may write the official firmware slot, settings or cartridges; every
  write goes through Chromatic Manager's journal, backup, read-back and Approve.
- Run the tests that cover your change ([docs/Building-from-source.md](docs/Building-from-source.md#tests))
  and say which ones you ran. The tests must never open a device.
- Plain words in anything a user reads. Keep the pixel style of the console and the Manager.
- Do not add files you may not share (firmware dumps, ModRetro's or Gowin's files, licences, real media).
- By contributing you agree that your contribution is licensed under GPL-3.0-or-later with the additional
  attribution term in [ADDITIONAL-TERMS.md](ADDITIONAL-TERMS.md).

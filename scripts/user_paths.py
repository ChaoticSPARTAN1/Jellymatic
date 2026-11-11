# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Where Jellymatic keeps your own files (never inside the program folder, never in Git).

Everything personal lives in one per-user folder, created on first use:

  Windows:  %LOCALAPPDATA%\\Jellymatic
  macOS / Linux:  $XDG_DATA_HOME/jellymatic  (usually ~/.local/share/jellymatic)

Set JELLYMATIC_HOME to use another folder. In it:

  chromatic-wifi-networks.txt     your Wi-Fi networks for the console (you write it; optional)
  jellyfin-connection.txt         this PC's Jellyfin sign-in (optional; see jellyfin-connection.example.txt)
  chromatic-manager/              journals of jobs (each step is written down before it happens)
  chromatic-backup-<UTC time>/    the full backup of both chips (taken once, before the first write)
  chromatic-manager-backups/      backups of the console's ESP32 flash, taken before every write
  chromatic-manager-gui/          window settings, logs, the local API discovery file
  chromatic-tools/                the full backup record (latest-backup-path.txt) and AI usage reading
  tools/                          programs you install for Jellymatic (openFPGALoader, ESP-IDF tools)

A second copy of every backup goes to SECOND_COPY (default: a Jellymatic-Backups folder in your
home folder, so usually on the same disk). For real safety put it on another disk or a USB stick
with JELLYMATIC_SECOND_COPY.

TOOLS is where helper programs are looked for (JELLYMATIC_TOOLS, default: the tools folder above).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _default_home():
    if sys.platform == 'win32':
        base = os.environ.get('LOCALAPPDATA') or str(Path.home() / 'AppData' / 'Local')
        return Path(base) / 'Jellymatic'
    base = os.environ.get('XDG_DATA_HOME') or str(Path.home() / '.local' / 'share')
    return Path(base) / 'jellymatic'


PRIVATE = Path(os.environ.get('JELLYMATIC_HOME') or _default_home())
SECOND_COPY = Path(os.environ.get('JELLYMATIC_SECOND_COPY') or (Path.home() / 'Jellymatic-Backups'))
TOOLS = Path(os.environ.get('JELLYMATIC_TOOLS') or (PRIVATE / 'tools'))


def ensure():
    """Create the per-user folder (empty) if it is not there yet. Returns it."""
    PRIVATE.mkdir(parents=True, exist_ok=True)
    return PRIVATE


try:  # created on first use, so every tool can write into it
    ensure()
except OSError:
    pass


if __name__ == '__main__':
    print(f'Your Jellymatic folder:  {PRIVATE}')
    print(f'Second backup copy:      {SECOND_COPY}')
    print(f'Helper programs:         {TOOLS}')

# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""ModRetro Chromatic support for Remote Pilot.

Everything Chromatic-specific lives in this package and in presets/chromatic/.
The shared receiver only routes /api/chromatic/* here (see api.py).

Modules:
  protocol          CHR1 wire protocol (paired, local LAN, bounded requests)
  screen            160x144 RGB565 launcher frame -> 24-bit BMP
  serial_transport  phase 1 read-only USB-serial line reader
  lan_transport     CHR1 client (needs launcher work before it can reach a console)
  simulator         host-side simulated serial device and CHR1 launcher endpoint
  manager_status    read-only, file-only view of the Chromatic Manager's state
  runtime           the preset adapter: config, transports, feature availability
  api               validated HTTP action dispatch used by receiver.py
  cli               command line client for a running viewer (for people and AI)
"""

PRESET_ID = "chromatic"

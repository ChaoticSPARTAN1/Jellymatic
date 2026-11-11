# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Offline gate for console writes that an AI assistant starts: the assistant's usage reading must be
fresh and leave a reserve (so it does not run out half-way through a recovery), and the full backup
must validate in both copies.

This does not open hardware or grant approval; the Manager still asks the person for every write.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from user_paths import PRIVATE  # noqa: E402  (per-user folder, see user_paths.py)

from connection_config import ROOT

MAX_START_USAGE_PERCENT = 75  # default reserve: no new console write at 75% used or more
# Assistants that report weekly usage as percent used with a smaller reserve: no new write once
# only 10% is left, so enough remains to recover.
MAX_START_USAGE_BY_REPORTER = {'claude': 90}
SNAPSHOT_MAX_AGE_SECONDS = 300


def check_usage(snapshot, now=None):
    now = now or datetime.now(timezone.utc)
    observed = datetime.fromisoformat(snapshot['observed_at'])
    if observed.tzinfo is None or not 0 <= (now - observed).total_seconds() <= SNAPSHOT_MAX_AGE_SECONDS:
        raise ValueError('Usage reading is stale; refresh it before a hardware experiment.')
    windows = snapshot.get('used_percent_by_window')
    if not isinstance(windows, list) or not windows:
        raise ValueError('Usage headroom is unknown; keep working in software.')
    for used in windows:
        if type(used) not in (int, float) or not 0 <= used <= 100:
            raise ValueError('Usage headroom is unknown; keep working in software.')
        if used >= MAX_START_USAGE_BY_REPORTER.get(snapshot.get('reporter'), MAX_START_USAGE_PERCENT):
            raise ValueError('Recovery reserve reached; do not start a hardware experiment.')
    if snapshot.get('ordinary_usage_allowed') is not True:
        raise ValueError('Usage is unavailable; do not start a hardware experiment.')


# recovery-validation.json, written into the full backup's folder by Chromatic Manager's full backup
# (chromatic_manager.Manager.full_backup) once both chips were read twice and saved: the non-writing
# recovery check made right after the reads. The FPGA was reconfigured from its own, untouched flash
# and the console's USB serial interface (made by the FPGA) came back, the GWU2X JTAG bridge that
# every console chip restore uses is still there, and no flash erase or program was requested.
# A record with extra keys (an older manual check also noted video and audio) is accepted.
CHECKPOINT_KEYS = ('serial_returned', 'jtag_remains_available', 'no_flash_program_requested')


def check_checkpoint(checkpoint):
    if not all(checkpoint.get(key) is True for key in CHECKPOINT_KEYS):
        raise ValueError('Stock checkpoint is incomplete; resolve recovery first.')


def check_backup_copies(folder, secondary):
    from verify_chromatic_backup import validate_reads
    validate_reads(folder)
    bundle = folder / 'restore-bundle'
    files = list(bundle.iterdir())
    if len(files) != 3 or not all(p.is_file() for p in files):
        raise ValueError('Original three-file restore bundle is incomplete.')
    for path in files:
        expected = hashlib.sha256(path.read_bytes()).digest()
        if hashlib.sha256((secondary / path.name).read_bytes()).digest() != expected:
            raise ValueError('Secondary backup differs; resolve backup integrity first.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--usage-snapshot', type=Path,
        default=PRIVATE / 'chromatic-tools/usage-snapshot.json')
    args = parser.parse_args()
    check_usage(json.loads(args.usage_snapshot.read_text(encoding='utf-8')))
    folder = Path((PRIVATE / 'chromatic-tools/latest-backup-path.txt').read_text(encoding='utf-8').strip())
    check_checkpoint(json.loads((folder / 'recovery-validation.json').read_text(encoding='utf-8')))
    receipt = json.loads((folder / 'completion-receipt.json').read_text(encoding='utf-8'))
    check_backup_copies(folder, Path(receipt['secondary_bundle']))
    print('PASS: fresh usage has recovery reserve; both saved stock backup copies validate.')
    print('Planning gate only. Fresh device identity, candidate checks and a bounded stock-restoring runner are still required.')
    print('No hardware was opened or changed.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('STOP: hardware preflight incomplete. Leave stock firmware in place and continue software work.')
        raise SystemExit(1)

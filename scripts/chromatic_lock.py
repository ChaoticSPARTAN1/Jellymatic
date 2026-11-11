# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""One Chromatic tool at a time, across Chromatic Manager and the older trial runner.

An operating-system lock on an open file: it is released when the process ends, however
it ends, so there is never a stale lock to clear by hand and a reused process ID can
never keep it held.
"""
from __future__ import annotations

import os
from pathlib import Path
from user_paths import PRIVATE  # noqa: E402  (per-user folder, see user_paths.py)

ROOT = Path(__file__).resolve().parents[1]
LOCK = PRIVATE / 'chromatic-device.lock'


class Busy(Exception):
    """Another Chromatic tool holds the lock."""


class DeviceLock:
    def __init__(self, path=LOCK):
        self.path, self.handle = Path(path), None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise Busy('Another Chromatic tool is running. Close it first.')
        self.handle = handle
        return self

    def __exit__(self, *exc):
        if self.handle is not None:
            try:
                if os.name == 'nt':
                    import msvcrt
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self.handle.close()
            self.handle = None

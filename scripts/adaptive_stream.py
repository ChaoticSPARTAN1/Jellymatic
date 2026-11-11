# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Adaptive picture quality for network streams (Wi-Fi to the Chromatic, link to the Pocket).

The encoder makes every picture at up to three JPEG qualities in one pass (see
`ladder()` and `stream_command(..., ladder=...)`), so the gateway can change quality
on any picture: no restart, no new session, no gap. The lowest level also sends every
other picture. Audio, frame size and the title's cadence never change, so a slow
network softens the picture instead of stopping playback.

The signal needs nothing from the client: the gateway compares how fast the client
takes audio (the media clock) with real time. A client that keeps up takes at least
real time, whether it is filling its buffer or pacing a full one; one that falls below
that is limited by the network. Gaps (pause, seek, loading) reset the measurement.
Moving back up is a probe that backs off if it fails, so quality does not oscillate.
"""
from collections import deque
import threading
import time

PTS_RATE = 90000
WINDOW = 4.0       # seconds of deliveries measured
MIN_SPAN = 2.0     # a decision needs at least this much
GAP = 1.5          # a pause between deliveries longer than this restarts measurement
DOWN_BELOW = 0.90  # media seconds per real second
UP_FROM = 0.97
UP_WAIT = 10.0     # first probe back up after this long at a level
UP_WAIT_MAX = 120.0


def ladder(quality):
    """FFmpeg MJPEG qscale per level, best first: the profile's own, then softer."""
    steps = [quality]
    for factor in (2, 4):
        step = min(31, quality * factor)
        if step > steps[-1]:
            steps.append(step)
    return tuple(steps)


class Adaptation:
    """Thread-safe level chooser. Level 0 is the profile's own quality."""

    def __init__(self, qualities, start=0, clock=time.monotonic):
        if not qualities:
            raise ValueError('Empty quality ladder')
        self.qualities = tuple(qualities)
        self.levels = len(self.qualities) + 1  # the last level also halves the pictures
        self.clock = clock
        self.lock = threading.Lock()
        self.level = max(0, min(int(start), self.levels - 1))
        self.samples = deque()
        self.changed = clock()
        self.up_wait = UP_WAIT
        self.last_up = None
        self.changes = 0

    def rank(self):
        """Which encoded copy (0 = best) carries pictures at this level."""
        return min(self.level, len(self.qualities) - 1)

    def keep(self, frame_index):
        return self.level < self.levels - 1 or frame_index % 2 == 0

    def delivered(self, pts):
        """Called when the client takes an audio packet with this timestamp."""
        with self.lock:
            now = self.clock()
            if self.samples and (now - self.samples[-1][0] > GAP or pts < self.samples[-1][1]):
                self.samples.clear()  # paused, seeking or a new session
            self.samples.append((now, pts))
            while now - self.samples[0][0] > WINDOW:
                self.samples.popleft()
            span = now - self.samples[0][0]
            if span < MIN_SPAN:
                return self.level
            rate = (pts - self.samples[0][1]) / PTS_RATE / span
            if rate < DOWN_BELOW and now - self.changed >= MIN_SPAN and self.level < self.levels - 1:
                # A failed probe up waits longer before the next one.
                if self.last_up is not None and now - self.last_up < 2 * UP_WAIT:
                    self.up_wait = min(UP_WAIT_MAX, self.up_wait * 2)
                self._set(self.level + 1, now)
            elif rate >= UP_FROM and self.level > 0 and now - self.changed >= self.up_wait:
                self.last_up = now
                self._set(self.level - 1, now)
            elif rate >= UP_FROM and self.level == 0 and now - self.changed >= UP_WAIT_MAX:
                self.up_wait = UP_WAIT  # long stable spell: forget past trouble
            return self.level

    def _set(self, level, now):
        self.level = level
        self.changed = now
        self.changes += 1
        self.samples.clear()

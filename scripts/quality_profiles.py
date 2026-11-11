# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Explicit quality/bandwidth choices for locally prepared Pocket videos."""
from dataclasses import dataclass


@dataclass(frozen=True)
class QualityProfile:
    key: str
    label: str
    width: int
    height: int
    fps: int
    pixel_format: str

    @property
    def bytes_per_second(self):
        return self.width * self.height * (2 if self.pixel_format == "rgb565le" else 1) * self.fps + 48000 * 4

    @property
    def max_seconds(self):
        return min(7200, (0xffffffff - 32) // self.bytes_per_second)

    @property
    def description(self):
        bits = 16 if self.pixel_format == "rgb565le" else 8
        return (f"{self.width}×{self.height}, {self.fps} fps, {bits}-bit colour; "
                f"{self.bytes_per_second/1000:,.0f} KB/s, "
                f"{self.bytes_per_second*60/1000000:.1f} MB/min; "
                f"up to {self.max_seconds//60} min per clip. Re-export to change quality.")

    @property
    def parameters(self):
        return dict(width=self.width, height=self.height, fps=self.fps, pixel_format=self.pixel_format)


PROFILES = (
    QualityProfile("economy", "Minimum — 24 fps, lower bandwidth", 160, 120, 24, "rgb8"),
    QualityProfile("balanced", "Standard — 30 fps, better colour", 160, 120, 30, "rgb565le"),
    QualityProfile("smooth", "High motion — 60 fps, experimental", 160, 120, 60, "rgb565le"),
    QualityProfile("detail", "High detail — 30 fps, experimental", 320, 240, 30, "rgb565le"),
)


def profile_by_key(key):
    return next(profile for profile in PROFILES if profile.key == key)

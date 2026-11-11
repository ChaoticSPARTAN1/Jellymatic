# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""PJMV reference encoder: a bounded SD playback proof, not live streaming."""
from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

HEADER = struct.Struct("<4sHHIIIIII")
WIDTH, HEIGHT, FPS, RATE, CHANNELS = 160, 120, 30, 48000, 2
VIDEO_BYTES = WIDTH * HEIGHT
AUDIO_BYTES = RATE // FPS * CHANNELS * 2
FRAME_BYTES = VIDEO_BYTES + AUDIO_BYTES


def tool(name):
    result = shutil.which(name)
    if not result:
        raise RuntimeError(f"Install or expose the existing {name} executable on PATH.")
    return result


def convert(source: str, destination: Path, seconds=30, start_seconds=0, pixel_format="rgb565le",
            width=WIDTH, height=HEIGHT, fps=FPS):
    if not 1 <= seconds <= 7200:
        raise ValueError("Clip length must be between 1 and 7200 seconds.")
    if not math.isfinite(start_seconds) or not 0 <= start_seconds <= 864000:
        raise ValueError("Start time must be between 0 and 864000 seconds.")
    if pixel_format not in {"rgb8", "rgb565le"}:
        raise ValueError("Unsupported Pocket colour format.")
    if (width, height) not in {(160, 120), (320, 240)} or fps not in {24, 30, 60}:
        raise ValueError("Unsupported Pocket video dimensions or frame rate.")
    version, flags = 3, int(pixel_format == "rgb565le")
    video_bytes = width * height * (2 if flags else 1)
    audio_bytes = RATE // fps * CHANNELS * 2
    maximum = min(7200, (0xffffffff-HEADER.size) // ((video_bytes+audio_bytes)*fps))
    if seconds > maximum:
        raise ValueError(f"This quality supports at most {maximum} seconds per clip. Split longer videos into sections.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    options = {"stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL,
               "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    probe = subprocess.run([tool("ffprobe"), "-v", "error", "-show_entries", "stream=codec_type",
                            "-of", "json", source], timeout=90, **options)
    if probe.returncode:
        raise RuntimeError("Could not inspect this video. No server details were logged.")
    streams = json.loads(probe.stdout).get("streams", [])
    if not any(s.get("codec_type") == "video" for s in streams):
        raise ValueError("This item has no video stream.")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    with tempfile.TemporaryDirectory(prefix="pocket-media-", dir=destination.parent) as temporary:
        folder = Path(temporary)
        video, audio = folder / "video.raw", folder / "audio.raw"
        command = [tool("ffmpeg"), "-nostdin", "-v", "error", "-ss", str(start_seconds), "-i", source,
                   "-map", "0:v:0", "-t", str(seconds), "-vf",
                   f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                   f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps}",
                   "-sws_dither", "none", "-pix_fmt", pixel_format, "-f", "rawvideo", str(video)]
        if has_audio:
            command += ["-map", "0:a:0", "-t", str(seconds), "-ac", str(CHANNELS),
                        "-ar", str(RATE), "-f", "s16le", str(audio)]
        result = subprocess.run(command, timeout=max(120, seconds * 4), **options)
        if result.returncode:
            raise RuntimeError("Video conversion failed. No server details were logged.")
        frames = video.stat().st_size // video_bytes
        if frames == 0 or video.stat().st_size % video_bytes:
            raise RuntimeError("Converter produced incomplete video frames.")
        output = folder / "complete.pjv"
        with video.open("rb") as pixels, output.open("wb") as out:
            out.write(HEADER.pack(b"PJMV", version, flags, width, height, fps, RATE, CHANNELS, frames))
            pcm = audio.open("rb") if has_audio else None
            try:
                # Planar audio allows contiguous prefetches independently of video FPS.
                for _ in range(frames):
                    samples = pcm.read(audio_bytes) if pcm else b""
                    out.write(samples.ljust(audio_bytes, b"\0"))
                shutil.copyfileobj(pixels, out)
            finally:
                if pcm:
                    pcm.close()
        inspect(output)
        output.replace(destination)
    return frames


def inspect(path: Path):
    with path.open("rb") as file:
        header = file.read(HEADER.size)
    if len(header) != HEADER.size:
        raise ValueError("Truncated PJMV header.")
    magic, version, flags, width, height, fps, rate, channels, frames = HEADER.unpack(header)
    if (version not in {1, 2, 3} or (magic, rate, channels) != (b"PJMV", RATE, CHANNELS)
            or flags not in ({0,1} if version == 3 else {0})
            or (width, height) not in {(160, 120), (320, 240)} or fps not in {10, 15, 24, 30, 60}):
        raise ValueError("Unsupported PJMV format.")
    is_16bit = version == 2 or (version == 3 and flags == 1)
    video_bytes = width * height * (2 if is_16bit else 1)
    frame_bytes = video_bytes + RATE // fps * CHANNELS * 2
    if (not 0 < frames <= fps * 7200 or path.stat().st_size > 0xffffffff or
            path.stat().st_size != HEADER.size + frames * frame_bytes):
        raise ValueError("PJMV payload length does not match the header.")
    return {"width": width, "height": height, "fps": fps, "frames": frames,
            "audio_rate": rate, "channels": channels, "version": version,
            "pixel_format": "rgb565le" if is_16bit else "rgb8", "video_bytes": video_bytes,
            "frame_bytes": frame_bytes}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("destination", type=Path)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--start-seconds", type=float, default=0)
    parser.add_argument("--pixel-format", choices=("rgb565le", "rgb8"), default="rgb565le")
    from quality_profiles import PROFILES, profile_by_key
    parser.add_argument("--profile", choices=[profile.key for profile in PROFILES])
    args = parser.parse_args()
    parameters = profile_by_key(args.profile).parameters if args.profile else dict(pixel_format=args.pixel_format)
    convert(args.source, args.destination, args.seconds, args.start_seconds, **parameters)
    print("Local video converted and structurally verified.")

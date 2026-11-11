# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Local image decoding/cache; no server URL or credentials leave the client."""
import hashlib
import subprocess
import secrets

from connection_config import ROOT  # noqa: F401
from user_paths import PRIVATE  # per-user folder, see user_paths.py
from pocket_media import tool

WIDTH, HEIGHT = 88, 50
POSTER_BYTES = WIDTH * HEIGHT * 2
# Mirrors PJ_ART_* in shared/bridge_rpc.h: the wire contract for both sources.
ART_THUMB, ART_POSTER, ART_BACKDROP = range(3)
ART_KINDS = {ART_THUMB: ("thumb", 96, 54), ART_POSTER: ("poster", 64, 96), ART_BACKDROP: ("backdrop", 320, 120)}


def decode_artwork(data, display_size=(88, 50)):
    if display_size not in {(88, 50), (144, 180)}:
        raise ValueError("Unsupported artwork display size.")
    outputs = []
    for size, options in ((display_size, ("-c:v", "png", "-f", "image2pipe")),
                          ((88, 50), ("-pix_fmt", "rgb565le", "-f", "rawvideo"))):
        width, height = size
        command = [tool("ffmpeg"), "-nostdin", "-v", "error", "-max_alloc", "16777216",
                   "-protocol_whitelist", "pipe", "-f", "image2pipe", "-i", "pipe:0",
                   "-frames:v", "1", "-vf",
                   f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
                   "-threads", "1", *options, "pipe:1"]
        try:
            result = subprocess.run(command, input=data, capture_output=True, timeout=15,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("Artwork could not be decoded locally.") from None
        if result.returncode or not result.stdout:
            raise RuntimeError("Artwork could not be decoded locally.")
        outputs.append(result.stdout)
    if len(outputs[1]) != POSTER_BYTES or not outputs[0].startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("Artwork has invalid dimensions or format.")
    return tuple(outputs)


def fetch_artwork(client, item, display_size=(88, 50)):
    data = client.artwork(item)
    if data is None:
        return None
    # Content-derived cache names contain no server address, user or item ID.
    cache = PRIVATE / "artwork-cache"
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(data + str(display_size).encode("ascii")).hexdigest()
    png, raw = cache / (key + ".png"), cache / (key + ".rgb565")
    if png.exists() and raw.exists() and raw.stat().st_size == POSTER_BYTES:
        return png.read_bytes(), raw.read_bytes()
    decoded = decode_artwork(data, display_size)
    for destination, content in zip((png, raw), decoded):
        temporary = destination.with_suffix(destination.suffix + "." + secrets.token_hex(6) + ".tmp")
        temporary.write_bytes(content)
        temporary.replace(destination)
    return decoded


def decode_card(data, kind):
    """Fill the frame and crop the overflow, as Jellyfin cards do. RGB565 bytes."""
    _, width, height = ART_KINDS[kind]
    command = [tool("ffmpeg"), "-nostdin", "-v", "error", "-max_alloc", "16777216",
               "-protocol_whitelist", "pipe", "-f", "image2pipe", "-i", "pipe:0", "-frames:v", "1",
               "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
                      f"crop={width}:{height}",
               "-threads", "1", "-pix_fmt", "rgb565le", "-f", "rawvideo", "pipe:1"]
    try:
        result = subprocess.run(command, input=data, capture_output=True, timeout=15,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Artwork could not be decoded locally.") from None
    if result.returncode or len(result.stdout) != width * height * 2:
        raise RuntimeError("Artwork has invalid dimensions or format.")
    return result.stdout


def fetch_card(client, item, kind):
    """RGB565 art for one card kind, or None when the server has none."""
    data = client.artwork(item, ART_KINDS[kind][0])
    if data is None:
        return None
    cache = PRIVATE / "artwork-cache"
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(data + b"card" + bytes([kind])).hexdigest()
    path = cache / (key + ".card565")
    _, width, height = ART_KINDS[kind]
    if path.exists() and path.stat().st_size == width * height * 2:
        return path.read_bytes()
    raw = decode_card(data, kind)
    temporary = path.with_suffix(path.suffix + "." + secrets.token_hex(6) + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(path)
    return raw


def save_poster(clip, artwork):
    if artwork is None:
        return False
    raw = artwork[1]
    if len(raw) != POSTER_BYTES:
        raise ValueError("Invalid Pocket poster size.")
    destination = clip.with_suffix(".poster.rgb565")
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(destination)
    return True

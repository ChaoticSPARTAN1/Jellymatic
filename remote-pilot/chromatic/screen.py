# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""Convert the launcher's 160x144 RGB565 overlay frame into a 24-bit BMP."""

import struct

from .protocol import SCREEN_BYTES, SCREEN_HEIGHT, SCREEN_WIDTH

BMP_BYTES = 54 + SCREEN_WIDTH * SCREEN_HEIGHT * 3


def rgb565_to_rgb(value):
    red, green, blue = value >> 11 & 0x1F, value >> 5 & 0x3F, value & 0x1F
    return (red << 3 | red >> 2, green << 2 | green >> 4, blue << 3 | blue >> 2)


def frame_to_bmp(frame):
    """frame: the launcher's frame[] bytes, little-endian RGB565, top row first."""
    if not isinstance(frame, (bytes, bytearray)) or len(frame) != SCREEN_BYTES:
        raise ValueError("Launcher frame must be 46,080 bytes of 160x144 RGB565")
    row_bytes = SCREEN_WIDTH * 3  # 480: already a multiple of four, no padding
    image = bytearray(BMP_BYTES)
    image[:2] = b"BM"
    struct.pack_into("<IHHI", image, 2, BMP_BYTES, 0, 0, 54)
    struct.pack_into("<IiiHHIIiiII", image, 14, 40, SCREEN_WIDTH, SCREEN_HEIGHT, 1, 24, 0,
                     row_bytes * SCREEN_HEIGHT, 2835, 2835, 0, 0)
    pixels = struct.unpack(f"<{SCREEN_WIDTH * SCREEN_HEIGHT}H", bytes(frame))
    for y in range(SCREEN_HEIGHT):
        out = 54 + (SCREEN_HEIGHT - 1 - y) * row_bytes  # BMP rows are bottom-up
        for x in range(SCREEN_WIDTH):
            red, green, blue = rgb565_to_rgb(pixels[y * SCREEN_WIDTH + x])
            image[out:out + 3] = bytes((blue, green, red))
            out += 3
    return bytes(image)


def valid_bmp(image):
    return (isinstance(image, bytes) and len(image) == BMP_BYTES and image[:2] == b"BM"
            and struct.unpack_from("<IiiHH", image, 14) == (40, SCREEN_WIDTH, SCREEN_HEIGHT, 1, 24))


def frame_to_png(frame, scale=1):
    """The same frame as an 8-bit RGB PNG (standard library only), optionally scaled up
    by whole pixels so a person or an AI agent can read the 160x144 text easily."""
    import zlib
    if not isinstance(frame, (bytes, bytearray)) or len(frame) != SCREEN_BYTES:
        raise ValueError("Launcher frame must be 46,080 bytes of 160x144 RGB565")
    if scale not in (1, 2, 3, 4):
        raise ValueError("PNG scale is 1..4")
    pixels = struct.unpack(f"<{SCREEN_WIDTH * SCREEN_HEIGHT}H", bytes(frame))
    rows = bytearray()
    for y in range(SCREEN_HEIGHT):
        line = bytearray()
        for x in range(SCREEN_WIDTH):
            line += bytes(rgb565_to_rgb(pixels[y * SCREEN_WIDTH + x])) * scale
        rows += (b"\x00" + line) * scale

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    header = struct.pack(">IIBBBBB", SCREEN_WIDTH * scale, SCREEN_HEIGHT * scale, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) \
        + chunk(b"IEND", b"")

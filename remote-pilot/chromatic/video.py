# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""CHV1: compressed 160x144 RGB565 frames for CHR1 `video` (see PROTOCOL.md).

Cheap enough for the ESP32: changed-row deltas plus 16-bit run-length coding,
with a raw keyframe whenever coding would not be smaller. Layout:

  header (20 bytes, little-endian)
    0  4  b"CHV1"
    4  4  frame sequence number (increases by one per frame sent on a session)
    8  4  base sequence: the frame this delta applies to (0 for keyframes)
   12  1  kind: 0 raw keyframe, 1 RLE keyframe, 2 RLE delta
   13  1  reserved, 0
   14  2  number of coded rows
   16  4  payload length
  payload
    raw:       46,080 bytes, the frame as is
    RLE key:   144 coded rows
    RLE delta: an 18-byte bitmap of changed rows (bit y = byte y//8, bit y%8),
               then one coded row per set bit
    coded row: runs of (count u8 1..255, pixel u16) adding up to exactly 160
"""

import struct

from .protocol import MAX_VIDEO, SCREEN_BYTES, SCREEN_HEIGHT, SCREEN_WIDTH, VIDEO_HEADER

MAGIC = b"CHV1"
RAW, RLE_KEY, RLE_DELTA = 0, 1, 2
_HEADER = struct.Struct("<4sIIBBHI")
_ROW_BYTES = SCREEN_WIDTH * 2
_BITMAP = SCREEN_HEIGHT // 8


def _rows(frame):
    return [frame[y * _ROW_BYTES:(y + 1) * _ROW_BYTES] for y in range(SCREEN_HEIGHT)]


def _code_row(row):
    pixels = struct.unpack(f"<{SCREEN_WIDTH}H", row)
    out, index = bytearray(), 0
    while index < SCREEN_WIDTH:
        value, count = pixels[index], 1
        while index + count < SCREEN_WIDTH and count < 255 and pixels[index + count] == value:
            count += 1
        out += struct.pack("<BH", count, value)
        index += count
    return bytes(out)


def encode(frame, seq, previous=None, base=0):
    """Device side (simulator). previous/base: the last frame sent on this session."""
    if len(frame) != SCREEN_BYTES:
        raise ValueError("Frame must be 160x144 RGB565")
    rows = _rows(frame)
    if previous is not None and len(previous) == SCREEN_BYTES and base:
        changed = [y for y, (now, old) in enumerate(zip(rows, _rows(previous))) if now != old]
        bitmap = bytearray(_BITMAP)
        for y in changed:
            bitmap[y // 8] |= 1 << (y % 8)
        payload = bytes(bitmap) + b"".join(_code_row(rows[y]) for y in changed)
        if len(payload) < SCREEN_BYTES:
            return _HEADER.pack(MAGIC, seq, base, RLE_DELTA, 0, len(changed), len(payload)) + payload
    payload = b"".join(_code_row(row) for row in rows)
    if len(payload) < SCREEN_BYTES:
        return _HEADER.pack(MAGIC, seq, 0, RLE_KEY, 0, SCREEN_HEIGHT, len(payload)) + payload
    return _HEADER.pack(MAGIC, seq, 0, RAW, 0, SCREEN_HEIGHT, SCREEN_BYTES) + bytes(frame)


def _decode_row(payload, offset):
    out, total = bytearray(), 0
    while total < SCREEN_WIDTH:
        if offset + 3 > len(payload):
            raise ValueError("CHV1 row is truncated")
        count, value = struct.unpack_from("<BH", payload, offset)
        offset += 3
        if not count or total + count > SCREEN_WIDTH:
            raise ValueError("CHV1 run overflows the row")
        out += struct.pack("<H", value) * count
        total += count
    return bytes(out), offset


class Decoder:
    """PC side. Keeps the last good frame; a broken chain asks for a keyframe."""

    def __init__(self):
        self.frame = None
        self.seq = 0
        self.bytes_in = 0

    def reset(self):
        self.frame, self.seq = None, 0

    @property
    def want(self):
        return "delta" if self.frame is not None else "key"

    def feed(self, data):
        if not isinstance(data, (bytes, bytearray)) or not VIDEO_HEADER <= len(data) <= MAX_VIDEO:
            raise ValueError("CHV1 frame size is out of range")
        magic, seq, base, kind, reserved, rows, length = _HEADER.unpack_from(data)
        payload = bytes(data[VIDEO_HEADER:])
        if magic != MAGIC or reserved or length != len(payload) or not seq:
            raise ValueError("Not a CHV1 frame")
        if kind == RAW:
            if length != SCREEN_BYTES or base:
                raise ValueError("CHV1 raw keyframe has the wrong size")
            frame = payload
        elif kind == RLE_KEY:
            if base or rows != SCREEN_HEIGHT:
                raise ValueError("CHV1 keyframe header is inconsistent")
            parts, offset = [], 0
            for _ in range(SCREEN_HEIGHT):
                row, offset = _decode_row(payload, offset)
                parts.append(row)
            if offset != length:
                raise ValueError("CHV1 keyframe has trailing bytes")
            frame = b"".join(parts)
        elif kind == RLE_DELTA:
            if self.frame is None or base != self.seq:
                self.reset()
                raise ValueError("CHV1 delta does not follow the last frame; a keyframe is needed")
            if length < _BITMAP:
                raise ValueError("CHV1 delta is truncated")
            changed = [y for y in range(SCREEN_HEIGHT) if payload[y // 8] >> (y % 8) & 1]
            if len(changed) != rows:
                raise ValueError("CHV1 delta row count is inconsistent")
            parts, offset = _rows(self.frame), _BITMAP
            for y in changed:
                parts[y], offset = _decode_row(payload, offset)
            if offset != length:
                raise ValueError("CHV1 delta has trailing bytes")
            frame = b"".join(parts)
        else:
            raise ValueError("Unknown CHV1 frame kind")
        if self.frame is not None and kind == RLE_DELTA and seq <= self.seq:
            raise ValueError("CHV1 sequence went backwards")
        self.frame, self.seq = frame, seq
        self.bytes_in += len(data)
        return frame

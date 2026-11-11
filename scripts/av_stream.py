# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Codec-neutral, bounded AV packets; usable by USB, Wi-Fi or a link driver."""
from dataclasses import dataclass
import struct
import zlib

HEADER = struct.Struct('<4sBBHIIQII')
CONFIG_PAYLOAD = struct.Struct('<HHHHIHHHH')
LIMIT = 32768
CONFIG, VIDEO, AUDIO, END = range(1, 5)
KEYFRAME, DISCONTINUITY = 1, 2
JPEG, H264 = 1, 2
IMA_MONO, PCM_MONO = 1, 2


class StreamError(ValueError):
    pass


@dataclass(frozen=True)
class Packet:
    kind: int
    session: int
    sequence: int
    pts: int
    payload: bytes = b''
    flags: int = 0


def encode(packet):
    p = packet
    if not (1 <= p.session <= 0xffffffff and 0 <= p.sequence <= 0xffffffff and
            0 <= p.pts <= 0xffffffffffffffff and p.kind in (CONFIG, VIDEO, AUDIO, END)
            and 0 <= p.flags <= 3 and len(p.payload) <= LIMIT):
        raise StreamError('Invalid AV packet')
    if p.kind == CONFIG and (p.sequence or p.pts or len(p.payload) != 20):
        raise StreamError('Invalid configuration envelope')
    if (p.kind == END) != (not p.payload):
        raise StreamError('Invalid AV payload length')
    prefix = HEADER.pack(b'PJAV', 1, p.kind, p.flags, p.session, p.sequence, p.pts, len(p.payload), 0)[:28]
    return prefix + struct.pack('<I', zlib.crc32(prefix + p.payload)) + p.payload


class Reader:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        if len(self.buffer) + len(data) > 2 * (HEADER.size + LIMIT):
            raise StreamError('AV receive buffer exceeded')
        self.buffer.extend(data)
        packets = []
        while len(self.buffer) >= HEADER.size:
            magic, version, kind, flags, session, sequence, pts, length, checksum = HEADER.unpack_from(self.buffer)
            if magic != b'PJAV' or version != 1 or length > LIMIT:
                raise StreamError('Invalid AV header')
            if len(self.buffer) < HEADER.size + length:
                break
            raw = bytes(self.buffer[:HEADER.size + length])
            packet = Packet(kind, session, sequence, pts, raw[HEADER.size:], flags)
            if encode(packet) != raw:
                raise StreamError('AV checksum mismatch')
            packets.append(packet)
            del self.buffer[:len(raw)]
        return packets

    def finish(self):
        if self.buffer:
            raise StreamError('AV packet interrupted')


def configuration(width=160, height=144, fps=30, video=JPEG, rate=16000, audio=IMA_MONO, block=128):
    if (width not in (128, 160, 256) or height not in (96, 128, 144) or fps not in (24, 25, 30, 60)
            or video not in (JPEG, H264) or rate not in (16000, 22050, 24000, 48000)
            or audio not in (IMA_MONO, PCM_MONO) or block not in (0, 128, 256, 512, 1024)):
        raise StreamError('Unsupported AV configuration')
    if (audio == IMA_MONO and not block) or (audio == PCM_MONO and block):
        raise StreamError('Invalid audio block configuration')
    return CONFIG_PAYLOAD.pack(width, height, fps, video, rate, audio, 1, block, 0)


class Receiver:
    """Session/sequence/keyframe recovery. Decoder and physical IO are separate."""
    def __init__(self):
        self.session = None
        self.next_sequence = 0
        self.need_keyframe = True
        self.closed = True
        self.lost_packets = 0
        self.last_pts = {}

    def accept(self, packet):
        p = packet
        if p.kind == CONFIG:
            values = CONFIG_PAYLOAD.unpack(p.payload)
            width, height, fps, video, rate, audio, channels, block, reserved = values
            if channels != 1 or reserved:
                raise StreamError('Unsupported AV channels/reserved fields')
            configuration(width, height, fps, video, rate, audio, block)
            self.session = p.session
            self.next_sequence = 1
            self.need_keyframe = True
            self.closed = False
            self.last_pts.clear()
            return p
        if p.session != self.session or self.closed:
            return None
        if p.sequence != self.next_sequence:
            gap = (p.sequence - self.next_sequence) & 0xffffffff
            if gap >= 0x80000000:
                return None  # old/duplicate packet
            self.lost_packets += gap
            self.need_keyframe = True
        self.next_sequence = (p.sequence + 1) & 0xffffffff
        if p.flags & DISCONTINUITY:
            self.need_keyframe = True
            self.last_pts.clear()
        if p.kind == END:
            self.closed = True
            return p
        if p.pts < self.last_pts.get(p.kind, 0):
            raise StreamError('AV timestamps moved backwards')
        self.last_pts[p.kind] = p.pts
        if p.kind == VIDEO:
            if p.flags & KEYFRAME:
                self.need_keyframe = False
            if self.need_keyframe:
                return None
        return p

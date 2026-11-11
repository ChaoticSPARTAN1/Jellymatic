# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Validate locally encoded baseline JPEG and omit repeated standard tables.

The FPGA has the standard JPEG Huffman tables in ROM. Removing the identical
tables from each frame saves link bandwidth; nonstandard tables are rejected.
No arbitrary JPEG input is treated as compatible without this check.
"""
import hashlib

STANDARD_DHT = {
    0x00: '9fb04cb1a8076318f33f273ffef967a9ea7291ecf3adeca79f643dfd300de425',
    0x01: 'af48d4f608dfb682fd19ee479c66dad5342dfbed5b1d1807b95393162a8715d1',
    0x10: 'f27ce9b718f91cd426885e6ce4a37d0ba4900db219f719eef29ecc1f7f4fd5f0',
    0x11: '8cf60aa835f8474bb590e73332bee9c246bec9ce1e368aa725674c096f3efd2c',
}


def compact(frame):
    if len(frame) > 32768 or frame[:2] != b'\xff\xd8':
        raise ValueError('Invalid JPEG frame')
    output = bytearray(frame[:2])
    offset = 2
    tables = set()
    geometry = None
    while offset + 4 <= len(frame):
        if frame[offset] != 255:
            raise ValueError('Invalid JPEG marker')
        marker = frame[offset + 1]
        length = int.from_bytes(frame[offset + 2:offset + 4], 'big')
        end = offset + 2 + length
        if length < 2 or end > len(frame):
            raise ValueError('Truncated JPEG marker')
        payload = frame[offset + 4:end]
        if marker == 0xc4:
            i = 0
            while i < len(payload):
                if i + 17 > len(payload):
                    raise ValueError('Truncated Huffman table')
                size = 17 + sum(payload[i+1:i+17])
                table = payload[i:i+size]
                if len(table) != size or hashlib.sha256(table).hexdigest() != STANDARD_DHT.get(table[0]):
                    raise ValueError('JPEG needs unsupported dynamic Huffman table')
                tables.add(table[0])
                i += size
        elif 0xe0 <= marker <= 0xef or marker == 0xfe:
            pass  # Do not forward application metadata/comments.
        elif marker == 0xc0:
            if len(payload) != 15 or payload[0] != 8 or payload[5] != 3 or payload[7] != 0x22 or payload[10] != 0x11 or payload[13] != 0x11:
                raise ValueError('JPEG needs 8-bit baseline 4:2:0')
            geometry = (int.from_bytes(payload[3:5], 'big'), int.from_bytes(payload[1:3], 'big'))
            output.extend(frame[offset:end])
        elif marker == 0xdb:
            i = 0
            while i < len(payload):
                if payload[i] >> 4 or i + 65 > len(payload):
                    raise ValueError('Unsupported JPEG quantization table')
                i += 65
            output.extend(frame[offset:end])
        elif marker == 0xda:
            if geometry is None or tables != set(STANDARD_DHT) or frame[-2:] != b'\xff\xd9':
                raise ValueError('Incomplete standard JPEG frame')
            entropy = frame[end:]
            i = 0
            while i < len(entropy):
                if entropy[i] != 255:
                    i += 1
                    continue
                if i + 1 >= len(entropy):
                    raise ValueError('Truncated JPEG entropy')
                code = entropy[i+1]
                if code == 0:
                    i += 2
                elif code == 0xd9 and i == len(entropy) - 2:
                    break
                else:
                    raise ValueError('Restart/progressive/multiscan JPEG unsupported')
            output.extend(frame[offset:])
            return bytes(output), geometry
        else:
            raise ValueError('Unsupported JPEG marker')
        offset = end
    raise ValueError('JPEG scan missing')

# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Corruption/refusal checks for the offline recovery validator; no hardware."""
import hashlib
import struct
import unittest

from verify_chromatic_backup import SIZE, validate_fpga, validate_mcu


def mcu_fixture():
    data = bytearray(b'\xff' * SIZE)
    data[0x1000] = data[0x10000] = 0xe9
    entries = bytearray()
    for kind, sub, off, size, label in [(1, 2, 0x9000, 0x6000, b'nvs'),
            (1, 1, 0xf000, 0x1000, b'phy_init'), (0, 0, 0x10000, 0x100000, b'factory')]:
        entry = bytearray(32)
        entry[:4] = bytes([0xaa, 0x50, kind, sub])
        struct.pack_into('<II', entry, 4, off, size)
        entry[12:12 + len(label)] = label
        entries.extend(entry)
    checksum = b'\xeb\xeb' + b'\xff' * 14 + hashlib.md5(entries).digest()
    data[0x8000:0x8000 + len(entries) + 32] = entries + checksum
    desc = bytearray(256)
    struct.pack_into('<I', desc, 0, 0xabcd5432)
    desc[16:19] = b'4.6'
    desc[48:54] = b'mcu_fw'
    data[0x10020:0x10120] = desc
    return data


class BackupValidationTests(unittest.TestCase):
    def test_valid_stock_layout(self):
        self.assertEqual(validate_mcu(mcu_fixture())['version'], '4.6')

    def test_bad_partition_checksum_rejected(self):
        data = mcu_fixture()
        data[0x8010] ^= 1
        with self.assertRaises(ValueError):
            validate_mcu(data)

    def test_truncated_or_blank_images_rejected(self):
        for checker in (validate_mcu, validate_fpga):
            for data in (b'', b'\xff' * SIZE, b'\0' * (SIZE - 1)):
                with self.assertRaises(ValueError):
                    checker(data)

    def test_missing_app_rejected(self):
        data = mcu_fixture()
        data[0x10020] ^= 1
        with self.assertRaises(ValueError):
            validate_mcu(data)

    def test_wrong_fpga_target_rejected(self):
        data = bytearray(b'\xff' * SIZE)
        data[24:34] = bytes.fromhex('a5c3060000000001281b')
        validate_fpga(data)
        data[33] ^= 1
        with self.assertRaises(ValueError):
            validate_fpga(data)


if __name__ == '__main__':
    unittest.main()

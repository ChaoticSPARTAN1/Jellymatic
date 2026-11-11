# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Build a dual-boot candidate from the stock backup and prove what it changes.

Offline only: reads private files, writes private files, opens no device. Reports
layout and pass/fail, never dump contents, settings or identifiers. The candidate
holds the owner's NVS, so it must stay under private/.
"""
import hashlib
import json
from pathlib import Path
from user_paths import PRIVATE  # noqa: E402  (per-user folder, see user_paths.py)
import struct

from connection_config import ROOT

FLASH_SIZE = 0x400000
BOOTLOADER_AT = 0x1000
TABLE_AT = 0x8000
TABLE_SECTOR = 0x1000
NVS_AT = 0x9000
BOOTLOADER_LIMIT = TABLE_AT - BOOTLOADER_AT
STOCK_ROWS = ((1, 2, 0x9000, 0x6000, 'nvs'), (1, 1, 0xf000, 0x1000, 'phy_init'), (0, 0, 0x10000, 0x100000, 'factory'))
BUILD = ROOT / 'staging/chromatic-launcher/build'
OUTPUT = PRIVATE / 'chromatic-launcher-candidate'


class Refused(ValueError):
    pass


def _app_image(desc_fields, payload=b'', load=0x3F400020):
    """A minimal, valid ESP32 app image (one segment, checksum and SHA-256 appended) whose app
    descriptor holds `desc_fields` ({offset: bytes}). Only for the synthetic stock image below."""
    desc = bytearray(256)
    struct.pack_into('<I', desc, 0, 0xabcd5432)
    for at, value in desc_fields.items():
        desc[at:at + len(value)] = value
    data = bytes(desc) + payload
    data += b'\0' * (-len(data) % 4)
    header = bytes([0xe9, 1, 2, 0x20]) + struct.pack('<I', 0x40080000)
    header += bytes([0xee, 0, 0, 0]) + struct.pack('<H', 0) + bytes([0]) + struct.pack('<HH', 0, 0xffff) + bytes(4) + bytes([1])
    body = header + struct.pack('<II', load, len(data)) + data
    checksum = 0xef
    for byte in data:
        checksum ^= byte
    body += b'\0' * (15 - len(body) % 16) + bytes([checksum])
    return body + hashlib.sha256(body).digest()


def synthetic_stock():
    """A made-up 4 MiB ESP32 flash image with the stock layout (bootloader, the exact stock partition
    table, an app named like ModRetro's stock project, blank settings, empty launcher area). It
    contains NO ModRetro code: it exists so the tests can run on any PC without a dump of a real
    console. Never write it to a console (it would not start)."""
    image = bytearray(b'\xff' * FLASH_SIZE)
    boot = _app_image({16: b'synthetic-bootloader'}, b'\x5a' * 4000, load=0x3FFF0000)
    image[BOOTLOADER_AT:BOOTLOADER_AT + len(boot)] = boot
    rows = b''
    for kind, subtype, offset, size, name in STOCK_ROWS:
        rows += b'\xaa\x50' + bytes([kind, subtype]) + struct.pack('<II', offset, size) + name.encode().ljust(16, b'\0') + bytes(4)
    table = rows + b'\xeb\xeb' + b'\xff' * 14 + hashlib.md5(rows).digest()
    image[TABLE_AT:TABLE_AT + len(table)] = table
    app = _app_image({16: b'v4.6-synthetic', 48: b'mcu_fw', 80: b'00:00:00', 96: b'Jan  1 2026', 112: b'v5.3'},
                     bytes(range(256)) * 512)
    image[0x10000:0x10000 + len(app)] = app
    return bytes(image)


def stock_dump(allow_synthetic=False):
    """A stock 4 MiB ESP32 flash image for offline checks: JELLYMATIC_STOCK_DUMP (a full read of
    YOUR console, for example esp32-flash.bin from one of Chromatic Manager's backups), else the
    newest full backup recorded by chromatic_backup.py. Tests pass allow_synthetic=True and get the
    made-up image above when neither exists."""
    import os
    named = os.environ.get('JELLYMATIC_STOCK_DUMP')
    if named:
        return Path(named).read_bytes()
    try:
        folder = Path((PRIVATE / 'chromatic-tools/latest-backup-path.txt').read_text(encoding='utf-8').strip())
        found = [path for path in (folder / 'restore-bundle').iterdir()
                 if 'esp32' in path.name.lower() and path.suffix == '.bin']
        if len(found) == 1:
            return found[0].read_bytes()
    except OSError:
        pass
    if allow_synthetic:
        return synthetic_stock()
    raise Refused('No stock flash image: set JELLYMATIC_STOCK_DUMP to a full read of your console.')


def parse_table(data):
    rows, raw = [], []
    for at in range(0, len(data), 32):
        entry = data[at:at + 32]
        if entry[:2] == b'\xeb\xeb':
            if entry[16:32] != hashlib.md5(data[:at]).digest():
                raise Refused('Partition table MD5 row is wrong')
            return rows, raw
        if entry[:2] != b'\xaa\x50':
            break
        kind, subtype = entry[2], entry[3]
        offset, size = struct.unpack('<II', entry[4:12])
        rows.append((kind, subtype, offset, size, entry[12:28].split(b'\0')[0].decode('ascii')))
        raw.append(entry)
    raise Refused('Partition table has no MD5 row')


def check_image(data, label):
    """Validate an ESP32 image: header, segments, XOR checksum, appended SHA-256."""
    if len(data) < 24 or data[0] != 0xe9:
        raise Refused(f'{label}: bad image magic')
    if struct.unpack_from('<H', data, 12)[0] != 0:
        raise Refused(f'{label}: image is not for ESP32')
    at, checksum = 24, 0xef
    for _ in range(data[1]):
        if at + 8 > len(data):
            raise Refused(f'{label}: truncated segment header')
        _, length = struct.unpack_from('<II', data, at)
        at += 8
        if at + length > len(data):
            raise Refused(f'{label}: truncated segment')
        for byte in data[at:at + length]:
            checksum ^= byte
        at += length
    end = (at + 16) & ~15  # one checksum byte, padded so it ends a 16-byte block
    if end > len(data) or data[end - 1] != checksum:
        raise Refused(f'{label}: checksum failed')
    if data[23] == 1:
        if end + 32 > len(data) or hashlib.sha256(data[:end]).digest() != data[end:end + 32]:
            raise Refused(f'{label}: SHA-256 failed')
        end += 32
    return end


FLASH_ARG_FILES = ('flash_args', 'flash_app_args', 'flash_bootloader_args', 'flash_project_args', 'app-flash_args',
                   'bootloader-flash_args', 'partition-table-flash_args', 'flasher_args.json')
POISON = '--REFUSED-this-would-overwrite-the-Chromatic-stock-firmware-use-scripts/chromatic_manager.py\n'


def poison_flash_args(build=BUILD):
    """IDF writes esptool argument files that put this app at 0x10000, over the stock
    firmware, and any later ninja run regenerates them. Every tool that runs the build
    calls this afterwards, so `esptool @flash_args` fails instead of overwriting stock."""
    for name in FLASH_ARG_FILES:
        path = Path(build) / name
        if path.exists():
            path.write_text(POISON if not name.endswith('.json') else
                            '{"refused": "Use scripts/chromatic_manager.py; never flash this build directly."}\n',
                            encoding='ascii')


def flash_args_poisoned(build=BUILD):
    return all('REFUSED' in (Path(build) / name).read_text(encoding='ascii', errors='replace').upper()
               for name in FLASH_ARG_FILES if (Path(build) / name).exists())


def changed_ranges(before, after, sector=0x1000):
    ranges = []
    for at in range(0, len(before), sector):
        if before[at:at + sector] != after[at:at + sector]:
            if ranges and ranges[-1][1] == at:
                ranges[-1][1] = at + sector
            else:
                ranges.append([at, at + sector])
    return ranges


def compose(stock, bootloader, table, launcher):
    """Pure: returns the candidate and evidence, or raises Refused."""
    if len(stock) != FLASH_SIZE:
        raise Refused('Stock dump is not 4 MiB')
    old_rows, old_raw = parse_table(stock[TABLE_AT:TABLE_AT + TABLE_SECTOR])
    if tuple(old_rows) != STOCK_ROWS:
        raise Refused('Stock table is not the verified layout')
    new_rows, new_raw = parse_table(table)
    if new_raw[:3] != old_raw:
        raise Refused('The stock partition rows changed')
    extra = new_rows[3:]
    if len(extra) != 1 or extra[0][:2] != (0, 0x10) or extra[0][4] != 'launcher':
        raise Refused('Expected exactly one added launcher slot')
    _, _, slot_at, slot_size, _ = extra[0]
    spans = sorted((row[2], row[2] + row[3]) for row in new_rows)
    for (_, end), (start, _) in zip(spans, spans[1:]):
        if end > start:
            raise Refused('Partitions overlap')
    if slot_at % 0x10000 or slot_at + slot_size > FLASH_SIZE:
        raise Refused('Launcher slot is misplaced')
    if stock[slot_at:slot_at + slot_size] != b'\xff' * slot_size:
        raise Refused('Launcher slot would overwrite existing flash data')
    if len(bootloader) > BOOTLOADER_LIMIT:
        raise Refused('Bootloader would run into the partition table')
    stock_boot = stock[BOOTLOADER_AT:TABLE_AT]
    if bootloader[2:4] != stock_boot[2:4]:
        raise Refused('Bootloader flash mode, speed or size differs from stock')
    if launcher[2:4] != stock[0x10000 + 2:0x10000 + 4]:
        raise Refused('Launcher flash mode, speed or size differs from stock')
    check_image(bootloader, 'bootloader')
    stock_end = check_image(stock[0x10000:0x110000], 'stock firmware')
    launcher_end = check_image(launcher, 'launcher')
    if launcher_end > slot_size or len(launcher) > slot_size:
        raise Refused('Launcher does not fit its slot')
    image = bytearray(stock)
    image[BOOTLOADER_AT:TABLE_AT] = bootloader + b'\xff' * (BOOTLOADER_LIMIT - len(bootloader))
    image[TABLE_AT:TABLE_AT + TABLE_SECTOR] = table + b'\xff' * (TABLE_SECTOR - len(table))
    image[slot_at:slot_at + len(launcher)] = launcher
    image = bytes(image)
    allowed = ((BOOTLOADER_AT, TABLE_AT + TABLE_SECTOR), (slot_at, slot_at + slot_size))
    ranges = changed_ranges(stock, image)
    for start, end in ranges:
        if not any(low <= start and end <= high for low, high in allowed):
            raise Refused('Candidate changes flash outside the allowed ranges')
    protected = {'nvs': (0x9000, 0xf000), 'phy_init': (0xf000, 0x10000), 'factory_slot': (0x10000, 0x110000),
                 'unreferenced_data_to_slot': (0x110000, slot_at)}
    identical = {}
    for name, (start, end) in protected.items():
        same = hashlib.sha256(stock[start:end]).digest() == hashlib.sha256(image[start:end]).digest()
        if not same:
            raise Refused(f'{name} is not byte-identical')
        identical[name] = True
    check_image(image[slot_at:slot_at + slot_size], 'launcher in place')
    check_image(image[BOOTLOADER_AT:TABLE_AT], 'bootloader in place')
    check_image(image[0x10000:0x110000], 'stock in place')
    evidence = {'identical_to_backup': identical, 'changed_ranges': [[hex(a), hex(b)] for a, b in ranges],
                'bootloader_bytes': len(bootloader), 'bootloader_limit': BOOTLOADER_LIMIT, 'stock_app_bytes': stock_end,
                'launcher_bytes': launcher_end, 'launcher_slot': [hex(slot_at), hex(slot_size)],
                'partitions': [[row[4], hex(row[2]), hex(row[3])] for row in new_rows]}
    return image, slot_at, evidence


def main():
    stock = stock_dump()
    bootloader = (BUILD / 'bootloader/bootloader.bin').read_bytes()
    table = (BUILD / 'partition_table/partition-table.bin').read_bytes()
    launcher = (BUILD / 'chromatic_launcher.bin').read_bytes()
    image, slot_at, evidence = compose(stock, bootloader, table, launcher)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / 'NOT-INSTALLED.txt').write_text(
        'Candidate only. Never written to any device. Contains owner NVS: keep private.\n'
        'Do not write the full image. Use write-plan.json, and only with the owner present.\n', encoding='ascii')
    (OUTPUT / 'candidate-full-4MiB.bin').write_bytes(image)
    pieces = (('launcher.bin', launcher, slot_at), ('partition-table.bin', table, TABLE_AT), ('bootloader.bin', bootloader, BOOTLOADER_AT))
    plan = []
    for name, data, offset in pieces:
        (OUTPUT / name).write_bytes(data)
        plan.append({'file': name, 'offset': hex(offset), 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    (OUTPUT / 'write-plan.json').write_text(json.dumps({
        'installed': False, 'requires_owner_present': True,
        'order_reason': 'Each step leaves a bootable console: stock ignores an extra slot, so the bootloader goes last.',
        'never_write': ['0x9000-0x10000 settings', '0x10000-0x110000 stock firmware', 'FPGA flash'], 'steps': plan}, indent=2),
        encoding='utf-8')
    evidence.update(installed=False, hardware_accessed=False, candidate_sha256=hashlib.sha256(image).hexdigest())
    (OUTPUT / 'report.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    public = {key: evidence[key] for key in ('changed_ranges', 'bootloader_bytes', 'bootloader_limit', 'launcher_bytes',
                                             'launcher_slot', 'partitions', 'identical_to_backup')}
    print(json.dumps(public, indent=1))
    print('PASS: candidate composed; stock firmware, settings and existing flash data proven unchanged. Nothing installed.')


if __name__ == '__main__':
    main()

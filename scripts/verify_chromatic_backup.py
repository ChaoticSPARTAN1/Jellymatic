# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Offline backup validation and bundle creation. Never opens a USB device.

Device identity and firmware contents are consumed locally; stdout is generic.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import struct

SIZE = 4 * 1024 * 1024


def sha(data):
    return hashlib.sha256(data).hexdigest()


def validate_mcu(data):
    if len(data) != SIZE or data[0x1000] != 0xe9:
        raise ValueError('MCU size/bootloader invalid')
    table = data[0x8000:0x8c00]
    parts = []
    checked = False
    for offset in range(0, len(table), 32):
        entry = table[offset:offset + 32]
        if entry[:2] == b'\xaa\x50':
            if checked:
                raise ValueError('Partition entry after checksum')
            start, length = struct.unpack_from('<II', entry, 4)
            if not length or start + length > SIZE:
                raise ValueError('Partition outside flash')
            parts.append((entry[2], entry[3], start, length))
        elif entry[:2] == b'\xeb\xeb':
            if checked or hashlib.md5(table[:offset]).digest() != entry[16:32]:
                raise ValueError('Partition checksum invalid')
            checked = True
        elif entry == b'\xff' * 32:
            break
        else:
            raise ValueError('Unknown partition table entry')
    if not checked or not parts:
        raise ValueError('Missing valid partition table')
    ordered = sorted(parts, key=lambda p: p[2])
    for a, b in zip(ordered, ordered[1:]):
        if a[2] + a[3] > b[2]:
            raise ValueError('Overlapping partitions')
    if (1, 2, 0x9000, 0x6000) not in parts or (1, 1, 0xf000, 0x1000) not in parts:
        raise ValueError('Stock NVS/PHY layout invalid')
    apps = [p for p in parts if p[0] == 0 and p[2] == 0x10000]
    if len(apps) != 1 or data[0x10000] != 0xe9:
        raise ValueError('Missing stock app image')
    desc = data[0x10020:0x10120]
    if struct.unpack_from('<I', desc)[0] != 0xabcd5432:
        raise ValueError('Invalid application descriptor')
    version = desc[16:48].split(b'\0')[0].decode('ascii')
    project = desc[48:80].split(b'\0')[0].decode('ascii')
    if project != 'mcu_fw' or not re.fullmatch(r'[A-Za-z0-9._+\-]{1,32}', version):
        raise ValueError('Unexpected application descriptor')
    return {'version': version, 'project': project,
            'shiny_marker_present': any(marker in data for marker in
                (b'Wi-Fi link: cwl [', b'cwl: wifi saved ('))}


def validate_fpga(data):
    if len(data) != SIZE:
        raise ValueError('FPGA size invalid')
    sync = data[:256].find(b'\xa5\xc3')
    if sync < 0 or data[sync + 2] != 0x06:
        raise ValueError('Missing stock FPGA sync/header')
    code = int.from_bytes(data[sync + 6:sync + 10], 'big') & 0x0fffffff
    if code != 0x1281b:
        raise ValueError('FPGA image target mismatch')


def validate_reads(folder):
    data = {}
    for role, checker in [('mcu', validate_mcu), ('fpga', validate_fpga)]:
        a = (folder / f'{role}-read-1.bin').read_bytes()
        b = (folder / f'{role}-read-2.bin').read_bytes()
        record = json.loads((folder / f'{role}-verified.json').read_text(encoding='utf-8'))
        if len(a) != SIZE or a != b or record['sha256'] != sha(a):
            raise ValueError('Readbacks or saved checksum disagree')
        checker(a)
        data[role] = a
    # Espressif's independent parser checks both executable image structures.
    # It computes digests/checksums but does not compare them automatically.
    from esptool.bin_image import ESP32FirmwareImage
    for start in (0x1000, 0x10000):
        image = ESP32FirmwareImage(io.BytesIO(data['mcu'][start:]))
        if image.chip_id != 0 or not image.segments:
            raise ValueError('Unexpected executable image target')
        if image.checksum != image.calculate_checksum():
            raise ValueError('Executable segment checksum invalid')
        if image.append_digest and image.stored_digest != image.calc_digest:
            raise ValueError('Executable appended SHA-256 invalid')
    proof = json.loads((folder / 'same-unit-usb-proof.json').read_text(encoding='utf-8'))
    if proof.get('serial_disappeared_during_fpga_read') is not True:
        raise ValueError('Missing same-unit USB proof')
    return data


def make_bundle(folder):
    data = validate_reads(folder)
    info = validate_mcu(data['mcu'])
    ident = json.loads((folder / 'mcu-identity.json').read_text(encoding='utf-8'))
    bundle = folder / 'restore-bundle'
    bundle.mkdir(exist_ok=False)
    prefix = 'chromatic-' + folder.name.removeprefix('chromatic-backup-') + '-'
    rec = {
        'format': 1, 'taken': dt.datetime.now(dt.timezone.utc).isoformat(),
        'stock': not info['shiny_marker_present'],
        'stock_evidence': 'ModRetro stock application descriptor and no known Shiny Relay markers. Not an authenticity attestation.',
        'firmware_at_backup': {'mcu': 'v' + info['version'].removeprefix('v'),
            'fpga': None, 'silent': True, 'fromFlash': True, 'project': info['project']},
        'esp': {'file': 'esp32_flash_4MB.bin', 'size': SIZE, 'sha256': sha(data['mcu']),
            'chip': 'ESP32-U4WDH', 'revision': ident['revision'] // 100,
            'mac': ident['mac'], 'flash_id': ident['flash_id']},
        'fpga': {'file': 'fpga_flash_4MB.bin', 'size': SIZE, 'sha256': sha(data['fpga']),
            'idcode': '0x0001281b', 'flash_jedec': '0xef4016'},
        'verification': {'independent_full_reads_per_chip': 2, 'mcu_chunk_md5_on_chip': True,
            'saved_files_rehashed': True, 'physical_restore_tested': False},
    }
    for role, content in [('esp', data['mcu']), ('fpga', data['fpga'])]:
        path = bundle / (prefix + rec[role]['file'])
        with path.open('xb') as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        if sha(path.read_bytes()) != rec[role]['sha256']:
            raise ValueError('Bundle saved-file mismatch')
    path = bundle / (prefix + 'backup.json')
    with path.open('x', encoding='utf-8') as out:
        json.dump(rec, out, indent=2)
        out.flush()
        os.fsync(out.fileno())
    return bundle


def copy_bundle(bundle, target):
    """Copy the three-file restore bundle to `target` (a new folder, usually on the second disk) and
    compare every byte of every copied file with the original. Returns `target`."""
    target = Path(target)
    if target.exists():
        raise ValueError('Refusing to overwrite secondary backup')
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(bundle, target)
    for p in Path(bundle).iterdir():
        with (target / p.name).open('r+b') as saved:
            os.fsync(saved.fileno())
        if p.read_bytes() != (target / p.name).read_bytes():
            raise ValueError('Secondary backup mismatch')
    return target


def write_json_durable(path, value):
    """Write `value` as JSON to a new file next to its final name, flush it to disk, then move it in."""
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as out:
        json.dump(value, out, indent=2)
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(path)


def write_completion_receipt(folder, secondary):
    """completion-receipt.json in the backup folder: where the second copy of the restore bundle is
    (`secondary_bundle`, read by chromatic_trial_runner.backup_folders, chromatic_fpga.OriginalBackup,
    hardware_preflight and the Manager core) and the SHA-256 of each bundle file in both copies. Every
    file is hashed again from disk here; a copy that differs refuses."""
    folder, secondary = Path(folder), Path(secondary)
    bundle = folder / 'restore-bundle'
    files = {}
    for path in sorted(bundle.iterdir()):
        digest = sha(path.read_bytes())
        if sha((secondary / path.name).read_bytes()) != digest:
            raise ValueError('Secondary backup mismatch')
        files[path.name] = digest
    if len(files) != 3:
        raise ValueError('Restore bundle must hold exactly three files')
    receipt = {'format': 1, 'completed_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
               'bundle': str(bundle), 'secondary_bundle': str(secondary), 'sha256': files,
               'both_copies_rehashed': True}
    write_json_durable(folder / 'completion-receipt.json', receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    parser.add_argument('--create-bundle', action='store_true')
    parser.add_argument('--second-copy', type=Path)
    args = parser.parse_args()
    folder = args.folder.resolve(strict=True)
    data = validate_reads(folder)
    print('PASS: both chips have matching complete 4 MiB reads; saved hashes and image structures validate.')
    if args.create_bundle:
        bundle = make_bundle(folder)
        print('PASS: three-file restore bundle created and read back.')
        if args.second_copy:
            target = copy_bundle(bundle, args.second_copy / folder.name)
            write_completion_receipt(folder, target)
            print('PASS: secondary local backup copy matches every byte; completion receipt written.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError, UnicodeError):
        # Do not leak identifiers/firmware strings through exception details.
        print('FAIL: backup validation incomplete or files invalid. No USB operation was performed.')
        raise SystemExit(1)

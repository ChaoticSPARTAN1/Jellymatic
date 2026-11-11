# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""The launcher's C provisioning reader (chromatic/launcher/shared/provision.h, v1 and v2,
newest wins) against the Python twin (chromatic_provision.py): every v2 test vector in
chromatic_provision_v2_vectors.json, v1 records, and sector-pair choices. Synthetic data
only; the harness prints counts and CRC-32s, never names or keys. No device."""
import json
import os
import subprocess
import zlib

import chromatic_provision as cp
from connection_config import ROOT

SOURCE = ROOT / 'chromatic/launcher'
OUT = ROOT / 'staging/chromatic-launcher'
OUT.mkdir(parents=True, exist_ok=True)  # build output, ignored by Git


def env():
    e = dict(os.environ)
    e['PATH'] = 'C:/msys64/ucrt64/bin;' + e['PATH']
    return e


def run(exe, mode, data):
    return subprocess.run([str(exe), mode], input=data, capture_output=True, check=True, env=env()).stdout.decode().strip()


def expected(record):
    names = '\n'.join(n.ssid for n in record.networks).encode('utf-8')
    port = str(record.gateway.port) if record.gateway else '-'
    key = f'{zlib.crc32(record.gateway.chr1_key) & 0xffffffff:08x}' if record.gateway else '00000000'
    url = f'{zlib.crc32(record.jellyfin.url.encode()) & 0xffffffff:08x}' if record.jellyfin else '-'
    return (f'ok version={record.version} seq={record.sequence} count={len(record.networks)} '
            f'names={zlib.crc32(names) & 0xffffffff:08x} open={sum(1 for n in record.networks if not n.psk)} '
            f'port={port} key={key} url={url}')


def main():
    exe = OUT / 'check_provision2.exe'
    subprocess.run(['C:/msys64/ucrt64/bin/gcc.exe', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                    str(SOURCE / 'check_provision2.c'), '-o', str(exe)], check=True, env=env(), cwd=SOURCE)
    # The Python twin is the reference: its live vectors, so a newly added vector is
    # checked at once even before the shared JSON copy is regenerated.
    stored = json.loads((ROOT / 'scripts/chromatic_provision_v2_vectors.json').read_text(encoding='utf-8'))
    vectors = {'vectors': cp.vectors()}
    if stored['vectors'] != vectors['vectors']:
        print('note: chromatic_provision_v2_vectors.json is older than chromatic_provision.vectors(); using the latter')
    good = refused = 0
    for vector in vectors['vectors']:
        sector = bytes.fromhex(vector['hex']).ljust(cp.SECTOR, b'\xff')
        got = run(exe, 'one', sector)
        if vector['expect'] == 'absent':
            assert got == 'absent', (vector['name'], got)
        elif vector['expect'] == 'refused':
            assert got == 'refused', (vector['name'], got)
            refused += 1
        else:
            assert got == expected(cp.parse_v2(sector)), (vector['name'], got)
            good += 1
    # Every single-byte corruption of the full record: refused by Python means refused by C.
    full = bytes.fromhex(vectors['vectors'][0]['hex']).ljust(cp.SECTOR, b'\xff')
    corrupt = 0
    for at in range(0, cp.SIZE_2, 7):
        damaged = bytearray(full)
        damaged[at] ^= 0x5a
        try:
            cp.parse_any(bytes(damaged))
            python = 'ok'
        except ValueError:
            python = 'refused'
        c = run(exe, 'one', bytes(damaged))
        assert (c == 'refused') == (python == 'refused'), (at, c, python)
        corrupt += 1
    # Version 1 reads as sequence 0 with one network and a gateway.
    v1 = cp.Provision('Old Network', 'password1', '10.77.0.5', 8443, bytes(32), bytes(range(32)), bytes(range(1, 33)))
    v1_sector = v1.sector()
    assert run(exe, 'one', v1_sector) == expected(cp.parse_any(v1_sector))
    # Newest wins; a tie goes to A; a damaged or erased sector counts as empty.
    nets = (cp.Network('Home', 'abcdefgh'),)
    older, newer = cp.Record(nets, None, None, 7).sector(), cp.Record((cp.Network('Other', ''),), None, None, 8).sector()
    tie_b = cp.Record((cp.Network('Tie B', ''),), None, None, 7).sector()
    erased = b'\xff' * cp.SECTOR
    broken = bytearray(newer)
    broken[20] ^= 1
    for a, b, want in ((older, newer, newer), (newer, older, newer), (older, tie_b, older), (erased, older, older),
                       (bytes(broken), older, older), (v1_sector, older, older), (v1_sector, erased, v1_sector)):
        assert run(exe, 'pair', a + b) == expected(cp.parse_any(want))
        record, _ = cp.current(a, b)
        assert run(exe, 'pair', a + b) == expected(record), 'C and Python pick differently'
    assert run(exe, 'pair', erased + erased) == 'none'
    print(f'PASS: launcher C provisioning reader matches the Python twin: {good} valid v2 vectors read identically, '
          f'{refused} invalid vectors refused, {corrupt} single-byte corruptions judged alike, v1 as sequence 0, '
          'newest-wins with tie to A and damaged sectors ignored. Synthetic data only.')


if __name__ == '__main__':
    main()

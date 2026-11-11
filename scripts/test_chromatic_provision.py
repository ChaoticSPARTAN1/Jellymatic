# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Provisioning record: Python writer/parser against the launcher's C parser (provision.h),
valid and tampered records, plus Chromatic Manager's handling of the sector on a
simulated console. Synthetic Wi-Fi details only; nothing is printed but pass/fail."""
import os
import struct
import subprocess
import zlib

import chromatic_manager as cm
import chromatic_provision as cp
from chromatic_trial_runner import EXIT_OK
from compose_chromatic_image import stock_dump
from connection_config import ROOT
from test_chromatic_manager import Harness, check, release_or_skip

SOURCE = ROOT / 'chromatic/launcher'
OUT = ROOT / 'staging/chromatic-launcher'
OUT.mkdir(parents=True, exist_ok=True)  # build output, ignored by Git


def sample(**changes):
    fields = dict(ssid='Test Network', psk='correct horse battery', gateway_ipv4='10.77.0.20',
                  gateway_port=8443, gateway_cert_sha256=bytes(range(32)), gateway_token=bytes(range(32, 64)),
                  chr1_key=bytes(range(64, 96)))
    fields.update(changes)
    return cp.Provision(**fields)


def c_parse(exe, sector):
    env = dict(os.environ)
    env['PATH'] = 'C:/msys64/ucrt64/bin;' + env['PATH']
    return subprocess.run([str(exe)], input=sector, capture_output=True, env=env, check=True).stdout.decode().strip()


def main():
    env = dict(os.environ)
    env['PATH'] = 'C:/msys64/ucrt64/bin;' + env['PATH']
    exe = OUT / 'check_provision.exe'
    subprocess.run(['C:/msys64/ucrt64/bin/gcc.exe', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                    str(SOURCE / 'check_provision.c'), '-o', str(exe)], check=True, env=env, cwd=SOURCE)

    good = sample().sector()
    check(cp.parse(good) == sample(), 'python round trip')
    check(c_parse(exe, good).startswith('1 12 21 8443 '), 'C parses the Python record')
    check(cp.parse(b'\xff' * 4096) is None and c_parse(exe, b'\xff' * 4096) == '0', 'erased sector is absent')
    open_net = sample(psk='').sector()
    check(cp.parse(open_net).psk == '' and c_parse(exe, open_net).startswith('1 12 0 '), 'open network')

    # Every single-byte corruption of the record is refused by both parsers.
    refused = 0
    for at in range(cp.SIZE):
        bad = bytearray(good)
        bad[at] ^= 0x5a
        try:
            cp.parse(bytes(bad))
            check(False, f'python accepted corruption at {at}')
        except ValueError:
            pass
        check(c_parse(exe, bytes(bad)) == '-1', f'C accepted corruption at {at}')
        refused += 1

    # Records with a valid checksum but invalid fields are refused too.
    def forged(body_edit):
        body = bytearray(good[:cp.BODY])
        body_edit(body)
        return bytes(body) + struct.pack('<I', zlib.crc32(bytes(body)) & 0xffffffff) + b'\xff' * (4096 - cp.SIZE)
    for label, edit in (('public gateway', lambda b: b.__setitem__(slice(106, 110), bytes([8, 8, 8, 8]))),
                        ('port zero', lambda b: b.__setitem__(slice(110, 112), b'\0\0')),
                        ('empty ssid', lambda b: b.__setitem__(8, 0)),
                        ('short password', lambda b: b.__setitem__(slice(41, 106), b'short'.ljust(65, b'\0'))),
                        ('unterminated ssid', lambda b: b.__setitem__(slice(8, 41), b'x' * 33))):
        bad = forged(edit)
        try:
            cp.parse(bad)
            check(False, f'python accepted {label}')
        except ValueError:
            pass
        check(c_parse(exe, bad) == '-1', f'C accepted {label}')
    for label, change in (('public gateway', dict(gateway_ipv4='8.8.8.8')), ('short password', dict(psk='short')),
                          ('long name', dict(ssid='x' * 33)), ('short key', dict(chr1_key=b'k'))):
        try:
            sample(**change).sector()
            check(False, f'writer accepted {label}')
        except ValueError:
            pass

    # Version 2: the vector file for the launcher's C parser matches the writer, and every
    # vector is read (or refused) as it says.
    import json
    vector_file = json.loads((ROOT / 'scripts/chromatic_provision_v2_vectors.json').read_text(encoding='utf-8'))
    check(vector_file['vectors'] == cp.vectors() and vector_file['size'] == cp.SIZE_2, 'v2 vector file is current')
    for vector in vector_file['vectors']:
        sector = bytes.fromhex(vector['hex']).ljust(4096, b'\xff')
        try:
            parsed = cp.parse_v2(sector)
            got = 'absent' if parsed is None else {
                'sequence': parsed.sequence, 'networks': [n.ssid for n in parsed.networks],
                'open_networks': [n.ssid for n in parsed.networks if not n.psk],
                'gateway_port': parsed.gateway.port if parsed.gateway else None,
                'jellyfin_url': parsed.jellyfin.url if parsed.jellyfin else None}
        except ValueError:
            got = 'refused'
        check(got == vector['expect'], f'v2 vector {vector["name"]}')
    full = cp.parse_v2(bytes.fromhex(vector_file['vectors'][0]['hex']).ljust(4096, b'\xff'))
    v2_refused = 0
    base = full.sector()
    for at in range(cp.SIZE_2):  # every single-byte corruption is refused
        bad = bytearray(base)
        bad[at] ^= 0x5a
        try:
            cp.parse_v2(bytes(bad))
            check(False, f'v2 accepted corruption at {at}')
        except ValueError:
            v2_refused += 1
    import dataclasses
    newer = dataclasses.replace(full, sequence=8).sector()
    check(cp.current(base, newer)[1] == 'B' and cp.current(newer, base)[1] == 'A', 'newest sequence wins')
    check(cp.current(base, base)[1] == 'A', 'a tie goes to A')
    torn = bytearray(newer)
    torn[100] ^= 1
    check(cp.current(base, bytes(torn))[1] == 'A' and cp.current(b'\xff' * 4096, bytes(torn)) == (None, None),
          'a torn sector falls back to the other one, or to "not set"')
    check(cp.parse_any(good).version == 1 and cp.parse_any(good).networks[0].ssid == 'Test Network', 'v1 read as v2')
    for label, change in (('five networks', dict(networks=(cp.Network('a', ''),) * 5)),
                          ('ftp jellyfin', dict(jellyfin=dataclasses.replace(full.jellyfin, url='ftp://x'))),
                          ('user id with dashes', dict(jellyfin=dataclasses.replace(full.jellyfin, user_id='a-b'))),
                          ('short token', dict(jellyfin=dataclasses.replace(full.jellyfin, access_token='abc')))):
        try:
            dataclasses.replace(full, **change).sector()
            check(False, f'v2 writer accepted {label}')
        except ValueError:
            pass

    # Manager: the setup goes to both sectors and nothing else; install and switch-off keep
    # it; remove erases it.
    stock = stock_dump(allow_synthetic=True)
    release = release_or_skip('the Manager part (install, switch-off and remove keep or erase the setup)')
    if release is None:
        return
    h = Harness(stock, release)
    check(h.manager().install() == EXIT_OK, 'install')
    installed = h.flash()
    record = dataclasses.replace(full, sequence=0)
    check(h.manager().provision(record) == EXIT_OK, 'provision')
    after = h.flash()
    check(cm.span(after, cm.PROVISION_A) == cm.span(after, cm.PROVISION_B)
          and cp.parse_v2(cm.span(after, cm.PROVISION_A)) == dataclasses.replace(record, sequence=1)
          and after[:cm.PROVISION[0]] == installed[:cm.PROVISION[0]], 'provision wrote both sectors only')
    check(h.state() == 'installed', 'still installed after provisioning')
    check(h.manager().provision(record) == EXIT_OK and h.flash() == after, 'second provision writes nothing')
    kept = cm.span(after, cm.PROVISION)
    check(h.manager().disable() == EXIT_OK and cm.span(h.flash(), cm.PROVISION) == kept, 'switch-off keeps it')
    check(h.manager().install() == EXIT_OK and cm.span(h.flash(), cm.PROVISION) == kept, 'reinstall keeps it')
    check(h.manager().remove() == EXIT_OK and h.flash() == stock, 'remove erases it: back to the exact original')
    check(h.manager().provision(cp.Record()) != EXIT_OK and h.flash() == stock, 'plain stock console: refused, install first')
    check(cm.ordered_regions(installed, after, False) == [(cm.PROVISION_A[0], cm.span(after, cm.PROVISION_A)),
                                                          (cm.PROVISION_B[0], cm.span(after, cm.PROVISION_B))],
          'the two sectors are written one at a time')

    # The real version gate: a launcher that reads only v1 (the pinned 1.1.x) is not given v2.
    import test_chromatic_manager as tm
    saved_gate, cm.PROVISION_V2_LAUNCHER = cm.PROVISION_V2_LAUNCHER, tm.REAL_V2_GATE
    try:
        check(not cm.reads_setup_v2('1.1.1') and cm.reads_setup_v2('1.1.2') and cm.reads_setup_v2('1.10.0')
              and not cm.reads_setup_v2(None) and not cm.reads_setup_v2('x'), 'launcher versions that read v2')
        h = Harness(stock, release)
        h.manager().install()
        # The release's own launcher relabelled 1.1.0, edited in place (as test_chromatic_manager does): the
        # launcher is larger than stock's 1 MiB app region, so it cannot be staged there.
        older = bytearray(release.launcher)
        older[0x20 + 16:0x20 + 48] = b'1.1.0'.ljust(32, b'\0')
        older = tm.fix_checksums(bytes(older))
        h.console.flash[cm.SLOT_APP[0]:cm.SLOT_APP[1]] = cm.put(h.flash(), cm.SLOT_APP, older)[
            cm.SLOT_APP[0]:cm.SLOT_APP[1]]
        before = h.flash()
        check(h.manager().provision(record) == cm.EXIT_REFUSED and h.flash() == before
              and any('needs launcher 1.1.2 or newer' in line for line in h.log), 'v1-only launcher 1.1.0: refused')
        h = Harness(stock, release)
        h.manager().install()
        check(h.manager().provision(record) == EXIT_OK, 'the pinned 1.1.2 launcher reads v2: allowed')
    finally:
        cm.PROVISION_V2_LAUNCHER = saved_gate
    for url in ('http://a\nb', 'http://a b', 'http://a\tb', 'http://a\x7fb'):
        try:
            dataclasses.replace(full, jellyfin=dataclasses.replace(full.jellyfin, url=url)).sector()
            check(False, f'control character accepted in {url!r}')
        except ValueError:
            pass

    # Backups and closed journals never keep the setup (Wi-Fi passwords, keys, Jellyfin token).
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        saved_dirs = cm.BACKUPS, cm.SECONDARY
        cm.BACKUPS, cm.SECONDARY = folder / 'h', folder / 'c'
        try:
            secret_image = cm.put(after, cm.PROVISION, cm.span(after, cm.PROVISION))
            manager = cm.Manager(folder=folder / 'work', say=lambda m: None)
            manager._save_backup(secret_image, {'chip': 'test'})
            for root in (cm.BACKUPS, cm.SECONDARY):
                saved_file = next(root.glob('*/esp32-flash.bin')).read_bytes()
                check(saved_file == cm.without_setup(secret_image) and cm.span(saved_file, cm.APP) == cm.span(
                    secret_image, cm.APP), f'backup on {root.name}: setup left out, the rest kept')
            check(len([name for name, _, _ in cm.backup_images() if name != 'original']) == 1,
                  'a setup-free backup still verifies')
        finally:
            cm.BACKUPS, cm.SECONDARY = saved_dirs
        h = Harness(stock, release)
        h.manager().install()
        closed = h.folder / 'work/closed'
        stale = closed / '20200101T000000-1'
        stale.mkdir(parents=True)
        os.utime(stale, (0, 0))
        check(h.manager().provision(record) == EXIT_OK, 'provision for the journal check')
        images = list(closed.glob('*/image-*.bin'))
        check(images and all(cm.span(i.read_bytes(), cm.PROVISION) == b'\xff' * (cm.PROVISION[1] - cm.PROVISION[0])
                             for i in images if i.stat().st_size == cm.SIZE), 'closed journals keep no setup')
        check(not stale.exists(), 'closed journals older than 30 days are deleted')

    # The one-question-at-a-time setup flow, with a throwaway identity and pairing paths
    # (and no Wi-Fi file, no Jellyfin settings: test_chromatic_manager keeps the owner's out).
    import tempfile
    from pathlib import Path
    import wifi_gateway
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        saved = (wifi_gateway.IDENTITY, cm.REMOTE_PILOT_PAIRING, cm.PROVISION_COPY, cm.lan_ipv4)
        wifi_gateway.IDENTITY = folder / 'identity'
        cm.REMOTE_PILOT_PAIRING, cm.PROVISION_COPY = folder / 'rp/pairing.json', folder / 'copy.json'
        cm.lan_ipv4 = lambda: '10.77.0.20'
        try:
            init = wifi_gateway.initialize
            wifi_gateway.initialize = lambda: init(wifi_gateway.IDENTITY)
            check(not cm.WIFI_FILE.exists() and not cm.jellyfin_settings_available(), 'owner files kept out')
            h = Harness(stock, release)
            h.manager().install()
            answers = iter(['yes', 'Home Wi-Fi'])
            result = cm.wifi_setup(h.manager(), lambda p: next(answers), lambda p: 'a long password')
            written = cp.current(cm.span(h.flash(), cm.PROVISION_A), cm.span(h.flash(), cm.PROVISION_B))[0]
            check(result == EXIT_OK and [n.ssid for n in written.networks] == ['Home Wi-Fi']
                  and written.gateway.ipv4 == '10.77.0.20' and written.jellyfin is None,
                  'setup flow writes the record')
            key = json.loads(cm.REMOTE_PILOT_PAIRING.read_text())['key']
            check(bytes.fromhex(key) == written.gateway.chr1_key, 'Remote Pilot gets the same pairing key')
            check(not any('a long password' in line for line in h.log), 'password never shown')
            answers = iter(['yes', 'Home Wi-Fi'])
            check(cm.wifi_setup(h.manager(), lambda p: next(answers), lambda p: 'short') != EXIT_OK,
                  'bad password refused before writing')
        finally:
            wifi_gateway.IDENTITY, cm.REMOTE_PILOT_PAIRING, cm.PROVISION_COPY, cm.lan_ipv4 = saved
            wifi_gateway.initialize = init

    print(f'PASS: provisioning record v1 (Python and C agree; {refused} corruptions and forged fields refused by '
          f'both) and v2 ({len(vector_file["vectors"])} vectors, {v2_refused} corruptions refused, newest-wins '
          'with torn-sector fallback); manager writes both sectors one at a time and nothing else, install and '
          'switch-off keep them, remove erases them. Synthetic data, no hardware.')


if __name__ == '__main__':
    main()

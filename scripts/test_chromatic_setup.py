# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""On-console setup for the Chromatic launcher: host checks of the screens, keyboard, scan list,
discovery/Quick Connect protocol and the single audited provisioning writer, plus the C record
builder and writer against the Python twin (chromatic_provision.py) on every v2 vector, source
rules for the new files, and PNG previews of every screen. Synthetic data only. No device."""
import os
from pathlib import Path
import re
import subprocess
import sys

import chromatic_provision as cp
from connection_config import ROOT

SOURCE = ROOT / 'chromatic/launcher'
OUT = ROOT / 'staging/chromatic-launcher'
OUT.mkdir(parents=True, exist_ok=True)  # build output, ignored by Git
PREVIEWS = OUT / 'setup-previews'
GCC = 'C:/msys64/ucrt64/bin/gcc.exe'
NEW_FILES = ('shared/setup_ui.h', 'shared/keyboard.h', 'shared/wifi_scan.h', 'shared/quickconnect.h',
             'shared/provision_build.h', 'shared/provision_write.h', 'shared/provision_write.c', 'main/setup_esp32.c',
             'main/setup_esp32.h')


def env():
    e = dict(os.environ)
    e['PATH'] = 'C:/msys64/ucrt64/bin;' + e['PATH']
    return e


def build():
    PREVIEWS.mkdir(parents=True, exist_ok=True)
    exe = OUT / 'check_setup.exe'
    subprocess.run([GCC, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', str(SOURCE / 'check_setup.c'),
                    str(SOURCE / 'shared/provision_write.c'), '-o', str(exe)], check=True, env=env(), cwd=SOURCE)
    return exe


def host_checks(exe):
    result = subprocess.run([str(exe), 'previews', str(PREVIEWS)], capture_output=True, text=True, env=env())
    if result.returncode:
        raise SystemExit(result.stderr.strip() or 'check_setup failed')
    print(result.stdout.strip())
    try:
        from PIL import Image
    except ImportError:
        print('note: Pillow missing, PPM previews only')
        return
    ppms = sorted(PREVIEWS.glob('ui-setup-*.ppm'))
    for ppm in ppms:
        Image.open(ppm).resize((480, 432), Image.NEAREST).save(ppm.with_suffix('.png'))
    cols, size = 5, (160 * 2 + 8, 144 * 2 + 8)
    rows = (len(ppms) + cols - 1) // cols
    sheet = Image.new('RGB', (cols * size[0], rows * size[1]), (40, 40, 40))
    for i, ppm in enumerate(ppms):
        sheet.paste(Image.open(ppm).resize((320, 288), Image.NEAREST), ((i % cols) * size[0], (i // cols) * size[1]))
    sheet.save(PREVIEWS / 'contact-sheet.png')
    print(f'PASS: {len(ppms)} setup screens rendered to {PREVIEWS} (PNG + contact-sheet.png).')


def run(exe, args, data):
    return subprocess.run([str(exe)] + args, input=data, capture_output=True, env=env())


def builder_against_python(exe):
    """Every valid Python vector, parsed by the C reader and rebuilt by provision_build.h, is the same bytes."""
    good = 0
    for vector in cp.vectors():
        if vector['expect'] not in ('absent', 'refused'):
            sector = bytes.fromhex(vector['hex']).ljust(cp.SECTOR, b'\xff')
            result = run(exe, ['build'], sector)
            if result.returncode or result.stdout != sector:
                raise SystemExit(f'C builder differs from Python on vector {vector["name"]}')
            good += 1
    # A record of our own with every field used, both ways.
    record = cp.Record((cp.Network('Home', 'password1'), cp.Network('Open cafe', '')),
                       cp.Gateway('10.77.0.9', 4443, b'\x11' * 32, b'\x22' * 32, b'\x33' * 32),
                       cp.Jellyfin('http://192.0.2.20:8096', '0123456789abcdef0123456789abcdef',
                                   'deadbeefcafef00ddeadbeefcafef00d', 'chromatic-a0a1a2a3a4a5a6a7a8a9aaab'), 7)
    if run(exe, ['build'], record.sector()).stdout != record.sector():
        raise SystemExit('C builder differs from Python on the full record')
    print(f'PASS: the C record builder reproduces the Python twin byte for byte on {good} vectors and a full record.')


def writer_against_python(exe):
    """Each on-console edit, applied by the C writer to sectors the Manager wrote, reads back in Python as expected."""
    base = cp.Record((cp.Network('Net0', 'password0'), cp.Network('Net1', 'password1')),
                     cp.Gateway('10.77.0.9', 4443, b'\x11' * 32, b'\x22' * 32, b'\x33' * 32),
                     cp.Jellyfin('http://192.0.2.20:8096', '0123456789abcdef0123456789abcdef',
                                 'tokentokentokentoken', 'chromatic-000000000000000000000000'), 5)
    new_jf = cp.Jellyfin('http://192.0.2.20:8096', '0123456789abcdef0123456789abcdef',
                         'deadbeefcafef00ddeadbeefcafef00d', 'chromatic-a0a1a2a3a4a5a6a7a8a9aaab')
    cases = {
        'add': (base.networks + (cp.Network('Cafe', 'latte-latte'),), base.jellyfin),
        'change': ((cp.Network('Net0', 'changed-password'), base.networks[1]), base.jellyfin),
        'forget': ((base.networks[1],), base.jellyfin),
        'first': ((base.networks[1], base.networks[0]), base.jellyfin),  # "Use this network now": Net1 to the top
        'jellyfin': (base.networks, new_jf),
        'clearjf': (base.networks, None),
        # 1.3.0 "Find my server again": only the address; the token, user and device id are the record's own
        'address': (base.networks, cp.Jellyfin('http://192.0.2.77:8096', base.jellyfin.user_id,
                                               base.jellyfin.access_token, base.jellyfin.device_id)),
    }
    layouts = {'both': (base.sector(), base.sector()), 'only-a': (base.sector(), b'\xff' * cp.SECTOR),
               'only-b': (b'\xff' * cp.SECTOR, base.sector()),
               'b-newer': (cp.Record(base.networks, base.gateway, base.jellyfin, 4).sector(), base.sector())}
    checked = 0
    for op, (networks, jellyfin) in cases.items():
        for name, (a, b) in layouts.items():
            result = run(exe, ['commit', op], a + b)
            if result.returncode or len(result.stdout) != 2 * cp.SECTOR:
                raise SystemExit(f'commit {op} on {name} failed: {result.stderr.decode(errors="replace").strip()}')
            got, where = cp.current(result.stdout[:cp.SECTOR], result.stdout[cp.SECTOR:])
            expected = cp.Record(networks, base.gateway, jellyfin, 6)
            if got != expected:
                raise SystemExit(f'commit {op} on {name}: Python reads a different record')
            other = result.stdout[cp.SECTOR:] if where == 'A' else result.stdout[:cp.SECTOR]
            if other != b'\xff' * cp.SECTOR:
                raise SystemExit(f'commit {op} on {name}: the old sector still holds data')
            checked += 1
    # No change: nothing written, both sectors as they were.
    result = run(exe, ['commit', 'nochange'], base.sector() * 2)
    if result.returncode or result.stdout != base.sector() * 2 or b'erases=0 writes=0' not in result.stderr:
        raise SystemExit('a no-op commit wrote something')
    # From nothing: sequence 1, only the new network.
    result = run(exe, ['commit', 'add'], b'\xff' * (2 * cp.SECTOR))
    got, where = cp.current(result.stdout[:cp.SECTOR], result.stdout[cp.SECTOR:])
    if got != cp.Record((cp.Network('Cafe', 'latte-latte'),), None, None, 1) or where != 'A':
        raise SystemExit('a first record is wrong')
    print(f'PASS: {checked} on-console edits by the C writer read back in Python as the expected record, sequence + 1, '
          'old sector erased; a no-op writes nothing; a first record starts at sequence 1 in A.')


def snprintf_calls(line):
    """The argument text of every snprintf( ... ) on a line."""
    calls, at = [], 0
    while (at := line.find('snprintf(', at)) >= 0:
        depth, start = 1, at + len('snprintf(')
        end = start
        while end < len(line) and depth:
            depth += {'(': 1, ')': -1}.get(line[end], 0)
            end += 1
        calls.append(line[start:end - 1])
        at = end
    return calls


def source_rules():
    """The single writer is the only place that erases or writes flash; the binding and screens never log secrets."""
    texts = {name: (SOURCE / name).read_text(encoding='utf-8') for name in NEW_FILES}
    code = {name: re.sub(r'/\*.*?\*/', '', text, flags=re.S) for name, text in texts.items()}
    writer = code['shared/provision_write.c']
    for name, text in code.items():
        if name == 'shared/provision_write.c':
            continue
        for forbidden in ('esp_flash_write', 'esp_flash_erase', 'esp_partition_write', 'esp_partition_erase', 'spi_flash_write',
                          'spi_flash_erase', 'nvs_flash_init', 'nvs_open', 'nvs_set', 'esp_ota_', 'bootloader_flash_write',
                          'esp_rom_spiflash', 'SPIWrite', 'SPIErase', 'esp_wifi_set_storage(WIFI_STORAGE_FLASH'):
            if forbidden in text:
                raise SystemExit(f'{name} must not contain {forbidden}')
    # The writer: exactly one erase and one write call, each inside a wrapper that checks the address first,
    # and the only entry points are the two commit functions.
    if writer.count('esp_flash_erase_region(') != 1 or writer.count('esp_flash_write(') != 1:
        raise SystemExit('provision_write.c must call esp_flash_erase_region and esp_flash_write exactly once each')
    for call in ('esp_flash_erase_region(', 'esp_flash_write(', 'esp_flash_read('):
        line = next(l for l in writer.splitlines() if call in l)
        if 'if(!pj_pw_allowed(offset,size))return -1;' not in line or not line.index('pj_pw_allowed') < line.index(call):
            raise SystemExit(f'{call} is not guarded by the address check on the same line')
    if 'return (offset==PJ_PROV_FLASH_A||offset==PJ_PROV_FLASH_B)&&size==PJ_PW_SECTOR;' not in writer:
        raise SystemExit('pj_pw_allowed changed')
    if not re.search(r'#define PJ_PROV_FLASH_A \(PJ_PROV_FLASH_SLOT\+PJ_PROVISION_OFFSET_A\)', code['shared/provision_write.h']) \
            or '#define PJ_PROV_FLASH_SLOT 0x260000u' not in code['shared/provision_write.h']:
        raise SystemExit('the writer addresses moved')
    functions = re.findall(r'^(?:static )?int (\w+)\(', writer, flags=re.M)
    if set(functions) != {'pj_pw_allowed', 'pj_pw_erased', 'pj_pw_same_record', 'pj_provision_commit_with', 'pj_pw_dev_read',
                          'pj_pw_dev_erase', 'pj_pw_dev_write', 'pj_provision_commit'}:
        raise SystemExit(f'provision_write.c grew functions: {functions}')
    # The screens and binding format no password or token into any text but the record and the Authorization header.
    for name in ('shared/setup_ui.h', 'main/setup_esp32.c', 'shared/quickconnect.h', 'shared/keyboard.h'):
        for line in code[name].splitlines():
            if re.search(r'(printf|pj_log_add|diag|ESP_LOG)\s*\(', line) and name != 'shared/quickconnect.h':
                if 'snprintf' not in line:
                    raise SystemExit(f'{name} prints: {line.strip()}')
            for call in snprintf_calls(line):
                dest = call.split(',', 1)[0].strip()
                if re.search(r'\b(psk|token|secret|access_token|kb\.text|k->text)\b', call) \
                        and not re.match(r'(s->edit\.|s->fresh\.|q->|out\b|out->|url\b|k->text\b)', dest):  # url, k->text: the field itself
                    raise SystemExit(f'{name} formats a secret into {dest}: {line.strip()}')
    # The password field is drawn only through pj_kb_revealed, which is false while Remote Pilot watches.
    kb = code['shared/keyboard.h']
    if 'if(pj_kb_revealed(k,watched)) {' not in kb or 'return !k->secret||(k->show&&!watched);' not in kb:
        raise SystemExit('keyboard reveal rule changed')
    # Logout is the only session call, and it is sent with this console\'s own token only.
    qc = code['shared/quickconnect.h']
    if qc.count('/Sessions/') != 1 or '/Sessions/Logout' not in qc or 'Sessions/Logout/All' in qc or '/Devices' in qc or '/Auth/Keys' in qc:
        raise SystemExit('Quick Connect module may only log out this console\'s own session')
    paths = set(re.findall(r'"(/[A-Za-z/]+(?:\?[a-z]+=)?)', qc))
    if paths != {'/QuickConnect/Enabled', '/QuickConnect/Initiate', '/QuickConnect/Connect?secret=',
                 '/Users/AuthenticateWithQuickConnect', '/Sessions/Logout', '/Users/', '/System/Info/Public'}:
        raise SystemExit(f'Quick Connect requests changed: {sorted(paths)}')
    # 1.3.0 Find my server again: the check is one GET of this console's own user (no POST, no session call).
    if qc.count('"/Users/%s",q->user_id') != 1 or 'case PJ_QC_VERIFY:out->method="GET";' not in qc \
            or 'case PJ_QC_INFO:out->method="GET";snprintf(out->path,sizeof(out->path),"/System/Info/Public");break;' not in qc:
        raise SystemExit('the Find my server again check must be a single GET of this console user')
    # Its commit changes the address only: set_address never carries a token (the writer keeps the record's own).
    ui = code['shared/setup_ui.h']
    find = ui[ui.index('if(s->find_verify&&!s->qc.pending)'):ui.index('if(pj_qc_request(&s->qc,now,&s->http))s->request=PJ_SU_REQ_HTTP;')]
    if 's->edit.set_address=1;' not in find or 'access_token' in find or 'set_jellyfin' in find:
        raise SystemExit('Find my server again must commit set_address with the address and user id only')
    w = code['shared/provision_write.c']
    block = w[w.index('if(edit->set_address)'):w.index('if(edit->clear_jellyfin)')]
    if 'access_token' in block or 'device_id' in block or 'strcmp(next.user_id,edit->user_id)' not in block:
        raise SystemExit('set_address may change the address only, for the same user')
    # The ESP32 binding: scan with RAM storage and no NVS when it starts the driver itself, no connect of its own.
    esp = code['main/setup_esp32.c']
    if 'esp_wifi_connect(' in esp or 'config.nvs_enable=0;' not in esp or esp.count('esp_wifi_init(') != 1 \
            or 'esp_wifi_set_storage(WIFI_STORAGE_RAM)' not in esp:
        raise SystemExit('setup_esp32.c Wi-Fi use changed')
    if esp.count('pj_provision_commit(') != 1:
        raise SystemExit('the commit must be called from one place in the binding')
    print('PASS: provision_write.c is the only file that erases or writes flash (one call each, address-checked first); '
          'screens and binding format no secret; password reveal gated on Remote Pilot; Quick Connect requests fixed, '
          'Logout of this console only; scan uses RAM storage and no NVS.')


def main():
    exe = build()
    host_checks(exe)
    builder_against_python(exe)
    writer_against_python(exe)
    source_rules()


if __name__ == '__main__':
    sys.exit(main())

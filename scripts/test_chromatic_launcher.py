# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Host logic, composer refusals, flash guard and source rules. No device access."""
import hashlib
import os
from pathlib import Path
from user_paths import TOOLS  # noqa: E402  (per-user folder, see user_paths.py)
import re
import struct
import subprocess

from compose_chromatic_image import (BOOTLOADER_LIMIT, BUILD, FLASH_SIZE, Refused, TABLE_AT, TABLE_SECTOR, check_image,
                                     compose, stock_dump)
from connection_config import ROOT

SOURCE = ROOT / 'chromatic/launcher'
# LAUNCHER_BUILD checks a candidate built with Build-ChromaticLauncher.ps1 -OutDir (its build folder), so the
# reviewed image Chromatic Manager installs from the default build folder is never rebuilt to test another.
if os.environ.get('LAUNCHER_BUILD'):
    BUILD = Path(os.environ['LAUNCHER_BUILD']).resolve()
OUT = ROOT / 'staging/chromatic-launcher'
OUT.mkdir(parents=True, exist_ok=True)  # build output, ignored by Git


def host_logic():
    env = dict(os.environ)
    env['PATH'] = 'C:/msys64/ucrt64/bin;' + env['PATH']
    exe = OUT / 'check_launcher.exe'
    subprocess.run(['C:/msys64/ucrt64/bin/gcc.exe', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                    str(SOURCE / 'check_launcher.c'), '-o', str(exe)], check=True, env=env, cwd=SOURCE)
    sample = OUT / 'diag-sample.txt'
    print(subprocess.run([str(exe)] + [str(OUT / f'ui-{name}.ppm') for name in ('menu', 'menu-jellyfin', 'media', 'other')]
                         + [str(sample)], check=True, env=env,
                         capture_output=True, text=True).stdout.strip())
    diagnostics_reach_tools(sample.read_bytes())


def diagnostics_reach_tools(sample):
    """The diagnostics a modeled boot prints must not upset Chromatic Manager's boot
    watch, and must pass Remote Pilot's serial rules unredacted (read-only checks)."""
    from chromatic_manager import launcher_boot_ok
    lines = sample.decode('ascii').splitlines()
    assert lines[-1] == 'PJ-LAUNCHER-TO-STOCK' and all(line.startswith('PJ-DIAG t=') for line in lines[:-1])
    version = re.search(r'#define PJ_LAUNCHER_VERSION "([0-9.]+)"',
                        (SOURCE / 'main/launcher_main.c').read_text(encoding='utf-8')).group(1)
    log = (b'PJ-LAUNCHER-READY ' + version.encode('ascii') + b'\r\n' + sample.replace(b'\n', b'\r\n')
           + b'rst:0xc (SW_CPU_RESET)\r\nstock\r\n')
    if not launcher_boot_ok(log, True):
        raise SystemExit('Diagnostics lines break the Manager boot watch')
    # Same patterns as RemotePilot/chromatic/serial_transport.py (_MAC, _LONG_HEX).
    mac = re.compile(r'\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b')
    long_hex = re.compile(r'\b(?:0x)?[0-9A-Fa-f]{16,}\b')
    for line in lines[:-1]:
        if len(line) > 240 or mac.search(line) or long_hex.search(line) or line.startswith(('PJ-PAIR', 'PJ-PROVISION', 'PJ-WIFI')):
            raise SystemExit(f'Remote Pilot would cut or redact a diagnostics line: {line}')
    print(f'PASS: {len(lines) - 1} diagnostics lines from a modeled boot keep the Manager boot watch passing and '
          'reach Remote Pilot unredacted.')


def image(body=b'\x11' * 64, flags=b'\x02\x20'):
    """A minimal valid ESP32 image, for refusal tests that need no real firmware."""
    header = bytes([0xe9, 1]) + flags + struct.pack('<I', 0x40080000) + bytes(4) + struct.pack('<H', 0) + bytes(9) + b'\x01'
    data = header + struct.pack('<II', 0x3ffb0000, len(body)) + body
    checksum = 0xef
    for byte in body:
        checksum ^= byte
    data += bytes(15 - len(data) % 16) + bytes([checksum])
    return data + hashlib.sha256(data).digest()


def table(rows):
    data = b''
    for kind, subtype, offset, size, label in rows:
        data += b'\xaa\x50' + bytes([kind, subtype]) + struct.pack('<II', offset, size)
        data += label.encode().ljust(16, b'\0') + bytes(4)
    data += b'\xeb\xeb' + b'\xff' * 14 + hashlib.md5(data).digest()
    return data.ljust(0xc00, b'\xff')


STOCK = [(1, 2, 0x9000, 0x6000, 'nvs'), (1, 1, 0xf000, 0x1000, 'phy_init'), (0, 0, 0x10000, 0x100000, 'factory')]
SLOT = (0, 0x10, 0x260000, 0x1a0000, 'launcher')


def fake_stock():
    flash = bytearray(b'\xff' * FLASH_SIZE)
    boot = image(b'\x22' * 32)
    flash[0x1000:0x1000 + len(boot)] = boot
    flash[TABLE_AT:TABLE_AT + TABLE_SECTOR] = table(STOCK).ljust(TABLE_SECTOR, b'\xff')
    flash[0x9000:0xa000] = b'\x5a' * 0x1000  # stand-in settings
    app = image(b'\x33' * 256)
    flash[0x10000:0x10000 + len(app)] = app
    flash[0x100000:0x255000] = b'\x77' * (0x255000 - 0x100000)  # unreferenced data
    return bytes(flash)


def refused(stock, boot, part, app, why):
    try:
        compose(stock, boot, part, app)
    except Refused as error:
        if why not in str(error):
            raise SystemExit(f'Refused for the wrong reason: {error} (wanted {why})')
        return
    raise SystemExit(f'Composer accepted a case it must refuse: {why}')


def composer_refusals():
    stock = fake_stock()
    boot, part, app = image(b'\x44' * 64), table(STOCK + [SLOT]), image(b'\x55' * 128)
    result, _, evidence = compose(stock, boot, part, app)
    assert result[0x9000:0x110000] == stock[0x9000:0x110000]
    assert result[0x110000:0x260000] == stock[0x110000:0x260000]
    assert all(evidence['identical_to_backup'].values())
    refused(stock, boot, table(STOCK + [(0, 0x10, 0x200000, 0x60000, 'launcher')]), app, 'overwrite existing')
    refused(stock, boot, table(STOCK + [(0, 0x10, 0x100000, 0x100000, 'launcher')]), app, 'overlap')
    refused(stock, boot, table(STOCK + [(0, 0x10, 0x3f0000, 0x20000, 'launcher')]), app, 'misplaced')
    moved = [(1, 2, 0x9000, 0x5000, 'nvs')] + STOCK[1:]
    refused(stock, boot, table(moved + [SLOT]), app, 'stock partition rows')
    refused(stock, boot, table(STOCK), app, 'one added launcher')
    refused(stock, boot, table(STOCK + [SLOT, (1, 0, 0x250000, 0x2000, 'otadata')]), app, 'one added launcher')
    bad_md5 = bytearray(part)
    bad_md5[4 * 32 + 20] ^= 1
    refused(stock, boot, bytes(bad_md5), app, 'MD5')
    refused(stock, image(b'\x44' * 64, b'\x00\x20'), part, app, 'flash mode')
    refused(stock, boot, part, image(b'\x55' * 128, b'\x02\x2f'), 'Launcher flash mode')
    refused(stock, image(b'\x44' * (BOOTLOADER_LIMIT + 16)), part, app, 'partition table')
    corrupt = bytearray(app)
    corrupt[40] ^= 1
    refused(stock, boot, part, bytes(corrupt), 'launcher: checksum')
    bad_hash = bytearray(app)
    bad_hash[-1] ^= 1
    refused(stock, boot, part, bytes(bad_hash), 'launcher: SHA-256')
    damaged = bytearray(stock)
    damaged[0x10000 + 40] ^= 1
    refused(bytes(damaged), boot, part, app, 'stock firmware: checksum')
    refused(stock[:-1], boot, part, app, '4 MiB')
    print('PASS: composer accepts the valid layout and refused all 14 unsafe cases.')


def single_writer_rules():
    """The launcher's one flash write: shared/provision_write.c and nothing else may erase or
    write flash, exactly once each, address-checked on the same line, only the two provisioning sectors.
    Every other .c/.h under chromatic/launcher is checked; the screens hide a password from Remote Pilot."""
    writer = SOURCE / 'shared/provision_write.c'
    for path in list(SOURCE.rglob('*.c')) + list(SOURCE.rglob('*.h')):
        if path == writer or 'build' in path.parts:
            continue
        body = re.sub(r'/\*.*?\*/', '', path.read_text(encoding='utf-8', errors='replace'), flags=re.S)
        for forbidden in ('esp_flash_write', 'esp_flash_erase', 'esp_partition_write', 'esp_partition_erase', 'spi_flash_write',
                          'spi_flash_erase', 'nvs_flash_init', 'nvs_open', 'nvs_set', 'esp_ota_', 'bootloader_flash_write',
                          'esp_flash_app_disable', 'region_protected', 'esp_flash_default_chip', 'os_func'):
            if forbidden in body:
                raise SystemExit(f'{path.relative_to(ROOT)} must not contain {forbidden}: only provision_write.c writes flash')
    code = re.sub(r'/\*.*?\*/', '', writer.read_text(encoding='utf-8'), flags=re.S)
    if code.count('esp_flash_erase_region(') != 1 or code.count('esp_flash_write(') != 1 or 'esp_flash_erase_chip' in code:
        raise SystemExit('provision_write.c must call esp_flash_erase_region and esp_flash_write exactly once each')
    for call in ('esp_flash_erase_region(', 'esp_flash_write(', 'esp_flash_read('):
        line = next(l for l in code.splitlines() if call in l)
        if 'if(!pj_pw_allowed(offset,size))return -1;' not in line or line.index('pj_pw_allowed') > line.index(call):
            raise SystemExit(f'{call} in provision_write.c is not guarded by the address check first')
    if 'return (offset==PJ_PROV_FLASH_A||offset==PJ_PROV_FLASH_B)&&size==PJ_PW_SECTOR;' not in code:
        raise SystemExit('provision_write.c address check changed')
    # Launcher 1.2.9: the move-to-top edit (Use this network now) is the fourth and last network op; only SAVE copies the
    # edit's network (with its password) into the record, FIRST only reorders the record's own entries.
    header = (SOURCE / 'shared/provision_write.h').read_text(encoding='utf-8')
    if 'enum {PJ_PROV_NET_KEEP,PJ_PROV_NET_SAVE,PJ_PROV_NET_FORGET,PJ_PROV_NET_FIRST};' not in header:
        raise SystemExit('the network edit ops changed')
    if code.count('next.net[at]=edit->net;') != 1 or code.count('edit->net.psk') != 0 or 'pj_prov_network first=next.net[at];' not in code:
        raise SystemExit('the writer must copy the edit network once (SAVE) and reorder from the record for FIRST')
    # Launcher 1.3.0 "Find my server again": the new edit op set_address replaces the address ONLY, only for the same
    # user, and never together with a sign-in change; it copies no token or device id (the record keeps its own).
    if 'uint8_t set_address;' not in header:
        raise SystemExit('the set_address edit op is missing from provision_write.h')
    block = code[code.index('if(edit->set_address)'):code.index('if(edit->clear_jellyfin)')]
    if ('strcmp(next.user_id,edit->user_id)' not in block or 'edit->set_jellyfin||edit->clear_jellyfin||!next.has_jellyfin' not in block
            or 'access_token' in block or 'device_id' in block or 'pj_prov_url(edit->url)' not in block
            or block.count('next.url') != 3 or 'memchr(edit->url,0,sizeof(edit->url))' not in block):
        raise SystemExit('set_address must change only the address, for the same user, with no other sign-in field')
    if code.count('edit->set_address') != 1:
        raise SystemExit('set_address is handled in one place')
    # The two sectors sit inside the running launcher partition, which IDF refuses (abort) on the default
    # chip. The erase and write go through a stack copy of the main chip whose hook table differs only in
    # region_protected, which passes exactly sector A or B, whole. Reads stay on the default chip, and the
    # no-OS hooks (esp_flash_app_disable_protect: other CPU left running with its cache off) are never used.
    if ('esp_flash_app_disable' in code or 'DANGEROUS_WRITE' in code or code.count('region_protected') != 1
            or 'device.os.region_protected=pj_pw_region;' not in code
            or 'return ((start==PJ_PROV_FLASH_A||start==PJ_PROV_FLASH_B)&&size==PJ_PW_SECTOR)?ESP_OK:ESP_ERR_NOT_SUPPORTED;' not in code
            or 'device.os=*esp_flash_default_chip->os_func;' not in code or 'device.chip.os_func=&device.os;' not in code
            or 'esp_flash_erase_region(&((pj_pw_device*)ctx)->chip,offset,size)' not in code
            or 'esp_flash_write(&((pj_pw_device*)ctx)->chip,data,offset,size)' not in code
            or 'esp_flash_read(NULL,out,offset,size)' not in code or re.search(r'^static [^(]*pj_pw_device', code, flags=re.M)):
        raise SystemExit('provision_write.c must erase/write through the narrowed stack chip copy only (region_protected = A or B)')
    sdk = (SOURCE / 'sdkconfig.defaults').read_text(encoding='utf-8')
    if 'DANGEROUS_WRITE_ALLOWED=y' in sdk or 'DANGEROUS_WRITE_FAILS=y' in sdk:
        raise SystemExit('sdkconfig.defaults must keep IDF\'s dangerous-write protection (aborts) for every other path')
    built = BUILD.parent / 'sdkconfig'
    if built.is_file():
        text = built.read_text(encoding='utf-8')
        if 'CONFIG_SPI_FLASH_DANGEROUS_WRITE_ABORTS=y' not in text or 'CONFIG_SPI_FLASH_DANGEROUS_WRITE_ALLOWED=y' in text:
            raise SystemExit('the built sdkconfig must keep CONFIG_SPI_FLASH_DANGEROUS_WRITE_ABORTS')
    header = (SOURCE / 'shared/provision_write.h').read_text(encoding='utf-8')
    if ('#define PJ_PROV_FLASH_SLOT 0x260000u' not in header or '(PJ_PROV_FLASH_SLOT+PJ_PROVISION_OFFSET_A)' not in header
            or '#define PJ_PROVISION_OFFSET_A 0x19e000u' not in (SOURCE / 'shared/provision.h').read_text(encoding='utf-8')):
        raise SystemExit('provisioning sector addresses moved (must be 0x3FE000 and 0x3FF000)')
    # The commit is reached from one place in the binding, and the screens are drawn with Remote Pilot's
    # connection state so a revealed password never reaches the screen action.
    main_code = re.sub(r'/\*.*?\*/', '', (SOURCE / 'main/launcher_main.c').read_text(encoding='utf-8'), flags=re.S)
    binding = re.sub(r'/\*.*?\*/', '', (SOURCE / 'main/setup_esp32.c').read_text(encoding='utf-8'), flags=re.S)
    if binding.count('pj_provision_commit(') != 1 or 'pj_provision_commit' in main_code:
        raise SystemExit('pj_provision_commit must be called from setup_esp32.c only, once')
    if 'pj_setup_draw(frame,setup,rmt.usb.state==PJ_LS_READY||rmt.lan.state==PJ_LS_READY,' not in main_code:
        raise SystemExit('the setup screens must be drawn with the Remote Pilot connection state')
    # Static DRAM: the setup is heap-only while open. Its files and hooks may declare no static arrays or
    # structs, only pointers, ints and flags; every SETUP HOOK in the shell sits behind PJ_ENABLE_SETUP,
    # whose default is 0 in the launcher CMakeLists (shipped builds carry no setup unless flipped there).
    for name in ('main/setup_esp32.c', 'shared/provision_write.c'):
        body = re.sub(r'/\*.*?\*/', '', (SOURCE / name).read_text(encoding='utf-8'), flags=re.S)
        for decl in re.findall(r'^static (?!inline|int |void |const char|volatile int|uint32_t |pj_setup_work \*)[^;(]*;', body, flags=re.M):
            raise SystemExit(f'{name} keeps static data: {decl.strip()}')
        if re.search(r'^static [^(;]*\[', body, flags=re.M):
            raise SystemExit(f'{name} keeps a static array')
    hook_statics = re.findall(r'^static [^;(]*\bsetup\w*\b[^;(]*;', main_code, flags=re.M)
    if hook_statics != ['static pj_setup *setup;'] or 'static pj_setup *setup;static uint8_t setup_refused;' not in main_code:
        raise SystemExit(f'setup statics in launcher_main.c changed: {hook_statics}')
    if 'static uint8_t sa[' in main_code or 'pj_setup setup;' in main_code:
        raise SystemExit('setup state must be heap-allocated while open, never static')
    main_text = (SOURCE / 'main/launcher_main.c').read_text(encoding='utf-8')
    if main_text.count('#if PJ_ENABLE_SETUP') != 5 or 'SETUP HOOK' not in main_text \
            or '#if PJ_ENABLE_SETUP\n#define PJ_SETUP_IS_OPEN (launcher.setup_open)\n#else\n#define PJ_SETUP_IS_OPEN 0\n#endif' \
            not in main_text.replace('\r\n', '\n'):
        raise SystemExit('every SETUP HOOK in launcher_main.c must sit behind #if PJ_ENABLE_SETUP (five blocks, PJ_SETUP_IS_OPEN 0 without it)')
    # While the setup is open the shell starts neither a radio task (the setup's scan may hold the driver)
    # nor the Jellyfin app (it would run under the setup's memory and take its buttons).
    # 1.3.4: the radio start condition is pj_radio_start_wanted (launcher_logic.h, host-tested with A pressed mid-join),
    # called in one place with the setup's open and busy flags.
    radio_start = [l for l in main_code.splitlines() if 'pj_radio_start_wanted(' in l]
    logic_code = (SOURCE / 'shared/launcher_logic.h').read_text(encoding='utf-8')
    app_start = next(l for l in main_code.splitlines() if 'record_jellyfin&&wifi_state==2&&!pj_jf_active()' in l)
    if (radio_start != ['        if(pj_radio_start_wanted(&launcher,record_wifi,radio_running,radio_done,PJ_SETUP_IS_OPEN,'
                        'rejoin_from_setup,rejoin_wanted,PJ_SETUP_IS_BUSY)) {']
            or '&&!radio_running&&!radio_done&&(!setup_open||rejoin_from_setup)' not in logic_code
            or '&&!rejoin_wanted&&!setup_busy;' not in logic_code or '!PJ_SETUP_IS_OPEN' not in app_start):
        raise SystemExit('the radio tasks and the Jellyfin app must not start while the setup is open')
    # Wi-Fi driver state: the netif layer, event loop and station interface are made once and kept, so
    # whichever of the radio check, the join and the setup's scan comes first, the others still work.
    if (main_code.count('wifi_base()') != 2 or main_code.count('esp_netif_create_default_wifi_sta(') != 1
            or 'if(!esp_netif_get_handle_from_ifkey("WIFI_STA_DEF"))esp_netif_create_default_wifi_sta();' not in main_code
            or 'e!=ESP_OK&&e!=ESP_ERR_INVALID_STATE' not in main_code):
        raise SystemExit('radio_task and join_task must share wifi_base (made-once netif, event loop and station)')
    if (binding.count('esp_netif_create_default_wifi_sta(') != 1
            or 'if(!esp_netif_get_handle_from_ifkey("WIFI_STA_DEF"))esp_netif_create_default_wifi_sta();' not in binding
            or 'radio_started' in binding or 'esp_wifi_get_mode(&mode)' not in binding or 'if(radio_busy)goto out;' not in binding
            or 'esp_event_loop_delete_default' in binding or 'esp_netif_destroy' in binding):
        raise SystemExit('setup_esp32.c must scan by the driver\'s real state and keep the shared netif and event loop')
    logic = (SOURCE / 'shared/launcher_logic.h').read_text(encoding='utf-8')
    if logic.count('#if PJ_ENABLE_SETUP') != 3 or '#define PJ_ENABLE_SETUP 0' not in logic:
        raise SystemExit('launcher_logic.h setup hooks must sit behind PJ_ENABLE_SETUP, default 0')
    cmake = (SOURCE / 'CMakeLists.txt').read_text(encoding='utf-8')
    switch = re.search(r'^set\(PJ_ENABLE_SETUP ([01])\)$', cmake, flags=re.M)
    if not switch:
        raise SystemExit('chromatic/launcher/CMakeLists.txt must set PJ_ENABLE_SETUP to 0 or 1')
    print(f'note: PJ_ENABLE_SETUP is {switch.group(1)} in the launcher CMakeLists (0 ships without the on-console setup).')
    if 'if(PJ_ENABLE_SETUP)' not in (SOURCE / 'main/CMakeLists.txt').read_text(encoding='utf-8'):
        raise SystemExit('main/CMakeLists.txt must add setup_esp32.c and provision_write.c only when PJ_ENABLE_SETUP')
    if 'esp_wifi_connect(' in binding or binding.count('esp_wifi_init(') != 1 or 'config.nvs_enable=0;' not in binding \
            or 'esp_wifi_set_storage(WIFI_STORAGE_RAM)' not in binding:
        raise SystemExit('setup_esp32.c may scan but never join, with RAM storage and NVS off')


def linked_writer_callers():
    """In the linked program, esp_flash_write and esp_flash_erase_region may be reached only from the
    audited writer's functions. Xtensa calls across IRAM/flash go through literals, so the check reads
    the relocations of every object in every built archive, names the function (section) that holds
    each reference, and refuses any such function that is linked into the ELF unless it is the
    writer's. With PJ_ENABLE_SETUP=0 neither symbol is linked at all, which also passes."""
    tools = Path(TOOLS / 'esp32-tools/tools/xtensa-esp-elf')
    objdump = next(tools.rglob('xtensa-esp32-elf-objdump.exe'))
    nm = next(tools.rglob('xtensa-esp32-elf-nm.exe'))
    symbols = subprocess.run([str(nm), str(BUILD / 'chromatic_launcher.elf')], capture_output=True, text=True, check=True).stdout
    defined = {line.split()[-1] for line in symbols.splitlines() if len(line.split()) == 3 and line.split()[1] in 'TtWw'}
    targets = ('esp_flash_write', 'esp_flash_erase_region')
    linked_targets = [t for t in targets if t in defined]
    allowed = {'pj_provision_commit', 'pj_provision_commit_with', 'pj_pw_dev_erase', 'pj_pw_dev_write'}
    holders, member, section = {}, None, None
    for archive in sorted((BUILD / 'esp-idf').rglob('lib*.a')):
        listing = subprocess.run([str(objdump), '-r', str(archive)], capture_output=True, text=True, errors='replace').stdout
        for line in listing.splitlines():
            if line.endswith('file format elf32-xtensa-le'):
                member = line.split(':')[0]
            elif line.startswith('RELOCATION RECORDS FOR ['):
                section = line[len('RELOCATION RECORDS FOR ['):-2]
            elif line.split() and line.split()[-1] in targets:
                holders.setdefault((archive.name, member, section), set()).add(line.split()[-1])
    offenders, writer_refs = [], 0
    for (archive, member, section), refs in holders.items():
        function = re.sub(r'^\.(?:text|literal)\.', '', section)
        if member == 'provision_write.c.obj' and function in allowed:
            writer_refs += 1
            continue
        if archive == 'libspi_flash.a' and member == 'esp_flash_api.c.obj':
            continue  # the driver's own code (esp_flash_erase_chip etc.); esp_flash_erase_chip is refused above by name
        if function in defined:
            offenders.append(f'{archive}({member}) {function} -> {", ".join(sorted(refs))}')
    if offenders:
        raise SystemExit('flash write/erase reachable outside the audited writer: ' + '; '.join(offenders))
    if linked_targets and (writer_refs < 2 or 'pj_provision_commit_with' not in defined):
        raise SystemExit(f'{linked_targets} linked but the writer references were not found: check the scan')
    if not linked_targets and 'pj_provision_commit_with' in defined:
        raise SystemExit('the writer is linked without esp_flash_write/erase: check the scan')
    print('PASS: ' + ('esp_flash_write/esp_flash_erase_region referenced only from the audited provisioning writer in the linked '
                      f'launcher (PJ_ENABLE_SETUP=1 build; {len(holders)} referencing functions scanned across all archives).'
                      if linked_targets else 'no flash write/erase linked (PJ_ENABLE_SETUP=0 build, or an older ELF); '
                      'the caller check is armed for a setup build.'))


def source_rules():
    text = (SOURCE / 'main/launcher_main.c').read_text(encoding='utf-8')
    text += (SOURCE / 'shared/launcher_logic.h').read_text(encoding='utf-8')
    text += (SOURCE / 'shared/launcher_diag.h').read_text(encoding='utf-8')
    text += (SOURCE / 'shared/log_ring.h').read_text(encoding='utf-8')
    for name in ('service.h', 'service_screen.h', 'md5.h', 'sha256.h', 'remote.h', 'chr1.h', 'provision.h',
                 # on-console setup: screens, keyboard, scan list, Quick Connect, record builder
                 'setup_ui.h', 'keyboard.h', 'wifi_scan.h', 'quickconnect.h', 'provision_build.h', 'provision_write.h'):
        text += (SOURCE / 'shared' / name).read_text(encoding='utf-8')
    text += (SOURCE / 'main/setup_esp32.c').read_text(encoding='utf-8')
    text += (SOURCE / 'bootloader_components/main/bootloader_start.c').read_text(encoding='utf-8')
    code = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    single_writer_rules()
    for forbidden in ('nvs_flash_init', 'nvs_open', 'nvs_set', 'esp_partition_write', 'esp_partition_erase', 'esp_flash_write',
                      'esp_flash_erase', 'spi_flash_write', 'esp_ota_', 'esp_deep_sleep', 'bootloader_flash_write'):
        if forbidden in code:
            raise SystemExit(f'Forbidden call in launcher source: {forbidden}')
    # Service mode reads flash and nothing else, and never takes over the console UART:
    # no UART0 driver and no change to how printf writes, so the boot markers behave as
    # before. Its protocol has a fixed verb list with no write, erase or settings verb.
    for forbidden in ('spi_flash_erase', 'esp_rom_spiflash', 'SPIWrite', 'SPIErase', 'esp_flash_set_chip_write_protect',
                      'uart_vfs', 'esp_vfs_dev_uart', 'uart_write_bytes(UART_NUM_0', 'uart_param_config(UART_NUM_0',
                      'uart_set_pin(UART_NUM_0'):
        if forbidden in code:
            raise SystemExit(f'Forbidden call in launcher source: {forbidden}')
    flash_calls = set(re.findall(r'\b(esp_flash_\w+|spi_flash_\w+|esp_partition_\w+)\s*\(', code))
    if flash_calls != {'esp_flash_read', 'esp_flash_read_id'}:
        raise SystemExit(f'Unexpected flash calls: {sorted(flash_calls)}')
    # The CHR1 handler's action list is fixed: nothing writes, erases or changes settings.
    remote = re.sub(r'/\*.*?\*/', '', (SOURCE / 'shared/remote.h').read_text(encoding='utf-8'), flags=re.S)
    actions = set(re.findall(r'!strcmp\(action,"([a-z-]+)"\)', remote))
    if actions != {'status', 'log', 'screen', 'video', 'input', 'hold', 'perf', 'console', 'ping', 'bye', 'svc-begin',
                   'svc-id', 'svc-read', 'svc-hash', 'svc-pct', 'svc-freeze', 'svc-end'}:
        raise SystemExit(f'CHR1 actions changed: {sorted(actions)}')
    # Pairing is answered only by a physical press, never by a remote one.
    if len(re.findall(r'pair_answer=\(', remote)) != 1 or 'pair_answer=(pressed&PJ_BTN_A)' not in remote.split(
            'static inline void pj_rmt_physical', 1)[1].split('static inline', 1)[0]:
        raise SystemExit('Pairing can be answered from somewhere other than a physical button')
    # Wi-Fi: joined in one place only, RAM storage and no NVS for every start of the driver.
    main_only = re.sub(r'/\*.*?\*/', '', (SOURCE / 'main/launcher_main.c').read_text(encoding='utf-8'), flags=re.S)
    inits = main_only.count('esp_wifi_init(')
    if (main_only.count('esp_wifi_connect(') != 1 or main_only.count('esp_wifi_set_storage(WIFI_STORAGE_RAM)') != inits
            or main_only.count('config.nvs_enable=0;') != inits or 'esp_wifi_set_storage(WIFI_STORAGE_FLASH' in main_only):
        raise SystemExit('Wi-Fi must join in one place, with RAM storage and NVS off')
    # Launcher 1.2.9: the one esp_wifi_connect sits in wifi_connect(); join_task tears a running driver down first
    # (disconnect, stop, deinit) and never creates a second event group; a rejoin waits for the Jellyfin app and
    # the LAN task to be gone; the LAN task leaves on lan_quit and never flips wifi_state itself.
    if ('static esp_err_t wifi_connect(void){connect_pending=1;return esp_wifi_connect();}' not in main_only
            or 'if(radio_started){esp_wifi_disconnect();esp_wifi_stop();esp_wifi_deinit();radio_started=0;}' not in main_only
            or 'if(!wifi_events)wifi_events=xEventGroupCreate();' not in main_only or main_only.count('xEventGroupCreate(') != 1
            # 1.3.0: the Pocket link gateway is a network user too: a rejoin waits for it as for the app
            or 'if(rejoin_wanted&&!radio_running&&!pj_jf_active()&&!pj_jl_active()&&!jf_closing&&!PJ_SETUP_IS_BUSY&&(int32_t)(now-rejoin_retry_at)>=0) {' not in main_only
            or 'while(!radio_stop&&!lan_quit) {' not in main_only or 'if(!radio_stop)wifi_state=3;' in main_only):
        raise SystemExit('the 1.2.9 Wi-Fi rejoin rules changed (single connect, teardown before rejoin, app and LAN task gone first)')
    if 'pj_jf_set_pump(io_pump);' not in main_only or main_only.count('static void io_pump(void){usb_pump(0);lan_pump();}') != 1:
        raise SystemExit('the USB/LAN pump must be handed to the Jellyfin binding for decode-time pumping')
    if len(re.findall(r'uart_ll_write_txfifo\(&UART0', main_code := re.sub(r'/\*.*?\*/', '', (
            SOURCE / 'main/launcher_main.c').read_text(encoding='utf-8'), flags=re.S))) != 1:
        raise SystemExit('UART0 is written outside the service pump')
    if 'pj_rmt_usb_quiet(&rmt))printf' in main_code.replace(' ', '') or main_code.count('if(!usb_quiet())printf(') != 2:
        raise SystemExit('Diagnostics lines could interleave with a USB CHR1 session')
    # 1.3.2 (Remote Pilot's USB drops with Jellyfin open, docs/Design-notes.md "USB pace"): CHR1 output goes
    # through the shared/usb_tx.h ring, filled on the main task (the CHR1 handler keeps one owner) and drained into the
    # FIFO every 5 ms from an esp_timer; the one FIFO write is that drain's. printf stays off the UART while the ring
    # holds bytes (usb_quiet), the ring is emptied and the drain stopped before the hand-over lines, and there is still
    # no UART0 TX driver (checked above). The model scripts/model_chromatic_usb_pace.py proves the pace.
    usb_tx = re.sub(r'/\*.*?\*/', '', (SOURCE / 'shared/usb_tx.h').read_text(encoding='utf-8'), flags=re.S)
    if ('static int usb_quiet(void){return pj_rmt_usb_quiet(&rmt)||!pj_utx_idle(&usb_ring);}' not in main_code
            or 'static void uart0_write(const uint8_t *data,uint32_t size){uart_ll_write_txfifo(&UART0,data,size);}' not in main_code
            or 'static void usb_drain(void *unused){(void)unused;pj_utx_drain(&usb_ring,uart0_room,uart0_write);}' not in main_code
            or main_code.count('pj_rmt_out(&rmt,&rmt.usb,') != 1 or main_code.count('pj_utx_put(&usb_ring,') != 1
            or main_code.count('pj_rmt_shutdown(&rmt);usb_finish();') != 2 or main_code.count('esp_timer_start_periodic(usb_drain_timer,') != 1
            or 'if(!usb_drain_timer)usb_drain(NULL);' not in main_code
            or '#define PJ_UTX_RING 1024u' not in usb_tx or 'pj_utx_commit(r,n)' not in usb_tx.split('pj_utx_drain', 1)[1]
            or usb_tx.split('pj_utx_drain', 1)[1].find('write(p,') > usb_tx.split('pj_utx_drain', 1)[1].find('pj_utx_commit(r,n)')):
        raise SystemExit('USB output must go through the drained ring (1.3.2), committed only after the FIFO write')
    # 1.3.3 (USB still dropped during playback, docs/Design-notes.md "USB during playback"): no task may wait
    # 0 ticks. At the launcher's 100 Hz tick pdMS_TO_TICKS(<10) is 0 and vTaskDelay(0) only yields, so 1.3.2's stream
    # task spun while a video played, an idle task starved, and the task watchdog printed a backtrace into UART0 (with
    # esp_rom_printf, whatever the log level) in the middle of Remote Pilot's CHR1 bytes every 5 s.
    hz = int((re.search(r'^CONFIG_FREERTOS_HZ=(\d+)', (SOURCE / 'sdkconfig.defaults').read_text(encoding='utf-8'), re.M)
              or [None, '100'])[1])
    for path in sorted((SOURCE / 'main').glob('*.c')):
        task_code = re.sub(r'/\*.*?\*/', '', path.read_text(encoding='utf-8'), flags=re.S)
        for wait in re.findall(r'vTaskDelay\((.*?)\);', task_code):
            ms = re.fullmatch(r'pdMS_TO_TICKS\((\d+)u?\)', wait.strip())
            if wait.strip() in ('0', '(0)') or (ms and int(ms.group(1)) * hz // 1000 == 0):
                raise SystemExit(f'{path.name}: vTaskDelay({wait}) is 0 ticks at {hz} Hz: it only yields, and a task that '
                                 'loops on it starves an idle task (the task watchdog then prints into the USB link)')
    jellyfin_code = re.sub(r'/\*.*?\*/', '', (SOURCE / 'main/jellyfin_esp32.c').read_text(encoding='utf-8'), flags=re.S)
    if ('#define PJ_JF_WAIT_TICK ((TickType_t)1)' not in jellyfin_code
            or 'if(sent<(size_t)n)vTaskDelay(PJ_JF_WAIT_TICK);' not in jellyfin_code):
        raise SystemExit("The stream task's back-pressure wait must sleep a whole tick (1.3.3)")
    remote_code = re.sub(r'/\*.*?\*/', '', (SOURCE / 'shared/remote.h').read_text(encoding='utf-8'), flags=re.S)
    if ('if(k->state==PJ_LS_READY&&pj_rmt_busy_output(k)&&sent!=k->sent_mark)k->c.last_ms=now;' not in remote_code
            or 'else if(k->state==PJ_LS_READY&&pj_chr1_idle(&k->c,now)) {' not in remote_code):
        raise SystemExit('A CHR1 session must stay alive only while its answer moves (1.3.3, 1.3.2 review)')
    # Review (each finding locked in):
    # UART0: an RX ring buffer only (no TX buffer, no VFS switch, so printf and the markers
    # are unchanged), read through the driver so a slow pass loses nothing.
    if (main_code.count('uart_driver_install(UART_NUM_0,4096,0,0,NULL,0)') != 1 or 'uart_ll_read_rxfifo' in main_code
            or 'uart_read_bytes(UART_NUM_0' not in main_code):
        raise SystemExit('UART0 receive must use the driver RX buffer and nothing else of the driver')
    # The pump's whole-line wait never waits for a body, and is bounded in time.
    pump = main_code.split('static void usb_pump', 1)[1].split('\nstatic ', 1)[0]
    if '!rmt.usb.body_len' not in pump or 'now_ms()-start<30u' not in pump or '!pj_utx_idle(&usb_ring)' not in pump:
        raise SystemExit('The USB pump can wait for a body or without a time limit')
    # True entropy until the radio starts, off before any radio task.
    if not (0 <= main_code.find('bootloader_random_enable();') < main_code.find('pj_rmt_init(')):
        raise SystemExit('Nonces and pairing keys would come from pseudo-random numbers')
    if not (0 <= main_code.find('bootloader_random_disable();') < main_code.find('xTaskCreate(record_wifi?join_task:radio_task')):
        raise SystemExit('The entropy source must be off before Wi-Fi starts')
    # LAN close handling does not depend on Wi-Fi still being up.
    if 'if(lan_rx) {' not in main_code or 'if(wifi_state==2) {' in main_code:
        raise SystemExit('A LAN link could stick after Wi-Fi loss')
    # Menu marker for Chromatic Manager's boot check: exact text, once.
    if main_code.count('printf("PJ-LAUNCHER-MENU\\n")') != 1:
        raise SystemExit('The menu marker is missing or printed more than once')
    # Secrets are never service payloads, and svc-* is USB only.
    service_code = (SOURCE / 'shared/service.h').read_text(encoding='utf-8')
    if (service_code.count('if(pj_svc_secret(off,len))return') != 2 or '#define PJ_SVC_NVS_START 0x9000u' not in service_code
            or '#define PJ_SVC_PROVISION_START 0x3fe000u' not in service_code or 'PJ_DONE_OPEN_BUTTONS_HELD' in
            service_code.split('pj_svc_hold_reason', 1)[1].split('}', 1)[0]):
        raise SystemExit('svc-read/svc-hash could serve secrets, or open-buttons-held is a hold reason')
    if '!strncmp(action,"svc-",4)&&k->kind!=PJ_LINK_USB' not in remote:
        raise SystemExit('Service actions must be USB only')
    # Only the messages the design needs may go to the FPGA: the two the stock FPGA uses, plus the
    # media register (0x1D, launcher 1.2.2): the sound bit only ever behind jf_sound, the Menu bit
    # only behind media_fpga / menu_to_us, and both of those come only from the FPGA's own version
    # message (the stock+media build: debug bit, minor 40). A clear (0) is allowed anywhere.
    sent = set(re.findall(r'send_fpga\((\w+)', code)) - {'unsigned'}
    if sent != {'PJ_TX_POKE', 'PJ_TX_BUTTONS_REQUEST', 'PJ_TX_VERSION_REQUEST', 'PJ_TX_MEDIA'}:
        raise SystemExit(f'Unexpected FPGA messages: {sent}')
    # The media register is sent from one place (media_bits, only on change); what it wants is
    # computed once: nothing unless media_fpga (the FPGA's own version message), the overlay bit
    # with it, Menu only while our screen is up, sound only behind jf_sound; 0 at the hand-over.
    if main_code.count('send_fpga(PJ_TX_MEDIA') != 1 or 'media_sent=want;send_fpga(PJ_TX_MEDIA,want);' not in main_code:
        raise SystemExit('The media register must be sent from media_bits only')
    wants = re.findall(r'media_bits\(([^;]*)\);', main_code)
    if sorted(wants) != sorted(['0', 'media_fpga?(uint16_t)(PJ_MEDIA_OVERLAY|(ours?PJ_MEDIA_MENU:0)|(jf_sound?PJ_MEDIA_AUDIO:0)):0']):
        raise SystemExit(f'Unexpected media register uses: {wants}')
    if re.findall(r'jf_sound=(?!0;)[^;]*;', main_code) != ['jf_sound=rx.version_seen&&pj_fpga_media_audio(rx.version);']:
        raise SystemExit('jf_sound must come only from the FPGA version message')
    if re.findall(r'media_fpga=[^;]*;', main_code) != ['media_fpga=rx.version_seen&&pj_fpga_media_audio(rx.version);']:
        raise SystemExit('media_fpga must come only from the FPGA version message')
    if re.findall(r'launcher\.menu_to_us=[^;]*;', main_code) != ['launcher.menu_to_us=(uint8_t)(media_fpga&&ours);', 'launcher.menu_to_us=0;']:
        raise SystemExit('menu_to_us must follow media_fpga && our screen, in one place')
    if main_code.count('send_fpga(PJ_TX_VERSION_REQUEST,0);') != 1:
        raise SystemExit('The version is requested once at start')
    # Chromatic Manager reads the version from the app image (PROJECT_VER); the boot
    # marker prints PJ_LAUNCHER_VERSION. They must agree.
    main_text = (SOURCE / 'main/launcher_main.c').read_text(encoding='utf-8')
    marker = re.search(r'#define PJ_LAUNCHER_VERSION "([0-9.]+)"', main_text).group(1)
    project = re.search(r'set\(PROJECT_VER "([0-9.]+)"\)', (SOURCE / 'CMakeLists.txt').read_text(encoding='utf-8')).group(1)
    if marker != project:
        raise SystemExit(f'Boot marker version {marker} differs from PROJECT_VER {project}')
    # Nothing of ours may be printed after the hand-over marker: the boot watch reads it.
    handover = main_text[main_text.index('printf("PJ-LAUNCHER-TO-STOCK'):]
    if not re.match(r'printf\("PJ-LAUNCHER-TO-STOCK\\n"\);fflush\(stdout\);\s*esp_restart\(\);', handover):
        raise SystemExit('Something runs between the hand-over marker and the restart')
    if not (OUT / 'sdkconfig').is_file() or not (BUILD / 'chromatic_launcher.elf').is_file():
        print('PASS: launcher source rules. SKIP: the checks of the built launcher (build it first with '
              'scripts/Build-ChromaticLauncher.ps1).')
        return
    config = (OUT / 'sdkconfig').read_text(encoding='utf-8')
    for line in ('# CONFIG_ESP_WIFI_NVS_ENABLED is not set', '# CONFIG_ESP_PHY_CALIBRATION_AND_DATA_STORAGE is not set',
                 'CONFIG_ESP_MAIN_TASK_STACK_SIZE=8192',
                 'CONFIG_BOOTLOADER_WDT_DISABLE_IN_USER_CODE=y', 'CONFIG_BOOTLOADER_WDT_ENABLE=y',
                 'CONFIG_ESPTOOLPY_FLASHMODE_DIO=y', 'CONFIG_ESPTOOLPY_FLASHFREQ="40m"', 'CONFIG_ESPTOOLPY_FLASHSIZE="4MB"'):
        if line not in config:
            raise SystemExit(f'Build config missing: {line}')
    # Read the linked program, not the map: NVS cannot be used until a partition is
    # mounted, so if no mount function exists the settings partition is out of reach.
    nm = next(Path(TOOLS / 'esp32-tools/tools/xtensa-esp-elf').rglob('xtensa-esp32-elf-nm.exe'))
    symbols = subprocess.run([str(nm), str(BUILD / 'chromatic_launcher.elf')], capture_output=True, text=True, check=True).stdout
    defined = {line.split()[-1] for line in symbols.splitlines() if len(line.split()) == 3 and line.split()[1] in 'TtWw'}
    # esp_flash_write and esp_flash_erase_region are now allowed in the image for the one audited
    # provisioning writer; linked_writer_callers() proves nothing else calls them.
    for forbidden in ('nvs_flash_init', 'nvs_flash_init_partition', 'nvs_flash_init_partition_ptr', 'nvs_flash_secure_init',
                      'nvs_flash_erase', 'esp_partition_write', 'esp_partition_erase_range', 'esp_ota_set_boot_partition',
                      'esp_deep_sleep_start', 'esp_flash_write_encrypted',
                      'esp_flash_erase_chip', 'spi_flash_write', 'spi_flash_erase_range', 'bootloader_flash_write',
                      'bootloader_flash_erase_sector', 'bootloader_flash_erase_range', 'wl_mount',
                      'spi_flash_erase_sector', 'uart_vfs_dev_use_driver', 'esp_vfs_dev_uart_use_driver'):
        if forbidden in defined:
            raise SystemExit(f'Forbidden function linked into the launcher: {forbidden}')
    linked_writer_callers()
    print('PASS: no NVS mount, partition write, flash write or erase, OTA or UART0 TX driver in the linked launcher; UART0 RX through the driver buffer; true entropy until the radio starts; bounded USB pump; LAN closes after Wi-Fi loss; menu marker once; secrets never svc payloads, svc-* USB only; 8 KiB main stack; '
          'fixed CHR1 actions (flash read only), pairing answered by physical buttons only, Wi-Fi joined in one place '
          'with RAM storage and NVS off; two FPGA messages only; config as required.')


def flash_guard():
    """The refusal must run before esptool on every flash target. Lists commands only."""
    ninja = next(Path(TOOLS / 'esp32-tools/tools/ninja').rglob('ninja.exe'))
    for target in ('flash', 'app-flash', 'bootloader-flash', 'partition-table-flash', 'erase_flash'):
        commands = subprocess.run([str(ninja), '-C', str(BUILD), '-t', 'commands', target],
                                  capture_output=True, text=True, check=True).stdout
        refusal, writer = commands.find('REFUSED'), commands.find('run_serial_tool')
        if refusal < 0 or writer < 0 or refusal > writer:
            raise SystemExit(f'{target} is not guarded')
    result = subprocess.run([str(ninja), '-C', str(BUILD), 'refuse_idf_flash'], capture_output=True, text=True)
    if result.returncode == 0:
        raise SystemExit('Flash refusal target did not fail')
    # idf.py runs erase-flash itself, not through ninja: the project extension replaces it.
    import importlib.util
    spec = importlib.util.spec_from_file_location('launcher_idf_ext', SOURCE / 'idf_ext.py')
    ext = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ext)
    actions = ext.action_extensions({}, str(SOURCE))['actions']
    for name in ('erase-flash', 'erase_flash', 'flash', 'app-flash', 'bootloader-flash', 'partition-table-flash'):
        try:
            actions[name]['callback'](name, None, None)
        except SystemExit as refusal:
            if 'REFUSED' not in str(refusal):
                raise SystemExit(f'idf.py {name} refusal is wrong')
        else:
            raise SystemExit(f'idf.py {name} is not refused')
    # The ninja runs above regenerate esptool argument files that would write the app over
    # stock; poisoning them again must leave none usable.
    from compose_chromatic_image import flash_args_poisoned, poison_flash_args
    poison_flash_args(BUILD)  # the build the ninja runs used (LAUNCHER_BUILD), never another folder
    if not flash_args_poisoned(BUILD):
        raise SystemExit('An esptool argument file still holds a usable command')
    print('PASS: every idf.py flash and erase command is refused, through ninja and through idf.py itself, '
          'and every esptool argument file is poisoned.')


def pocket_link_rules():
    """Launcher 1.3.0, the Pocket link (docs/Design-notes.md "Launcher 1.3.0"): only on the link FPGA (18.41),
    never beside the Jellyfin app, out before every hand-over to games, the fast UART only through the keyed handshake and
    always back to 115,200 on leaving; the app's menu row and the error screen's Find exits wired to their screens."""
    main = re.sub(r'/\*.*?\*/', '', (SOURCE / 'main/launcher_main.c').read_text(encoding='utf-8'), flags=re.S)
    binding = re.sub(r'/\*.*?\*/', '', (SOURCE / 'main/jellyfin_esp32.c').read_text(encoding='utf-8'), flags=re.S)
    logic = (SOURCE / 'shared/launcher_logic.h').read_text(encoding='utf-8')
    if '#define PJ_FPGA_LINK_MINOR 41u' not in logic or \
            'return ((version>>12)&1u)&&((version>>6)&63u)==PJ_FPGA_LINK_MINOR&&(version&63u)==PJ_FPGA_MEDIA_MAJOR;' not in logic:
        raise SystemExit('pj_fpga_pocket_link must recognise only 18.41 with the debug bit')
    if 'static int link_fpga(void){return rx.version_seen&&pj_fpga_pocket_link(rx.version);}' not in main:
        raise SystemExit('the link FPGA test changed')
    # pj_jl_begin is called in one place, under link_fpga(); the app's row is offered only on the link FPGA.
    if main.count('pj_jl_begin(') != 1 or 'int started=link_fpga()&&record_jellyfin&&wifi_state==2?pj_jl_begin(&jf_creds,now):-1;' not in main:
        raise SystemExit('the Pocket link may start only on the link FPGA')
    if main.count('pj_jf_set_pocket_link(') != 1 or 'pj_jf_set_pocket_link(link_fpga());' not in main \
            or 'int to_plink=pj_jf_exit_to_plink()&&link_fpga();' not in main:
        raise SystemExit('the app\'s Pocket link row must follow the link FPGA')
    # Never both: the app does not start while the link is up or wanted, the link does not start while the app is up.
    if '!plink_wanted&&!pj_jl_active()&&(int32_t)(now-plink_hold_until)>=0&&' not in main \
            or 'if(plink_wanted&&launcher.phase==PJ_L_MEDIA&&!pj_jf_active()&&!jf_closing&&!PJ_SETUP_IS_OPEN) {' not in main \
            or 'if(app||bufs||plink||!c||!c->url[0]||!c->user[0])return -1;' not in binding \
            or 'if(app||bufs||plink||!c||!c->url[0])return -1;' not in binding:
        raise SystemExit('the Jellyfin app and the Pocket link must never be in memory together')
    # Out before games: the link ends first in the hand-over, and whenever the Jellyfin screen is left.
    handover = main[main.index('if(action.restart_to_stock) {'):]
    if handover.index('if(pj_jl_active())plink_end();') > handover.index('media_bits(0);'):
        raise SystemExit('the Pocket link must end before the media bits are cleared for games')
    if 'if(pj_jl_active()){plink_end();launcher.app_owns_media=0;diag("plink end left-media");}' not in main:
        raise SystemExit('the Pocket link must end when the Jellyfin screen is left')
    # UART1: the gateway's bytes go to its own parser beside the launcher's; its writes never block; the rate changes only
    # in jl_set_baud (after the queue drained) and back to 115,200 in pj_jl_end.
    if 'if(pj_jl_active())pj_jl_rx(bytes,count,now);' not in main:
        raise SystemExit('UART1 bytes must reach the gateway parser')
    # 1.3.6: one place sets the rate (jl_rate). To the fast rate only with the line proven empty
    # (jl_drained: the wait's result is used, and the driver's ring and FIFO looked at after it); back to 115,200
    # always, after the same bounded wait, in the handshake's fallback, the leave and pj_jl_end.
    sure_ms = re.search(r'#define PJ_JL_SURE_MS (\d+)u', binding)
    if (binding.count('uart_set_baudrate(') != 1
            or 'static void jl_rate(uint32_t baud) {\n    size_t left=jl_line_pending();\n'
               '    uart_set_baudrate(UART_NUM_1,baud);jl_line_baud=baud;jl_line_until=esp_timer_get_time();jl_line_add(left);\n}' not in binding
            or 'static void jl_set_baud(void *ctx,int fast) {\n    (void)ctx;\n    if(!jl_drained()&&fast)return;\n'
               '    jl_rate(fast?PJ_TETHER_UART_FAST_BAUD:115200u);\n}' not in binding
            or 'static int jl_drained(void) {\n    for(unsigned turn=0;turn<3;turn++) {\n'
               '        if(uart_wait_tx_done(UART_NUM_1,pdMS_TO_TICKS(PJ_JL_SURE_MS))!=ESP_OK)return 0;\n'
               '        if(!jl_driver_pending()){jl_line_until=esp_timer_get_time();return 1;}\n    }\n    return 0;\n}' not in binding
            or 'jl_drained();jl_rate(115200u);' not in binding or binding.count('jl_rate(') != 3
            or binding.count('uart_wait_tx_done(') != 1 or not sure_ms or not 110 <= int(sure_ms.group(1)) <= 150
            or 'uart_set_baudrate' in main):
        raise SystemExit('the UART rate may change only in the keyed handshake, with the line empty, and back to 115,200 on leaving')
    # The gateway's writes never wait: at most PJ_JL_TX_ROOM bytes are left waiting for the wire, counted by the
    # binding itself (the driver's free-size figure knows only the packet it is moving, not its ring), so
    # uart_write_bytes always finds room in the 1024-byte ring. The host model refuses at the same figure.
    room = re.search(r'#define PJ_JL_TX_ROOM (\d+)u', binding)
    model_room = re.search(r'#define MODEL_TX_ROOM (\d+)u', (SOURCE / 'check_pocket_link.c').read_text(encoding='utf-8-sig'))
    if (not room or not model_room or room.group(1) != model_room.group(1) or not 144 <= int(room.group(1)) <= 512
            or 'if(jl_tx_pending(NULL)+size>PJ_JL_TX_ROOM)return 0;\n    if(uart_write_bytes(UART_NUM_1,(const char *)packet,size)!=(int)size)return 0;\n    jl_line_add(size);return 1;' not in binding
            or 'static size_t jl_tx_pending(void *ctx) {\n    (void)ctx;size_t driver=jl_driver_pending(),line=jl_line_pending();\n    return driver>line?driver:line;\n}' not in binding
            or binding.count('uart_get_tx_buffer_free_size(') != 1 or binding.count('uart_write_bytes(') != 2
            or binding.count('jl_line_add(size)') != 2):
        raise SystemExit('the gateway must never block on the FPGA UART')
    # 1.3.1: the leave is never dropped (drain, send, drain) and said at both rates; the media bits are
    # said again after the link ends; the credit counts what still sits in the ESP32's transmit ring and FIFO; no heartbeat
    # while the two ends may be at different rates.
    plink = re.sub(r'/\*.*?\*/', '', (SOURCE / 'shared/pocket_link.h').read_text(encoding='utf-8'), flags=re.S)
    if ('static const pj_plink_extra jl_extra={jl_send_sure,jl_tx_pending,jl_micros,jl_idle_us};' not in binding or binding.count('pj_plink_extras(plink,jl_extra);') != 2
            or 'static int jl_send_sure(void *ctx,const uint8_t *packet,size_t size) {\n    (void)ctx;\n    if(!jl_drained()){if(jl_leave_unsent<255)jl_leave_unsent++;return 0;}\n'
               '    if(uart_write_bytes(UART_NUM_1,(const char *)packet,size)!=(int)size)return 0;\n    jl_line_add(size);\n    return jl_drained();\n}' not in binding
            or 'static void plink_end(void) {' not in main or 'if(link_fpga())media_sent=0xffffu;' not in main
            or main.count('pj_jl_end();') != 1):
        raise SystemExit('the leave must be sent surely and the media bits said again after the link (1.3.1)')
    if ('static inline void pj_plink_leave(pj_plink *l) {' not in plink or plink.count('pj_plink_ctrl_sure(l,PJ_TETHER_CTRL_LEAVE);') != 3
            or 'l->held=l->x.tx_pending?(uint32_t)l->x.tx_pending(l->io.ctx):0;l->pass_sent=0;' not in plink
            or 'if(l->fast!=PJ_PF_REQUESTED&&(uint32_t)(now-l->heartbeat_ms)>=(l->fast==PJ_PF_SWITCHED?PJ_PLINK_CONFIRM_HB_MS:PJ_PLINK_HEARTBEAT_MS)' not in plink
            or re.search(r'pj_plink_ctrl\(l,PJ_TETHER_CTRL_LEAVE\)', plink)):
        raise SystemExit('pocket_link.h: sure leave at both rates, ring-aware credit and gated heartbeats (1.3.1)')
    # Find my server again: the error screen's exit opens Setup straight at it.
    if 'int to_find=PJ_ENABLE_SETUP&&pj_jf_exit_to_find();' not in main or 'if(find_wanted){find_wanted=0;pj_setup_find_begin(setup,1);}' not in main:
        raise SystemExit('Find my server again must open from the app\'s error screen')
    # Memory: the heap check before anything is taken, and the gateway on the heap (never static, never a stack).
    # 1.3.7: in four pieces of at most 24 KB (the console's largest free block was 36,864 B with 90 KB free); the total
    # checked is the same as the one block's, the largest block only has to hold the largest piece; a missing piece
    # frees them all (release) and says "not enough memory".
    if 'size_t need=PJ_PLINK_MEMORY+sizeof(pj_jf_buffers)+2u*PJ_JF_STACK+PJ_JL_STREAM_BUFFER+PJ_JL_RESERVE;' not in binding \
            or 'if(esp_get_free_heap_size()<need||heap_caps_get_largest_free_block(caps)<pj_plink_piece_max())return -5;' not in binding \
            or 'plink=heap_caps_calloc(1,sizeof(pj_plink),caps);' not in binding or re.search(r'^static pj_plink [a-z_]+;', binding + main, re.M) \
            or 'if(!plink||!plink_buf||!plink_response||!plink_scratch||!bufs||!req_go||!stream_lock){release();return -2;}' not in binding \
            or any(binding.count(f'if({p}){{memset({p},0,') != 1 or binding.count(f'heap_caps_free({p});{p}=NULL;') != 1
                   for p in ('plink', 'plink_buf', 'plink_response', 'plink_scratch')) \
            or '#define PJ_PLINK_PIECE_LIMIT 24576u' not in plink \
            or '#define PJ_PLINK_MEMORY (sizeof(pj_plink)+PJ_PLINK_BUF+PJ_PLINK_RESPONSE+sizeof(pj_plink_scratch))' not in plink \
            or re.search(r'uint8_t (buf|response)\[PJ_PLINK_|pj_plink_scratch scratch;', plink):
        raise SystemExit('the Pocket link gateway must be heap pieces of at most 24 KB, taken only with its reserve free, all freed together (1.3.7)')
    # 1.3.8 (the stream failed with 532 B free). A connection per artwork request left ~90 lwIP
    # TIME_WAIT pcbs (~22 KB, 120 s each). The link's GETs share one kept connection (a kept one that did not answer
    # at all is tried once more on a new one); the app and any POST keep a connection per request; no link request
    # runs beside a stream's connection; the kept one goes while a title plays and at the end; a counts-only
    # "plink net" line follows the 5 s line.
    if ('if(!plink||post) {' not in binding or binding.count('http_exchange(') != 3 or 'if(answered||!reused||' not in binding
            or 'if(keep&&(stream_sb||' not in binding or 'for(TickType_t t0=xTaskGetTickCount();stream_conn&&' not in binding
            or binding.count('stream_conn=') != 2 or 'http_drop(&keep);\n    http_alive=0;vTaskDelete(NULL);' not in binding
            or 'if(pj_jl_net(body,sizeof(body)))diag(body);' not in main):
        raise SystemExit('the Pocket link\'s requests must share one kept connection, never beside a stream\'s, with the "plink net" line (1.3.8)')
    # 1.3.9. (a) Every START failed "picture size not supported on the Pocket": the server keeps the
    # title's shape (128x72 for 16:9) and writes its own Huffman tables, the Pocket's decoder takes only its fixed sizes and
    # the Annex K tables in ROM. The request is a box (maxWidth/maxHeight), each picture goes through the fit (re-coded with
    # Annex K, letterboxed, tables left out) before it is sent, the size check before CONFIG stays and checks the fitted
    # size, and the fit's tables live in the scratch area behind the PJAV ring. (b) The console was handed to stock 165 s in:
    # a link failure found in pj_plink_step (status stale, arming) leaves at once (back to 115,200 at both ends), and the
    # silent-FPGA rule is longer, still bounded, on the Media screen while the link runs; the menu keeps 1.5 s.
    logic = (SOURCE / 'shared/launcher_logic.h').read_text(encoding='utf-8')
    fit_src = (SOURCE / 'shared/pocket_jpeg_fit.h').read_text(encoding='utf-8')
    if ('"&maxWidth=%u&maxHeight=%u&maxFramerate=%u&videoBitRate=%u&startTimeTicks=%llu"' not in plink
            or re.search(r'"[^"]*&width=%u&height=%u', plink)
            or 'int fit=pj_fit_jpeg(l->buf,l->avi.frame_size,PJ_PLINK_BUF,l->cfg_w,l->cfg_h,0,&l->scratch->fit.work,&size);' not in plink
            or 'unsigned out_h=pj_fit_height(a->width,a->height);' not in plink
            or 'if(!out_h||!pj_tether_geometry_ok(a->width,out_h)){l->stream_state=PJ_PS_FAILED;l->why="picture size not supported on the Pocket";return -1;}' not in plink
            or 'struct {uint8_t ring_room[PJ_PLINK_RING];pj_fit_work work;} fit;' not in plink
            or 'if(2u*len+PJ_FIT_HEADROOM>cap)return PJ_FIT_ROOM;' not in fit_src
            or 'if(w->full||w->p>=*w->guard){w->full=1;return;}' not in fit_src):
        raise SystemExit('the link stream must ask for a box and fit every picture to the Pocket\'s decoder (1.3.9)')
    if (plink.count('pj_plink_fail(l,"FPGA status stale");pj_plink_leave(l);pj_plink_stream_close(l);return -1;') != 1
            or 'pj_plink_leave(l);pj_plink_stream_close(l);return -1;\n    }\n    if(l->state==PJ_PL_READY&&(uint32_t)(now-l->status_ms)>PJ_PLINK_STATUS_STALE_MS)' not in plink
            or '#define PJ_LAUNCH_SILENT_MS 1500u' not in logic or '#define PJ_LAUNCH_LINK_SILENT_MS 6000u' not in logic
            or 'int32_t silent=(int32_t)(l->phase==PJ_L_MEDIA&&l->media_link?PJ_LAUNCH_LINK_SILENT_MS:PJ_LAUNCH_SILENT_MS);' not in logic
            or 'launcher.media_link=(uint8_t)(pj_jl_active()!=0);\n        pj_launch_action action=pj_launch_step(&launcher,now);' not in main):
        raise SystemExit('a link failure must leave at once, and only the Media screen with the link running may wait longer for the FPGA (1.3.9)')
    # 1.4.0 (the first real playback got ~20 KB/s of the ~30 a 24 fps title needs, each READ one
    # 4 KB ring in ~200 ms; the first Play failed "the stream failed" and the second played). A READ takes the ring, fills
    # it again and takes again up to PJ_PLINK_READ_MAX; at most two pictures are fitted per stream step; the step sends
    # before and after its stream work; the pass's end reads UART1 again and pumps; the allowance for a late status is
    # dropped only after 400 idle byte times; the link's stream open is tried three times (a 4xx is final); "plink
    # speed" lines follow "plink net"; Remote Pilot's live frames are throttled while a title plays over the link.
    if ('#define PJ_PLINK_FITS_PER_STEP 2u' not in plink or '#define PJ_PLINK_READ_REFILLS 3u' not in plink
            or '#define PJ_PLINK_IDLE_BYTES 400u' not in plink
            or 'if(n>=PJ_PLINK_READ_MAX||refill>=PJ_PLINK_READ_REFILLS||(l->stream_state!=PJ_PS_OPEN&&l->emit_kind==PJ_PE_NONE))break;' not in plink
            or 'if(fits>=PJ_PLINK_FITS_PER_STEP)return;' not in plink
            or plink.count('pj_plink_send_down(l);') != 3
            or 'if(l->x.idle_us(l->io.ctx)>=(uint32_t)((uint64_t)PJ_PLINK_IDLE_BYTES*10000000u/baud))l->busy=0;' not in plink
            or 'if(l->busy&&!l->held&&l->x.idle_us) {' not in plink
            or '#define PJ_JL_OPEN_TRIES 3' not in binding or 'if(status>=400&&status<500)break;' not in binding
            or 'for(int attempt=0;attempt<(plink?PJ_JL_OPEN_TRIES:1)&&!tasks_stop&&generation==stream_generation;attempt++) {' not in binding
            or 'if(pj_jl_speed(body,sizeof(body),now,&uart1_counts,0)){diag(body);if(pj_jl_speed(body,sizeof(body),now,&uart1_counts,1))diag(body);}' not in main
            or 'rmt.video_slow=(uint8_t)(pj_jf_playing()||pj_jl_streaming());' not in main
            or 'if(!plink||closing||jl_failed||pj_plink_rate_changing(plink))return;\n    pj_plink_pump(plink,now);' not in binding):
        raise SystemExit('the link stream must answer READs of up to 8 KB, fit at most two pictures a step, feed the UART '
                         'before and after its work and at the pass end, retry its first open, and say "plink speed" (1.4.0)')
    print('PASS: Pocket link (1.3.0/1.3.1): only on the link FPGA 18.41, the leave sent surely at both rates and the media bits said again after it, credit counting the ESP32 TX ring, no heartbeat while the rates may differ, never beside the Jellyfin app, ended before games and when the '
          'Jellyfin screen is left, UART1 shared without blocking, the rate changed only by the keyed handshake and reset to '
          '115,200 on leaving, heap-checked in pieces of at most 24 KB (1.3.7); Find my server again opens from the app\'s error screen.')


def link_diag_rules():
    """Launcher 1.3.6, link diagnostics (docs/Design-notes.md "Launcher 1.3.6 link diagnostics"): the lines
    are added after the unchanged 5 s line, UART1 trouble is counted through the driver's event queue, the receive
    interrupt fires early, the parser's counters survive the 3 s retry, and the frame acceptance test is the committed
    one (nothing loosened). Source rules only; the behaviour is in check_pocket_link.c and check_launcher.c."""
    main = re.sub(r'/\*.*?\*/', '', (SOURCE / 'main/launcher_main.c').read_text(encoding='utf-8'), flags=re.S)
    binding = re.sub(r'/\*.*?\*/', '', (SOURCE / 'main/jellyfin_esp32.c').read_text(encoding='utf-8'), flags=re.S)
    link = (SOURCE / 'shared/pocket_link.h').read_text(encoding='utf-8')
    diag = (SOURCE / 'shared/pocket_link_diag.h').read_text(encoding='utf-8')
    # The 5 s line keeps every field in its place (Manager and Remote Pilot logs are read by eye and by tools).
    if '"plink state=%lu view=%u level=%lu faults=%lu fast=%lu stream=%lu heap=%lu heap-min=%lu block=%lu"' not in main:
        raise SystemExit('the 5 s "plink state=" line must keep its fields (1.3.6 adds lines, it changes none)')
    if ('pj_jl_diag_periodic(body,sizeof(body),now,plink_rx_line++,&uart1_counts)' not in main
            or main.count('pj_jl_diag_event(body,sizeof(body),now)') != 1 or 'plink_rx_line=0;' not in main):
        raise SystemExit('the "plink rx" lines follow the 5 s line and the capture line is offered every loop (1.3.6)')
    # UART1: an event queue, the early receive interrupt, the watch before every read of the line.
    full = re.search(r'#define PJ_UART1_RX_FULL (\d+)', main)
    events = re.search(r'#define PJ_UART1_EVENTS (\d+)', main)
    if (main.count('uart_driver_install(UART_NUM_1,4096,1024,PJ_UART1_EVENTS,&uart1_events,0)') != 1
            or 'uart_set_rx_full_threshold(UART_NUM_1,PJ_UART1_RX_FULL)' not in main
            or not full or not 16 <= int(full.group(1)) <= 96 or not events or int(events.group(1)) != 16):
        raise SystemExit('UART1 needs its event queue (16 entries: about 0.3 KB of heap in every mode, review of 1.3.6) '
                         'and a receive interrupt well before the 128-byte FIFO is full')
    if (main.count('uart1_watch();') < 3
            or not re.search(r'uart1_watch\(\);\s*uart1_read\(now\);', main)  # 1.4.0: the read is a function, also at the pass's end
            or not re.search(r'static void uart1_read\(uint32_t now\) \{\s*uint8_t bytes\[128\];\s*int count;\s*while\(\(count=uart_read_bytes\(UART_NUM_1,bytes,sizeof\(bytes\),0\)\)>0\)', main)
            or 'if(pj_jl_active()){uint32_t end=now_ms();uart1_watch();uart1_read(end);pj_jl_pump(end);}' not in main):
        raise SystemExit('UART1 events must be counted every loop, before the line is read (1.3.6)')
    for event, field in (('UART_FIFO_OVF', 'ovf'), ('UART_BUFFER_FULL', 'full'), ('UART_PARITY_ERR', 'parity'),
                         ('UART_FRAME_ERR', 'frame'), ('UART_BREAK', 'brk')):
        if f'case {event}:uart1_counts.{field}++;break;' not in main:
            raise SystemExit(f'{event} must be counted (1.3.6)')
    if 'if(uxQueueSpacesAvailable(uart1_events)==0)uart1_counts.evq++;' not in main:
        raise SystemExit('a full UART1 event queue must be counted: the driver drops what does not fit (1.3.6)')
    # The bytes reach the gateway from the same read, whole, whatever the fault bits say.
    if 'if(pj_jl_active())pj_jl_rx(bytes,count,now);' not in main:
        raise SystemExit('every UART1 byte read goes to the link parser')
    # The retry keeps the parser's counters; only a new link session clears them.
    if binding.count('memset(&jl_rx,0,sizeof(jl_rx));') != 1 or binding.count('pj_plink_rx_reset(&jl_rx);') != 1:
        raise SystemExit('the 3 s retry must keep the damaged-packet counts (1.3.6)')
    if ('u.pkt_crc=jl_rx.crc_status;u.pkt_other=jl_rx.crc_other;u.pkt_len=jl_rx.bad_len;' not in binding
            or 'return pj_plink_say_periodic(plink,&jl_said,&u,now,index,out,size);' not in binding
            or 'return pj_plink_say_event(plink,&jl_said,now,out,size);' not in binding):
        raise SystemExit('the link diagnostics lines must come from pocket_link_diag.h alone, through the gate of '
                         'pocket_link.h (1.3.6)')
    # Review of 1.3.6: a link that works is not disturbed by its diagnostics. The only way to a console line is the
    # gate in pocket_link.h, which tells the printer whether the screen says connected, syncing or playing; the
    # printer then says one short sum per 30 s without a byte in hex, and a refused frame at most once in 10 s.
    for name, text in (('launcher_main.c', main), ('jellyfin_esp32.c', binding)):
        if 'pj_pld_periodic(' in text or 'pj_pld_event(' in text:
            raise SystemExit(f'{name} must not call the diagnostics printer past the gate of pocket_link.h (1.3.6)')
    if (link.count('return what==PJ_PLV_CONNECTED||what==PJ_PLV_SYNCING||what==PJ_PLV_STREAMING;') != 1
            or link.count('return pj_pld_periodic(p,&l->d,u,now,index,pj_plink_linked(l,now),out,size);') != 1
            or link.count('return pj_pld_event(p,&l->d,now,pj_plink_linked(l,now),out,size);') != 1
            or link.count('pj_pld_periodic(') != 1 or link.count('pj_pld_event(') != 1):
        raise SystemExit('linked means connected, syncing or streaming, and both printer calls pass it (1.3.6)')
    linked = re.search(r'"(plink rx linked bytes=[^"]*)"', diag)
    if (not linked or 'hex' in linked.group(1) or 'head' in linked.group(1) or len(linked.group(1)) > 80
            or diag.count('"plink rx linked bytes=') != 1 or diag.count('plink rx linked') != 2
            or 'if(linked)p->cap_seq=d->done_seq;' not in diag
            or '#define PJ_PLD_REPEAT_MS 30000u' not in diag or '#define PJ_PLD_SAME_MS 10000u' not in diag):
        raise SystemExit('linked: one short sum per 30 s without bytes, no capture line, a reject per 10 s (1.3.6)')
    # The printer knows when the link began (the "plink rx first" line tells its age): set with every new session.
    if binding.count('pj_pld_printer_init(&jl_said,now);') != 1 or '"plink rx first age=%lu after=%lu n=%u code=%s bits=%u at=%u hex=%s"' not in diag:
        raise SystemExit('the "plink rx first" line needs the link\'s begin time (1.3.6)')
    # The retry keeps the previous fault bits too: a bit that stuck through it is one appearance.
    if 'l->d.fault_prev=0' in link:
        raise SystemExit('a retry must not forget the fault bits it saw last (1.3.6)')
    # Review of the 18.41 bridge. It may still be at the fast rate when a link starts and then hears nothing for up
    # to 3 s (its heartbeat guard): ENTER is said again while arming and arming outlasts the guard by a second, so
    # no failure is shown for it. And the bridge does not resend a status packet across a rate change: the fast rate
    # is asked for only with nothing owed (and the cable quiet, during the hold). Nothing is said at the fast rate
    # outside media mode: the platform's rate is only ever raised by the handshake and by the leave at both rates.
    arm = re.search(r'#define PJ_PLINK_ARM_MS (\d+)u', link)
    again = re.search(r'#define PJ_PLINK_ENTER_MS (\d+)u', link)
    if (not arm or not again or int(arm.group(1)) < 4000 or not 100 <= int(again.group(1)) <= 500
            or link.count('if(l->state==PJ_PL_ARMING&&(uint32_t)(now-l->again_ms)>=PJ_PLINK_ENTER_MS&&pj_plink_ctrl(l,PJ_TETHER_CTRL_ENTER))l->again_ms=now;') != 1):
        raise SystemExit('arming must say ENTER again and outlast the bridge\'s 3 s heartbeat guard (1.3.6)')
    if (link.count('int owed=l->tx_active||l->work!=PJ_PW_NONE||l->http_busy;') != 1
            or link.count('if(l->fast==PJ_PF_SLOW&&idle&&pj_plink_ctrl(l,PJ_TETHER_CTRL_FAST)){l->fast=PJ_PF_REQUESTED;l->fast_ms=now;}') != 1
            or link.count('int idle=!owed&&!ring&&(') != 1 or link.count('PJ_TETHER_CTRL_FAST)') != 1
            or link.count('if(count)l->quiet_ms=now;') != 1):
        raise SystemExit('the fast UART rate is asked for in one place and only while nothing is owed (1.3.6)')
    # 1.3.6, the transmit ring. "Nothing owed" did not count the reply tail still in the MCU's transmit ring: the
    # request was queued behind it and the MCU changed its rate 5 ms later, so the tail and the request left at the
    # wrong rate. Now: the request only into an empty ring, the rate changed only with the ring empty again, a ring
    # that does not empty ends the try without any rate change, and the launcher's own FPGA messages wait meanwhile.
    drain_ms = re.search(r'#define PJ_PLINK_FAST_DRAIN_MS (\d+)u', link)
    host = (SOURCE / 'check_pocket_link.c').read_text(encoding='utf-8-sig')
    room = re.search(r'#define PJ_JL_TX_ROOM (\d+)u', binding)
    if (not room or link.count('size_t ring=(l->fast==PJ_PF_SLOW||l->fast==PJ_PF_REQUESTED)&&l->x.tx_pending?l->x.tx_pending(l->io.ctx):0;') != 1
            or link.count('else if(l->fast==PJ_PF_REQUESTED&&!ring&&(uint32_t)(now-l->fast_ms)>=PJ_PLINK_FAST_SWITCH_MS){l->io.set_baud(l->io.ctx,1);l->fast=PJ_PF_SWITCHED;') != 1
            or not drain_ms or not 100 <= int(drain_ms.group(1)) <= 500
            or not re.search(r'else if\(l->fast==PJ_PF_REQUESTED&&\(uint32_t\)\(now-l->fast_ms\)>=PJ_PLINK_FAST_DRAIN_MS\) \{\s*/\*[^/]*\*/\s*'
                             r'l->fast=PJ_PF_OFF;if\(l->fast_failed<255\)l->fast_failed\+\+;l->heartbeat_ms=now;l->why="fast UART request not sent";\s*\}', link)
            or link.count('static inline int pj_plink_rate_changing(const pj_plink *l){return l->fast==PJ_PF_REQUESTED;}') != 1):
        raise SystemExit('the fast rate is asked for and taken only with the transmit ring empty, within a bound (1.3.6)')
    re_review_rules(main, binding, link, host)
    if ('int pj_jl_rate_changing(void){return plink!=NULL&&!closing&&!jl_failed&&pj_plink_rate_changing(plink);}' not in binding
            or 'if(want==media_sent)return;\n    if(pj_jl_rate_changing())return; \n    media_sent=want;send_fpga(PJ_TX_MEDIA,want);' not in main
            or main.count('if(action.request_buttons&&!pj_jl_rate_changing())send_fpga(PJ_TX_BUTTONS_REQUEST,0);') != 1
            or main.count('send_fpga(PJ_TX_BUTTONS_REQUEST') != 1):
        raise SystemExit('the launcher must say nothing to the FPGA while the link\'s UART rate changes (1.3.6)')
    # The host model holds the gateway to it: the rate goes up only with the model's ring empty, the request is
    # never queued behind anything, in every case but the one run of the old rule; large replies at three cable rates.
    if (host.count('if(fast){if(p->ring_used>p->ring_at_fast)p->ring_at_fast=p->ring_used;CHECK(p->allow_ring||!p->ring_used);}') != 1
            or host.count('if(!p->allow_ring)CHECK(!p->ring_used);') != 1 or host.count('allow_ring=1') != 1
            or host.count('static const unsigned cable[3]={64,256,1024};') != 1
            or host.count('rpc(PJ_RPC_HOME,"{}",cable[c],&n)') != 1
            or host.count('if(p->strict&&p->uart_fast!=(p->fast||p->fast_pending)){p->wrong++;return 1;}') != 1):
        raise SystemExit('check_pocket_link.c must check the transmit ring at every rate change (1.3.6)')
    if (link.count('set_baud(l->io.ctx,1)') != 2 or binding.count('PJ_TETHER_UART_FAST_BAUD') != 1
            or 'PJ_TETHER_UART_FAST_BAUD' in main or '524288' in main or '524288' in binding):
        raise SystemExit('the fast rate is set by the handshake and by the leave at both rates only (1.3.6)')
    # The diagnostics header knows nothing of Wi-Fi or Jellyfin: link-protocol bytes, counts and codes only.
    includes = re.findall(r'#include [<"]([^>"]+)[>"]', diag)
    if sorted(includes) != ['stddef.h', 'stdint.h', 'stdio.h', 'string.h'] or re.search(r'ssid|password|token|record|creds|jf_', diag, re.I):
        raise SystemExit(f'pocket_link_diag.h must stay free of Wi-Fi and Jellyfin data: {includes}')
    # Nothing loosened: the test that accepts a frame is, text for text, the committed one of 1.3.5.
    accept = ('if(!pj_rpc_validate(l->incoming,size)&&!(size>=16&&size<=1040&&!memcmp(l->incoming,"PJB1",4)&&l->incoming[5]==0&&\n'
              '               size==16+pj_av_u16(l->incoming+6)&&(pj_av_crc_part(pj_av_crc_part(0xffffffffu,l->incoming,12),l->incoming+16,size-16)^0xffffffffu)==pj_av_u32(l->incoming+12)))')
    if link.replace('\r\n', '\n').count(accept) != 1:
        raise SystemExit('the frame acceptance test must stay exactly as released in 1.3.5 (validation is not loosened)')
    print('PASS: link diagnostics (1.3.6): the 5 s line unchanged and the "plink rx" lines after it, UART1 overflow, full '
          'ring, parity, framing and break counted through the event queue (a full queue counted too), the receive '
          f'interrupt at {full.group(1)} of 128 bytes, damaged-packet counts kept across the retry, the diagnostics header '
          'free of Wi-Fi and Jellyfin data, the frame acceptance test text-identical to 1.3.5; review: a 16-entry event '
          'queue, every console line through the gate of pocket_link.h (connected/syncing/streaming: one short sum '
          'per 30 s without bytes, a reject per 10 s), the "plink rx first" line with the link\'s begin time; bridge '
          'review: ENTER said again while arming for 4.5 s, the fast rate asked for only with nothing owed; '
          f'the fast rate asked for and taken only with the MCU\'s transmit ring empty (a ring that does not '
          f'empty ends the try after {drain_ms.group(1)} ms without a rate change), the launcher\'s own FPGA messages '
          f'held back meanwhile, at most {room.group(1)} bytes left waiting for the wire (the same in the host model); '
          'the fault-clear ENTER held while the rate request waits, "fast UART request not sent" told from '
          '"fast UART not confirmed" (log, screen and host check), a leave a stuck UART kept back said in one safe log '
          'line, the host model\'s sure sends kept in a real ring, the never-pausing case run over every status phase.')


def re_review_rules(main, binding, link, host):
    """Launcher 1.3.6 rate-change rules (a to f). Source rules; the behaviour is in check_pocket_link.c."""
    # a. The ENTER that clears a fault bit is held, like the heartbeat, while the rate request waits in the ring:
    # it would leave at 115,200 to a bridge that may already be fast. The model checks every ENTER it is handed.
    if (link.count('if(l->fast!=PJ_PF_REQUESTED&&(uint32_t)(now-l->clear_ms)>=PJ_PLINK_HEARTBEAT_MS&&pj_plink_ctrl(l,PJ_TETHER_CTRL_ENTER))l->clear_ms=now;') != 1
            or link.count('pj_plink_ctrl(l,PJ_TETHER_CTRL_ENTER)') != 3
            or host.count('if(m[1]==PJ_TX_PLINK_CTRL&&m[3]==1)CHECK(L.fast!=PJ_PF_REQUESTED);') != 1
            or host.count('if(m[3]==1){CHECK(m[4]==0x50&&m[7]==0x4b);p->enters++;if(p->mode!=2)p->fast=0;p->mode=2;p->faults=0;}') != 1):
        raise SystemExit('the fault-clear ENTER must wait while the fast rate request is in the ring, and the host model '
                         'must check it against a bridge that keeps its rate in media mode (1.3.6)')
    # b. Two reasons, two texts: the ring never emptied (no rate change at all) / the bridge never confirmed.
    screen = (SOURCE / 'shared/pocket_link_screen.h').read_text(encoding='utf-8')
    if (link.count('l->why="fast UART request not sent";') != 1 or link.count('l->why="fast UART not confirmed";') != 1
            or screen.count('"fast UART request not sent"') != 2 or screen.count('"fast UART not confirmed"') != 2
            or host.count('"fast UART request not sent"') < 1 or host.count('"fast UART not confirmed"') < 1):
        raise SystemExit('the drain bound and the confirm timeout need their own reasons, both known to the screen and '
                         'checked by the host (1.3.6)')
    # c. A leave the stuck UART kept back is counted and said once, in a line that is safe for the console log.
    said = re.search(r'snprintf\(out,size,"(plink leave not-sent n=%u [^"]*)",\(unsigned\)jl_leave_unsent\);\s*jl_leave_unsent=0;return 1;', binding)
    if (not said or len(said.group(1)) > 120 or re.search(r'[^ -~]|PJ-LAUNCHER|rst:|[0-9a-fA-F]{12}', said.group(1))
            or said.group(1).count('%') != 1 or binding.count('jl_leave_unsent++') != 1
            or binding.index('if(jl_leave_unsent) {') > binding.index('if(!plink||closing){last_key=0xffffffffu;last_why=NULL;return 0;}')
            or 'if(pj_jl_trace(body,sizeof(body)))diag(body);' not in main):
        raise SystemExit('a leave kept back by a stuck UART must be said once, link open or not, in a safe line (1.3.6)')
    # d. jl_drained's reasoning needs the UART1 interrupt and the loop on one core: the driver is installed inside
    # buses_init, called by app_main (the interrupt is allocated on the calling core), and the main task is pinned to
    # CPU0 (IDF's default, which sdkconfig.defaults must not change; the built sdkconfig is checked when there is one).
    defaults = (SOURCE / 'sdkconfig.defaults').read_text(encoding='utf-8')
    built = BUILD.parent / 'sdkconfig'
    if 'MAIN_TASK_AFFINITY' in defaults or 'CONFIG_FREERTOS_UNICORE' in defaults or (
            built.is_file() and 'CONFIG_ESP_MAIN_TASK_AFFINITY_CPU0=y' not in built.read_text(encoding='utf-8')):
        raise SystemExit('the main task must stay pinned to CPU0: jl_drained relies on it (1.3.6)')
    raw = (SOURCE / 'main/jellyfin_esp32.c').read_text(encoding='utf-8')
    buses = main.split('static void buses_init(void)', 1)[-1].split('\n}', 1)[0]
    if (main.count('uart_driver_install(UART_NUM_1,') != 1 or 'uart_driver_install(UART_NUM_1,' not in buses
            or main.count('buses_init();') != 1 or not re.search(r'void app_main\(void\).*?buses_init\(\);', main, re.S)
            or 'CONFIG_ESP_MAIN_TASK_AFFINITY_CPU0=y' not in raw or 'uart_driver_install' not in raw):
        raise SystemExit('jl_drained relies on the UART1 interrupt and the main loop sharing a core: the driver installed '
                         'from app_main, and the comment at jl_drained saying what must be re-checked (1.3.6)')
    # e. The model keeps the rate of the newest sure sends (a ring), and the leave checks start from a marked ring.
    if (host.count('p->sure_rate[p->sure%8]=p->uart_fast;p->sure++;') != 1 or 'if(p->sure<8)' in host
            or host.count('sure_mark();') < 3 or 'P.sure=0' in host):
        raise SystemExit('check_pocket_link.c: the rates of the newest sure sends must be the ones checked (1.3.6)')
    # f. The never-pausing case runs across the bridge's status periods and phases and bounds what it measured.
    late = re.search(r'#define MODEL_HOLD_LATE_MS (\d+)u', host)
    # (1.3.6 credit review: the status line gained a stale form for rule g; the fresh one is its `else`.)
    if (host.count('if(now_ms%status_period==status_phase) {') != 1 or host.count('        else status(now_ms,NULL,0);') != 1
            or host.count('CHECK(runs==270);') != 1
            or not re.search(r'periods\[3\]=\{10,30,50\}', host) or not late or not 650 <= int(late.group(1)) <= 1500
            or host.count('asked-began<=PJ_PLINK_FAST_HOLD_MS+MODEL_HOLD_LATE_MS') != 1):
        raise SystemExit('check_pocket_link.c: the never-pausing case must run over every status phase with a bound '
                         'that is not tuned to one of them (1.3.6)')
    # g. Credit review: a status is read up to one pass after the bridge wrote it, so the bytes "sent since" it start
    # from the previous pass: what the UART still held when that pj_plink_step began (plus one whole packet for the one
    # in flight) and everything handed over since, and what the margin lacks of three packets (144 bytes) when the UART
    # had work in the pass before that (a status written up to ~200 UART byte times before that step: arbiter wait,
    # its own 14 bytes, the receive FIFO and scheduling;, was one packet, 16 bytes). The rule before (only what the UART
    # holds at the status) is gone; jl_tx_pending itself is unchanged (pinned above). The host runs it with statuses
    # one pass old and written up to 200 UART bytes earlier still (each also with the transmit count one byte short:
    # the shift register), a stopped and a crawling cable, a
    # UART a little under a whole number of packets a pass, and a transmit ring counted to the byte.
    code = re.sub(r'/\*.*?\*/', '', link, flags=re.S)
    # 1.4.0: pj_plink_step times the pass and the step around pj_plink_step_timed, which begins as the step did
    step = code.split('static inline int pj_plink_step_timed(pj_plink *l,uint32_t now) {', 1)[-1]
    if (code.count('l->sent_since=l->held+(l->held?PJ_PLINK_DATA_BYTES:0u)+(l->busy?3u*PJ_PLINK_DATA_BYTES-PJ_PLINK_MARGIN:0u)+l->pass_sent;') != 1
            or code.count('l->sent_since=') != 1 or code.count('l->held=') != 1 or code.count('l->pass_sent=0;') != 1
            or code.count('l->busy=') != 2 or code.count('l->busy=0;') != 1  # 1.4.0: the idle rule's only other write
            or code.count('l->tx=probe;l->sent_since+=n;l->pass_sent+=n;l->d.down_bytes+=n;') != 1 or code.count('l->sent_since+=') != 1
            or 'l->sent_since=l->x.tx_pending' in code or 'uint32_t held,pass_sent,busy;' not in code
            or not re.match(r'\s*l->now=now;\s*pj_pld_poll\(&l->d,now\);\s*if\(l->state==PJ_PL_OFF\)return 0;\s*'
                            r'l->busy=l->held\|\|l->pass_sent;'
                            r'l->held=l->x\.tx_pending\?\(uint32_t\)l->x\.tx_pending\(l->io\.ctx\):0;l->pass_sent=0;', step)):
        raise SystemExit('pocket_link.h: the credit must count from the previous pass (what the UART held when it began, '
                         'a packet in flight, everything handed since, 144 bytes more when the UART had work the pass '
                         'before), sampled first thing in pj_plink_step (credit review, 1.3.6)')
    if (host.count('static size_t tx_pending(void *c){platform *p=c;size_t n=p->ring_used?p->ring_used-(p->async&&p->budget<p->ring_used?p->budget:0u):0u;return n>shift_short?n-shift_short:0u;}') != 1
            or host.count('if(P.stale){unsigned real=P.level;P.level=stale_level;status(now_ms,NULL,0);P.level=real;}') != 1
            or host.count('P.async=1;P.stale=1;P.budget=0;') != 1
            or host.count('if(P.stale==2)stale_level-=P.tail<stale_level?P.tail:stale_level;') != 1
            or host.count('static const unsigned earlies[CREDIT_EARLIES]={0,20,52,68,76,100,155,200};') != 1
            or host.count('#define CREDIT_EARLIES 8u') != 1
            or host.count('for(unsigned s=0;s<2;s++)for(unsigned e=0;e<CREDIT_EARLIES;e++)') != 1
            or host.count('shift_short=s;P.stale=earlies[e]?2:1;P.early=earlies[e];') != 1
            or host.count('static const unsigned uarts[3]={113,115,524},cables[6]={0,7,20,80,320,640};') != 1
            or host.count('CHECK(P.max_level<=PJ_TETHER_QUEUE&&P.overflows==overflows&&!L.fpga_faults&&!P.faults&&L.state==PJ_PL_READY);') != 1
            or host.count('CHECK(runs==2u*2u*108u*CREDIT_EARLIES&&peak>=PJ_TETHER_QUEUE/2u&&fast_ticks<=CREDIT_FAST_TICKS);') != 1
            # 1.4.0: every run also with the line's idle time (the allowance dropped after 400 idle byte times), really used
            or host.count('for(unsigned w=0;w<2;w++)for(unsigned s=0;s<2;s++)for(unsigned e=0;e<CREDIT_EARLIES;e++)') != 1
            or host.count('L.x.idle_us=w?model_idle_us:NULL;') != 1 or host.count('CHECK(idle_cleared>100u);') != 1
            or not re.search(r'#define CREDIT_FAST_TICKS (2\d|3[0-2])u', host)):
        raise SystemExit('check_pocket_link.c: the credit case must run stale statuses (one pass old, and written up to '
                         '200 UART bytes before that, with and without the shift-register byte) over the stopped, '
                         'crawling and slow cables with a byte-exact '
                         'ring, and bound the fast rate\'s cost (credit review, 1.3.6)')
    # h. The profile table (shared/pocket_tether.h) grew to 8 rows with a cable rate each: the gateway takes a row by
    # its exact name and row 0 (the smallest) for a name it does not know; the host walks every row.
    if (code.count('const pj_tether_profile *p=pj_tether_profile_find(profile);if(!p)p=pj_tether_profiles(NULL);') != 1
            or 'PJ_TETHER_LINK_PROFILES' in code or 'link_khz' in code.replace('\\"link_khz\\"', '')
            or host.count('CHECK(rows==8u&&') != 1):
        raise SystemExit('pocket_link.h: a profile is taken by name from the shared table, an unknown name is its first '
                         'row, and the host check walks all 8 rows (credit review, 1.3.6)')
    # 1.5.0: the Pocket-acknowledged credit (docs/Design-notes.md "Launcher 1.5.0"). The bound is
    # down_total - acked, used only while an ACK moved it within 200 ms, else the 1.4.0 rule above unchanged; down_total
    # counts every 0x1F payload byte and nothing else; an ACK past it is refused; a repeated reply keeps its first end;
    # the margin is still 48 bytes and the UART room still caps every send (fpga_send). The host runs it over stale,
    # late, sparse and damaged ACKs and every cable, and a +200 B lie must break the bound and overflow the queue.
    tether = (ROOT / 'shared/pocket_tether.h').read_text(encoding='utf-8-sig')
    if (code.count('static inline uint32_t pj_plink_unsure(const pj_plink *l) {\n'
                   '    if(l->ack_have&&(uint32_t)(l->now-l->ack_ms)<=PJ_PLINK_ACK_FRESH_MS)return l->down_total-l->acked;\n'
                   '    return l->level+l->sent_since;\n}') != 1
            or '#define PJ_PLINK_ACK_FRESH_MS 200u' not in code or '#define PJ_TETHER_MARGIN 48u' not in tether
            or code.count('pj_plink_unsure(l)+PJ_PLINK_DATA_BYTES+PJ_PLINK_MARGIN') != 2
            or 'l->level+l->sent_since+PJ_PLINK_DATA_BYTES' in code
            or code.count('l->down_total+=') != 1 or code.count('l->d.down_bytes+=n;l->down_total+=n;') != 1
            or code.count('if((int32_t)(l->down_total-at)<0){l->ack_bad++;return;}') != 1
            or code.count('if(!l->ack_have||at!=l->acked)l->ack_ms=now;') != 1 or code.count('l->ack_ms=') != 1
            or code.count('for(unsigned i=0;i<PJ_PLINK_ACK_LOG;i++)if(l->ack_log[i].used&&l->ack_log[i].seq==seq)return;') != 1
            or code.count('pj_plink_tx_begin(l);') != 2 or code.count('pj_wire_tx_init(&l->tx,') != 1
            or code.count('if(!l->io.fpga_send(l->io.ctx,m,pj_plink_encode(m,PJ_TX_PLINK_DATA,data,n))){l->sp.stall_room++;break;}') != 1
            or 'pj_plink_puts(l,",\\"credit_ack\\":1");' not in code
            or host.count('ack_checks(ops,extra);') != 1 or host.count('CHECK(broken>0&&P.overflows>overflows);') != 1
            or host.count('CHECK(ack_bound_ok());') < 4):
        raise SystemExit('pocket_link.h: the ACK credit must be the bound down_total - acked while fresh and the 1.4.0 rule '
                         'otherwise, counted from 0x1F payload bytes only, refusing ACKs past it, a repeat kept at its first '
                         'end, the 48-byte margin and the UART room kept, with its host cases and negative control (1.5.0)')


def real_images():
    stock = stock_dump(allow_synthetic=True)
    for name, data in (('stock firmware', stock[0x10000:0x110000]),
                       ('built bootloader', (BUILD / 'bootloader/bootloader.bin').read_bytes()),
                       # LAUNCHER_IMAGE checks a candidate kept outside the build folder (the build folder
                       # holds no launcher image until it is reviewed, because the Manager installs from it).
                       ('built launcher', Path(os.environ.get('LAUNCHER_IMAGE') or BUILD / 'chromatic_launcher.bin').read_bytes())):
        check_image(data, name)
    print('PASS: stock firmware, built bootloader and built launcher validate as ESP32 images.')


if __name__ == '__main__':
    host_logic()
    composer_refusals()
    source_rules()
    pocket_link_rules()
    link_diag_rules()
    if (BUILD / 'chromatic_launcher.elf').is_file():
        flash_guard()
        real_images()
    else:
        print('SKIP: flash-target guard and image checks need a built launcher (scripts/Build-ChromaticLauncher.ps1).')

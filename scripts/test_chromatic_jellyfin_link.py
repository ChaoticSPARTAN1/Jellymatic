# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""The Chromatic's own Jellyfin sign-in (Quick Connect from the PC) and Chromatic Manager's
Wi-Fi setup, Re-link and Forget, against a fake Jellyfin server on 127.0.0.1 and the
simulated console. Synthetic users, passwords, networks and tokens only: the owner's
private files and server are never touched (test_chromatic_manager blocks them).
Prints only pass/fail.

Run: private/chromatic-tools/venv/Scripts/python.exe -B scripts/test_chromatic_jellyfin_link.py
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit

import chromatic_jellyfin_link as jl
import chromatic_manager as cm
import chromatic_provision as cp
from connection_config import Connection
from compose_chromatic_image import stock_dump
from test_chromatic_manager import Harness, check, release_or_skip

USER_ID = '6a1b2c3d4e5f60718293a4b5c6d7e8f9'
PASSWORD = 'Synthetic-Passw0rd!'
WIFI_PASSWORDS = ('first network pass', 'second network pass')


class FakeJellyfin:
    """Just enough of Jellyfin: Quick Connect, name sign-in, Users/Me, Logout, and the habit
    some versions have of reissuing the same token for the same DeviceId and user (the
    harder case for Re-link; the owner's 10.11.11 issues a new one instead)."""

    def __init__(self):
        self.quick_connect = True
        self.same_device = 'reissue'  # or 'replace' (10.11.11 seen)
        self.tokens = {}       # token -> {'device_id', 'user', 'client', 'device'}
        self.codes = {}        # code -> {'secret', 'device_id', 'authorized'}
        self.requests = []     # (method, path, headers, body) for privacy checks
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _auth(self):
                header = self.headers.get('Authorization', '')
                fields = dict(re.findall(r'(\w+)="([^"]*)"', header))
                token = self.headers.get('X-Emby-Token') or fields.get('Token')
                return fields, token

            def _reply(self, status, body=None):
                data = b'' if body is None else json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _issue(self, fields, client=None):
                device_id = fields.get('DeviceId', '')
                for token, info in list(fake.tokens.items()):
                    if info['device_id'] == device_id and info['user'] == USER_ID:
                        if fake.same_device == 'reissue':
                            return token  # some Jellyfin versions
                        del fake.tokens[token]  # 10.11: the old sign-in of that device ends
                token = secrets.token_hex(16)
                fake.tokens[token] = {'device_id': device_id, 'user': USER_ID, 'client': fields.get('Client'),
                                      'device': fields.get('Device')}
                return token

            def handle_any(self, method):
                parts = urlsplit(self.path)
                length = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(length) if length else b''
                fake.requests.append((method, self.path, dict(self.headers), raw))
                body = json.loads(raw) if raw else None
                fields, token = self._auth()
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                path = parts.path
                if path == '/System/Info/Public' and method == 'GET':
                    return self._reply(200, {'Version': '10.11.0'})
                if path == '/QuickConnect/Enabled' and method == 'GET':
                    return self._reply(200, fake.quick_connect)
                if path == '/QuickConnect/Initiate' and method == 'POST':
                    if not fake.quick_connect or not fields.get('DeviceId'):
                        return self._reply(401)
                    code, secret = f'{secrets.randbelow(10 ** 6):06d}', secrets.token_hex(32)
                    fake.codes[code] = {'secret': secret, 'device_id': fields['DeviceId'], 'authorized': False}
                    return self._reply(200, {'Secret': secret, 'Code': code, 'DeviceId': fields['DeviceId']})
                if path == '/Users/AuthenticateByName' and method == 'POST':
                    if body != {'Username': 'owner', 'Pw': PASSWORD}:
                        return self._reply(401)
                    return self._reply(200, {'AccessToken': self._issue(fields), 'User': {'Id': USER_ID}})
                if path == '/QuickConnect/Authorize' and method == 'POST':
                    if token not in fake.tokens or query.get('userId') != USER_ID or query.get('code') not in fake.codes:
                        return self._reply(401)
                    fake.codes[query['code']]['authorized'] = True
                    return self._reply(200, True)
                if path == '/Users/AuthenticateWithQuickConnect' and method == 'POST':
                    entry = next((c for c in fake.codes.values() if c['secret'] == (body or {}).get('Secret')), None)
                    if entry is None or not entry['authorized'] or entry['device_id'] != fields.get('DeviceId'):
                        return self._reply(400)
                    return self._reply(200, {'AccessToken': self._issue(fields),
                                             'User': {'Id': USER_ID[:8] + '-' + USER_ID[8:12] + '-' + USER_ID[12:16]
                                                      + '-' + USER_ID[16:20] + '-' + USER_ID[20:]}})
                if path == '/Users/Me' and method == 'GET':
                    return self._reply(200, {'Id': USER_ID}) if token in fake.tokens else self._reply(401)
                if path == '/Sessions/Logout' and method == 'POST':
                    if token not in fake.tokens:
                        return self._reply(401)
                    del fake.tokens[token]
                    return self._reply(204)
                return self._reply(404)

            def do_GET(self):
                self.handle_any('GET')

            def do_POST(self):
                self.handle_any('POST')

            def do_DELETE(self):
                self.handle_any('DELETE')

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def console_tokens(self):
        return {t: i for t, i in self.tokens.items() if i['device'] == jl.DEVICE}

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def main():
    server = FakeJellyfin()
    pc = Connection(server.url, 'owner', PASSWORD)
    state = tempfile.TemporaryDirectory()
    linker = jl.Linker(pc, state_file=Path(state.name) / 'linker.json')

    # ---- Quick Connect on the PC ----
    check(linker.reachable() and linker.quick_connect_enabled(), 'server reachable, Quick Connect on')
    first = linker.link()
    check(first.url == server.url and first.user_id == USER_ID and first.device_id.startswith('chromatic-')
          and linker.works(first), 'console sign-in made and works')
    entry = server.tokens[first.access_token]
    check(entry['client'] == jl.CLIENT and entry['device'] == jl.DEVICE and entry['device_id'] == first.device_id,
          'shows as its own device "Chromatic" in Dashboard > Devices')
    check(first.access_token not in [t for t, i in server.tokens.items() if i['device'] != jl.DEVICE],
          'a separate token from the PC\'s own sign-in')
    password_seen = [(m, p) for m, p, h, b in server.requests if PASSWORD.encode() in b or PASSWORD in json.dumps(h)]
    check(password_seen == [('POST', '/Users/AuthenticateByName')], 'the password goes only to the linker sign-in')
    check(not any(first.access_token in p for _, p, _, _ in server.requests), 'tokens never in URLs')
    second = linker.link()
    check(second.access_token != first.access_token and linker.works(first) and linker.works(second),
          'a fresh DeviceId gives a new token (Jellyfin reissues for the same DeviceId)')
    again = linker.link(device_id=second.device_id)
    check(again.access_token == second.access_token, 'same DeviceId: the server reissues the same token')
    check(linker.revoke(second.access_token, second.device_id) and not linker.works(second),
          'logout revokes the console token')
    # Owner rule: linking never ends any other session. Other devices are signed in (the PC
    # companion with its fixed DeviceId, a TV, a phone, a browser); they all survive linking,
    # re-linking and forgetting, with either server behaviour. The linker keeps one dedicated
    # session of its own and reuses its token.
    from jellyfin_client import JellyfinClient

    def other_sessions():
        return {t: dict(i) for t, i in server.tokens.items()
                if i['device'] != jl.DEVICE and i['client'] != jl.APPROVER_CLIENT}
    for behaviour in ('replace', 'reissue'):
        server.same_device = behaviour
        companion = JellyfinClient(pc)
        companion.connect()
        for client, device, device_id in (('Jellyfin Android TV', 'Living Room TV', 'tv-1234'),
                                          ('Jellyfin iOS', 'Phone', 'phone-5678'),
                                          ('Jellyfin Web', 'Firefox', 'web-9012')):
            server.tokens[secrets.token_hex(16)] = {'device_id': device_id, 'user': USER_ID, 'client': client,
                                                    'device': device}
        before = other_sessions()
        third = linker.link()
        fourth = linker.link()
        check(linker.revoke(third.access_token, third.device_id), 'revoke one console sign-in')
        check(other_sessions() == before and companion._token in server.tokens,
              f'{behaviour}: every other session survives linking and revoking (PC companion included)')
        check(len([i for i in server.tokens.values() if i['client'] == jl.APPROVER_CLIENT]) == 1,
              f'{behaviour}: the linker keeps exactly one session of its own')
        check(not linker.revoke(next(t for t, i in server.tokens.items() if i['client'] == jl.APPROVER_CLIENT),
                                jl.APPROVER_PREFIX + 'x') and not linker.revoke(companion._token,
                                                                                 'pocket-testing-local-pc'),
              f'{behaviour}: revoke refuses anything but a Chromatic console sign-in')
        check(other_sessions() == before, f'{behaviour}: still all there')
        linker.revoke(fourth.access_token, fourth.device_id)
        for token in before:
            server.tokens.pop(token, None)
    server.same_device = 'reissue'
    approvals = [h for m, p, h, b in server.requests if p == '/Users/AuthenticateByName'
                 and jl.APPROVER_CLIENT in h.get('Authorization', '')]
    check(len(approvals) == 1, 'the linker signed in by password once and reused its token after that')
    check(all('pocket-testing-local-pc' not in h.get('Authorization', '')
              for m, p, h, b in server.requests if p.startswith('/QuickConnect')),
          'approvals never use the companion\'s DeviceId')
    check(json.loads(linker.state_file.read_text())['device_id'].startswith(jl.APPROVER_PREFIX),
          'the linker\'s DeviceId is kept in its private file')
    check(linker.revoke(second.access_token, second.device_id), 'revoking twice is fine')
    linker.revoke(first.access_token, first.device_id)
    server.quick_connect = False
    try:
        linker.link()
        check(False, 'Quick Connect off must refuse')
    except jl.LinkError as error:
        check('Quick Connect' in str(error), 'Quick Connect off: a clear reason')
    server.quick_connect = True
    nowhere = jl.Linker(Connection('http://127.0.0.1:9', 'owner', PASSWORD))
    check(not nowhere.reachable(), 'unreachable server reported')
    try:
        jl.Linker(Connection(server.url, 'owner', 'wrong password')).link()
        check(False, 'bad PC sign-in must refuse')
    except jl.LinkError as error:
        check(PASSWORD not in str(error) and 'wrong' not in str(error), 'bad PC sign-in: generic reason')
    check(not server.console_tokens(), 'every test token logged out')
    check('access_token' not in repr(first) and first.access_token not in repr(first), 'records never print secrets')

    # ---- Chromatic Manager on the simulated console ----
    stock = stock_dump(allow_synthetic=True)
    release = release_or_skip('the Chromatic Manager part (sign-in written to and erased from a simulated console)')
    if release is None:
        return
    import wifi_gateway
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        wifi_file = folder / 'wifi.txt'
        wifi_file.write_text(f'ssid=Home Network\npassword={WIFI_PASSWORDS[0]}\n'
                             f'ssid = Upstairs\npassword = {WIFI_PASSWORDS[1]}\n', encoding='utf-8')
        names = ('WIFI_FILE', 'REMOTE_PILOT_PAIRING', 'PROVISION_COPY', 'lan_ipv4', 'jellyfin_settings_available',
                 'jellyfin_linker')
        saved = {name: getattr(cm, name) for name in names}
        saved_identity, init = wifi_gateway.IDENTITY, wifi_gateway.initialize
        try:
            wifi_gateway.IDENTITY = folder / 'identity'
            wifi_gateway.initialize = lambda: init(wifi_gateway.IDENTITY)
            cm.WIFI_FILE = wifi_file
            cm.REMOTE_PILOT_PAIRING, cm.PROVISION_COPY = folder / 'rp/pairing.json', folder / 'copy.json'
            cm.lan_ipv4 = lambda: '192.0.2.20'
            cm.jellyfin_settings_available = lambda: True
            cm.jellyfin_linker = lambda server_url=None: jl.Linker(pc if server_url is None else
                                                                   Connection(server_url), state_file=linker.state_file)
            h = Harness(stock, release)
            check(h.manager().install() == cm.EXIT_OK, 'install')
            installed = h.flash()
            server.same_device = 'replace'  # the owner's 10.11.11
            from jellyfin_client import JellyfinClient
            pc_companion = JellyfinClient(pc)
            pc_companion.connect()
            others = {pc_companion._token: dict(server.tokens[pc_companion._token])}
            for device_id in ('tv-owner', 'phone-owner'):
                token = secrets.token_hex(16)
                server.tokens[token] = others[token] = {'device_id': device_id, 'user': USER_ID,
                                                        'client': 'Jellyfin Media Player', 'device': device_id}

            def record():
                a, b = cm.span(h.flash(), cm.PROVISION_A), cm.span(h.flash(), cm.PROVISION_B)
                return cp.current(a, b)[0], a == b

            # Wi-Fi setup links Jellyfin by itself.
            check(cm.wifi_setup(h.manager(), lambda p: 'yes', lambda p: 'unused') == cm.EXIT_OK, 'Wi-Fi setup')
            linked, mirrored = record()
            check(mirrored and linked.sequence == 1 and [n.ssid for n in linked.networks] == ['Home Network', 'Upstairs']
                  and linked.gateway.ipv4 == '192.0.2.20' and linked.jellyfin is not None,
                  'record v2: both networks, PC pairing and Jellyfin, same in both sectors')
            check(linked.jellyfin.url == server.url and linker.works(linked.jellyfin), 'console token works')
            check(h.flash()[:cm.PROVISION[0]] == installed[:cm.PROVISION[0]], 'only the provisioning sectors changed')
            check(any('Jellyfin linked: yes; server reachable: yes.' == line for line in h.log), 'generic result shown')
            secrets_used = (PASSWORD, *WIFI_PASSWORDS, linked.jellyfin.access_token, linked.jellyfin.user_id,
                            linked.jellyfin.device_id, 'Home Network', 'Upstairs')
            check(not any(s in line for s in secrets_used for line in h.log), 'no password, token or name printed')
            h.log.clear()
            check(h.manager().status() == cm.EXIT_OK and 'Console setup: 2 Wi-Fi network(s), PC pairing yes, '
                  'Jellyfin linked.' in h.log, 'status shows the setup in counts and yes/no')

            # Re-link: a new token; the old one is logged out; Wi-Fi unchanged.
            old = linked.jellyfin
            check(cm.jellyfin_relink(h.manager()) == cm.EXIT_OK, 'Re-link Jellyfin')
            relinked, mirrored = record()
            check(mirrored and relinked.sequence == 2 and relinked.networks == linked.networks
                  and relinked.gateway == linked.gateway and relinked.jellyfin.access_token != old.access_token,
                  'new sign-in written, Wi-Fi and pairing kept')
            check(linker.works(relinked.jellyfin) and not linker.works(old), 'old token logged out, new one works')
            check(len(server.console_tokens()) == 1, 'exactly one console sign-in on the server')

            # A power cut in the middle of a re-link: the job is finished, one sign-in survives.
            h.console.cut_after = 0
            result = cm.jellyfin_relink(h.manager())
            if result not in (cm.EXIT_OK, cm.EXIT_RECOVERED):
                check(h.manager().resume() in (cm.EXIT_OK, cm.EXIT_RECOVERED), 'finish the interrupted re-link')
            after_cut, mirrored = record()
            check(after_cut is not None and mirrored and linker.works(after_cut.jellyfin), 'after a power cut the '
                  'console holds a working sign-in in both sectors')

            # Wi-Fi setup again while Quick Connect is off: Wi-Fi is written, Jellyfin kept.
            server.quick_connect = False
            kept = after_cut.jellyfin
            check(cm.wifi_setup(h.manager(), lambda p: 'yes', lambda p: 'unused') == cm.EXIT_OK, 'Wi-Fi setup, QC off')
            check(record()[0].jellyfin == kept and any(line.startswith('Jellyfin linked: no.') for line in h.log),
                  'Jellyfin sign-in kept and the reason said')
            server.quick_connect = True

            # Forget: removed from both sectors and logged out; Wi-Fi stays.
            check(cm.jellyfin_forget(h.manager()) == cm.EXIT_OK, 'Forget Jellyfin')
            forgotten, mirrored = record()
            check(mirrored and forgotten.jellyfin is None and len(forgotten.networks) == 2 and not linker.works(kept),
                  'sign-in gone from both sectors and logged out')
            check(not server.console_tokens(), 'no console sign-in left on the server')
            check(cm.jellyfin_forget(h.manager()) == cm.EXIT_OK and record()[0] == forgotten, 'forget twice: no write')
            check(not any(kept.access_token.encode() in h.flash()[o:o + 4096] for o in range(0, cm.SIZE, 4096)),
                  'the old token is nowhere in flash')

            # Re-link needs a Wi-Fi setup first, and a reachable server; both refuse before writing.
            fresh = Harness(stock, release)
            fresh.manager().install()
            before = fresh.flash()
            tokens = len(server.tokens)
            check(cm.jellyfin_relink(fresh.manager()) == cm.EXIT_REFUSED and fresh.flash() == before
                  and len(server.tokens) == tokens, 'no setup yet: refused, no sign-in made')
            cm.jellyfin_linker = lambda server_url=None: jl.Linker(Connection('http://127.0.0.1:9', 'owner', PASSWORD),
                                                                   state_file=linker.state_file)
            fresh.log.clear()
            check(cm.jellyfin_relink(fresh.manager()) == cm.EXIT_REFUSED and fresh.log == [
                'Jellyfin server reachable: no. Nothing was read or written.'], 'unreachable: refused before reading')

            # A version-1 record (one sector) is read and upgraded by the next setup.
            cm.jellyfin_linker = lambda server_url=None: jl.Linker(pc if server_url is None else
                                                                   Connection(server_url), state_file=linker.state_file)
            v1 = cp.Provision('Old Network', 'old password 1', '192.0.2.20', 8443, bytes(32), bytes(32),
                              bytes(32)).sector()
            legacy = Harness(stock, release)
            legacy.manager().install()
            legacy.console.flash[cm.PROVISION_B[0]:cm.PROVISION_B[1]] = v1
            legacy.log.clear()
            legacy.manager().status()
            check('Console setup: 1 Wi-Fi network(s), PC pairing yes, Jellyfin not set.' in legacy.log, 'v1 read')
            check(cm.wifi_setup(legacy.manager(), lambda p: 'yes', lambda p: 'unused') == cm.EXIT_OK, 'upgrade')
            upgraded = cp.current(cm.span(legacy.flash(), cm.PROVISION_A), cm.span(legacy.flash(), cm.PROVISION_B))[0]
            # Wi-Fi setup merges: the old record's network is kept with its password.
            check(upgraded.version == 2 and upgraded.jellyfin is not None and len(upgraded.networks) == 3
                  and upgraded.networks[2] == cp.Network('Old Network', 'old password 1'), 'now v2, old network kept')
            cm.jellyfin_forget(legacy.manager())

            # Remove and restore erase the console setup: they log exactly that console's own
            # sign-in out (no orphan device left); switch-off keeps it, and it stays live.
            def linked_console():
                c = Harness(stock, release)
                c.manager().install()
                check(cm.wifi_setup(c.manager(), lambda p: 'yes', lambda p: 'unused') == cm.EXIT_OK, 'linked console')
                rec = cp.current(cm.span(c.flash(), cm.PROVISION_A), cm.span(c.flash(), cm.PROVISION_B))[0]
                return c, rec.jellyfin
            c, token = linked_console()
            check(c.manager().disable() == cm.EXIT_OK and linker.works(token), 'switch-off keeps the sign-in live')
            check(c.manager().install() == cm.EXIT_OK and linker.works(token), 'install keeps it live')
            c.log.clear()
            check(c.manager().remove() == cm.EXIT_OK and c.flash() == stock and not linker.works(token)
                  and 'The console\'s Jellyfin sign-in was logged out on the server.' in c.log,
                  'remove erases the setup and logs that console sign-in out')
            check(not any(s in line for s in (token.access_token, token.device_id) for line in c.log), 'nothing secret said')
            c, token = linked_console()
            check(c.manager().restore_stock() == cm.EXIT_OK and c.flash() == stock and not linker.works(token),
                  'restore stock erases the setup and logs that console sign-in out')
            # A job recovery closed as superseded wrote nothing: the new sign-in is withdrawn and the
            # one the console still holds stays live.
            kept_now, unused = linker.link(), linker.link()
            m = c.manager()
            check(m.written_setup() == (False, None), 'a fresh manager has written nothing')
            cm._settle_jellyfin(m, cm.EXIT_OK, unused, kept_now)
            check(not linker.works(unused) and linker.works(kept_now), 'superseded: new withdrawn, old kept')
            linker.revoke(kept_now.access_token, kept_now.device_id)

            check(all(server.tokens.get(t) == i for t, i in others.items()),
                  'Wi-Fi setup, Re-link, Forget, remove, restore and a power cut left every other session signed in')
            for token in others:
                server.tokens.pop(token, None)
            server.same_device = 'reissue'

            # Install and switch-off keep the setup; remove erases it.
            check(h.manager().disable() == cm.EXIT_OK and record()[0] == forgotten, 'switch-off keeps it')
            check(h.manager().install() == cm.EXIT_OK and record()[0] == forgotten, 'install keeps it')
            check(h.manager().remove() == cm.EXIT_OK and h.flash() == stock, 'remove: back to the exact original')
        finally:
            for name, value in saved.items():
                setattr(cm, name, value)
            wifi_gateway.IDENTITY, wifi_gateway.initialize = saved_identity, init
    check(not server.console_tokens(), 'no test sign-in left behind')
    used = {(m, urlsplit(p).path) for m, p, h, b in server.requests}
    allowed = {('GET', '/System/Info/Public'), ('GET', '/QuickConnect/Enabled'), ('POST', '/QuickConnect/Initiate'),
               ('POST', '/Users/AuthenticateByName'), ('POST', '/QuickConnect/Authorize'),
               ('POST', '/Users/AuthenticateWithQuickConnect'), ('GET', '/Users/Me'), ('POST', '/Sessions/Logout')}
    check(used <= allowed, f'only these endpoints were used, nothing broad: {sorted(used - allowed)}')
    logouts = [h for m, p, h, b in server.requests if p == '/Sessions/Logout']
    check(logouts and all('DeviceId="chromatic-' in h.get('Authorization', '') and jl.APPROVER_PREFIX not in
                          h.get('Authorization', '') for h in logouts), 'every logout was a console sign-in')
    server.stop()
    print('PASS: Chromatic Jellyfin sign-in: Quick Connect on the PC gives the console its own revocable device '
          'token (password only to the PC sign-in, tokens never in URLs, reissue handled); Wi-Fi setup writes '
          'record v2 to both sectors with up to 4 networks and Jellyfin; Re-link logs the old token out; Forget '
          'removes it from flash and the server; remove and restore log the erased console sign-in out; a '
          'superseded job withdraws only the unused new sign-in; power cut, Quick Connect off, no setup, unreachable server and '
          'a v1 record handled; other sessions (PC companion, TV, phone, browser) survive every path; only console '
          'sign-ins are ever logged out; nothing secret printed. Fake server, simulated console, no hardware.')


if __name__ == '__main__':
    main()

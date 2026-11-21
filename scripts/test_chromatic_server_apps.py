# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""The optional server setups (scripts/chromatic_server_apps.py) against a fake Jellyfin server that already has the
owner's own tuner, guide source, libraries, devices and other signed-in sessions. No real server, NAS or network: the
public channel lists are canned, the "share" is a temporary folder the fake server lists as its library folders.

Live TV: the list (English only; pay-TV re-streams, geo-blocked, bare-IP, other-country entries never in it); the dry
run sends nothing that changes; Add copies the list, proves the server sees it, adds ONE tuner, verifies, keeps a
backup; refused/unclean adds leave nothing; Remove deletes exactly that tuner and the file and compares with the backup;
an earlier tool's tuner is recognised. ChromaTube: generic files (pinned digest, host network explained, no cookies,
360, direct_serve_media_playlist False, no owner data), the tool check, the folder found, the library added and removed
with every other library unchanged. Sessions: a saved token is used as it is; a password is a temporary device logged
out at the end; no other session or device is ever signed out; nothing private is printed.

Run: private/chromatic-tools/venv/Scripts/python.exe -B scripts/test_chromatic_server_apps.py
"""
import json
import re
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

import test_chromatic_manager as t  # the sandbox (server-setup records and backups in a temporary folder)
import chromatic_server_apps as sa

check = t.check
SECRETS = ['TOKEN-admin', 'TOKEN-user', 'owner-name', 'Den TV', 'Sample Tuner Box', 'tuner-box.invalid', '/media/movies',
           '/config/ChromaTube', 'hunter2', 'Movies', 'admin-name']

FEEDS = [{'channel': c, 'id': 'SD', 'languages': [lang]} for c, lang in (
    ('KidsToons.us', 'eng'), ('Pluto90s.us', 'eng'), ('CartoonNetwork.us', 'eng'), ('Dibujos.mx', 'spa'), ('UKToons.uk', 'eng'),
    ('FoxWeather.us', 'eng'), ('WxNation.us', 'eng'), ('TheWeatherChannel.us', 'eng'), ('MeteoFR.fr', 'fra'),
    ('ABCNewsLive.us', 'eng'), ('Telemundo.us', 'spa'), ('FoxNews.us', 'eng'), ('NBCNewsNow.us', 'eng'),
    ('MST3K.us', 'eng'), ('Archive.us', 'eng'), ('GeoBlocked.us', 'eng'), ('BareIp.us', 'eng'))]


def m3u(*entries):
    lines = ['#EXTM3U']
    for ident, name, url in entries:
        lines += [f'#EXTINF:-1 tvg-id="{ident}" group-title="X",{name}', url]
    return '\n'.join(lines) + '\n'


LISTS = {
    'animation': m3u(('KidsToons.us@SD', 'Kids Toons', 'https://cdn.example/kids.m3u8'),
                     ('Pluto90s.us@SD', '90s Kids', 'https://cdn.example/90s.m3u8'),
                     ('CartoonNetwork.us@SD', 'Cartoon Network', 'https://pirate.example/cn.m3u8'),
                     ('Dibujos.mx@SD', 'Dibujos', 'https://cdn.example/dib.m3u8'),
                     ('UKToons.uk@SD', 'UK Toons', 'https://cdn.example/uk.m3u8'),
                     ('BareIp.us@SD', 'Bare IP Toons', 'http://10.1.2.3:8080/x.m3u8')),
    'weather': m3u(('FoxWeather.us@SD', 'Fox Weather', 'https://cdn.example/fw.m3u8'),
                   ('WxNation.us@SD', 'WeatherNation', 'https://cdn.example/wn.m3u8'),
                   ('TheWeatherChannel.us@SD', 'The Weather Channel', 'https://pirate.example/twc.m3u8'),
                   ('MeteoFR.fr@SD', 'Meteo', 'https://cdn.example/meteo.m3u8'),
                   ('GeoBlocked.us@SD', 'Some Weather [Geo-blocked]', 'https://cdn.example/geo.m3u8')),
    'news': m3u(('ABCNewsLive.us@SD', 'ABC News Live', 'https://cdn.example/abc.m3u8'),
                ('Telemundo.us@SD', 'Telemundo', 'https://cdn.example/tm.m3u8'),
                ('FoxNews.us@SD', 'Fox News', 'https://pirate.example/fn.m3u8'),
                ('NBCNewsNow.us@SD', 'NBC News NOW', 'https://cdn.example/nbc.m3u8')),
    'classic': m3u(('MST3K.us@SD', 'MST3K', 'https://cdn.example/mst.m3u8'),
                   ('Archive.us@SD', 'The Archive', 'https://cdn.example/archive.m3u8'),
                   ('KidsToons.us@SD', 'Kids Toons', 'https://cdn.example/kids.m3u8')),
}


def fetch(url):
    if url == sa.FEEDS:
        return json.dumps(FEEDS).encode()
    return LISTS[url.rsplit('/', 1)[1][:-4]].encode()


class Fake:
    """A Jellyfin server: the owner's tuner and guide, two libraries whose folders are the local share, other sessions."""

    def __init__(self, share, mode=''):
        self.share, self.mode = Path(share), mode
        self.tuners = [{'Id': 'owner1', 'Type': 'm3u', 'Url': 'http://tuner-box.invalid/channels.m3u', 'FriendlyName': 'Sample Tuner Box'}]
        self.guides = [{'Id': 'g1', 'Type': 'xmltv'}]
        self.libraries = [{'Name': 'Movies', 'ItemId': 'lib-movies', 'CollectionType': 'movies', 'Locations': ['/media/movies'],
                           'LibraryOptions': {'x': 1}},
                          {'Name': 'Shows', 'ItemId': 'lib-shows', 'CollectionType': 'tvshows', 'Locations': ['/media/shows'],
                           'LibraryOptions': {'y': 2}}]
        self.tokens = {'TOKEN-admin': 'admin', 'TOKEN-user': 'user', 'TOKEN-den-tv': 'user', 'TOKEN-companion': 'admin'}
        self.devices = ['tv-in-den', 'pocket-testing-local-pc', 'chromatic-console']
        self.logouts, self.seen, self.headers, self.polls, self.refreshed = [], [], [], 0, []
        self.passwords = {'admin-name': ('hunter2', 'admin'), 'owner-name': ('pw', 'user')}

    def server_path(self, path):
        """Server folders: /media/movies and /media/shows are the share's Movies and Shows; /config holds ChromaTube."""
        path = PurePosixPath(path)
        roots = {'/media/movies': self.share / 'Movies', '/media/shows': self.share / 'Shows', '/config': self.share.parent / 'config'}
        for root, local in roots.items():
            root = PurePosixPath(root)
            if path == root or root in path.parents:
                return local.joinpath(*path.relative_to(root).parts)
        return None

    def listing(self, path):
        if path == '/media':
            return [{'Name': 'movies', 'Path': '/media/movies', 'Type': 'Directory'}, {'Name': 'shows', 'Path': '/media/shows', 'Type': 'Directory'}]
        local = self.server_path(path)
        if local is None or not local.is_dir():
            return None
        out = []
        for entry in sorted(local.iterdir()):
            if self.mode == 'blind' and entry.name == sa.LIST_FOLDER:
                continue  # the server cannot see what the PC wrote there
            out.append({'Name': entry.name, 'Path': str(PurePosixPath(path) / entry.name), 'Type': 'Directory' if entry.is_dir() else 'File'})
        return out

    def channels(self):
        items = [{'Id': f'own{i}', 'Name': 'Den TV'} for i in range(10)]
        if any(t['FriendlyName'] == sa.TUNER_NAME for t in self.tuners):
            self.polls += 1
            if self.polls > 2:
                items += [{'Id': f'new{i}'} for i in range(4)]
        return {'Items': items, 'TotalRecordCount': len(items)}

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, value=None):
                body = json.dumps(value).encode() if value is not None else b''
                self.send_response(code)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def who(self):
                token = self.headers.get('X-Emby-Token', '')
                return token, fake.tokens.get(token)

            def handle_any(self, method):
                path, _, raw = self.path.partition('?')
                query = dict(urllib.parse.parse_qsl(raw))
                body = self.rfile.read(int(self.headers.get('Content-Length', 0) or 0))
                fake.seen.append((method, path))
                fake.headers.append((method, path, self.headers.get('Authorization', '')))
                if (method, path) == ('POST', '/Users/AuthenticateByName'):
                    data = json.loads(body)
                    known = fake.passwords.get(data.get('Username'))
                    if not known or known[0] != data.get('Pw'):
                        return self.reply(401)
                    device = re.search(r'DeviceId="([^"]+)"', self.headers.get('Authorization', '')).group(1)
                    token = 'TOKEN-temp-' + device
                    fake.tokens[token] = known[1]
                    fake.devices.append(device)
                    return self.reply(200, {'AccessToken': token, 'User': {'Id': 'u' * 32}})
                token, role = self.who()
                if role is None:
                    return self.reply(401)
                if method == 'GET':
                    if path == '/Users/Me':
                        return self.reply(200, {'Id': 'u' * 32, 'Name': 'owner-name', 'Policy': {'IsAdministrator': role == 'admin'}})
                    if role != 'admin' and path in ('/System/Configuration/livetv', '/Environment/DirectoryContents', '/Library/VirtualFolders'):
                        return self.reply(403)
                    if path == '/System/Configuration/livetv':
                        return self.reply(200, {'TunerHosts': fake.tuners, 'ListingProviders': fake.guides, 'Other': 1})
                    if path == '/LiveTv/Channels':
                        return self.reply(200, fake.channels())
                    if path == '/Library/VirtualFolders':
                        return self.reply(200, fake.libraries)
                    if path == '/Environment/DirectoryContents':
                        found = fake.listing(query.get('path', ''))
                        return self.reply(200, found) if found is not None else self.reply(500)
                    simple = {'/Users': [{'Id': 'u' * 32}, {'Id': 'v' * 32}], '/Devices': {'Items': [{'Id': d} for d in fake.devices
                                                                                                      if not d.startswith('chromatic-manager-setup-')]},
                              '/Plugins': [], '/ScheduledTasks': [{'Key': 'RefreshGuide', 'Triggers': []}],
                              '/System/Configuration/encoding': {'HardwareAccelerationType': 'none'}}
                    return self.reply(200, simple[path]) if path in simple else self.reply(404)
                if (method, path) == ('POST', '/Sessions/Logout'):
                    fake.logouts.append(token)
                    fake.tokens.pop(token, None)
                    return self.reply(204)
                if (method, path) == ('POST', '/LiveTv/TunerHosts'):
                    if fake.mode == 'add-refused':
                        return self.reply(500)
                    info = dict(json.loads(body), Id='tuner-new')
                    fake.tuners.append(info)
                    if fake.mode == 'add-two':
                        fake.tuners.append({'Id': 'surprise', 'FriendlyName': 'Something else', 'Type': 'hdhomerun'})
                    return self.reply(200, info)
                if (method, path) == ('DELETE', '/LiveTv/TunerHosts'):
                    assert query['id'] != 'owner1', 'the owner\'s tuner may never be deleted'
                    if fake.mode == 'delete-fails':
                        return self.reply(500)
                    fake.tuners = [x for x in fake.tuners if x['Id'] != query['id']]
                    return self.reply(204)
                if (method, path) == ('POST', '/Library/VirtualFolders'):
                    assert query['name'] == sa.LIBRARY_NAME and query['refreshLibrary'] == 'false', 'no server-wide scan'
                    options = json.loads(body)['LibraryOptions']
                    fake.libraries.append({'Name': query['name'], 'ItemId': 'lib-ct', 'CollectionType': query['collectionType'],
                                           'Locations': [p['Path'] for p in options['PathInfos']], 'LibraryOptions': options})
                    return self.reply(204)
                if (method, path) == ('POST', '/Items/lib-ct/Refresh'):
                    fake.refreshed.append(query.get('Recursive'))
                    return self.reply(204)
                if (method, path) == ('DELETE', '/Library/VirtualFolders'):
                    assert query['name'] == sa.LIBRARY_NAME, 'only the ChromaTube library may be deleted'
                    fake.libraries = [x for x in fake.libraries if x['Name'] != query['name']]
                    return self.reply(204)
                raise AssertionError(f'unexpected request {method} {path}')

            def do_GET(self):
                self.handle_any('GET')

            def do_POST(self):
                self.handle_any('POST')

            def do_DELETE(self):
                self.handle_any('DELETE')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def changing(self):
        return [(m, p) for m, p in self.seen if m != 'GET' and p != '/Users/AuthenticateByName']


def make_share(root):
    share = Path(root) / 'share'
    for library, names in (('Movies', ('A (2001)', 'B (2002)', 'C (2003)', 'D (2004)', 'E (2005)', 'F (2006)')),
                           ('Shows', ('S1', 'S2', 'S3', 'S4', 'S5'))):
        for name in names:
            (share / library / name).mkdir(parents=True)
    channels = Path(root) / 'config' / 'ChromaTube'
    for name in ('Channel One', 'Channel Two'):
        (channels / name).mkdir(parents=True)
    (Path(root) / 'config' / 'empty' / 'ChromaTube').mkdir(parents=True)
    return share


def clean(lines, name, extra=()):
    text = '\n'.join(lines)
    bad = [s for s in SECRETS + list(extra) if s in text]
    check(not bad, f'{name}: nothing private printed ({bad})')


TEMPLATE = Path(__file__).resolve().parents[2] / 'ChromaTube' / 'server'  # a ChromaTube clone beside this one: read only, when present


def settings_only(compose):
    """The compose file's settings without comments, each volume's source replaced (the Manager writes the user's
    absolute folders where the public template has ./ files and a .env variable)."""
    out = []
    for line in compose.splitlines():
        line = line.split('#', 1)[0].rstrip()
        if not line.strip():
            continue
        m = re.fullmatch(r'(\s*- )"?(\$\{[^}]*\}|\.?/[^:"]*):(/[^":]*)(:ro)?"?', line)
        out.append(f'{m.group(1)}SOURCE:{m.group(3)}{m.group(4) or ""}' if m else line)
    return out


def check_chromatube_files(root):
    """C1 of the publication audit: the generated files match ChromaTube's public server template (sign-in required,
    settings read-only, no untested capability changes, no privilege escalation, the digest note, the firewall step as
    the real protection: the final audit's CT-1/CT-2)."""
    compose = sa.compose_text('/volume2/docker/chromatube/media', '/volume2/docker/chromatube')
    plain = settings_only(compose)
    mounts = [line for line in plain if line.strip().startswith('- SOURCE:')]
    check(sa.IMAGE in compose and '@sha256:' in compose and 'network_mode: host' in plain[plain.index('    network_mode: host')]
          and 'Terms of Service' in compose and not any(s in compose for s in SECRETS) and 'cookies' not in compose.lower(),
          'compose: pinned digest, host network explained, terms named, none of the fixture\'s private names')
    check(len(mounts) == 5 and sum(m.endswith('.json:ro') for m in mounts) == 4 and mounts[0].endswith(':/media/ChromaTube'),
          'compose: the four settings files mounted read-only (:ro); the list folder writable')
    check(not any('cap_drop' in line or 'cap_add' in line for line in plain)
          and '      - no-new-privileges:true' in plain and '    pids_limit: 256' in plain and '        max-size: "10m"' in plain,
          'compose: no untested capability changes, no-new-privileges, process limit, rotated logs')
    check('latest" build of 2026-10-25 (upstream commit 7d4a047)' in compose and 'not a release tag' in compose
          and 'from your Jellyfin server only (required)' in compose and 'REQUIRED' in compose
          and 'The firewall is the real protection' in compose and 'command' in compose
          and 'guards the settings pages' in compose and 'mounted read-only' in compose,
          'compose comments: the digest note, the firewall rule required (the real protection), the sign-in required '
          'for the settings pages, read-only settings')
    check('0.0.0.0' not in '\n'.join(plain) and 'ports:' not in compose and 'privileged: true' not in compose
          and 'docker.sock' not in compose, 'no catch-all address, no published ports, no privileged mode, no Docker socket')
    signin = sa.new_signin()
    files = sa.settings_files('192.168.1.20', ['https://www.youtube.com/@SomeChannel', '@Other'], signin)
    youtube, config = json.loads(files['yt_config.json']), json.loads(files['config.json'])
    check(youtube['video_quality'] == '360' and youtube['direct_serve_media_playlist'] == 'False' and youtube['cookie_value'] == ''
          and youtube['jellyfin_api_key'] == '' and youtube['jellyfin_integration'] == 'False' and youtube['jellyfin_base_url'] == ''
          and config['ytdlp2strm_host'] == '192.168.1.20'
          and json.loads(files['channel_list.json']) == ['https://www.youtube.com/@SomeChannel', 'https://www.youtube.com/@Other'],
          'settings: 360, direct_serve_media_playlist False, no cookies, no Jellyfin key or link, the channels')
    check(config['ytdlp2strm_web_username'] == signin['username'] and config['ytdlp2strm_web_password'] == signin['password']
          and signin['username'].startswith('chromatube-') and len(signin['password']) >= 24
          and sa.new_signin()['password'] != signin['password'], 'config.json: the page sign-in, random, set (never empty)')
    for bad in (None, {}, {'username': '', 'password': ''}, {'username': 'chromatube-x1', 'password': 'short'}):
        try:
            sa.settings_files('192.168.1.20', ['@a'], bad)
            check(False, 'settings without a page sign-in are refused')
        except ValueError:
            pass
    for host in ('8.8.8.8', 'example.com', ''):
        try:
            sa.settings_files(host, ['@a'], signin)
            check(False, f'a public address is refused: {host}')
        except ValueError:
            pass
    if TEMPLATE.is_dir():  # ChromaTube's public template, read only
        public = settings_only((TEMPLATE / 'docker-compose.yml').read_text(encoding='utf-8'))
        check(plain == public, 'compose settings equal ChromaTube\'s public server/docker-compose.yml (only the folders differ)'
              + ('' if plain == public else f': {[(a, b) for a, b in zip(plain, public) if a != b][:3] or (len(plain), len(public))}'))
        template = {name: json.loads((TEMPLATE / name).read_text(encoding='utf-8'))
                    for name in ('yt_config.json', 'crons.json', 'config.example.json')}
        check(youtube == template['yt_config.json'] and json.loads(files['crons.json']) == template['crons.json']
              and sorted(config) == sorted(template['config.example.json'])
              and all(config[k] == v for k, v in template['config.example.json'].items() if not str(v).startswith('CHANGE-ME')),
              'yt_config.json, crons.json and config.json equal the public templates (config: its CHANGE-ME values filled)')
    else:
        print('INFO ChromaTube\'s public template is not on this PC: compared with the built-in expectations only')
    # Save files: the five files, the sign-in kept privately (made once, then reused), never printed or said
    out = Path(root) / 'tube-files'
    if sa.CHROMATUBE_SIGNIN.exists():
        sa.CHROMATUBE_SIGNIN.unlink()
    names = sa.save_server_files(out, '192.168.1.20', '/volume2/docker/chromatube/media', '/volume2/docker/chromatube', ['@Some'])
    kept = sa.saved_signin()
    written = json.loads((out / 'config.json').read_text(encoding='utf-8'))
    check(names == ['channel_list.json', 'config.json', 'crons.json', 'docker-compose.yml', 'yt_config.json']
          and sorted(p.name for p in out.iterdir()) == names and kept is not None
          and written['ytdlp2strm_web_username'] == kept['username'] and written['ytdlp2strm_web_password'] == kept['password']
          and str(sa.CHROMATUBE_SIGNIN).startswith(str(sa.BACKUPS.parent)),
          'Save files: five files; config.json and the private copy on this PC hold the same random sign-in')
    import os
    import subprocess
    if os.name == 'nt':
        acl = subprocess.run(['icacls', str(sa.CHROMATUBE_SIGNIN)], capture_output=True, text=True).stdout
        entries = [line for line in acl.splitlines()[:-1] if ':(' in line]
        check(len(entries) == 1 and '(I)' not in acl and os.environ.get('USERNAME', '?').lower() in entries[0].lower()
              and '(F)' in entries[0], 'the private sign-in file: only this PC\'s user may open it (no inherited permissions)')
    else:
        check(sa.CHROMATUBE_SIGNIN.stat().st_mode & 0o077 == 0, 'the private sign-in file: mode 0600')
    sa.save_server_files(out, '192.168.1.21', '/volume2/docker/chromatube/media', '/volume2/docker/chromatube', ['@Some'])
    again = json.loads((out / 'config.json').read_text(encoding='utf-8'))
    check(again['ytdlp2strm_web_password'] == kept['password'] and again['ytdlp2strm_host'] == '192.168.1.21',
          'saving again keeps the same sign-in (the running server\'s), with the new address')
    import contextlib
    import io
    import chromatic_manager as cm
    said, printed = [], io.StringIO()
    manager = type('M', (), {'say': lambda self, line: said.append(line)})()
    answers = iter([str(Path(root) / 'tube-text'), '192.168.1.20', '/volume2/x/media', '/volume2/x', '@One', ''])
    with contextlib.redirect_stdout(printed):
        code = cm.chromatube_menu(manager, lambda prompt: 'files' if prompt.startswith('Type files') else next(answers))
    text = printed.getvalue() + '\n'.join(said)
    check(code == cm.EXIT_OK and (Path(root) / 'tube-text' / 'config.json').exists() and kept['password'] not in text
          and kept['username'] not in text and 'chromatube-server-signin.json' in text and '3. REQUIRED' in text,
          'the text menu: files written, where the sign-in is said, the sign-in itself never printed; the steps shown')
    check(kept['password'] not in sa.SIGNIN_WHERE + '\n'.join(sa.STEPS) + compose, 'the sign-in is in no shown text')


def check_tool_page():
    """Check it answers, against local stand-ins of the tool's page: one with the sign-in in force (Basic auth), one
    open to anyone. The sign-in goes only in the Authorization header to that address."""
    kept = sa.saved_signin()
    seen = []

    def page(open_page):
        def do_get(self):
            seen.append(self.headers.get('Authorization'))
            import base64
            want = 'Basic ' + base64.b64encode(f'{kept["username"]}:{kept["password"]}'.encode()).decode()
            ok = open_page or self.headers.get('Authorization') == want
            self.send_response(200 if ok else 401)
            if not ok:
                self.send_header('WWW-Authenticate', 'Basic realm="ytdlp2STRM"')
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'ok')
        server = ThreadingHTTPServer(('127.0.0.1', 0), type('P', (BaseHTTPRequestHandler,), {
            'log_message': lambda self, *a: None, 'do_GET': do_get}))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server
    guarded, open_page = page(False), page(True)
    try:
        port = guarded.server_port
        results = (sa.tool_check('127.0.0.1', port), sa.tool_check('127.0.0.1', open_page.server_port),
                   sa.tool_check('127.0.0.1', port, signin={'username': 'chromatube-other1', 'password': 'x' * 30}),
                   sa.tool_check('127.0.0.1', 1), sa.tool_check('8.8.8.8'))
        check(results == ('ok', 'open', 'wrong', 'down', 'lan') and sa.tool_answers('127.0.0.1', port)
              and not sa.tool_answers('127.0.0.1', open_page.server_port),
              'Check it answers: with the sign-in and refused without it = yes; open to anyone, a wrong sign-in, not '
              'running = no, each said plainly; a public address is never asked')
        saved = sa.CHROMATUBE_SIGNIN.read_bytes()
        sa.CHROMATUBE_SIGNIN.unlink()
        check(sa.tool_check('127.0.0.1', port) == 'unknown', 'no sign-in kept on this PC: said so')
        sa.CHROMATUBE_SIGNIN.write_bytes(saved)
        check(seen[0] is None and all(a is None or a.startswith('Basic ') for a in seen) and kept['password'] not in
              ''.join(sa.CHECK_WORDS.values()), 'the first ask carries no sign-in; the sign-in only in the Authorization header')
    finally:
        for server in (guarded, open_page):
            server.shutdown()
            server.server_close()
    # review M2 of 1.6.5: a redirect is never followed, so the sign-in cannot reach another host
    elsewhere_seen = []

    def serve(handler):
        server = ThreadingHTTPServer(('127.0.0.1', 0), type('R', (BaseHTTPRequestHandler,), {
            'log_message': lambda self, *a: None, 'do_GET': handler}))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def elsewhere_get(self):
        elsewhere_seen.append(self.headers.get('Authorization'))
        self.send_response(200)
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'ok')
    elsewhere = serve(elsewhere_get)
    target = f'http://127.0.0.1:{elsewhere.server_port}/'

    def answer(self, code, location=None):
        self.send_response(code)
        if location:
            self.send_header('Location', location)
        if code == 401:
            self.send_header('WWW-Authenticate', 'Basic realm="ytdlp2STRM"')
        self.send_header('Content-Length', '0')
        self.end_headers()
    signed_redirect = serve(lambda self: answer(self, 302, target) if self.headers.get('Authorization') else answer(self, 401))
    bare_redirect = serve(lambda self: answer(self, 301, target))
    try:
        results = (sa.tool_check('127.0.0.1', signed_redirect.server_port), sa.tool_check('127.0.0.1', bare_redirect.server_port))
        check(results == ('down', 'down') and elsewhere_seen == [],
              'redirects are never followed: a 302 to the signed ask and a 301 to the bare one are "down"; the other host '
              'never sees a request, let alone the sign-in (review M2)')
    finally:
        for server in (elsewhere, signed_redirect, bare_redirect):
            server.shutdown()
            server.server_close()


def main():
    sa.BACKUPS.mkdir(parents=True, exist_ok=True)
    # ---- the list ----
    text, kept, dropped = sa.build_list([g.key for g in sa.GROUPS], fetch)
    # English cartoons from any country (livetv_common.py); the other groups US only
    check(kept == {'cartoons': 3, 'weather': 2, 'news': 2, 'classic': 2}, f'English, US and allowed channels only ({kept})')
    check('UK Toons' in text, 'English cartoons are not limited to the US')
    for name in ('Cartoon Network', 'The Weather Channel', 'Fox News', 'Telemundo', 'Dibujos', 'Meteo', 'Geo-blocked', '10.1.2.3'):
        check(name not in text, f'never in the list: {name}')
    check(dropped['twice'] == 1 and text.count('Kids Toons') == 1 and 'group-title="Classic TV"' in text
          and 'group-title="English cartoons"' in text, 'a channel in two groups is listed once; groups named')
    check(sa.build_list(['weather'], fetch)[1] == {'weather': 2}, 'one group alone')
    for bad in ([], ['nope']):
        try:
            sa.build_list(bad, fetch)
            check(False, 'no group: refused')
        except sa.SetupError:
            pass
    try:
        sa.fetch_url('https://example.com/list.m3u')
        check(False, 'only iptv-org is fetched')
    except sa.SetupError:
        pass

    with tempfile.TemporaryDirectory() as root:
        share = make_share(root)
        # ---- sessions ----
        fake = Fake(share).start()
        try:
            s = sa.Session.with_token(fake.url, 'TOKEN-admin')
            check(s.admin and not s.temporary and all(not a for m, p, a in fake.headers), 'a saved token: used as it is, no device header')
            check(s.close() and not fake.logouts, 'a saved token is never signed out')
            s = sa.typed_session(fake.url, 'admin-name', 'hunter2')
            device = next(re.search(r'DeviceId="([^"]+)"', a).group(1) for m, p, a in fake.headers if p == '/Users/AuthenticateByName')
            check(s.admin and s.temporary and device.startswith(sa.SESSION_PREFIX) and device in fake.devices,
                  'a typed administrator: a temporary device of this job')
            check(s.close() and fake.logouts == ['TOKEN-temp-' + device] and 'TOKEN-den-tv' in fake.tokens and 'TOKEN-companion' in fake.tokens,
                  'closed: only its own session signed out; every other session still signed in')
            check(not sa.Session.with_token(fake.url, 'TOKEN-user').admin, 'a non-administrator is seen as one')
            for bad in (('admin-name', 'wrong'), ('nobody', 'x')):
                try:
                    sa.typed_session(fake.url, *bad)
                    check(False, 'a wrong password is refused')
                except sa.SetupError:
                    pass
            try:
                sa.typed_session('not a url', 'a', 'b')
                check(False, 'a bad address is refused')
            except sa.SetupError:
                pass
            s = sa.Session.with_token(fake.url, 'TOKEN-admin')
            for method, path in (('POST', '/Library/Refresh'), ('DELETE', '/Devices'), ('POST', '/Sessions/Logout'), ('POST', '/System/Restart'),
                                 ('DELETE', '/LiveTv/ListingProviders'), ('DELETE', '/Library/VirtualFolders')):
                sent = len(fake.seen)
                try:
                    s.send(method, path, sa.LIVETV_ALLOWED, query={'id': 'owner1', 'name': 'Movies'})
                    check(False, f'refused unsent: {method} {path}')
                except sa.SetupError:
                    pass
                check(len(fake.seen) == sent, f'never sent: {method} {path}')
            for name in ('Movies', 'Shows'):
                try:
                    s.send('DELETE', '/Library/VirtualFolders', sa.library_allowed(), query={'name': name})
                    check(False, 'another library may not be deleted')
                except sa.SetupError:
                    pass
        finally:
            fake.stop()

        # ---- Live TV: dry run, add, remove ----
        fake = Fake(share).start()
        lines = []
        try:
            s = sa.typed_session(fake.url, 'admin-name', 'hunter2')
            plan = sa.livetv_plan(s, [g.key for g in sa.GROUPS], share, fetch)
            check(plan.ready and plan.folder == share / 'Movies' and plan.server_folder == '/media/movies' and plan.tuners_now == 1
                  and plan.guides_now == 1, 'dry run: ready, the movies library folder found by its names')
            check(not fake.changing(), 'the dry run sent nothing that changes the server')
            shown = plan.text()
            check('nothing has been sent' in shown and sa.TUNER_NAME in shown and '"TunerCount": 2' in shown, 'the dry run says it all')
            clean([shown], 'dry run', [fake.url, str(share)])
            check(sa.livetv_add(s, plan, lines.append, sleep=lambda x: None, wait=1), 'add: done')
            check(len(fake.tuners) == 2 and fake.tuners[0]['FriendlyName'] == 'Sample Tuner Box' and fake.tuners[1]['FriendlyName'] == sa.TUNER_NAME
                  and fake.tuners[1]['Url'] == '/media/movies/ChromaticTV/chromatic-tv.m3u' and fake.tuners[1]['TunerCount'] == 2,
                  'one tuner added, reading the file as the server sees it; the owner\'s tuner untouched')
            check((share / 'Movies' / sa.LIST_FOLDER / sa.LIST_FILE).read_text(encoding='utf-8') == plan.m3u, 'the list file is in place')
            check(sa.LIVETV_STATE.exists() and any(p.name.startswith('before-add-') for p in sa.BACKUPS.iterdir()), 'backup and record kept')
            state = json.loads(sa.LIVETV_STATE.read_text(encoding='utf-8'))
            check(sa.TUNER_NAME == 'Chromavision Live TV (added by Chromavision tools)' and state['name'] == sa.TUNER_NAME
                  and state['tuner_id'] == 'tuner-new' and state['added_utc'] and state['backup'].startswith('before-add-'),
                  'new installs use the public tool\'s name and its record keys (tuner_id, backup, added_utc), plus the name used')
            check(fake.changing() == [('POST', '/LiveTv/TunerHosts')], f'the only change: one POST ({fake.changing()})')
            check(any('new channels listed: 4' in l for l in lines), 'its channels appear')
            check(not sa.livetv_add(s, sa.livetv_plan(s, ['weather'], share, fetch), lines.append, sleep=lambda x: None, wait=1)
                  and len(fake.tuners) == 2, 'a second add is refused')
            check(sa.livetv_status(s) == {'readable': True, 'added': True, 'recorded': True}, 'status')
            check(sa.livetv_remove(s, lines.append) and fake.tuners == [fake.tuners[0]] and fake.tuners[0]['Id'] == 'owner1'
                  and not (share / 'Movies' / sa.LIST_FOLDER).exists() and not sa.LIVETV_STATE.exists(),
                  'remove: exactly that tuner, its file and the folder it made; the settings equal the backup')
            check(not sa.livetv_remove(s, lines.append), 'remove with nothing recorded: refused')
            check(s.close() and len(fake.logouts) == 1, 'the job\'s own session signed out, nothing else')
            clean(lines, 'add and remove', [fake.url, str(share)])
        finally:
            fake.stop()
        # refused, unclean and blind adds leave nothing behind; a delete that fails keeps the record
        for mode in ('add-refused', 'add-two', 'blind', 'delete-fails'):
            fake = Fake(share, mode).start()
            lines = []
            try:
                s = sa.Session.with_token(fake.url, 'TOKEN-admin')
                plan = sa.livetv_plan(s, ['weather', 'news'], share, fetch)
                added = sa.livetv_add(s, plan, lines.append, sleep=lambda x: None, wait=1, look=2)
                if mode == 'delete-fails':
                    check(added and not sa.livetv_remove(s, lines.append) and sa.LIVETV_STATE.exists(), 'delete fails: the record is kept')
                    fake.mode = ''
                    check(sa.livetv_remove(s, lines.append) and not sa.LIVETV_STATE.exists(), 'tried again: removed')
                else:
                    check(not added and not sa.LIVETV_STATE.exists() and not (share / 'Movies' / sa.LIST_FOLDER).exists(),
                          f'{mode}: nothing of ours left (file and folder removed)')
                    check(all(x['FriendlyName'] != sa.TUNER_NAME for x in fake.tuners) and fake.tuners[0]['Id'] == 'owner1',
                          f'{mode}: no Chromatic TV tuner left; the owner\'s untouched')
                clean(lines, mode, [fake.url, str(share)])
            finally:
                fake.stop()
        # a folder that matches nothing, and a non-administrator
        fake = Fake(share).start()
        try:
            s = sa.Session.with_token(fake.url, 'TOKEN-admin')
            check(not sa.livetv_plan(s, ['weather'], Path(root) / 'config', fetch).ready, 'no matching folder: not ready')
            check(not sa.livetv_add(s, sa.livetv_plan(s, ['weather'], Path(root) / 'config', fetch), lines.append), 'not ready: no add')
            s = sa.Session.with_token(fake.url, 'TOKEN-user')
            plan = sa.livetv_plan(s, ['weather'], share, fetch)
            check(not plan.ready and any('administrator' in p for p in plan.problems), 'a non-administrator: not ready, said')
            check(not fake.changing(), 'nothing sent')
        finally:
            fake.stop()
        # the earlier tool's tuner (its record has no list file) is recognised and removed; its file stays
        fake = Fake(share).start()
        try:
            s = sa.Session.with_token(fake.url, 'TOKEN-admin')
            before = sa.livetv_settings(s)
            sa._write(sa.BACKUPS / 'before-add-earlier.json', {'livetv': before, 'fingerprint': sa.probe.take_fingerprint(s)[0]})
            fake.tuners.append({'Id': 'earlier', 'FriendlyName': sa.livetv_tuner.NAME, 'Url': '/media/movies/ChromaticTV/cartoons.m3u'})
            sa._write(sa.LIVETV_STATE, {'tuner_id': 'earlier', 'backup': 'before-add-earlier.json'})
            check(not sa.livetv_plan(s, ['weather'], share, fetch).ready, 'already added by the earlier tool: Add is not offered')
            lines = []
            check(sa.livetv_remove(s, lines.append) and len(fake.tuners) == 1 and any('earlier tool' in l for l in lines),
                  'the earlier tool\'s tuner removed exactly; its list file left where it is')
        finally:
            fake.stop()
        # review M3 of 1.6.4, the owner's case: the tuner was added long ago and Live TV changed since (another guide, another
        # tuner, an option): Remove deletes exactly our tuner, keeps every later change, says so, and closes the record
        fake = Fake(share).start()
        try:
            s = sa.Session.with_token(fake.url, 'TOKEN-admin')
            sa._write(sa.BACKUPS / 'before-add-long-ago.json', {'livetv': sa.livetv_settings(s), 'fingerprint': {}})
            fake.tuners.append({'Id': 'long-ago', 'FriendlyName': sa.livetv_tuner.NAME, 'Url': '/media/movies/ChromaticTV/x.m3u'})
            sa._write(sa.LIVETV_STATE, {'tuner_id': 'long-ago', 'backup': 'before-add-long-ago.json'})
            fake.guides.append({'Id': 'g2', 'Type': 'schedulesdirect'})                       # the owner's later changes
            fake.tuners.append({'Id': 'hdhr', 'FriendlyName': 'Second Tuner Box', 'Type': 'hdhomerun'})
            lines = []
            ok = sa.livetv_remove(s, lines.append)
            ids = [t['Id'] for t in fake.tuners]
            check(ok and ids == ['owner1', 'hdhr'] and [g['Id'] for g in fake.guides] == ['g1', 'g2'] and not sa.LIVETV_STATE.exists()
                  and any(l.startswith('PASS tuner removed; nothing else') for l in lines)
                  and any('changes made since then are kept' in l for l in lines) and not any(l.startswith('FAIL') for l in lines),
                  'remove after the owner changed Live TV since the add: exactly our tuner deleted, the later changes kept and '
                  'reported, the record closed (review M3)')
            check(not sa.livetv_plan(s, ['weather'], share, fetch).problems or
                  not any('already there' in p for p in sa.livetv_plan(s, ['weather'], share, fetch).problems),
                  'after it, Add is offered again (no stuck record)')
            # the recorded id now names a tuner that is not ours: nothing is deleted
            fake.tuners.append({'Id': 'reused', 'FriendlyName': 'Somebody else', 'Type': 'm3u'})
            sa._write(sa.LIVETV_STATE, {'tuner_id': 'reused', 'backup': 'before-add-long-ago.json'})
            lines = []
            check(not sa.livetv_remove(s, lines.append) and any(t['Id'] == 'reused' for t in fake.tuners) and sa.LIVETV_STATE.exists(),
                  'a recorded id that now belongs to another tuner: refused, nothing deleted')
            sa.LIVETV_STATE.unlink()
            clean(lines, 'remove after changes', [fake.url, str(share)])
        finally:
            fake.stop()
        # review M2 of 1.6.4: the list file goes only into a Movies library: a match with a music library is refused
        fake = Fake(share).start()
        try:
            fake.libraries[0] = dict(fake.libraries[0], CollectionType='music')
            s = sa.Session.with_token(fake.url, 'TOKEN-admin')
            plan = sa.livetv_plan(s, ['weather'], share, fetch)
            lines = []
            check(not plan.ready and plan.folder is None and any('not a movies library' in p for p in plan.problems)
                  and not sa.livetv_add(s, plan, lines.append) and not (share / 'Movies' / sa.LIST_FOLDER).exists()
                  and not (share / 'Shows' / sa.LIST_FOLDER).exists() and not fake.changing(),
                  'only a music or TV shows library matches: not ready, said in plain words, nothing written anywhere (review M2)')
        finally:
            fake.stop()

        # ---- ChromaTube ----
        check_chromatube_files(root)
        check(sa.private_host('nas.local') and sa.private_host('10.0.0.5') and not sa.private_host('93.184.216.34')
          and not sa.private_host('nas.example.com'), 'LAN addresses only')
        for bad in (['https://example.com/@x'], ['https://www.youtube.com/watch?v=abc'], [], ['@ok'] * 1 + ['@x%d' % i for i in range(21)]):
            try:
                sa.channels_from('\n'.join(bad))
                check(False, f'channels refused: {bad[:1]}')
            except ValueError:
                pass
        for bad in ('../x', 'relative/path', '/ok/../no'):
            try:
                sa.compose_text(bad, '/x')
                check(False, 'a bad folder is refused')
            except ValueError:
                pass
        check_tool_page()
        fake = Fake(share).start()
        lines = []
        try:
            s = sa.typed_session(fake.url, 'admin-name', 'hunter2')
            found = sa.find_chromatube_folders(s)
            check(found == ['/config/ChromaTube'], f'the folder Jellyfin already sees is found ({len(found)})')
            for path, why in (('/media/movies', 'inside'), ('/config/empty/ChromaTube', 'no channel folders'), ('/nowhere', 'does not see')):
                problems, _ = sa.chromatube_plan(s, path)
                check(problems and why in problems[-1], f'refused: {why}')
            problems, text = sa.chromatube_plan(s, '/config/ChromaTube')
            check(not problems and 'nothing has been sent' in text and not fake.changing(), 'dry run: ready, nothing sent')
            clean([text], 'chromatube dry run', [fake.url])
            before = [dict(x) for x in fake.libraries]
            check(sa.chromatube_add(s, '/config/ChromaTube', lines.append), 'add: done')
            mine = fake.libraries[-1]
            check(len(fake.libraries) == 3 and fake.libraries[:2] == before and mine['Name'] == 'ChromaTube'
                  and mine['CollectionType'] == 'tvshows' and mine['Locations'] == ['/config/ChromaTube']
                  and mine['LibraryOptions']['EnableInternetProviders'] is False and mine['LibraryOptions']['EnableRealtimeMonitor'] is False,
                  'one library, quiet settings; the others untouched')
            check(not sa.chromatube_add(s, '/config/ChromaTube', lines.append) and len(fake.libraries) == 3, 'a second add is refused')
            record = json.loads(sa.CHROMATUBE_STATE.read_text(encoding='utf-8'))
            fake.libraries[-1] = dict(mine, ItemId='someone-else')
            check(not sa.chromatube_remove(s, lines.append) and len(fake.libraries) == 3, 'remove refuses a library that is not ours')
            fake.libraries[-1] = mine
            check(record['item_id'] == 'lib-ct' and sa.chromatube_remove(s, lines.append) and fake.libraries == before
                  and (Path(root) / 'config' / 'ChromaTube' / 'Channel One').exists() and not sa.CHROMATUBE_STATE.exists(),
                  'remove: exactly that library; its files untouched; the others as before')
            # review L1 of 1.6.4: no record on this PC (an earlier tool or another PC added it)
            check(sa.chromatube_add(s, '/config/ChromaTube', lines.append) and sa.CHROMATUBE_STATE.exists(), 'added again')
            sa.CHROMATUBE_STATE.unlink()
            check(sa.chromatube_unrecorded(s) is True and not sa.chromatube_remove(s, lines.append) and len(fake.libraries) == 3,
                  'no record: a ChromaTube-like library is not removed without the owner\'s confirmation')
            check(sa.chromatube_remove(s, lines.append, unrecorded_ok=True) and fake.libraries == before
                  and (Path(root) / 'config' / 'ChromaTube' / 'Channel One').exists(),
                  'no record, confirmed: removed, its files untouched')
            fake.libraries.append({'Name': 'ChromaTube', 'ItemId': 'lib-other', 'CollectionType': 'tvshows',
                                   'Locations': ['/config/empty/ChromaTube'], 'LibraryOptions': {}})
            check(sa.chromatube_unrecorded(s) is False and not sa.chromatube_remove(s, lines.append, unrecorded_ok=True)
                  and len(fake.libraries) == 3, 'no record and not a ChromaTube folder (no channel folders): refused even if confirmed')
            fake.libraries[-1]['Locations'] = ['/media/shows']
            check(sa.chromatube_unrecorded(s) is False and not sa.chromatube_remove(s, lines.append, unrecorded_ok=True)
                  and len(fake.libraries) == 3, 'no record and a folder not named ChromaTube: refused')
            fake.libraries.pop()
            check(set(fake.changing()) <= {('POST', '/Library/VirtualFolders'), ('DELETE', '/Library/VirtualFolders'),
                                           ('POST', '/Items/lib-ct/Refresh')} and fake.refreshed == ['true', 'true'],
                  'only the library requests and one scan of that library alone changed anything')
            check(s.close() and len(fake.logouts) == 1, 'the job\'s own session signed out, nothing else')
            clean(lines, 'chromatube', [fake.url])
        finally:
            fake.stop()
    steps = '\n'.join(sa.STEPS)
    check('Terms of Service' in sa.TERMS and 'Undo everything' in steps and 'no sign-in of its own' not in steps
          and re.search(r'^3\. REQUIRED: on the Docker machine\'s firewall, allow port 5005 only from your Jellyfin '
                        r'server; allow this PC only while', steps, re.M)
          and 'The firewall is the real protection' in steps and 'command channel' in steps
          and 'capability' not in steps and 'refuses anyone' not in steps
          and 'mounted read-only' in steps and '7d4a047' in steps and 'not a release tag' in steps
          and 'asks for the sign-in' in steps and 'remove the PC\'s firewall rule' in steps,
          'the shown steps: sign-in required (settings pages only), the firewall step REQUIRED and the real protection '
          '(PC only while checking), read-only settings, the digest note, the check with the sign-in, the undo steps '
          '(C1, CT-1)')
    print('PASS: optional server setups against a fake Jellyfin (owner\'s tuner, guide, libraries and other sessions present): '
          'the Live TV list (English only; pay-TV, geo-blocked, bare-IP, other-country never; duplicates once); dry runs send '
          'nothing that changes; Add copies the list into a matched library folder, proves the server sees it, adds one tuner '
          '(2 streams), backup and record kept; refused, unclean and blind adds leave nothing; Remove deletes exactly that '
          'tuner and its file and equals the backup; the earlier tool\'s tuner is recognised; ChromaTube files are generic '
          '(pinned digest, host network explained, no cookies, 360, direct_serve_media_playlist False), the tool check, the '
          'folder found, one quiet library added and removed with the others untouched; a saved token is never signed out, '
          'a password is a temporary device signed out at the end, no other session ever; forbidden requests never sent; '
          'nothing private printed. No real server.')


if __name__ == '__main__':
    main()

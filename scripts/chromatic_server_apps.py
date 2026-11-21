# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Optional server setups for the console's TV and ChromaTube apps, run from Chromatic Manager.

Games, Jellyfin and the TV row's built-in Local Weather channel never depend on anything here. Each setup is
optional and independent, and follows these server rules (docs/Design-notes.md):

  * Add-only. Live TV channels adds ONE tuner ("Chromavision Live TV (added by Chromavision tools)", the public tool's
    name; the owner's earlier "Chromatic TV (added by Chromatic Manager)" counts as ours too, so it is never doubled),
    at most 2 streams; ChromaTube adds ONE library ("ChromaTube", without the server-wide scan; then that library alone
    is scanned). Nothing else is edited: no other tuner, guide source, library,
    user, device, plugin, task or setting, and no session is signed out except this job's own temporary one.
  * Backup first: the Live TV settings (or the library list) and a fingerprint of libraries, users, devices,
    plugins, tasks and conversion settings are saved in the private folder before anything changes.
  * Dry run first: plan() sends only GETs (and, with a password, this job's own sign-in) and says exactly what
    would be added. Nothing changes before the owner's Approve in the window.
  * Remove deletes exactly what was added (by its recorded id and name) and compares the result with the backup.
  * An administrator sign-in is needed. The PC's saved one is used only when it is an administrator and the owner
    agrees for this one job; otherwise the owner types one, which stays in memory for this job only. A password
    sign-in is a temporary device of this job ("Chromatic Manager" on "PC", a fresh DeviceId), logged out at the end:
    the only sign-out this module ever sends. A saved access token is used as it is (never signed out).
  * Nothing here prints, logs or returns an address, a path on the server, a name, a token or a channel title:
    results are counts and plain words. The private state files hold what Remove needs.

The records are kept in the per-user private folder (livetv-backups/), in the format of the earlier tools, so a tuner
or library those tools added is recognised and can be removed here too.
"""
from __future__ import annotations

import ipaddress
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import livetv_common as probe  # noqa: E402  (canonical, take_fingerprint, read)
import livetv_common as livetv_tuner  # noqa: E402  (the earlier tuner name, without)
import livetv_common as cartoons  # noqa: E402  (the English cartoon rules)
from user_paths import PRIVATE  # noqa: E402  (per-user folder, see user_paths.py)
from jellyfin_client import ClientError  # noqa: E402

BACKUPS = PRIVATE / 'livetv-backups'
LIVETV_STATE = BACKUPS / 'state.json'
CHROMATUBE_STATE = BACKUPS / 'chromatube-library.json'
# One tuner name for new installs, shared with the public Chromavision tool (tools/livetv_tuner.py), so either tool's
# Remove recognises what the other added. A tuner added by an earlier Chromatic Manager carries
# the older name: both names count as ours for status, "already there" and Remove (never a second tuner).
TUNER_NAME = 'Chromavision Live TV (added by Chromavision tools)'
TUNER_NAMES = (TUNER_NAME, livetv_tuner.NAME)
TUNER_STREAMS = 2
LIBRARY_NAME = 'ChromaTube'
LIST_FOLDER = 'ChromaticTV'      # inside a library folder the server already reads (a movies library ignores it)
LIST_FILE = 'chromatic-tv.m3u'
SESSION_CLIENT, SESSION_DEVICE, SESSION_PREFIX = 'Chromatic Manager', 'PC', 'chromatic-manager-setup-'
TIMEOUT = 30
AGENT = 'chromatic-manager (tv channel list)'  # nothing personal; iptv-org's public lists only


class SetupError(Exception):
    """A plain reason, never server data."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SetupError('The Jellyfin server redirected the request; use its final address.')


class _NeverFollow(urllib.request.HTTPRedirectHandler):
    """The ChromaTube tool check's rule (review M2 of 1.6.5): a redirect is never followed, so the page sign-in in the
    Authorization header can only ever go to the address typed; the 3xx comes back as an HTTPError with its code."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# ---- one administrator session for one job ----------------------------------------------------------------------
class Session:
    """GETs for anything; changing requests only from the job's own allow-list (send). The server address and
    the token stay inside; nothing about them is returned."""

    def __init__(self, server_url, token='', device_id='', temporary=False, opener=None):
        self.server_url = str(server_url).rstrip('/')
        self._token, self.device_id, self.temporary = token, device_id, temporary
        self._http = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())
        self.user_id, self.admin, self.sent, self.closed = '', False, [], False

    @classmethod
    def with_token(cls, server_url, token, opener=None):
        """The PC's saved access token, used as it is: no Authorization header names another device, so the token's
        own device is never rewritten; nothing is signed in or out."""
        session = cls(server_url, token=token, opener=opener)
        session._who()
        return session

    @classmethod
    def sign_in(cls, server_url, username, password, opener=None):
        """A temporary device of this job, logged out by close(). The password is used once and not kept."""
        session = cls(server_url, device_id=SESSION_PREFIX + secrets.token_hex(8), temporary=True, opener=opener)
        status, signed = session._request('POST', '/Users/AuthenticateByName', body={'Username': username, 'Pw': password})
        token = signed.get('AccessToken', '') if status == 200 and isinstance(signed, dict) else ''
        if not token:
            raise SetupError('The Jellyfin server did not accept that sign-in.')
        session._token = token
        session._who()
        return session

    def _who(self):
        status, me = self._request('GET', '/Users/Me')
        if status != 200 or not isinstance(me, dict) or not me.get('Id'):
            raise SetupError('The Jellyfin server did not accept that sign-in.')
        self.user_id = str(me['Id'])
        self.admin = bool((me.get('Policy') or {}).get('IsAdministrator'))

    def _headers(self):
        headers = {'Accept': 'application/json'}
        if self._token:
            headers['X-Emby-Token'] = self._token
        if self.temporary:
            headers['Authorization'] = (f'MediaBrowser Client="{SESSION_CLIENT}", Device="{SESSION_DEVICE}", '
                                        f'DeviceId="{self.device_id}", Version="1.6.4"'
                                        + (f', Token="{self._token}"' if self._token else ''))
        return headers

    def _request(self, method, path, query=None, body=None, timeout=TIMEOUT):
        if not path.startswith('/') or path.startswith('//'):
            raise SetupError('Bad request path.')
        url = self.server_url + path + ('?' + urllib.parse.urlencode(query) if query else '')
        headers = self._headers()
        data = None
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        elif method in ('POST', 'DELETE'):
            data = b'' if method == 'POST' else None
        try:
            with self._http.open(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=timeout) as reply:
                raw = reply.read(16 * 1024 * 1024)
                try:
                    return reply.status, (json.loads(raw) if raw.strip() else None)
                except ValueError:
                    return reply.status, None
        except SetupError:
            raise
        except urllib.error.HTTPError as error:
            error.close()
            return error.code, None
        except (OSError, ValueError):
            raise SetupError('The Jellyfin server could not be reached from this PC.') from None

    def json(self, path, query=None):
        """GET as JSON; ClientError with the status otherwise (probe.read reads this)."""
        status, data = self._request('GET', path, query)
        if status != 200:
            raise ClientError('request refused by the server', status)
        return data

    def send(self, method, path, allowed, query=None, body=None):
        """One changing request from `allowed` ((method, path regex, query check)); anything else is refused unsent."""
        if not any(method == m and re.fullmatch(p, path) and (check is None or check(query or {})) for m, p, check in allowed):
            raise SetupError('Refusing a request this job is not allowed to send.')
        self.sent.append((method, path))
        status, data = self._request(method, path, query, body)
        if status not in (200, 204):
            raise ClientError('refused by the server', status)
        return data

    def close(self):
        """Log THIS job's temporary session out (its own token only). A saved token is never signed out."""
        if self.closed:
            return True
        self.closed = True
        if not self.temporary or not self._token:
            return True
        try:
            status, _ = self._request('POST', '/Sessions/Logout')
        except SetupError:
            return False
        self.sent.append(('POST', '/Sessions/Logout'))
        self._token = ''
        return status in (200, 204, 401)


def saved_session(opener=None):
    """(Session, None) from the PC's saved Jellyfin settings, or (None, reason). A token is used as it is; a password
    signs in as this job's temporary device. Says nothing about the settings themselves."""
    try:
        from connection_config import DEFAULT_CONFIG, load_connection
        connection = load_connection(DEFAULT_CONFIG)
    except (OSError, ValueError):
        return None, 'This PC has no saved Jellyfin settings (Settings > Jellyfin server).'
    try:
        if connection.access_token:
            return Session.with_token(connection.server_url, connection.access_token, opener), None
        return Session.sign_in(connection.server_url, connection.username, connection.password, opener), None
    except SetupError as error:
        return None, str(error)


def typed_session(server_url, username, password, opener=None):
    """An administrator typed for this job only: signed in as this job's temporary device (logged out at the end)."""
    from connection_config import validate
    try:
        validate({'server_url': server_url, 'username': username, 'password': password})
    except ValueError:
        raise SetupError('Type the server address (http://... or https://...), a user name and a password.') from None
    return Session.sign_in(str(server_url).strip().rstrip('/'), username, password, opener)


# ---- what is on the server (read only) -------------------------------------------------------------------------
def livetv_settings(session):
    value, status = probe.read(session, '/System/Configuration/livetv')
    return value if status == 'ok' and isinstance(value, dict) else None


def tuners_of(settings):
    return list((settings or {}).get('TunerHosts') or [])


def channel_ids(session):
    value, status = probe.read(session, '/LiveTv/Channels', {'userId': session.user_id, 'limit': 2000, 'enableUserData': 'false'})
    return {str(c.get('Id')) for c in (value or {}).get('Items') or []} if status == 'ok' else None


def libraries(session):
    value, status = probe.read(session, '/Library/VirtualFolders')
    return value if status == 'ok' and isinstance(value, list) else None


def listing(session, path):
    """Names in a server folder (the read-only directory API, administrator), or None."""
    value, status = probe.read(session, '/Environment/DirectoryContents',
                               {'path': str(path), 'includeFiles': 'true', 'includeDirectories': 'true'})
    return value if status == 'ok' and isinstance(value, list) else None


def _record(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _write(path, data):
    from chromatic_trial_runner import write_durable
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    write_durable(Path(path), json.dumps(data, indent=1).encode('utf-8'))


# ---- Live TV channels -------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Group:
    key: str
    label: str
    url: str
    country: str   # '' any country (the language still must be English), else the iptv-org country code


GROUPS = (
    Group('cartoons', 'English cartoons', 'https://iptv-org.github.io/iptv/categories/animation.m3u', ''),
    Group('weather', 'US weather', 'https://iptv-org.github.io/iptv/categories/weather.m3u', 'us'),
    Group('news', 'US news', 'https://iptv-org.github.io/iptv/categories/news.m3u', 'us'),
    Group('classic', 'Classic TV', 'https://iptv-org.github.io/iptv/categories/classic.m3u', 'us'),
)
FEEDS = 'https://iptv-org.github.io/api/feeds.json'
# Never offered: pay-TV channels re-streamed by third parties (iptv-org lists only publicly available streams, and its
# own blocklist removes channels on request; these names are left out here as well, whatever a list says).
PAY_TV = re.compile(r'\b(?:Weather Channel|Cartoon Network|Cartoonito|Boomerang|Disney|Nickelodeon|Nick Jr|Nicktoons|TeenNick|'
                    r'MTV|Starz|HBO|Showtime|Cinemax|ESPN|Toonami|DreamWorks|Animax|AMC|FX|TNT|TBS|CNN|Fox News|MSNBC|CNBC|'
                    r'Discovery|Nat ?Geo|National Geographic|History|A&E|Lifetime|Hallmark|Paramount|Comedy Central|BET|'
                    r'USA Network|Syfy|Bravo|Food Network|HGTV|TLC|Animal Planet|Sky|Sling)\b', re.I)


def fetch_url(url, timeout=90):
    """A public iptv-org file (https only, no sign-in in it), as bytes."""
    if not url.startswith('https://iptv-org.github.io/'):
        raise SetupError('Only iptv-org\'s public lists are fetched.')
    request = urllib.request.Request(url, headers={'User-Agent': AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return reply.read(64 * 1024 * 1024)
    except (OSError, ValueError):
        raise SetupError('The public channel lists (iptv-org) could not be read. Check this PC\'s internet connection.') from None


def _entries(text):
    """[(#EXTINF line, [other lines])] of an M3U."""
    lines, out, index = text.splitlines(), [], 0
    while index < len(lines):
        if not lines[index].startswith('#EXTINF'):
            index += 1
            continue
        head, rest = lines[index], []
        index += 1
        while index < len(lines) and not lines[index].startswith('#EXTINF'):
            if lines[index].strip():
                rest.append(lines[index])
            index += 1
        out.append((head, rest))
    return out


def build_list(keys, fetch=fetch_url):
    """(M3U text, {group: kept}, {reason: dropped}) for the chosen groups. Only English channels of iptv-org's public
    lists; never geo-blocked entries, bare IP addresses, pay-TV re-streams, or the cartoon list's left-out channels."""
    chosen = [g for g in GROUPS if g.key in set(keys)]
    if not chosen:
        raise SetupError('Choose at least one channel group.')
    english = {}
    for feed in json.loads(fetch(FEEDS)):
        languages = feed.get('languages') or []
        english[f'{feed.get("channel")}@{feed.get("id")}'] = bool(languages) and languages[0] == 'eng'
    out, kept, dropped, seen = ['#EXTM3U'], {}, {'other language or none known': 0, 'geo-blocked': 0,
                                                 'pay-TV or not for this list': 0, 'other country': 0, 'twice': 0}, set()
    for group in chosen:
        kept[group.key] = 0
        for head, rest in _entries(fetch(group.url).decode('utf-8', 'replace')):
            ident = (re.search(r'tvg-id="([^"]*)"', head) or [None, ''])[1]
            name = head.split(',')[-1]
            urls = [line for line in rest if not line.startswith('#')]
            if not english.get(ident):
                dropped['other language or none known'] += 1
            elif '[Geo-blocked]' in head:
                dropped['geo-blocked'] += 1
            elif (PAY_TV.search(name) or cartoons.DROP.search(name) or cartoons.DROP_SEEN.search(name)
                  or any(cartoons.BARE_IP.match(u) for u in urls) or not urls or not all(u.startswith('https://') or
                                                                                         u.startswith('http://') for u in urls)):
                dropped['pay-TV or not for this list'] += 1
            elif group.country and not re.search(rf'\.{group.country}(?:@|$)', ident, re.I):
                dropped['other country'] += 1
            elif ident in seen or any(u in seen for u in urls):
                dropped['twice'] += 1
            else:
                seen.update([ident, *urls])
                head = re.sub(r'group-title="[^"]*"', f'group-title="{group.label}"', head) if 'group-title="' in head else \
                    head.replace('#EXTINF:-1 ', f'#EXTINF:-1 group-title="{group.label}" ', 1)
                out += [head, *rest]
                kept[group.key] += 1
    if not sum(kept.values()):
        raise SetupError('No channel of the chosen groups is available today.')
    return '\n'.join(out) + '\n', kept, dropped


def share_matches(session, base):
    """[(local library folder, server folder, is movies)] where a folder on this PC (`base`, or one folder inside it)
    lists exactly the same names as a library folder the server reads (both read only; at least five names, so a
    match is not chance). Movies libraries first (a folder without video is ignored there)."""
    base = Path(base)
    if not base.is_dir():
        return []
    candidates = [base] + [p for p in sorted(base.iterdir()) if p.is_dir()]
    local = {}
    for folder in candidates:
        try:
            local[folder] = {x.name for x in folder.iterdir()}
        except OSError:
            continue
    found = []
    for library in libraries(session) or []:
        for location in library.get('Locations') or []:
            server = {e.get('Name') for e in listing(session, location) or []}
            for folder, inside in local.items():
                if len(server) >= 5 and server == inside - {LIST_FOLDER}:
                    found.append((folder, str(location), library.get('CollectionType') == 'movies'))
    return sorted(found, key=lambda m: not m[2])


@dataclass
class LiveTvPlan:
    """The dry run's result: what Add would do. `text` is shown before the Approve; it names no path or address."""
    groups: list
    kept: dict
    dropped: dict
    m3u: str
    folder: Path | None
    server_folder: str | None
    tuners_now: int
    guides_now: int
    channels_now: int
    admin: bool
    problems: list = field(default_factory=list)

    @property
    def ready(self):
        return not self.problems

    def body(self, server_path='(the list file, as the server sees it)'):
        return {'Type': 'm3u', 'Url': server_path, 'FriendlyName': TUNER_NAME, 'TunerCount': TUNER_STREAMS,
                'AllowHWTranscoding': False, 'AllowStreamSharing': True, 'EnableStreamLooping': False,
                'ImportFavoritesOnly': False, 'IgnoreDts': True}

    def text(self):
        labels = {g.key: g.label for g in GROUPS}
        lines = ['Dry run: nothing has been sent that changes the server.',
                 f'Now: {self.tuners_now} tuner(s), {self.guides_now} guide source(s), {self.channels_now} channel(s).',
                 'Channels in the list: ' + ', '.join(f'{labels[k]} {n}' for k, n in self.kept.items()) +
                 f' ({sum(self.kept.values())} in all; left out: ' + ', '.join(f'{n} {why}' for why, n in self.dropped.items() if n) + ').',
                 f'Add would: 1. copy the list file into a new folder "{LIST_FOLDER}" inside a movies library folder your '
                 'server already reads (found by comparing names; nothing else in it changes), 2. wait until the server lists the '
                 'file, 3. add ONE tuner:',
                 '   ' + json.dumps(self.body()),
                 '4. check that exactly one tuner was added and nothing else changed. Backup first; Remove undoes it.']
        lines += [f'Not ready: {p}' for p in self.problems]
        return '\n'.join(lines)


def livetv_plan(session, keys, base, fetch=fetch_url):
    """Read only: the dry run for Add (Live TV channels)."""
    problems = []
    if not session.admin:
        problems.append('this sign-in is not a Jellyfin administrator (adding a tuner needs one).')
    settings = livetv_settings(session)
    if settings is None:
        problems.append('the server\'s Live TV settings could not be read.')
    tuners = tuners_of(settings)
    if any(t.get('FriendlyName') in TUNER_NAMES for t in tuners) or LIVETV_STATE.exists():
        problems.append('the Chromatic TV tuner is already there (use Remove first to change it).')
    channels = channel_ids(session)
    m3u, kept, dropped = build_list(keys, fetch)
    found = share_matches(session, base) if base else []
    # Review M2 of 1.6.4: the list file goes only into a Movies library's folder. In a music or mixed library Jellyfin
    # reads .m3u files as playlists, and in a TV shows library the new folder could show up as a series.
    matches = [m for m in found if m[2]]
    if found and not matches:
        problems.append('the library folder that matched is not a movies library (music, TV shows or mixed libraries '
                        'would show the list as a playlist or a series). Choose the folder that holds a movies library\'s '
                        'folder.')
    elif not matches:
        problems.append('no folder on this PC matched a library folder your server reads. Choose the folder (a network share) '
                        'that holds your Jellyfin library folders.')
    folder, server_folder = (matches[0][0], matches[0][1]) if matches else (None, None)
    return LiveTvPlan(list(keys), kept, dropped, m3u, folder, server_folder, len(tuners),
                      len((settings or {}).get('ListingProviders') or []), len(channels or ()), session.admin, problems)


LIVETV_ALLOWED = (('POST', r'/LiveTv/TunerHosts', None),
                  ('DELETE', r'/LiveTv/TunerHosts', lambda q: bool(re.fullmatch(r'[A-Za-z0-9-]{1,64}', str(q.get('id', ''))))))


def livetv_add(session, plan, out=print, sleep=time.sleep, wait=300, look=12):
    """After the Approve: copy the list, prove the server sees it, add the one tuner, verify; on any failure undo what
    this run did. Lines are plain words and counts. True when added and nothing else changed."""
    if not plan.ready or plan.folder is None:
        out('FAIL the dry run was not ready; nothing was changed')
        return False
    before = livetv_settings(session)
    tuners = tuners_of(before)
    if before is None or LIVETV_STATE.exists() or any(t.get('FriendlyName') in TUNER_NAMES for t in tuners):
        out('FAIL the Live TV settings changed since the dry run, or the tuner is already there; nothing was changed')
        return False
    fingerprint, _ = probe.take_fingerprint(session)
    channels = channel_ids(session) or set()
    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup = BACKUPS / f'before-add-{stamp}.json'
    _write(backup, {'taken': stamp, 'taken_utc': stamp, 'livetv': before, 'fingerprint': fingerprint, 'channels': len(channels)})
    out(f'PASS backup saved (private folder); before: {len(tuners)} tuner(s), {len(channels)} channel(s)')
    spot = Path(plan.folder) / LIST_FOLDER
    made = not spot.exists()
    target = spot / LIST_FILE
    if target.exists():
        out('FAIL a channel list file is already in that folder; nothing was changed (remove it first)')
        return False
    try:
        spot.mkdir(exist_ok=True)
        target.write_text(plan.m3u, encoding='utf-8', newline='\n')
    except OSError:
        out('FAIL the list file could not be written to that folder; nothing on the server was changed')
        return False

    def undo_file():
        try:
            target.unlink()
            if made and not any(spot.iterdir()):
                spot.rmdir()
        except OSError:
            out('INFO the list file could not be removed again; delete the folder ' + LIST_FOLDER + ' by hand')
    seen = None
    for attempt in range(look):
        entry = next((e for e in listing(session, plan.server_folder) or [] if e.get('Name') == LIST_FOLDER), None)
        inside = listing(session, entry.get('Path')) if entry else None
        seen = next((e for e in inside or [] if e.get('Name') == LIST_FILE), None)
        if seen:
            break
        sleep(5)
    if not seen or not str(seen.get('Path') or ''):
        undo_file()
        out('FAIL the server does not see the list file in that folder; the file was removed again, nothing on the server changed')
        return False
    out('PASS the server sees the list file (read-only folder listing)')
    try:
        session.send('POST', '/LiveTv/TunerHosts', LIVETV_ALLOWED, body=plan.body(str(seen['Path'])))
    except ClientError:
        pass  # judged by what the server now holds
    after = livetv_settings(session)
    old = {str(t.get('Id')) for t in tuners}
    fresh = [t for t in tuners_of(after) if str(t.get('Id')) not in old]
    if len(fresh) != 1 or fresh[0].get('FriendlyName') != TUNER_NAME:
        for extra in fresh:
            if extra.get('FriendlyName') == TUNER_NAME:
                try:
                    session.send('DELETE', '/LiveTv/TunerHosts', LIVETV_ALLOWED, query={'id': str(extra.get('Id'))})
                except ClientError:
                    pass
        undo_file()
        out('FAIL the tuner was not added cleanly; what this run added was removed again')
        return False
    ident = str(fresh[0]['Id'])
    # the public tool's record keys (tuner_id, backup, added_utc) plus the name used and what Remove must also undo here
    _write(LIVETV_STATE, {'tuner_id': ident, 'backup': backup.name, 'added_utc': stamp, 'added': stamp, 'name': TUNER_NAME,
                          'by': 'Chromatic Manager',
                          'list_file': str(target), 'created_folder': made, 'groups': plan.groups})
    same = probe.canonical(livetv_tuner.without(after, ident)) == probe.canonical(before)
    out(f'PASS one tuner added ({TUNER_STREAMS} streams at most); everything else in the Live TV settings is unchanged' if same
        else 'FAIL the tuner was added, but other Live TV settings changed (see Dashboard > Live TV); Remove undoes it')
    new = set()
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        new = (channel_ids(session) or set()) - channels
        if new:
            break
        sleep(5)
    out(f'{"PASS" if new else "INFO"} new channels listed: {len(new)}' + ('' if new else ' (the server may still be reading the list)'))
    after_print, _ = probe.take_fingerprint(session)
    unchanged = after_print == fingerprint
    out('PASS libraries, users, devices, plugins, tasks and conversion settings unchanged' if unchanged
        else 'FAIL the server fingerprint differs from before')
    return same and unchanged


def livetv_remove(session, out=print):
    """Delete exactly the recorded tuner (also one the earlier tool added) and remove the list file this Manager wrote.

    Review M3 of 1.6.4: the owner may change Live TV (guides, other tuners, options) any time after the add, so the
    settings are not compared with the day-of-add backup to decide. What is checked is this run's own change: the
    settings after the delete must equal the settings just before it with our tuner taken out. A difference, and any
    difference from the backup, is reported in plain words but does not block. The record is closed once our tuner is
    verifiably gone. True when it is gone."""
    record = _record(LIVETV_STATE)
    if not record or not record.get('tuner_id'):
        out('FAIL no recorded Chromatic TV tuner on this PC; nothing was changed')
        return False
    saved = _record(BACKUPS / str(record.get('backup', '')))
    settings = livetv_settings(session)
    if settings is None:
        out('FAIL the Live TV settings could not be read; nothing was changed')
        return False
    ident = str(record['tuner_id'])
    names = (record['name'],) if record.get('name') in TUNER_NAMES else TUNER_NAMES  # the earlier tools' records name none
    ours = [t for t in tuners_of(settings) if str(t.get('Id')) == ident and t.get('FriendlyName') in names]
    others = [t for t in tuners_of(settings) if str(t.get('Id')) == ident and t.get('FriendlyName') not in names]
    if others:
        out('FAIL the recorded tuner id now belongs to a tuner that is not ours; nothing was changed')
        return False
    if ours:
        try:
            session.send('DELETE', '/LiveTv/TunerHosts', LIVETV_ALLOWED, query={'id': ident})
        except ClientError:
            pass  # judged by what the server now holds
    else:
        out('INFO the recorded tuner is not on the server any more')
    after = livetv_settings(session)
    gone = after is not None and not any(str(t.get('Id')) == ident for t in tuners_of(after))
    if not gone:
        out('FAIL the tuner is still on the server (or the settings could not be read again); the record is kept, '
            'try Remove again')
        return False
    same = probe.canonical(after) == probe.canonical(livetv_tuner.without(settings, ident))
    out('PASS tuner removed; nothing else in the Live TV settings changed' if same else
        'INFO tuner removed; other Live TV settings also differ from just before (check Dashboard > Live TV)')
    if saved is not None and probe.canonical(after) != probe.canonical(saved.get('livetv')):
        out('INFO the Live TV settings differ from the day the tuner was added: changes made since then are kept')
    left = [t for t in tuners_of(after) if t.get('FriendlyName') in TUNER_NAMES]
    if left:
        out(f'INFO {len(left)} other tuner(s) with a Chromatic name are on the server; they are not this record\'s and stay')
    fingerprint, _ = probe.take_fingerprint(session)
    if saved is not None:
        out('PASS libraries, users, devices, plugins, tasks and conversion settings as on the day it was added'
            if fingerprint == saved.get('fingerprint') else
            'INFO the server fingerprint differs from the day it was added (devices or plugins may have changed since)')
    if gone:
        listed = record.get('list_file')
        if listed:
            path = Path(listed)
            try:
                if path.name == LIST_FILE and path.parent.name == LIST_FOLDER and path.exists():
                    path.unlink()
                if record.get('created_folder') and path.parent.name == LIST_FOLDER and path.parent.exists() \
                        and not any(path.parent.iterdir()):
                    path.parent.rmdir()
                out('PASS the channel list file this Manager wrote was removed')
            except OSError:
                out('INFO the channel list file could not be removed; delete the folder ' + LIST_FOLDER + ' by hand')
        else:
            out('INFO the channel list file was placed by the earlier tool; it stays where it is')
        LIVETV_STATE.unlink()
        out('INFO its channels leave the server\'s list at the next guide refresh')
    return gone


def livetv_status(session):
    settings = livetv_settings(session)
    return {'readable': settings is not None,
            'added': any(t.get('FriendlyName') in TUNER_NAMES for t in tuners_of(settings)),
            'recorded': LIVETV_STATE.exists()}


# ---- ChromaTube ------------------------------------------------------------------------------------------------
IMAGE = 'fe80grau/ytdlp2strm@sha256:2757a5e7293d7e885d5dc057b26a9e8cb9b5a5b2a6affe6a3e9f7f8c5fb9ec7c'
# The digest above is the "latest" build of 2026-10-25 (upstream commit 7d4a047), the image this project tested; it is
# not a release tag (the same note as ChromaTube's public server/docker-compose.yml).
IMAGE_NOTE = ('the "latest" build of 2026-10-25 (upstream commit 7d4a047), the image this project tested; it is not a '
              'release tag')
TOOL_PORT = 5005
# The tool's page sign-in, made here at random when the files are first saved: written only into the config.json saved
# for the server and into this private file on this PC (Git-ignored). Never printed, logged or returned to a caller.
CHROMATUBE_SIGNIN = PRIVATE / 'chromatube-server-signin.json'
SIGNIN_WHERE = ('The tool\'s page sign-in (a random user name and password) is in config.json in the folder you chose, '
                'and a copy is kept in this PC\'s private folder (the file chromatube-server-signin.json). Open either '
                'file to read it; '
                'never share config.json or put it in Git.')
TERMS = ('ChromaTube uses ytdlp2STRM, a third-party tool that fetches YouTube videos with yt-dlp on your own server. '
         'That conflicts with YouTube\'s Terms of Service. Setting it up is your own private choice; this project does '
         'not ship, host or run it for you, and nothing is downloaded or kept (videos are fetched when played).')
STEPS = (
    '1. Save files writes docker-compose.yml and four settings files. config.json holds the tool\'s page sign-in, made '
    'at random (REQUIRED: it guards the tool\'s settings pages; it does not cover the tool\'s command channel, so the '
    'firewall in step 3 is the real protection). A copy of '
    'the sign-in stays in this PC\'s private folder (the file chromatube-server-signin.json). On the machine that runs '
    'Docker (it can be '
    'your Jellyfin server), copy the five files into the settings folder you typed.',
    '2. Choose the media folder: a folder your Jellyfin container can ALREADY see (for example a folder inside its '
    '/config or media mount), but NOT inside one of your library folders. Then Jellyfin needs no restart and no new mount.',
    '3. REQUIRED: on the Docker machine\'s firewall, allow port 5005 only from your Jellyfin server; allow this PC only '
    'while you press Check it answers or open the tool\'s page, then remove that rule. Never forward 5005 on your '
    'router. The firewall is the real protection: in the pinned version the sign-in guards only the settings pages, '
    'while the tool\'s command channel has no sign-in, accepts connections from any web page and can start programs '
    'inside the container. The tool uses the host\'s network (network_mode: host) because it listens only on the '
    'address in config.json (this machine\'s home-network address, never 0.0.0.0).',
    '4. In your Docker app, make a new project named chromatube and paste docker-compose.yml (or run: docker compose -p '
    'chromatube up -d in the settings folder). The image is pinned by its digest, ' + IMAGE_NOTE + ': it never changes '
    'by itself. It runs with no privilege escalation. If it cannot read its settings, check the owner and permissions '
    'of config.json on the Docker machine.',
    '5. The four settings files are mounted read-only, so the tool\'s page cannot change them (or its sign-in). To change '
    'a setting or your channels, edit the file on the Docker machine, then: docker compose -p chromatube restart.',
    '6. No YouTube account or cookies are used (cookie_value stays empty). Keep it that way.',
    '7. When the container runs, allow this PC on the firewall for a moment and press Check it answers here: it signs '
    'in with the copy kept on this PC and checks that the settings page asks for the sign-in. Then remove the PC\'s '
    'firewall rule again and add the ChromaTube library.',
    'Undo everything: Remove here (the library only; files untouched), then in your Docker app stop and delete the '
    'chromatube project (docker compose -p chromatube down -v), delete the settings folder and the ChromaTube media '
    'folder, and the firewall rule.')


def private_host(host):
    """A LAN address of the tool: a private IPv4 address, or a name ending in .local / .lan / .home.arpa."""
    host = str(host or '').strip()
    try:
        return ipaddress.IPv4Address(host).is_private
    except ValueError:
        return bool(re.fullmatch(r'[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63})*\.(local|lan|home\.arpa)', host))


_CHANNEL = re.compile(r'https://www\.youtube\.com/(?:@[A-Za-z0-9._-]{1,100}|channel/UC[A-Za-z0-9_-]{22}|c/[A-Za-z0-9._-]{1,100}|'
                      r'user/[A-Za-z0-9._-]{1,100})')


def channels_from(text):
    """The channel addresses typed one a line: YouTube channel pages only (https://www.youtube.com/@name, /channel/UC...,
    /c/..., /user/...); at most 20 (the plan's limit). ValueError with plain words for anything else."""
    found = []
    for line in str(text or '').splitlines():
        line = line.strip().rstrip('/')
        if not line:
            continue
        if line.startswith('@'):
            line = 'https://www.youtube.com/' + line
        line = line.replace('https://youtube.com/', 'https://www.youtube.com/').replace('http://', 'https://')
        if not _CHANNEL.fullmatch(line):
            raise ValueError('One line is not a YouTube channel address (like https://www.youtube.com/@name).')
        if line not in found:
            found.append(line)
    if not found:
        raise ValueError('Type at least one YouTube channel address, one a line.')
    if len(found) > 20:
        raise ValueError('At most 20 channels (each lists its newest 15 videos).')
    return found


def compose_text(media_folder='/CHANGE/ME/media', settings_folder='/CHANGE/ME/chromatube'):
    """The docker-compose.yml for the user's own Docker host. Generic: no owner data; the two folders are the user's."""
    for folder in (media_folder, settings_folder):
        if not re.fullmatch(r'/[A-Za-z0-9 ._/-]{1,200}', folder) or '..' in folder:
            raise ValueError('Folders on the Docker machine are written like /volume1/docker/chromatube.')
    def mount(source, target, read_only=False):
        line = f'{source}:{target}' + (':ro' if read_only else '')
        return f'      - "{line}"' if ' ' in source else f'      - {line}'
    settings = [mount(f'{settings_folder}/{name}', target, True) for name, target in (
        ('config.json', '/opt/ytdlp2STRM/config/config.json'), ('crons.json', '/opt/ytdlp2STRM/config/crons.json'),
        ('yt_config.json', '/opt/ytdlp2STRM/plugins/youtube/config.json'),
        ('channel_list.json', '/opt/ytdlp2STRM/plugins/youtube/channel_list.json'))]
    return '\n'.join([
        '# ChromaTube server side: ytdlp2STRM (MIT, https://github.com/fe80Grau/ytdlp2STRM), not included here: Docker',
        '# downloads the image below from Docker Hub when you start it. It lists YouTube channels as small .strm files for',
        '# one separate Jellyfin library and fetches a video only when it is played. Nothing is stored or downloaded ahead.',
        '# YouTube\'s Terms of Service: tools like this conflict with them. Using it is your own private choice.',
        '#',
        '# Before you start it:',
        '#   1. config.json (next to this file) holds the tool\'s page sign-in, REQUIRED: it guards the settings pages',
        '#      only. Never share config.json.',
        '#   2. allow port 5005 on this machine\'s firewall from your Jellyfin server only (required); allow your PC only',
        '#      while you check it, then remove that rule. The firewall is the real protection: the tool\'s command',
        '#      channel has no sign-in and can start programs inside the container.',
        '# The settings files are mounted read-only: edit them on this machine, then restart the container.',
        '# Start:   docker compose -p chromatube up -d          (or paste it as a project in your NAS\'s Docker app)',
        '# Remove:  docker compose -p chromatube down -v        then delete its folders and the firewall rule.',
        'name: chromatube',
        'services:',
        '  ytdlp2strm:',
        '    # Pinned by digest: it cannot change by itself. Update only on purpose.',
        '    # This digest is the "latest" build of 2026-10-25 (upstream commit 7d4a047), the image the project tested; it is',
        '    # not a release tag. If the publisher ever prunes untagged images it may stop being pullable: then choose a newer',
        '    # digest on purpose.',
        f'    image: {IMAGE}',
        '    container_name: chromatube-ytdlp2strm',
        '    # This version of the tool listens only on the address written in config.json, so it uses the host\'s',
        '    # own network; with Docker\'s default bridge network its page cannot be reached. The address must be the',
        '    # server\'s LAN address (never 0.0.0.0), and port 5005 must stay closed to the internet (no port forward on',
        '    # your router; firewall: allow it from your Jellyfin server only, your PC only while checking).',
        '    network_mode: host',
        '    environment:',
        '      - AM_I_IN_A_DOCKER_CONTAINER=Yes',
        f'      - DOCKER_PORT={TOOL_PORT}',
        '    volumes:',
        '      # The ONE folder the tool writes into: one your Jellyfin container already sees, NOT inside a library folder.',
        mount(media_folder, '/media/ChromaTube'),
        '      # Its own working files (cache, logs); nothing of Jellyfin\'s.',
        '      - chromatube-data:/opt/ytdlp2STRM',
        '      # Settings: these four files, READ-ONLY: the tool\'s page cannot change them (or its sign-in).',
        '      # To change a setting or your channels, edit the file on this machine, then: docker compose -p chromatube restart',
        *settings,
        '    restart: unless-stopped',
        '    # Small on purpose: it lists channels and hands Jellyfin one video address at a time.',
        '    mem_limit: 1g',
        '    cpus: 1.5',
        '    pids_limit: 256',
        '    # No privilege escalation. (Dropping Linux capabilities is optional extra hardening that was not tested with',
        '    # this image and can stop it reading its settings.)',
        '    security_opt:',
        '      - no-new-privileges:true',
        '    logging:',
        '      driver: json-file',
        '      options:',
        '        max-size: "10m"',
        '        max-file: "3"',
        'volumes:',
        '  chromatube-data:',
        ''])


def _signin_ok(signin):
    return (isinstance(signin, dict) and re.fullmatch(r'[A-Za-z0-9._-]{8,64}', str(signin.get('username', '')))
            and re.fullmatch(r'[A-Za-z0-9_-]{24,128}', str(signin.get('password', ''))) is not None)


def new_signin():
    """A random page sign-in: a user name and a long password (only letters, digits, - and _)."""
    return {'username': 'chromatube-' + secrets.token_hex(4), 'password': secrets.token_urlsafe(24)}


def saved_signin(path=None):
    """The page sign-in kept on this PC, or None. Never printed."""
    record = _record(path or CHROMATUBE_SIGNIN)
    return {'username': record['username'], 'password': record['password']} if _signin_ok(record) else None


def settings_files(host, channels, signin):
    """{file name: text} for the settings folder. `host`: the Docker machine's LAN address (the address Jellyfin uses to
    reach the tool); `signin`: the page sign-in (required: new_signin() or saved_signin()). As ChromaTube's public
    templates: no cookies, no account, video_quality 360 and direct_serve_media_playlist False (the combination that
    gives Jellyfin H.264 with sound), no Jellyfin key or link."""
    if not private_host(host):
        raise ValueError('The Docker machine\'s address must be a home-network address (like 192.168.1.20 or nas.local).')
    if not _signin_ok(signin):
        raise ValueError('The tool\'s page sign-in is required.')
    channels = channels_from('\n'.join(channels))
    config = {'ytdlp2strm_host': host, 'ytdlp2strm_port': str(TOOL_PORT), 'ytdlp2strm_keep_old_strm': 'False',
              'ytdlp2strm_temp_file_duration': '3600', 'ytdlp2strm_web_username': signin['username'],
              'ytdlp2strm_web_password': signin['password']}
    youtube = {'strm_output_folder': '/media/ChromaTube', 'channels_list_file': './plugins/youtube/channel_list.json',
               'days_dateafter': '30', 'videos_limit': '15', 'sponsorblock': False, 'sponsorblock_cats': 'sponsor',
               'cookies': 'cookies', 'cookie_value': '', 'lang': 'en', 'episode_format': 'sequential',
               'video_quality': '360', 'download_subtitles': 'False', 'convert_subtitles_to_srt': 'True',
               'keep_vtt_subtitles': 'True', 'direct_stream_cache_hours': '4', 'direct_serve_media_playlist': 'False',
               'direct_prewarm_latest_per_channel': '0', 'direct_prewarm_neighbors': 'False', 'jellyfin_integration': 'False',
               'jellyfin_base_url': '', 'jellyfin_api_key': '', 'jellyfin_library_name': LIBRARY_NAME}
    crons = [{'every': 'hours', 'qty': '6', 'at': '', 'timezone': '', 'do': ['--media', 'youtube', '--params', 'direct']}]
    dump = lambda value: json.dumps(value, indent=4) + '\n'
    return {'config.json': dump(config), 'yt_config.json': dump(youtube), 'crons.json': dump(crons),
            'channel_list.json': dump(channels)}


def user_only(path):
    """Only this PC's user may open `path` (review of 1.6.5): mode 0600 on macOS and Linux; on Windows the file's
    inherited permissions are replaced by one full-control entry for the signed-in user (icacls, the user's own file).
    True when that was done; False where it could not be (the file is still in the Git-ignored private folder)."""
    import os
    import subprocess
    path = Path(path)
    try:
        if os.name != 'nt':
            os.chmod(path, 0o600)
            return True
        user, domain = os.environ.get('USERNAME', ''), os.environ.get('USERDOMAIN', '')
        if not user:
            return False
        who = f'{domain}\\{user}' if domain else user
        done = subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', f'{who}:(F)'], capture_output=True,
                              timeout=30)
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def save_server_files(folder, host, media_folder, settings_folder, channels):
    """Write docker-compose.yml and the four settings files into `folder` on this PC, each read back. The page sign-in
    is the one kept on this PC, or a new random one kept there first (private\\chromatube-server-signin.json). Returns
    the file names; never the sign-in. ValueError (bad input, nothing written) or OSError (could not be written)."""
    signin = saved_signin()
    files = settings_files(host, channels, signin or new_signin())  # validates everything before any write
    files['docker-compose.yml'] = compose_text(media_folder, settings_folder)
    if signin is None:
        made = json.loads(files['config.json'])
        signin = {'username': made['ytdlp2strm_web_username'], 'password': made['ytdlp2strm_web_password']}
        _write(CHROMATUBE_SIGNIN, dict(signin, about='ChromaTube server: the tool\'s page sign-in (also in its '
                                                     'config.json). Private: never share it.'))
        if saved_signin() != signin:
            raise OSError('the sign-in could not be kept on this PC')
    user_only(CHROMATUBE_SIGNIN)   # also an older copy, each time
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        data = text.encode('utf-8')
        (folder / name).write_bytes(data)
        if (folder / name).read_bytes() != data:
            raise OSError(f'{name} did not read back')
    return sorted(files)


CHECK_WORDS = {
    'ok': 'The ChromaTube tool answers on your home network, with its sign-in; its settings page asks for it. Now '
          'remove the firewall rule for this PC (keep only your Jellyfin server): the sign-in does not cover the '
          'tool\'s command channel.',
    'open': 'The ChromaTube tool answers WITHOUT a sign-in: anyone who can reach it could use its page. Copy the '
            'config.json saved here onto the Docker machine and restart the chromatube project.',
    'wrong': 'The ChromaTube tool asks for a sign-in, but not the one kept on this PC. Copy the config.json saved here '
             'onto the Docker machine and restart the chromatube project.',
    'unknown': 'The ChromaTube tool asks for a sign-in, but this PC keeps none. Save files here, copy config.json onto '
               'the Docker machine and restart the chromatube project.',
    'down': 'The ChromaTube tool does not answer yet. Is the chromatube project running, and does the firewall allow '
            'port 5005 from this PC for now (the temporary rule for checking)?',
    'lan': 'Type the Docker machine\'s home-network address first.'}


def tool_check(host, port=TOOL_PORT, opener=None, timeout=4, signin=None):
    """Asks the tool's own page on the home network twice (GET http://host:5005/): without a sign-in (it must refuse:
    401) and with the sign-in kept on this PC (it must answer: 200). One of CHECK_WORDS' keys. Nothing else is sent; the
    sign-in goes only to that address, in the request's Authorization header, and is never printed. Redirects are
    never followed (any 3xx is 'down'), so the sign-in cannot be forwarded to another host (review M2 of 1.6.5). Each
    ask waits at most `timeout` seconds (two asks: at most about 8 s; the window runs it off its own thread)."""
    if not private_host(host):
        return 'lan'
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NeverFollow())
    signin = signin if signin is not None else saved_signin()
    url = f'http://{host}:{int(port)}/'

    def status(headers):
        try:
            with opener.open(urllib.request.Request(url, headers=dict(headers, **{'User-Agent': 'chromatic-manager'})),
                             timeout=timeout) as reply:
                return reply.status
        except urllib.error.HTTPError as error:
            return error.code
        except (OSError, ValueError, urllib.error.URLError):
            return None
    bare = status({})
    if bare is None:
        return 'down'
    if bare == 200:
        return 'open'
    if bare != 401:
        return 'down'
    if not _signin_ok(signin):
        return 'unknown'
    import base64
    token = base64.b64encode(f'{signin["username"]}:{signin["password"]}'.encode('utf-8')).decode('ascii')
    signed = status({'Authorization': 'Basic ' + token})
    return 'ok' if signed == 200 else 'wrong' if signed == 401 else 'down'


def tool_answers(host, port=TOOL_PORT, opener=None, timeout=10, signin=None):
    """True when the tool answers with the sign-in and refuses without it (tool_check() == 'ok')."""
    return tool_check(host, port, opener, timeout, signin) == 'ok'


COMMON_ROOTS = ('/config', '/media', '/data', '/mnt', '/srv')


def find_chromatube_folders(session):
    """Server folders named ChromaTube that hold at least one channel folder, found by listing (read only) the library
    folders' parents and the usual container mounts. Never inside a library folder."""
    locations = [PurePosixPath(str(p).replace('\\', '/')) for f in libraries(session) or [] for p in (f.get('Locations') or [])]
    queue, seen, found = [], set(), []
    for location in locations:
        queue += list(location.parents)
    queue += [PurePosixPath(p) for p in COMMON_ROOTS]
    for path in queue:
        text = str(path)
        if text in seen or text in ('/', '.'):
            continue
        seen.add(text)
        for entry in listing(session, text) or []:
            if entry.get('Name') == LIBRARY_NAME and entry.get('Type') == 'Directory':
                server = str(entry.get('Path') or '')
                if server and not inside_library(server, locations) and server not in found:
                    inner = listing(session, server) or []
                    if any(e.get('Type') == 'Directory' for e in inner):
                        found.append(server)
    return found


def inside_library(path, locations):
    """Is `path` inside a library folder, or a library folder inside it? (Either would scan it twice.)"""
    path = PurePosixPath(str(path).replace('\\', '/'))
    for location in locations:
        location = PurePosixPath(str(location).replace('\\', '/'))
        if path == location or location in path.parents or path in location.parents:
            return True
    return False


def _others(folders):
    return sorted(probe.canonical({k: v for k, v in f.items() if k not in ('RefreshStatus', 'RefreshProgress')})
                  for f in folders if f.get('Name') != LIBRARY_NAME)


def library_allowed():
    """Only the library named ChromaTube, never with the server-wide scan (refreshLibrary false, as the public tool)."""
    ours = lambda q: q.get('name') == LIBRARY_NAME and q.get('refreshLibrary') == 'false'
    return (('POST', r'/Library/VirtualFolders', ours), ('DELETE', r'/Library/VirtualFolders', ours))


def refresh_allowed(item_id):
    """A scan of the one library this job just added, nothing else."""
    if not re.fullmatch(r'[A-Za-z0-9-]{1,64}', str(item_id or '')):
        return ()
    return (('POST', rf'/Items/{re.escape(str(item_id))}/Refresh', lambda q: q.get('Recursive') == 'true'),)


def same_path(a, b):
    """Two server paths name the same folder (slashes and a trailing slash aside)."""
    norm = lambda p: str(p).replace('\\', '/').rstrip('/') or '/'
    return norm(a) == norm(b)


LIBRARY_OPTIONS = {'Enabled': True, 'EnableRealtimeMonitor': False, 'EnablePhotos': False, 'EnableInternetProviders': False,
                   'SaveLocalMetadata': False, 'EnableChapterImageExtraction': False,
                   'ExtractChapterImagesDuringLibraryScan': False, 'EnableTrickplayImageExtraction': False,
                   'ExtractTrickplayImagesDuringLibraryScan': False, 'AutomaticRefreshIntervalDays': 0}


def chromatube_plan(session, server_path):
    """Read only: the dry run for adding the ChromaTube library. (problems, text)."""
    problems = []
    if not session.admin:
        problems.append('this sign-in is not a Jellyfin administrator (adding a library needs one).')
    folders = libraries(session)
    if folders is None:
        problems.append('the server\'s libraries could not be read.')
    elif any(f.get('Name') == LIBRARY_NAME for f in folders):
        problems.append('a library named ChromaTube is already there (use Remove first).')
    locations = [p for f in folders or [] for p in (f.get('Locations') or [])]
    inner = listing(session, server_path) if server_path else None
    if not server_path or inner is None:
        problems.append('the server does not see that folder (choose one it can list).')
    elif not any(e.get('Type') == 'Directory' for e in inner):
        problems.append('that folder has no channel folders yet: let the tool run once (it lists channels every 6 hours, '
                        'or press its own "run now"), then try again.')
    elif inside_library(server_path, locations):
        problems.append('that folder is inside one of your library folders (it would be scanned twice); choose another.')
    text = '\n'.join(['Dry run: nothing has been sent that changes the server.',
                      f'Now: {len(folders or [])} librar{"y" if len(folders or []) == 1 else "ies"}.',
                      'Add would add ONE library named ChromaTube, type Shows, on the folder you chose (shown masked), '
                      'with no internet metadata, no chapter or trickplay images, no real-time monitor and no automatic '
                      'refresh, without the server-wide scan; then check that exactly one library was added and every '
                      'other library is unchanged, and scan that library alone once. '
                      'Backup first; Remove undoes it (its files are never touched).']
                     + [f'Not ready: {p}' for p in problems])
    return problems, text


def chromatube_add(session, server_path, out=print):
    problems, _ = chromatube_plan(session, server_path)
    if problems:
        out('FAIL ' + problems[0] + ' Nothing was changed.')
        return False
    before = libraries(session)
    fingerprint, _ = probe.take_fingerprint(session)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup = BACKUPS / f'before-library-{stamp}.json'
    # the public ChromaTube tool's backup and record keys (taken_utc/libraries/fingerprint; name/item_id/path/backup/
    # added_utc), plus the earlier tool's keys, so each tool recognises and can undo what the other added
    _write(backup, {'taken_utc': stamp, 'libraries': before, 'fingerprint': fingerprint})
    _write(CHROMATUBE_STATE, {'name': LIBRARY_NAME, 'path': server_path, 'backup': backup.name, 'added_utc': stamp,
                              'taken': stamp, 'libraries_before': before, 'fingerprint_before': fingerprint,
                              'by': 'Chromatic Manager'})
    out(f'PASS backup saved (private folder); libraries before: {len(before)}')
    try:
        session.send('POST', '/Library/VirtualFolders', library_allowed(),
                     query={'name': LIBRARY_NAME, 'collectionType': 'tvshows', 'refreshLibrary': 'false'},
                     body={'LibraryOptions': dict(LIBRARY_OPTIONS, PathInfos=[{'Path': server_path}])})
    except ClientError:
        pass
    after = libraries(session) or []
    mine = [f for f in after if f.get('Name') == LIBRARY_NAME]
    same = _others(after) == _others(before) and len(after) == len(before) + 1
    if len(mine) != 1:
        CHROMATUBE_STATE.unlink()
        out('FAIL the library was not added; nothing else was changed')
        return False
    locations = [str(p) for p in mine[0].get('Locations') or []]
    right_folder = len(locations) == 1 and same_path(locations[0], server_path)
    record = _record(CHROMATUBE_STATE) or {}
    record.update(item_id=str(mine[0].get('ItemId', '')))
    _write(CHROMATUBE_STATE, record)
    out('PASS library ChromaTube added on its folder; every other library is unchanged' if same and right_folder
        else 'FAIL the library was added, but another library changed or the folder is not the one asked; Remove undoes it')
    after_print, _ = probe.take_fingerprint(session)
    devices_users = all(after_print.get(k) == fingerprint.get(k) for k in ('users', 'devices', 'plugins', 'tasks', 'encoding'))
    out('PASS users, devices, plugins, tasks and conversion settings unchanged' if devices_users
        else 'FAIL users, devices, plugins, tasks or conversion settings differ from before')
    # Added without the server-wide scan (refreshLibrary false, as the public tool): THIS library alone is scanned once.
    item = record['item_id']
    try:
        session.send('POST', f'/Items/{item}/Refresh', refresh_allowed(item),
                     query={'Recursive': 'true', 'MetadataRefreshMode': 'Default', 'ImageRefreshMode': 'Default'})
        out('PASS a scan of the ChromaTube library alone was started')
    except (ClientError, SetupError):
        out('INFO the ChromaTube library is scanned at Jellyfin\'s next library scan')
    return same and right_folder and devices_users


def _looks_like_chromatube(session, library):
    """A library this PC has no record of counts as a ChromaTube library only when its one folder is named ChromaTube and
    holds at least one channel folder (what the tool writes), read only (review L1 of 1.6.4)."""
    paths = [str(p) for p in library.get('Locations') or []]
    if len(paths) != 1 or PurePosixPath(paths[0].replace('\\', '/')).name != LIBRARY_NAME:
        return False
    return any(e.get('Type') == 'Directory' for e in listing(session, paths[0]) or [])


def chromatube_unrecorded(session):
    """Read only. None when this PC holds a record of the add (Remove needs no extra question); else True when the one
    library named ChromaTube looks like a ChromaTube folder (Remove may go ahead once the owner confirms), False when
    it does not (Remove refuses)."""
    if CHROMATUBE_STATE.exists():
        return None
    mine = [f for f in libraries(session) or [] if f.get('Name') == LIBRARY_NAME]
    return len(mine) == 1 and _looks_like_chromatube(session, mine[0])


def chromatube_remove(session, out=print, unrecorded_ok=False):
    """Delete exactly the library named ChromaTube (its recorded id and folder must still match when recorded); files
    untouched; every other library must be unchanged. With no record on this PC (an earlier tool, another PC) it
    refuses unless the library's one folder is named ChromaTube and holds channel folders AND the owner confirmed that
    one (`unrecorded_ok`, after chromatube_unrecorded): review L1 of 1.6.4."""
    before = libraries(session)
    if before is None:
        out('FAIL the libraries could not be read; nothing was changed')
        return False
    mine = [f for f in before if f.get('Name') == LIBRARY_NAME]
    if len(mine) != 1:
        out('FAIL no single library named ChromaTube; nothing was changed')
        return False
    record = _record(CHROMATUBE_STATE) or {}
    if not record:
        if not _looks_like_chromatube(session, mine[0]):
            out('FAIL this PC has no record of adding the ChromaTube library, and its folder does not look like a '
                'ChromaTube folder (one folder named ChromaTube with channel folders); nothing was changed')
            return False
        if not unrecorded_ok:
            out('FAIL this PC has no record of adding the ChromaTube library; confirm that this one (its folder is named '
                'ChromaTube and holds channel folders) is the one to remove; nothing was changed')
            return False
        out('INFO no record on this PC: removing the library you confirmed (its folder is named ChromaTube and holds '
            'channel folders)')
    paths = [str(p) for p in mine[0].get('Locations') or []]
    if (record.get('item_id') and str(mine[0].get('ItemId')) != record['item_id']) or \
            (record.get('path') and not (len(paths) == 1 and same_path(paths[0], record['path']))) or \
            (record.get('name') and record['name'] != LIBRARY_NAME):
        out('FAIL the ChromaTube library on the server is not the one recorded here (id or folder differs); nothing was changed')
        return False
    try:
        session.send('DELETE', '/Library/VirtualFolders', library_allowed(), query={'name': LIBRARY_NAME, 'refreshLibrary': 'false'})
    except ClientError:
        pass
    after = libraries(session) or []
    gone = not any(f.get('Name') == LIBRARY_NAME for f in after) and _others(after) == _others(before)
    out('PASS library ChromaTube removed; every other library is unchanged (its files were not touched)' if gone
        else 'FAIL the library is still there or another library changed')
    saved = _record(BACKUPS / str(record.get('backup', ''))) if record.get('backup') else None
    before_add = (saved or {}).get('libraries', record.get('libraries_before'))
    if gone and before_add is not None:
        out('PASS the library list equals the backup from before the add' if _others(after) == _others(before_add)
            and len(after) == len(before_add) else 'INFO the library list differs from the backup (libraries changed since)')
    if gone and CHROMATUBE_STATE.exists():
        CHROMATUBE_STATE.unlink()
    return gone


def chromatube_status(session):
    folders = libraries(session)
    return {'readable': folders is not None, 'added': any(f.get('Name') == LIBRARY_NAME for f in folders or []),
            'recorded': CHROMATUBE_STATE.exists()}

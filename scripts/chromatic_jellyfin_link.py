# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""The Chromatic's own Jellyfin sign-in, made on the PC with Quick Connect.

The console never gets the owner's password. Local code on this PC:
  1. asks the server for a Quick Connect code as a new device ("Chromatic Jellyfin" on
     "Chromatic", a fresh random DeviceId),
  2. approves that code for the owner from the linker's own dedicated session ("Chromatic
     Manager" on "PC"; its token is reused, and it signs in with the PC's private connection
     file only when it has none that works), never as any other device,
  3. collects the device's own access token,
and Chromatic Manager writes server address, user id, that token and the DeviceId into
the console's provisioning record (chromatic_provision.Jellyfin). The device shows in the
server's Dashboard > Devices as "Chromatic", where it can be revoked; Forget and Re-link
log the old token out (POST /Sessions/Logout with that token), which removes it.

Why a fresh DeviceId each time: what a server does with a second sign-in from the same
DeviceId differs between Jellyfin versions (some reissue the same token; the owner's
10.11.11 issued a new one). With a fresh DeviceId the console's old sign-in
stays valid until the console holds the new one, and only then is it logged out.

Nothing here prints, logs or returns the password, tokens, user id or server address:
results are yes/no. Errors are generic. No proxy, no redirects.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from user_paths import PRIVATE  # noqa: E402  (per-user folder, see user_paths.py)
import secrets
import urllib.error
import urllib.parse
import urllib.request

import chromatic_provision as cp

CLIENT, DEVICE, VERSION = 'Chromatic Jellyfin', 'Chromatic', '1.2.0'
# The owner's approval comes from one dedicated session of the linker's own, never another
# device's (such as the PC companion's "pocket-testing-local-pc"), so linking cannot sign
# anything else out. Its DeviceId and token are kept here (private, Git-ignored).
APPROVER_CLIENT, APPROVER_DEVICE, APPROVER_PREFIX = 'Chromatic Manager', 'PC', 'chromatic-manager-linker-'
LINKER_FILE = PRIVATE / 'chromatic-jellyfin-linker.json'
# The only sign-out this module ever sends: POST /Sessions/Logout with one console token
# (the one being replaced or forgotten). No user-wide logout, device deletion or key calls.
TIMEOUT = 15


class LinkError(Exception):
    """A generic, printable reason; never carries server data."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LinkError('The Jellyfin server redirected the request; use its final address in the PC settings.')


def new_device_id():
    return 'chromatic-' + secrets.token_hex(12)


def device_header(device_id, token=None, client=CLIENT, device=DEVICE):
    header = (f'MediaBrowser Client="{client}", Device="{device}", DeviceId="{device_id}", '
              f'Version="{VERSION}"')
    return header + (f', Token="{token}"' if token else '')


class Linker:
    """Quick Connect against the server in the PC's private connection file."""

    def __init__(self, connection, opener=None, state_file=None):
        self.connection = connection
        self.state_file = Path(state_file) if state_file else LINKER_FILE
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())

    def _call(self, method, path, query=None, body=None, device_id=None, token=None, pc_token=None,
              client=CLIENT, device=DEVICE):
        url = self.connection.server_url + path + ('?' + urllib.parse.urlencode(query) if query else '')
        headers = {'Accept': 'application/json'}
        if device_id:
            headers['Authorization'] = device_header(device_id, token, client, device)
        if pc_token:
            headers['X-Emby-Token'] = pc_token
        data = None
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=TIMEOUT) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw.strip() else None)
        except LinkError:
            raise
        except urllib.error.HTTPError as error:
            error.close()
            return error.code, None
        except (OSError, ValueError):
            raise LinkError('The Jellyfin server could not be reached from this PC.') from None

    def reachable(self):
        try:
            status, body = self._call('GET', '/System/Info/Public')
        except LinkError:
            return False
        return status == 200 and isinstance(body, dict)

    def quick_connect_enabled(self):
        status, body = self._call('GET', '/QuickConnect/Enabled')
        return status == 200 and body is True

    def link(self, device_id=None):
        """A new device sign-in for the console: chromatic_provision.Jellyfin."""
        device_id = device_id or new_device_id()
        if not self.quick_connect_enabled():
            raise LinkError('Quick Connect is switched off on the Jellyfin server (Dashboard > General).')
        status, started = self._call('POST', '/QuickConnect/Initiate', device_id=device_id)
        if status == 405 or status == 404:  # servers before 10.9 used GET
            status, started = self._call('GET', '/QuickConnect/Initiate', device_id=device_id)
        if status != 200 or not isinstance(started, dict) or not started.get('Secret') or not started.get('Code'):
            raise LinkError('The Jellyfin server did not start Quick Connect.')
        self._approve(started['Code'])
        status, result = self._call('POST', '/Users/AuthenticateWithQuickConnect',
                                    body={'Secret': started['Secret']}, device_id=device_id)
        if status != 200 or not isinstance(result, dict):
            raise LinkError('The Jellyfin server did not issue the console\'s sign-in.')
        user = (result.get('User') or {}).get('Id', '')
        token = result.get('AccessToken', '')
        try:
            linked = cp.Jellyfin(self.connection.server_url, user.replace('-', '').lower(), token, device_id).check()
        except ValueError:
            self.revoke(token, device_id)
            raise LinkError('The Jellyfin server returned a sign-in this console cannot store.') from None
        if self.connection.server_url.rstrip('/') != linked.url:
            raise LinkError('The Jellyfin address in the PC settings is not usable on the console.')
        return linked

    def _owner_session(self, fresh=False):
        """The owner's approving session, as keyword arguments for _call, and the user id.
        `fresh` (the Manager's Jellyfin page testing a typed password): skip the kept token and
        sign in by password, as the same dedicated device, so the password itself is proven.

        Owner rule: linking must never end any other session on the server.
        So this never signs in as another device: with a token in the PC settings it uses
        that; otherwise it keeps one dedicated session of its own ("Chromatic Manager" on
        "PC", a DeviceId made once and kept in LINKER_FILE with its token), reuses that
        token while it works, and signs in by password only when it has none that works.
        A new password sign-in uses the same dedicated DeviceId, so at most it replaces this
        session's own earlier token. Nothing here logs anything out."""
        if self.connection.access_token:  # a settings file with a token instead of a password: use it, keep it
            status, me = self._call('GET', '/Users/Me', pc_token=self.connection.access_token)
            user = me.get('Id', '') if status == 200 and isinstance(me, dict) else ''
            if not user:
                raise LinkError('The PC could not sign in to Jellyfin with its private settings.')
            return {'pc_token': self.connection.access_token}, user
        state = self._load_state()
        session = {'device_id': state['device_id'], 'client': APPROVER_CLIENT, 'device': APPROVER_DEVICE}
        if state.get('token') and not fresh and state.get('user') in (None, self._user_mark()):
            status, me = self._call('GET', '/Users/Me', token=state['token'], **session)
            if status == 200 and isinstance(me, dict) and me.get('Id'):
                return dict(session, token=state['token']), me['Id']
        status, signed = self._call('POST', '/Users/AuthenticateByName', **session,
                                    body={'Username': self.connection.username, 'Pw': self.connection.password})
        token = signed.get('AccessToken', '') if status == 200 and isinstance(signed, dict) else ''
        user = (signed.get('User') or {}).get('Id', '') if token else ''
        if not token or not user:
            raise LinkError('The PC could not sign in to Jellyfin with its private settings.')
        self._save_state(dict(state, token=token, user=self._user_mark()))
        return dict(session, token=token), user

    def _user_mark(self):
        """A short one-way mark of the PC's user name (never the name itself), so a kept token is
        reused only for the user it was made for: after the user changes on the Jellyfin page,
        the next approval signs in again instead of approving as the old user."""
        import hashlib
        return hashlib.sha256(('chromatic-linker\0' + self.connection.username).encode('utf-8')).hexdigest()[:16]

    def _load_state(self):
        try:
            state = json.loads(self.state_file.read_text(encoding='utf-8'))
            if state.get('server') == self.connection.server_url and str(state.get('device_id', '')).startswith(
                    APPROVER_PREFIX):
                return state
        except (OSError, ValueError, AttributeError):
            pass
        return {'server': self.connection.server_url, 'device_id': APPROVER_PREFIX + secrets.token_hex(8)}

    def _save_state(self, state):
        """Private, Git-ignored; written whole or not at all. A failure only means a password
        sign-in next time (as the same dedicated device)."""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_file.with_suffix('.tmp')
            temporary.write_text(json.dumps(state), encoding='utf-8')
            os.replace(temporary, self.state_file)
        except OSError:
            pass

    def _approve(self, code):
        """Approve the code as the owner from the linker's own session (see _owner_session)."""
        session, user = self._owner_session()
        status, approved = self._call('POST', '/QuickConnect/Authorize', query={'code': code, 'userId': user},
                                      **session)
        if status != 200 or approved is False:
            raise LinkError('The Jellyfin server did not accept the Quick Connect approval.')

    def works(self, linked):
        """Does the console's token sign in? (GET /Users/Me as that device.)"""
        return self.token_state(linked) == 'works'

    def token_state(self, linked):
        """'works', 'refused' (the server answered 401/403: the sign-in was removed, for example
        in Dashboard > Devices) or 'unreachable' (no answer, or any other answer). One GET
        /Users/Me with the console's own token, as that console device; nothing is changed."""
        try:
            status, body = self._call('GET', '/Users/Me', device_id=linked.device_id, token=linked.access_token)
        except LinkError:
            return 'unreachable'
        if status == 200 and isinstance(body, dict):
            return 'works'
        return 'refused' if status in (401, 403) else 'unreachable'

    def revoke(self, token, device_id):
        """Log a console token out; the server then removes that device. True also when
        it was already gone."""
        if not token:
            return True
        if not str(device_id).startswith('chromatic-') or str(device_id).startswith(APPROVER_PREFIX):
            return False  # only ever a Chromatic console's own sign-in
        try:
            status, _ = self._call('POST', '/Sessions/Logout', device_id=device_id, token=token)
        except LinkError:
            return False
        return status in (200, 204, 401, 403)


def load_pc_connection():
    """The PC's private connection file, read by local code only."""
    from connection_config import DEFAULT_CONFIG, load_connection
    return load_connection(DEFAULT_CONFIG)


def pc_connection_available():
    from connection_config import DEFAULT_CONFIG
    return DEFAULT_CONFIG.exists()


def check_pc_connection(connection, opener=None, state_file=None):
    """The Manager's Jellyfin page: does this PC's connection work? Yes/no only:
    {'reachable', 'signed_in', 'quick_connect'}. A password is proven by signing in as the
    linker's own dedicated device ("Chromatic Manager" on "PC"), which at most replaces that
    device's own earlier token; a token is checked with GET /Users/Me. Nothing else is signed
    in or out, and nothing read is returned."""
    linker = Linker(connection, opener, state_file)
    result = {'reachable': linker.reachable(), 'signed_in': False, 'quick_connect': False}
    if not result['reachable']:
        return result
    try:
        linker._owner_session(fresh=True)
        result['signed_in'] = True
    except LinkError:
        pass
    try:
        result['quick_connect'] = linker.quick_connect_enabled()
    except LinkError:
        pass
    return result

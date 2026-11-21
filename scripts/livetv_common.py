# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Small shared helpers of the optional Live TV set-up (scripts/chromatic_server_apps.py): read-only requests,
the server fingerprint taken before and compared after a change, the tuner name an earlier Chromatic Manager used,
and the rules for English cartoon channels from iptv-org's public lists.

Nothing here changes a server or prints a server value. Pure Python, no network of its own.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jellyfin_client import ClientError  # noqa: E402

# The tuner name an earlier Chromatic Manager gave the one tuner it added; it still counts as ours.
NAME = 'Chromatic TV (added by Chromatic Manager)'


def read(client, path, query=None):
    """(value, status): status is 'ok', 'denied', 'missing' or 'failed'. Never raises."""
    try:
        return client.json(path, query), 'ok'
    except ClientError as error:
        return None, {401: 'denied', 403: 'denied', 404: 'missing'}.get(error.status, 'failed')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def take_fingerprint(client):
    """What must not change: libraries, users, devices, plugins, task triggers, conversion settings."""
    fingerprint, shown = {}, []
    for name, path, key in (('libraries', '/Library/VirtualFolders', None), ('users', '/Users', None),
                            ('devices', '/Devices', 'Items'), ('plugins', '/Plugins', None),
                            ('tasks', '/ScheduledTasks', None), ('encoding', '/System/Configuration/encoding', None)):
        value, status = read(client, path)
        if status != 'ok':
            fingerprint[name] = {'status': status}
            shown.append(f'{name} not readable')
            continue
        if name == 'devices':  # last-activity times change by themselves; keep identity only
            value = sorted(str(d.get('Id', '')) for d in (value.get('Items') or []))
        if name == 'users':
            value = sorted(str(u.get('Id', '')) for u in value)
        if name == 'tasks':  # run times change by themselves
            value = sorted((str(t.get('Key', '')), canonical(t.get('Triggers')).decode()) for t in value)
        fingerprint[name] = {'status': 'ok', 'sha256': hashlib.sha256(canonical(value)).hexdigest(),
                             'count': len(value) if isinstance(value, list) else None}
        if isinstance(value, list):
            shown.append(f'{name} {len(value)}')
    return fingerprint, shown


def without(config, ident):
    """The Live TV settings with one tuner left out, for comparing."""
    return dict(config, TunerHosts=[x for x in config.get('TunerHosts') or [] if str(x.get('Id')) != ident])


# English cartoon channels: left out are pay-TV channels re-streamed by third parties, feeds made for other regions,
# and entries that are not cartoons. Streams served from a bare IP address are left out too.
DROP = re.compile(r'Cartoon Network|Cartoonito|DreamWorks|Animax|Toonami Aftermath|BBC|MCN6|Placid|'
                  r'\((Germany|Sweden|Poland|Pakistan)\)| (DK|NO|GB|CA)$|Asia|En Francais', re.I)
# Left out after a test viewing: another language mixed into its single sound track.
DROP_SEEN = re.compile(r'^Sonic TV\b', re.I)
BARE_IP = re.compile(r'https?://\d{1,3}(\.\d{1,3}){3}[:/]')

# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Jellyfin's LAN server discovery for Chromatic Manager's "Find servers" (the same UDP broadcast the
console's own "Find servers nearby" and Jellyfin's apps use). Sends one broadcast on the local network,
reads the replies; nothing else."""
from __future__ import annotations

import json
import socket
import time
import urllib.parse

DISCOVERY_PORT = 7359


class DiscoveryError(Exception):
    pass


def normalise_address(text):
    """A server address as typed: adds http:// and the default port, refuses credentials."""
    from connection_config import validate
    text = (text or '').strip().rstrip('/')
    if text and '://' not in text:
        text = 'http://' + text
        parts = urllib.parse.urlsplit(text)
        if parts.port is None:
            text = f'http://{parts.hostname}:8096{parts.path}'
    try:
        return validate({'server_url': text, 'access_token': 'x'}).server_url
    except ValueError:
        raise DiscoveryError('Enter a server address like your-server:8096.') from None


def discover(timeout=2.0, targets=None):
    """Jellyfin's LAN discovery: a UDP broadcast on port 7359 that servers answer with their
    name and address. Returns [{'name', 'address'}], deduplicated."""
    targets = [('255.255.255.255', DISCOVERY_PORT)] if targets is None else targets
    found, seen = [], set()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.25)
        for target in targets:
            try:
                sock.sendto(b'Who is JellyfinServer?', target)
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                reply = json.loads(data)
                address = normalise_address(str(reply.get('Address', '')))
            except (ValueError, DiscoveryError, AttributeError):
                continue
            if address not in seen:
                seen.add(address)
                found.append({'name': str(reply.get('Name') or 'Jellyfin')[:60], 'address': address})
    return found

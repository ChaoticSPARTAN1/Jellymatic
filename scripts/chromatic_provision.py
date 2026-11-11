# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Provisioning record for Chromatic launcher 1.2 (Wi-Fi, PC pairing and Jellyfin), the
Python twin of chromatic/launcher/shared/provision.h. Owner data: callers keep it in
private files and never print it. Version 1 (one network, one sector) is still read;
Chromatic Manager writes version 2 (up to 4 networks, optional Jellyfin device sign-in,
two sectors). Spec: docs/Design-notes.md; v1: docs/CHROMATIC-LAUNCHER-1.2.md."""
from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import re
import struct
from urllib.parse import urlsplit
import zlib

SECTOR = 4096
OFFSET_IN_FLASH = 0x3FF000            # last sector of the launcher slot
VERSION = 1
BODY = 4 + 2 + 2 + 33 + 65 + 4 + 2 + 32 + 32 + 32
SIZE = BODY + 4


@dataclass(frozen=True)
class Provision:
    ssid: str
    psk: str
    gateway_ipv4: str
    gateway_port: int
    gateway_cert_sha256: bytes
    gateway_token: bytes
    chr1_key: bytes

    def check(self):
        ssid, psk = self.ssid.encode('utf-8'), self.psk.encode('utf-8')
        if not 1 <= len(ssid) <= 32:
            raise ValueError('The Wi-Fi name must be 1 to 32 bytes.')
        if psk and not 8 <= len(psk) <= 64:
            raise ValueError('The Wi-Fi password must be empty (open network) or 8 to 64 characters.')
        address = ipaddress.IPv4Address(self.gateway_ipv4)
        if not address.is_private or address.is_loopback:
            raise ValueError('The PC address must be a private LAN IPv4 address.')
        if not 0 < self.gateway_port < 65536:
            raise ValueError('Bad gateway port.')
        for name in ('gateway_cert_sha256', 'gateway_token', 'chr1_key'):
            if len(getattr(self, name)) != 32:
                raise ValueError(f'{name} must be 32 bytes.')
        return self

    def sector(self):
        self.check()
        body = (b'PJPV' + struct.pack('<HH', VERSION, BODY) + self.ssid.encode('utf-8').ljust(33, b'\0') +
                self.psk.encode('utf-8').ljust(65, b'\0') + ipaddress.IPv4Address(self.gateway_ipv4).packed +
                struct.pack('<H', self.gateway_port) + self.gateway_cert_sha256 + self.gateway_token + self.chr1_key)
        assert len(body) == BODY
        record = body + struct.pack('<I', zlib.crc32(body) & 0xffffffff)
        return record.ljust(SECTOR, b'\xff')


def parse(sector):
    """Provision, None for an erased sector, or ValueError for anything damaged."""
    if sector[:SIZE] == b'\xff' * SIZE:
        return None
    if sector[:4] != b'PJPV' or struct.unpack_from('<HH', sector, 4) != (VERSION, BODY):
        raise ValueError('Not a provisioning record.')
    if zlib.crc32(sector[:BODY]) & 0xffffffff != struct.unpack_from('<I', sector, BODY)[0]:
        raise ValueError('Provisioning record checksum failed.')
    at = 8
    ssid = sector[at:at + 33]; at += 33
    psk = sector[at:at + 65]; at += 65
    ip = str(ipaddress.IPv4Address(sector[at:at + 4])); at += 4
    port = struct.unpack_from('<H', sector, at)[0]; at += 2
    cert, token, key = sector[at:at + 32], sector[at + 32:at + 64], sector[at + 64:at + 96]
    if ssid[32] or psk[64]:
        raise ValueError('Unterminated text in the provisioning record.')
    text = lambda raw: raw.split(b'\0')[0].decode('utf-8')
    return Provision(text(ssid), text(psk), ip, port, cert, token, key).check()



# ---- version 2 -------------------------------------------------------------------------
# Two sectors at the end of the launcher slot. Chromatic Manager writes the same record
# to both (so no older secret is left behind); the launcher's own on-console edits write
# the other sector with sequence + 1. The newest valid record wins; a tie goes to A.
SECTOR_A, SECTOR_B = 0x3FE000, 0x3FF000
VERSION_2 = 2
MAX_NETWORKS = 4
FLAG_GATEWAY, FLAG_JELLYFIN = 0x01, 0x02
SSID_FIELD, PSK_FIELD = 33, 65
URL_FIELD, USER_FIELD, TOKEN_FIELD, DEVICE_FIELD = 129, 33, 65, 65
NETWORKS_AT = 16
GATEWAY_AT = NETWORKS_AT + MAX_NETWORKS * (SSID_FIELD + PSK_FIELD)       # 408
JELLYFIN_AT = GATEWAY_AT + 4 + 2 + 2 + 32 + 32 + 32                       # 512
BODY_2 = JELLYFIN_AT + URL_FIELD + USER_FIELD + TOKEN_FIELD + DEVICE_FIELD + 4  # 808 (4 reserved zero bytes)
SIZE_2 = BODY_2 + 4

_TOKEN = re.compile(r'[A-Za-z0-9]{16,64}')
_DEVICE = re.compile(r'[A-Za-z0-9._-]{1,64}')
_USER = re.compile(r'[0-9a-f]{32}')


@dataclass(frozen=True)
class Network:
    ssid: str = field(repr=False)
    psk: str = field(repr=False)

    def check(self):
        ssid, psk = self.ssid.encode('utf-8'), self.psk.encode('utf-8')
        if not 1 <= len(ssid) <= 32 or b'\0' in ssid:
            raise ValueError('A Wi-Fi name must be 1 to 32 bytes.')
        if psk and not 8 <= len(psk) <= 64 or b'\0' in psk:
            raise ValueError('A Wi-Fi password must be empty (open network) or 8 to 64 characters.')
        return self


@dataclass(frozen=True)
class Gateway:
    """The PC companion (paired TLS gateway and Remote Pilot key), as in version 1."""
    ipv4: str
    port: int
    cert_sha256: bytes = field(repr=False)
    token: bytes = field(repr=False)
    chr1_key: bytes = field(repr=False)

    def check(self):
        Provision('x', '', self.ipv4, self.port, self.cert_sha256, self.token, self.chr1_key).check()
        return self


@dataclass(frozen=True)
class Jellyfin:
    """The console's own Jellyfin sign-in: a per-device access token (revocable in the
    server's Dashboard > Devices), never the owner's password."""
    url: str = field(repr=False)
    user_id: str = field(repr=False)
    access_token: str = field(repr=False)
    device_id: str = field(repr=False)

    def check(self):
        url = self.url
        try:
            parts = urlsplit(url)
            good = (all(32 < ord(c) < 127 for c in url) and len(url) <= URL_FIELD - 1
                    and parts.scheme in ('http', 'https')
                    and parts.hostname and not parts.username and not parts.password and not parts.query
                    and not parts.fragment and not url.endswith('/') and parts.port != 0)
        except ValueError:
            good = False
        if not good:
            raise ValueError('The Jellyfin address must be http(s)://host[:port][/path], at most 128 characters.')
        if not _USER.fullmatch(self.user_id):
            raise ValueError('The Jellyfin user id must be 32 lowercase hex digits.')
        if not _TOKEN.fullmatch(self.access_token):
            raise ValueError('The Jellyfin access token must be 16 to 64 letters or digits.')
        if not _DEVICE.fullmatch(self.device_id):
            raise ValueError('The Jellyfin device id must be 1 to 64 of A-Z a-z 0-9 . _ -.')
        return self


@dataclass(frozen=True)
class Record:
    networks: tuple = ()
    gateway: Gateway | None = None
    jellyfin: Jellyfin | None = None
    sequence: int = 0
    version: int = VERSION_2   # 1 when read from a version-1 sector

    def check(self):
        if len(self.networks) > MAX_NETWORKS:
            raise ValueError(f'At most {MAX_NETWORKS} Wi-Fi networks.')
        for network in self.networks:
            network.check()
        if self.gateway is not None:
            self.gateway.check()
        if self.jellyfin is not None:
            self.jellyfin.check()
        if not 0 <= self.sequence <= 0xFFFFFFFF:
            raise ValueError('Bad sequence number.')
        return self

    def summary(self):
        """What may be shown: counts and yes/no, never names, keys or tokens."""
        return {'networks': len(self.networks), 'pc_gateway': self.gateway is not None,
                'jellyfin': self.jellyfin is not None, 'version': self.version}

    def sector(self):
        self.check()
        flags = (FLAG_GATEWAY if self.gateway else 0) | (FLAG_JELLYFIN if self.jellyfin else 0)
        body = bytearray(BODY_2)
        body[0:16] = b'PJPV' + struct.pack('<HHIBBH', VERSION_2, BODY_2, self.sequence, len(self.networks), flags, 0)
        for index, network in enumerate(self.networks):
            at = NETWORKS_AT + index * (SSID_FIELD + PSK_FIELD)
            body[at:at + SSID_FIELD] = network.ssid.encode('utf-8').ljust(SSID_FIELD, b'\0')
            body[at + SSID_FIELD:at + SSID_FIELD + PSK_FIELD] = network.psk.encode('utf-8').ljust(PSK_FIELD, b'\0')
        if self.gateway:
            g = self.gateway
            body[GATEWAY_AT:JELLYFIN_AT] = (ipaddress.IPv4Address(g.ipv4).packed + struct.pack('<HH', g.port, 0)
                                            + g.cert_sha256 + g.token + g.chr1_key)
        if self.jellyfin:
            j, at = self.jellyfin, JELLYFIN_AT
            for value, size in ((j.url, URL_FIELD), (j.user_id, USER_FIELD), (j.access_token, TOKEN_FIELD),
                                (j.device_id, DEVICE_FIELD)):
                body[at:at + size] = value.encode('ascii').ljust(size, b'\0')
                at += size
        body = bytes(body)
        return (body + struct.pack('<I', zlib.crc32(body) & 0xffffffff)).ljust(SECTOR, b'\xff')


def _text(raw, what):
    """A NUL-terminated field: text, then only zero bytes (refused otherwise)."""
    end = raw.find(b'\0')
    if end < 0 or any(raw[end:]):
        raise ValueError(f'Unterminated or unpadded {what} in the provisioning record.')
    return raw[:end].decode('utf-8')


def parse_v2(sector):
    """Record, None for an erased sector, or ValueError for anything damaged or invalid."""
    if len(sector) < SIZE_2:
        raise ValueError('Provisioning sector too short.')
    if sector[:SIZE_2] == b'\xff' * SIZE_2:
        return None
    magic, version, length, sequence, count, flags, reserved = struct.unpack_from('<4sHHIBBH', sector, 0)
    if magic != b'PJPV' or version != VERSION_2 or length != BODY_2:
        raise ValueError('Not a version-2 provisioning record.')
    if zlib.crc32(sector[:BODY_2]) & 0xffffffff != struct.unpack_from('<I', sector, BODY_2)[0]:
        raise ValueError('Provisioning record checksum failed.')
    if reserved or count > MAX_NETWORKS or flags & ~(FLAG_GATEWAY | FLAG_JELLYFIN) or any(sector[BODY_2 - 4:BODY_2]):
        raise ValueError('Provisioning record has bad header or reserved fields.')
    networks = []
    for index in range(MAX_NETWORKS):
        at = NETWORKS_AT + index * (SSID_FIELD + PSK_FIELD)
        slot = sector[at:at + SSID_FIELD + PSK_FIELD]
        if index >= count:
            if any(slot):
                raise ValueError('Unused Wi-Fi slot is not empty.')
            continue
        networks.append(Network(_text(slot[:SSID_FIELD], 'Wi-Fi name'), _text(slot[SSID_FIELD:], 'Wi-Fi password')))
    gateway_raw = sector[GATEWAY_AT:JELLYFIN_AT]
    gateway = None
    if flags & FLAG_GATEWAY:
        port, spare = struct.unpack_from('<HH', gateway_raw, 4)
        if spare:
            raise ValueError('Provisioning record has bad reserved fields.')
        gateway = Gateway(str(ipaddress.IPv4Address(gateway_raw[:4])), port, gateway_raw[8:40], gateway_raw[40:72],
                          gateway_raw[72:104])
    elif any(gateway_raw):
        raise ValueError('Gateway fields set without the gateway flag.')
    jellyfin_raw = sector[JELLYFIN_AT:BODY_2 - 4]
    jellyfin = None
    if flags & FLAG_JELLYFIN:
        values, at = [], 0
        for size, what in ((URL_FIELD, 'Jellyfin address'), (USER_FIELD, 'Jellyfin user id'),
                           (TOKEN_FIELD, 'Jellyfin token'), (DEVICE_FIELD, 'Jellyfin device id')):
            values.append(_text(jellyfin_raw[at:at + size], what))
            at += size
        jellyfin = Jellyfin(*values)
    elif any(jellyfin_raw):
        raise ValueError('Jellyfin fields set without the Jellyfin flag.')
    return Record(tuple(networks), gateway, jellyfin, sequence).check()


def parse_any(sector):
    """A version-1 or version-2 sector as a Record (version 1: sequence 0), None if erased."""
    if sector[:4] == b'PJPV' and struct.unpack_from('<H', sector, 4)[0] == VERSION:
        old = parse(sector)
        return Record((Network(old.ssid, old.psk),),
                      Gateway(old.gateway_ipv4, old.gateway_port, old.gateway_cert_sha256, old.gateway_token,
                              old.chr1_key), None, 0, VERSION)
    return parse_v2(sector)


def current(sector_a, sector_b):
    """The record the launcher uses: the newest valid one (tie: A). A damaged sector
    counts as empty, so a torn write only ever means falling back to the other one."""
    best = None
    for name, sector in (('A', sector_a), ('B', sector_b)):
        try:
            record = parse_any(sector)
        except ValueError:
            record = None
        if record is not None and (best is None or record.sequence > best[0].sequence):
            best = (record, name)
    return best or (None, None)


def vectors():
    """Synthetic test vectors for the launcher's C parser (docs/Design-notes.md).
    Each: name, the first SIZE_2 bytes as hex (the rest of the sector is 0xFF), and the
    expected result: 'absent', 'refused', or a summary of what must be read."""
    gateway = Gateway('10.77.0.20', 8443, bytes(range(32)), bytes(range(32, 64)), bytes(range(64, 96)))
    jellyfin = Jellyfin('http://10.77.0.10:8096', '0123456789abcdef0123456789abcdef',
                        '00112233445566778899aabbccddeeff', 'chromatic-0011223344556677')
    nets = (Network('Test Network', 'correct horse battery'), Network('Open Cafe', ''),
            Network('Upstairs', 'x' * 64), Network('Garage', 'abcdefgh'))
    full = Record(nets, gateway, jellyfin, 7)
    good = [('full', full), ('wifi-only', Record(nets[:1], None, None, 1)),
            ('jellyfin-only', Record((), None, jellyfin, 2)), ('gateway-only', Record((), gateway, None, 3)),
            ('https-path', Record(nets[1:2], None, Jellyfin('https://media.example.lan/jellyfin', jellyfin.user_id,
                                                              jellyfin.access_token, 'chromatic-x'), 0xFFFFFFFF))]
    out = []
    for name, record in good:
        out.append({'name': name, 'hex': record.sector()[:SIZE_2].hex(), 'expect': {
            'sequence': record.sequence, 'networks': [n.ssid for n in record.networks],
            'open_networks': [n.ssid for n in record.networks if not n.psk],
            'gateway_port': record.gateway.port if record.gateway else None,
            'jellyfin_url': record.jellyfin.url if record.jellyfin else None}})
    out.append({'name': 'erased', 'hex': 'ff' * SIZE_2, 'expect': 'absent'})
    base = full.sector()[:SIZE_2]

    def resealed(edit):
        body = bytearray(base[:BODY_2])
        edit(body)
        return (bytes(body) + struct.pack('<I', zlib.crc32(bytes(body)) & 0xffffffff)).hex()
    bad_crc = bytearray(base)
    bad_crc[BODY_2] ^= 1
    for name, data in (
            ('bad-crc', bytes(bad_crc).hex()),
            ('version-3', resealed(lambda b: b.__setitem__(slice(4, 6), struct.pack('<H', 3)))),
            ('bad-length', resealed(lambda b: b.__setitem__(slice(6, 8), struct.pack('<H', BODY_2 - 4)))),
            ('five-networks', resealed(lambda b: b.__setitem__(12, 5))),
            ('unknown-flag', resealed(lambda b: b.__setitem__(13, 0x07))),
            ('reserved-header', resealed(lambda b: b.__setitem__(14, 1))),
            ('reserved-tail', resealed(lambda b: b.__setitem__(BODY_2 - 1, 1))),
            ('unused-slot-not-empty', resealed(lambda b: (b.__setitem__(12, 3)))),
            ('unterminated-ssid', resealed(lambda b: b.__setitem__(slice(16, 49), b'y' * 33))),
            ('garbage-after-nul', resealed(lambda b: b.__setitem__(16 + 20, 0x41))),
            ('empty-ssid', resealed(lambda b: b.__setitem__(slice(16, 49), bytes(33)))),
            ('short-password', resealed(lambda b: b.__setitem__(slice(49, 114), b'short'.ljust(65, b'\0')))),
            ('public-gateway', resealed(lambda b: b.__setitem__(slice(GATEWAY_AT, GATEWAY_AT + 4), bytes([8, 8, 8, 8])))),
            ('gateway-port-zero', resealed(lambda b: b.__setitem__(slice(GATEWAY_AT + 4, GATEWAY_AT + 6), b'\0\0'))),
            ('jellyfin-ftp-url', resealed(lambda b: b.__setitem__(slice(JELLYFIN_AT, JELLYFIN_AT + 4), b'ftp:'))),
            ('jellyfin-url-control-char', resealed(lambda b: b.__setitem__(JELLYFIN_AT + 10, 0x0a))),
            ('jellyfin-url-space', resealed(lambda b: b.__setitem__(JELLYFIN_AT + 10, 0x20))),
            ('jellyfin-user-not-hex', resealed(lambda b: b.__setitem__(JELLYFIN_AT + URL_FIELD, ord('Z')))),
            ('jellyfin-without-flag', resealed(lambda b: b.__setitem__(13, FLAG_GATEWAY))),
            ('version-1-length-with-version-2', resealed(lambda b: b.__setitem__(slice(6, 8),
                                                                                 struct.pack('<H', BODY))))):
        out.append({'name': name, 'hex': data, 'expect': 'refused'})
    return out

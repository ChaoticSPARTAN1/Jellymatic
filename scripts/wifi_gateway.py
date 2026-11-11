# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""PC TLS transport for PJB1. No console firmware or firewall changes.

The future Chromatic client must pin identity/server.der and authenticate with
identity/pairing.bin. Neither is a Jellyfin credential. This program never sends
the configured Jellyfin address or authentication material to the peer.
"""
import argparse
import hmac
import ipaddress
from pathlib import Path
from user_paths import PRIVATE  # noqa: E402  (per-user folder, see user_paths.py)
import secrets
import socket
import ssl
import subprocess
import sys
import time

from connection_config import ROOT, DEFAULT_CONFIG, load_connection
from jellyfin_client import JellyfinClient
from link_gateway import Gateway

IDENTITY = PRIVATE / "wifi-gateway"
AUTH_MAGIC = b"PJT1"
AUTH_ACK = b"PJA1"
AUTH_SIZE = 32
OPENSSL = Path("C:/Program Files/Git/usr/bin/openssl.exe")


def initialize(folder=IDENTITY, openssl=OPENSSL):
    """Explicit first-time local identity creation; refuse partial/old identities."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    names = ("server.key", "server.pem", "server.der", "pairing.bin")
    if any((folder / name).exists() for name in names):
        raise ValueError("Identity already exists or is incomplete.")
    try:
        subprocess.run([str(openssl), "req", "-x509", "-newkey", "ec", "-pkeyopt",
            "ec_paramgen_curve:P-256", "-nodes", "-sha256", "-days", "3650",
            "-subj", "/CN=pocket-media.local", "-addext", "subjectAltName=DNS:pocket-media.local",
            "-keyout", str(folder / "server.key"),
            "-out", str(folder / "server.pem")], check=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([str(openssl), "x509", "-in", str(folder / "server.pem"),
            "-outform", "DER", "-out", str(folder / "server.der")], check=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (folder / "pairing.bin").write_bytes(secrets.token_bytes(AUTH_SIZE))
    except Exception:
        # Only this invocation's files can exist after the preflight above.
        for name in names:
            (folder / name).unlink(missing_ok=True)
        raise


def load_identity(folder=IDENTITY):
    folder = Path(folder)
    token = (folder / "pairing.bin").read_bytes()
    if len(token) != AUTH_SIZE:
        raise ValueError("Invalid local pairing identity.")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(folder / "server.pem", folder / "server.key")
    der = ssl.PEM_cert_to_DER_cert((folder / "server.pem").read_text(encoding="ascii"))
    if not hmac.compare_digest(der, (folder / "server.der").read_bytes()):
        raise ValueError("Local certificate files disagree.")
    return context, token


def local_bind(value):
    address = ipaddress.IPv4Address(value)
    networks = ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    if not any(address in ipaddress.IPv4Network(n) for n in networks):
        raise ValueError("Select a loopback or private LAN IPv4 interface.")
    return str(address)


def authenticate(stream, token, timeout=10.0):
    # Absolute deadline bounds peers sending one byte per socket timeout.
    deadline = time.monotonic() + timeout
    received = bytearray()
    while len(received) < len(AUTH_MAGIC) + AUTH_SIZE:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("Pairing timed out.")
        stream.settimeout(left)
        chunk = stream.recv(len(AUTH_MAGIC) + AUTH_SIZE - len(received))
        if not chunk:
            raise ValueError("Pairing interrupted.")
        received.extend(chunk)
    if not hmac.compare_digest(bytes(received), AUTH_MAGIC + token):
        raise ValueError("Pairing refused.")


def serve_peer(raw, context, token, factory):
    """One TLS connection owns one gateway, opaque handle set and stream worker."""
    gateway = None
    raw.settimeout(10.0)
    try:
        with context.wrap_socket(raw, server_side=True) as secure:
            authenticate(secure, token)
            # Authentication completes before any Jellyfin connection is made, and the
            # ACK follows only once Jellyfin is reachable: the device arms media mode on
            # the ACK, so it must never get one for a session that cannot serve.
            gateway = factory()
            secure.sendall(AUTH_ACK)
            secure.settimeout(30.0)
            with secure.makefile("rb") as reader, secure.makefile("wb") as writer:
                gateway.serve(reader, writer)
        return True
    except Exception:
        # Do not expose request bodies, peer IDs, upstream errors or secrets.
        return False
    finally:
        if gateway is not None:
            gateway.close()
        raw.close()


def real_gateway():
    client = JellyfinClient(load_connection(DEFAULT_CONFIG))
    client.connect()
    return Gateway(client, transport="paired-tls-pjb1")


def serve(bind, port, context, token, factory=real_gateway):
    # Single owner avoids multiple encoders and cross-peer opaque handle reuse.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((local_bind(bind), port))
        listener.listen(1)
        print("Paired TLS gateway running. No console connection has been established.", flush=True)
        while True:
            raw, _ = listener.accept()
            serve_peer(raw, context, token, factory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init", action="store_true", help="Create local TLS/pairing files once; do not listen")
    parser.add_argument("--bind", default="127.0.0.1", help="Literal private LAN interface; defaults to loopback")
    parser.add_argument("--port", type=int, default=8443)
    args = parser.parse_args()
    try:
        if args.init:
            initialize()
            print("Private gateway identity created locally. No connection or listener started.")
            return 0
        if not 1 <= args.port <= 65535:
            raise ValueError("Invalid port.")
        context, token = load_identity()
        serve(args.bind, args.port, context, token)
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("Local TLS gateway stopped; details and credentials were not output.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

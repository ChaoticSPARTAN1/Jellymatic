# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Connection-file/manual-input groundwork; no Pocket or network transport yet."""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from getpass import getpass
from pathlib import Path
from user_paths import PRIVATE  # noqa: E402  (per-user folder, see user_paths.py)
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PRIVATE / "jellyfin-connection.txt"


@dataclass(frozen=True)
class Connection:
    server_url: str = field(repr=False)
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    access_token: str = field(default="", repr=False)


def validate(values: dict[str, str]) -> Connection:
    allowed = {"server_url", "username", "password", "access_token"}
    if values.keys() - allowed:
        raise ValueError("Unknown connection setting; use the supplied example keys.")
    url = values.get("server_url", "").strip().rstrip("/")
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                 and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment)
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            valid = False
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Provide an HTTP(S) server URL without embedded credentials, query, or fragment.")
    username = values.get("username", "")
    password = values.get("password", "")
    token = values.get("access_token", "")
    if not token and not username:
        raise ValueError("Provide a username or access token.")
    if token and (username or password):
        raise ValueError("Use either an access token or username/password.")
    return Connection(url, username, password, token)


def load_connection(path: Path = DEFAULT_CONFIG) -> Connection:
    values: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip().lower()
        if not separator or key in values:
            raise ValueError(f"Malformed or duplicate setting on line {number}.")
        # Preserve password/token spaces, '#' and '=' literally; no inline comments.
        values[key] = value
    return validate(values)


def save_connection(connection: Connection, path: Path = DEFAULT_CONFIG) -> Connection:
    """Write the connection file whole or not at all (a temporary file, then a rename), in the
    format load_connection reads. Used by Chromatic Manager's Jellyfin page; local code only:
    values are never printed, logged or put in arguments. Returns what load_connection will read."""
    values = {"server_url": connection.server_url}
    if connection.access_token:
        values["access_token"] = connection.access_token
    else:
        values["username"], values["password"] = connection.username, connection.password
    if any(ch in value for value in values.values() for ch in "\r\n\0"):
        raise ValueError("A setting cannot contain a line break.")
    checked = validate(values)
    text = ("# Jellyfin connection for this PC. Private: Git-ignored; never share or commit it.\n"
            "# Written by Chromatic Manager (Settings > Jellyfin server).\n"
            + "".join(f"{key}={value}\n" for key, value in values.items()))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    if load_connection(path) != checked:
        raise ValueError("The saved settings do not read back.")
    return checked


def manual_connection() -> Connection:
    return validate({"server_url": input("Server URL: ").strip(),
                     "username": input("Username: ").strip(),
                     "password": getpass("Password: ")})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--manual", action="store_true", help="Prompt without saving credentials")
    args = parser.parse_args()
    try:
        if args.manual or not args.config.exists():
            manual_connection()
        else:
            load_connection(args.config)
    except (OSError, ValueError, EOFError) as error:
        # Do not echo values or exception payloads containing filesystem/user data.
        message = str(error) if isinstance(error, ValueError) else "Could not read connection settings."
        print(message)
        return 1
    print("Connection settings validated. No network connection or Pocket playback was attempted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

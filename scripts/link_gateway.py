# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Bounded RPC for the future Pocket/Chromatic bridge, not a hardware driver.

Jellyfin authentication stays in this PC process. A peer receives opaque
session handles, display metadata, RGB565 art, requested original-media bytes
and bounded live compressed PJAV sessions. No server URLs, tokens, passwords or
upstream media paths are returned.
The current CLI is a binary stdin/stdout integration harness, not live Pocket
connectivity. It opens no network listener and changes no console firmware.
"""
import argparse
from collections import OrderedDict
import hashlib
import json
import struct
import sys
import zlib

from connection_config import DEFAULT_CONFIG, load_connection
from jellyfin_client import ClientError, JellyfinClient
from jellyfin_artwork import ART_KINDS, ART_POSTER, ART_THUMB, fetch_card

HEADER = struct.Struct("<4sBBHII")
MAGIC = b"PJB1"
MAX_PAYLOAD = 32768
CHUNK = 4096
HELLO, HOME, BROWSE, DETAILS, ART, MEDIA = range(1, 7)
STREAM_START, STREAM_READ, STREAM_STOP = range(7, 10)
RESPONSE, ERROR = 1, 2


class ProtocolError(ValueError):
    pass


def encode_packet(command, sequence, payload=b"", flags=0):
    if len(payload) > MAX_PAYLOAD:
        raise ProtocolError("Bridge payload exceeds its limit.")
    prefix = struct.pack("<4sBBHI", MAGIC, command, flags, len(payload), sequence)
    checksum = zlib.crc32(prefix + payload)
    return prefix + struct.pack("<I", checksum) + payload


def read_exact(stream, length):
    output = bytearray()
    while len(output) < length:
        chunk = stream.read(length - len(output))
        if not chunk:
            raise ProtocolError("Bridge packet was interrupted.")
        output.extend(chunk)
    return bytes(output)


def read_packet(stream):
    first = stream.read(1)
    if not first:
        return None
    header = first + read_exact(stream, HEADER.size - 1)
    magic, command, flags, length, sequence, checksum = HEADER.unpack(header)
    if magic != MAGIC or length > MAX_PAYLOAD or flags & ~(RESPONSE | ERROR):
        raise ProtocolError("Invalid bridge header.")
    payload = read_exact(stream, length)
    if zlib.crc32(header[:12] + payload) != checksum:
        raise ProtocolError("Bridge checksum mismatch.")
    return command, flags, sequence, payload


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class Gateway:
    def __init__(self, client, streams=None, transport="integration-harness", link_khz=None):
        self.client = client
        self.transport = transport
        self.link_khz = link_khz  # set only for a peer proven to follow that clock
        self.handles = {}
        self.reverse = {}
        self.art_cache = {}
        from stream_worker import StreamManager
        self.streams = streams if streams is not None else StreamManager(client)
        self.mutation_replies = OrderedDict()

    def close(self):
        self.streams.close()

    def public_item(self, item):
        identifier = item.get("Id", "")
        if not identifier:
            raise ProtocolError("Invalid server item.")
        if identifier not in self.reverse:
            if len(self.handles) >= 16384:
                raise ProtocolError("Bridge session is full. Reconnect to refresh it.")
            handle = len(self.handles) + 1
            self.reverse[identifier] = handle
            self.handles[handle] = item
        handle = self.reverse[identifier]
        self.handles[handle] = item
        user = item.get("UserData") or {}
        year = item.get("ProductionYear")
        if type(year) is not int or not 1 <= year <= 9999:
            year = None
        subtitle = ""
        if item.get("Type") == "Episode":
            season, episode = item.get("ParentIndexNumber"), item.get("IndexNumber")
            number = f"S{season}:E{episode}" if type(season) is int and type(episode) is int else ""
            subtitle = " - ".join(part for part in (str(item.get("SeriesName", ""))[:60], number) if part)
        return {"handle": handle, "title": str(item.get("Name", "Untitled"))[:160], "subtitle": subtitle,
                "rating": str(item.get("OfficialRating", ""))[:10],
                "type": str(item.get("Type", "Folder"))[:32], "folder": bool(item.get("IsFolder")),
                "duration_ticks": max(0, int(item.get("RunTimeTicks") or 0)),
                "resume_ticks": max(0, int(user.get("PlaybackPositionTicks") or 0)),
                "played": bool(user.get("Played")), "year": year}

    def resolve(self, request):
        handle = request.get("handle")
        if type(handle) is not int or handle not in self.handles:
            raise ProtocolError("Unknown media handle. Refresh the library.")
        return self.handles[handle]

    def dispatch(self, command, request):
        if command == HELLO:
            hello = {"version": 1, "transport": self.transport, "hardware_connected": False,
                     "max_payload": MAX_PAYLOAD, "media_encoding": "original-server-bytes",
                     "pocket_decoder_ready": False, "compressed_stream": "PJAV-JPEG-IMA",
                     "stream_commands": [STREAM_START, STREAM_READ, STREAM_STOP]}
            if self.link_khz:
                hello["link_khz"] = self.link_khz
            return hello
        if command == STREAM_START:
            return self.streams.start(self.resolve(request), request)
        if command == STREAM_READ:
            return self.streams.read(request)
        if command == STREAM_STOP:
            return self.streams.stop(request)
        if command == HOME:
            rows = self.client.home()
            result = {"rows": [], "truncated": len(rows) > 12}
            for row in rows[:12]:
                public = {"title": row["title"][:96],
                          "shape": ART_THUMB if row.get("shape") == "thumb" else ART_POSTER,
                          "items": [self.public_item(item) for item in row["items"][:12]]}
                result["rows"].append(public)
                while len(json_bytes(result)) > MAX_PAYLOAD and public["items"]:
                    public["items"].pop()
                    result["truncated"] = True
                if len(json_bytes(result)) > MAX_PAYLOAD:
                    result["rows"].pop()
                    result["truncated"] = True
                    break
            return result
        if command == BROWSE:
            parent = self.resolve(request)["Id"] if request.get("handle") else ""
            start = request.get("start", 0)
            search = request.get("search", "")
            if type(start) is not int or not 0 <= start <= 1000000 or not isinstance(search, str) or len(search) > 120:
                raise ProtocolError("Invalid library query.")
            result = self.client.browse(parent, search, start)
            # The upstream page is 100 items. Return 12 per bridge response and
            # keep next-page offsets aligned, rather than exposing source IDs.
            items = result.get("Items", [])[:12]
            # Episodes and libraries are landscape in the real client; everything else is a poster.
            wide = items and all(item.get("Type") in ("Episode", "CollectionFolder", "UserView") for item in items)
            return {"items": [self.public_item(item) for item in items],
                    "shape": ART_THUMB if wide else ART_POSTER,
                    "start": start, "next": start + len(items), "total": result.get("TotalRecordCount", len(items))}
        if command == DETAILS:
            item = self.client.item(self.resolve(request)["Id"])
            result = self.public_item(item)
            result["overview"] = str(item.get("Overview", ""))[:1800]
            result["genres"] = [str(g)[:80] for g in item.get("Genres", [])[:8]]
            return result
        if command in (ART, MEDIA):
            item = self.resolve(request)
            offset = request.get("offset", 0)
            if type(offset) is not int or not 0 <= offset <= 0xffffffff:
                raise ProtocolError("Invalid media offset.")
            if command == ART:
                kind = request.get("kind", ART_POSTER)
                if type(kind) is not int or kind not in ART_KINDS:
                    raise ProtocolError("Unknown artwork kind.")
                key = self.reverse[item["Id"]], kind
                if key not in self.art_cache:
                    self.art_cache[key] = fetch_card(self.client, item, kind) or b""
                    if len(self.art_cache) > 32:
                        del self.art_cache[next(iter(self.art_cache))]
                data = self.art_cache[key]
                _, width, height = ART_KINDS[kind]
                return struct.pack("<IHH", len(data), width, height) + data[offset:offset + CHUNK]
            if item.get("IsFolder") or item.get("Type") not in {"Movie", "Episode", "Video", "MusicVideo"}:
                raise ProtocolError("Choose a playable video.")
            with self.client.open_video(item["Id"], f"bytes={offset}-{offset + CHUNK - 1}") as response:
                if response.status != 206 or not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                    raise ProtocolError("This server did not honor the media byte range.")
                return response.read(CHUNK)
        raise ProtocolError("Unsupported bridge request.")

    def serve(self, reader, writer):
        try:
            self._serve(reader, writer)
        finally:
            self.close()

    def _serve(self, reader, writer):
        while (packet := read_packet(reader)) is not None:
            command, flags, sequence, payload = packet
            if flags:
                raise ProtocolError("Bridge request used response flags.")
            try:
                key = command, sequence
                digest = hashlib.sha256(payload).digest()
                if command in (STREAM_START, STREAM_STOP) and key in self.mutation_replies:
                    old_digest, answer = self.mutation_replies[key]
                    if digest != old_digest:
                        raise ProtocolError("Request ID reused with different settings.")
                    writer.write(answer)
                    writer.flush()
                    continue
                request = json.loads(payload or b"{}")
                if not isinstance(request, dict):
                    raise ProtocolError("Invalid bridge request.")
                response = self.dispatch(command, request)
                encoded = response if isinstance(response, bytes) else json_bytes(response)
                answer = encode_packet(command, sequence, encoded, RESPONSE)
                if command in (STREAM_START, STREAM_STOP):
                    self.mutation_replies[key] = digest, answer
                    if len(self.mutation_replies) > 4:
                        self.mutation_replies.popitem(last=False)
            except Exception:
                # Never reflect exception text, URLs, request bodies or upstream
                # responses into this protocol or terminal output.
                answer = encode_packet(command, sequence, json_bytes({"error": "Request failed locally."}), RESPONSE | ERROR)
            writer.write(answer)
            writer.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true", help="Check real-server home/media access locally; print only generic counts.")
    args = parser.parse_args()
    try:
        client = JellyfinClient(load_connection(DEFAULT_CONFIG))
        client.connect()
        gateway = Gateway(client)
        if args.probe:
            result = gateway.dispatch(HOME, {})
            rows = result["rows"]
            candidates = client.json("/Items", {"userId": client.user_id, "recursive": "true",
                "includeItemTypes": "Movie,Episode,Video", "limit": 1}).get("Items", [])
            if not candidates:
                raise ProtocolError("No accessible video.")
            public = gateway.public_item(candidates[0])
            detail = gateway.dispatch(DETAILS, {"handle": public["handle"]})
            art = gateway.dispatch(ART, {"handle": public["handle"]})
            media = gateway.dispatch(MEDIA, {"handle": public["handle"]})
            if not media or not detail.get("title"):
                raise ProtocolError("Server media check failed.")
            print(f"Live server authentication/home/details/media passed locally: {len(rows)} populated rows; artwork available: {len(art)>8}. Pocket transport is not connected.")
        else:
            gateway.serve(sys.stdin.buffer, sys.stdout.buffer)
    except Exception:
        print("Local gateway failed; no connection details were output.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

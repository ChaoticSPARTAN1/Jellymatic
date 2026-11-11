# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Local-only Jellyfin backend. Credentials never enter logs, URLs or subprocesses."""
from __future__ import annotations

import json
import re
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from connection_config import Connection


class ClientError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a Jellyfin token/password to a redirected destination.
        raise ClientError("Server redirected the request. Use its final base URL in the local settings file.")


def item_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ClientError("Invalid library item identifier.")
    return value


class JellyfinClient:
    def __init__(self, connection: Connection):
        self._connection = connection
        self._token = connection.access_token
        self.user_id = ""
        # Do not inherit an external HTTP proxy from the environment.
        self._http = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())
        self._device_id = "pocket-testing-local-pc"

    def _open(self, path, query=None, payload=None, headers=None, timeout=30):
        if not path.startswith("/") or path.startswith("//"):
            raise ClientError("Invalid API path.")
        url = self._connection.server_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request_headers = {
            "Accept": "application/json",
            "Authorization": 'MediaBrowser Client="Pocket Local Companion", Device="PC", '
                             f'DeviceId="{self._device_id}", Version="0.1.0"',
        }
        if self._token:
            request_headers["X-Emby-Token"] = self._token
        request_headers.update(headers or {})
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        try:
            return self._http.open(urllib.request.Request(url, data=data, headers=request_headers), timeout=timeout)
        except ClientError:
            raise
        except urllib.error.HTTPError as error:
            messages = {401: "Jellyfin rejected the sign-in. Check the local settings.",
                        403: "This Jellyfin account cannot access the requested item.",
                        404: "Jellyfin could not find this API or media item."}
            error.close()
            raise ClientError(messages.get(error.code, f"Jellyfin request failed (HTTP {error.code})."), error.code) from None
        except (OSError, ValueError, urllib.error.URLError):
            raise ClientError("Could not reach Jellyfin securely. Check the address, certificate and connection locally.") from None

    def json(self, path, query=None, payload=None):
        with self._open(path, query, payload) as response:
            try:
                return json.load(response)
            except (ValueError, OSError):
                raise ClientError("Jellyfin returned an invalid response.") from None

    def connect(self):
        if self._token:
            user = self.json("/Users/Me")
        else:
            result = self.json("/Users/AuthenticateByName", payload={
                "Username": self._connection.username, "Pw": self._connection.password})
            self._token = result.get("AccessToken", "")
            user = result.get("User", {})
        self.user_id = item_id(user.get("Id", ""))
        if not self._token:
            raise ClientError("Jellyfin did not return an access token.")

    def libraries(self):
        try:
            result = self.json("/UserViews", {"userId": self.user_id})
        except ClientError as error:
            if error.status != 404:
                raise
            result = self.json(f"/Users/{self.user_id}/Views")
        return result.get("Items", [])

    def browse(self, parent="", search="", start=0):
        query = {"userId": self.user_id, "startIndex": max(0, int(start)), "limit": 100,
                 "sortBy": "SortName", "sortOrder": "Ascending", "enableUserData": "true",
                 "fields": "Overview,Genres,ChildCount,RecursiveItemCount,ParentId,Width,Height"}
        if parent:
            query["parentId"] = item_id(parent)
        if search:
            query.update(searchTerm=search, recursive="true", includeItemTypes="Movie,Episode,Video")
        try:
            return self.json("/Items", query)
        except ClientError as error:
            if error.status != 404:
                raise
            return self.json(f"/Users/{self.user_id}/Items", query)

    def home(self):
        """Real Jellyfin home rows, following the pinned MiSTer API reference."""
        libraries = self.libraries()
        rows = [{"title": "My Media", "items": libraries, "shape": "thumb"}]
        query = {"userId": self.user_id, "limit": 12, "mediaTypes": "Video",
                 "fields": "Overview,Genres,ParentId", "enableUserData": "true"}
        continuing = []
        seen = set()
        for path in ("/UserItems/Resume", "/Shows/NextUp"):
            try:
                result = self.json(path, query)
            except ClientError as error:
                if error.status != 404:
                    raise
                continue
            for item in result.get("Items", []):
                if item.get("Id") and item["Id"] not in seen:
                    continuing.append(item)
                    seen.add(item["Id"])
        if continuing:
            rows.append({"title": "Continue Watching", "items": continuing[:12], "shape": "thumb"})
        for library in libraries:
            recent_query = dict(query, parentId=item_id(library.get("Id", "")))
            try:
                result = self.json("/Items/Latest", recent_query)
            except ClientError as error:
                if error.status != 404:
                    raise
                result = self.json(f"/Users/{self.user_id}/Items/Latest", recent_query)
            items = result if isinstance(result, list) else result.get("Items", [])
            if items:
                rows.append({"title": "Latest in " + library.get("Name", "Library"), "items": items,
                             "shape": "poster"})
        return rows

    def item(self, identifier):
        return self.json(f"/Users/{self.user_id}/Items/{item_id(identifier)}")

    def open_video(self, identifier, byte_range=None):
        headers = {"Accept": "*/*"}
        if byte_range:
            if not re.fullmatch(r"bytes=\d*-\d*", byte_range):
                raise ClientError("Invalid media range.")
            headers["Range"] = byte_range
        return self._open(f"/Videos/{item_id(identifier)}/stream", {"static": "true"}, headers=headers, timeout=60)

    def artwork(self, item, kind="poster"):
        """Fetch bounded image bytes from this server, with header-only authentication.

        Image/parent selection uses the fallback order Jellyfin clients commonly use (own art,
        then series or parent art); the response never supplies a URL to another process.
        kind picks what Jellyfin itself would show: wide art for thumbs and heroes.
        """
        tags = item.get("ImageTags") or {}
        candidates = []
        wide = []
        if tags.get("Thumb"):
            wide.append((item.get("Id"), "Thumb", tags["Thumb"]))
        if item.get("Type") == "Episode" and tags.get("Primary"):
            wide.append((item.get("Id"), "Primary", tags["Primary"]))  # episode still
        for key, id_key in (("BackdropImageTags", "Id"), ("ParentBackdropImageTags", "ParentBackdropItemId")):
            if item.get(key):
                wide.append((item.get(id_key), "Backdrop", item[key][0]))
        if kind == "backdrop":
            wide.sort(key=lambda entry: entry[1] != "Backdrop")
        if kind in ("thumb", "backdrop"):
            candidates.extend(wide)
        if item.get("Type") == "Episode" and item.get("SeriesPrimaryImageTag"):
            candidates.append((item.get("SeriesId"), "Primary", item["SeriesPrimaryImageTag"]))
        if tags.get("Primary"):
            candidates.append((item.get("Id"), "Primary", tags["Primary"]))
        if item.get("ParentPrimaryImageTag"):
            candidates.append((item.get("ParentPrimaryImageItemId"), "Primary", item["ParentPrimaryImageTag"]))
        if item.get("SeriesPrimaryImageTag"):
            candidates.append((item.get("SeriesId"), "Primary", item["SeriesPrimaryImageTag"]))
        if tags.get("Thumb"):
            candidates.append((item.get("Id"), "Thumb", tags["Thumb"]))
        for key, id_key in (("BackdropImageTags", "Id"), ("ParentBackdropImageTags", "ParentBackdropItemId")):
            if item.get(key):
                candidates.append((item.get(id_key), "Backdrop", item[key][0]))
        # Some library views omit image tags even when a primary image exists.
        candidates.append((item.get("Id"), "Primary", ""))
        seen = set()
        for identifier, kind, tag in candidates:
            if not identifier or (identifier, kind) in seen:
                continue
            seen.add((identifier, kind))
            query = {"maxWidth": 640 if kind == "backdrop" else 352, "maxHeight": 360,
                     "format": "Png", "quality": 90}
            if tag:
                query["tag"] = str(tag)[:256]
            try:
                with self._open(f"/Items/{item_id(identifier)}/Images/{kind}", query,
                                headers={"Accept": "image/png"}, timeout=8) as response:
                    if not response.headers.get("Content-Type", "").lower().startswith("image/"):
                        raise ClientError("Jellyfin returned invalid artwork.")
                    data = response.read(2 * 1024 * 1024 + 1)
                    if not data or len(data) > 2 * 1024 * 1024:
                        raise ClientError("Jellyfin artwork exceeds the local image limit.")
                    return data
            except ClientError as error:
                if error.status != 404:
                    raise
        return None


@contextmanager
def media_relay(client: JellyfinClient, identifier: str):
    """Give FFmpeg a one-use loopback URL; Jellyfin details stay in this process."""
    route = "/" + secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path != route or self.client_address[0] != "127.0.0.1":
                self.send_error(404)
                return
            try:
                with client.open_video(identifier, self.headers.get("Range")) as upstream:
                    self.send_response(upstream.status)
                    for key in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                        if upstream.headers.get(key):
                            self.send_header(key, upstream.headers[key])
                    self.end_headers()
                    while chunk := upstream.read(128 * 1024):
                        self.wfile.write(chunk)
            except (ClientError, OSError):
                # Disconnected player/exporter is normal. Never log upstream details.
                self.close_connection = True

    class QuietServer(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            pass

    server = QuietServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}{route}"
    finally:
        server.shutdown()
        server.server_close()

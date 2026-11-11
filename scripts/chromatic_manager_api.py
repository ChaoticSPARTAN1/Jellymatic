# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Chromatic Manager local API: the tools a model (Claude, Codex) or another local program
may use, served over HTTP on 127.0.0.1 by the open window and over stdio by the MCP
server (`chromatic_manager_mcp.py`). No Qt here.

Read tools run freely, and so do the two window tools (show or hide the log panel, read the
log). The read-only status (it stops a game and resets the console) and every write are
only *requests*: each waits until the owner presses Approve in the Chromatic Manager window,
or, if the owner allowed AI control in that window's pop-up, starts after the project's
hardware usage check, with every safety check. Refused when no window is open. No argument,
header, token or environment variable grants that: only the owner's click in the window does.

The HTTP endpoint listens on 127.0.0.1 only, on a random port, and needs the random
per-run token from the discovery file (private/chromatic-manager-gui/api.json, Git-
ignored). It refuses requests carrying a browser Origin or a foreign Host header. The
token only proves the caller runs on this PC as this user; it grants nothing more.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from user_paths import PRIVATE  # noqa: E402  (per-user folder, see user_paths.py)
import secrets
import threading
import urllib.error
import urllib.request

from chromatic_manager_core import BY_KEY, PROGRAM_WRITE_KEYS, ROOT, api_requester, requester_name

DISCOVERY = PRIVATE / 'chromatic-manager-gui/api.json'
API_VERSION = 1

_WRITE_TITLES = '; '.join(f'{key}: {BY_KEY[key].title}' for key in PROGRAM_WRITE_KEYS)  # never the guide-only jobs

TOOLS = (
    {'name': 'chromatic_get_state',
     'description': 'Chromatic Manager at a glance: whether a job runs and who asked for it, the last status '
                    'summary (state, what it boots, stock and launcher versions), whether an unfinished job waits, '
                    'the current write request and the last result. Reads nothing from the console.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
     'annotations': {'readOnlyHint': True, 'openWorldHint': False}},
    {'name': 'chromatic_get_progress',
     'description': 'The running job: device step, percent (null while it cannot be counted), elapsed and '
                    'estimated time left, console mode (download mode means its screen is frozen: expected), '
                    'any question waiting for the owner, and the last log lines (redacted).',
     'inputSchema': {'type': 'object', 'properties': {
         'lines': {'type': 'integer', 'minimum': 0, 'maximum': 400, 'default': 40,
                   'description': 'How many log lines to return.'}}, 'additionalProperties': False},
     'annotations': {'readOnlyHint': True, 'openWorldHint': False}},
    {'name': 'chromatic_get_last_result',
     'description': 'The last finished job: action, exit code, headline, what the console does next, who asked, '
                    'who confirmed, timings, its last messages (redacted) and its log file path.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
     'annotations': {'readOnlyHint': True, 'openWorldHint': False}},
    {'name': 'chromatic_list_backups',
     'description': 'Backup folders (both copies) by path and file name only. Contents, hashes and device '
                    'identities are never returned.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
     'annotations': {'readOnlyHint': True, 'openWorldHint': False}},
    {'name': 'chromatic_get_update_guide',
     'description': 'The guided official ModRetro update: its four steps, whether one is running, its current '
                    'step and any question waiting for the owner. The tool never starts ModRetro\'s updater, and '
                    'only the person at the window answers its questions.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
     'annotations': {'readOnlyHint': True, 'openWorldHint': False}},
    {'name': 'chromatic_set_log_panel',
     'description': 'Show (open=true) or hide (open=false) the log panel in the Chromatic Manager window, to '
                    'show the owner what matters. Changes nothing on the console. Refused when no window is open.',
     'inputSchema': {'type': 'object', 'properties': {
         'open': {'type': 'boolean', 'description': 'true shows the log panel, false hides it.'}},
         'required': ['open'], 'additionalProperties': False},
     'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'openWorldHint': False}},
    {'name': 'chromatic_read_log',
     'description': "The window's log (timestamped, newest last), with identifiers, hashes and Wi-Fi details "
                    'withheld, and whether the log panel is open.',
     'inputSchema': {'type': 'object', 'properties': {
         'lines': {'type': 'integer', 'minimum': 0, 'maximum': 400, 'default': 100,
                   'description': 'How many lines to return.'}}, 'additionalProperties': False},
     'annotations': {'readOnlyHint': True, 'openWorldHint': False}},
    {'name': 'chromatic_start_status',
     'description': 'ASK the owner to run the read-only status. It writes nothing, but it stops any game in '
                    'progress, holds the console in download mode (screen frozen) for about 20 seconds (about 7 '
                    'minutes when its stock firmware is new to this tool and is read in full) and ends '
                    'on the start-up menu, so it runs only after the owner presses Approve in the '
                    'Chromatic Manager window, or at once if the owner has allowed AI control there (the '
                    'first request of a run may show the owner that question). Refused when no window is open. '
                    'Then poll chromatic_get_state (request state), chromatic_get_progress and '
                    'chromatic_get_last_result.',
     'inputSchema': {'type': 'object', 'properties': {
         'reason': {'type': 'string', 'maxLength': 300, 'description': 'Shown to the owner on the prompt.'}},
         'additionalProperties': False},
     'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'openWorldHint': False}},
    {'name': 'chromatic_request_action',
     'description': 'ASK the owner for a write. Nothing is written by this call: the Chromatic Manager window '
                    'shows the owner a prompt naming you, and the job runs only if they press Approve '
                    'there. If the owner has allowed AI control in the window (chromatic_get_state, ai_control), '
                    "it starts without that prompt once the project's hardware usage check passes (a fresh "
                    'usage snapshot is needed); every safety check still runs and the owner can press Stop. '
                    'Refused when no window is open, while a job runs, or while another request waits. '
                    "Poll chromatic_get_state for the request's state (waiting, declined, running, finished, "
                    f'withdrawn). Actions: {_WRITE_TITLES}.',
     'inputSchema': {'type': 'object', 'properties': {
         'action': {'type': 'string', 'enum': list(PROGRAM_WRITE_KEYS)},
         'reason': {'type': 'string', 'maxLength': 300, 'description': 'Shown to the owner on the prompt.'}},
         'required': ['action'], 'additionalProperties': False},
     'annotations': {'readOnlyHint': False, 'destructiveHint': True, 'openWorldHint': False}},
    {'name': 'chromatic_update_pocket_link',
     'description': 'ASK the owner for the one-step Pocket link update (console chip image 18.41, then the start-up '
                    'menu when the console has an older one). Nothing is loaded or written by this call: the '
                    'Chromatic Manager window opens Update the Pocket link and shows the owner the Approve prompt '
                    'naming you. It starts ONLY when the owner presses Approve there (that starts the try from the '
                    'chip\'s RAM and writes nothing); allowed AI control never starts it and never answers for the '
                    'owner. The job tries the reviewed image from RAM and checks it by itself; then, with the new '
                    'image running, the window asks the owner once more (start a game, look, Approve or Not right). '
                    'Only after that second Approve is it written, with a full read-back, and the console checked '
                    'reporting it from its flash; a failed check or Not right writes nothing. A start-up menu '
                    'install that follows has its own Approve. Refused when no window is open, '
                    'while a job runs, while another request waits, or while the owner is in another guided setup. '
                    'Poll chromatic_get_state (request state; guided_setup is the screen the owner sees, '
                    'starts_next the job the window starts next) and chromatic_get_progress (owner_action_needed: '
                    'what the owner must physically do now, with the seconds allowed).',
     'inputSchema': {'type': 'object', 'properties': {
         'reason': {'type': 'string', 'maxLength': 300, 'description': 'Shown to the owner on the prompt.'}},
         'additionalProperties': False},
     'annotations': {'readOnlyHint': False, 'destructiveHint': True, 'openWorldHint': False}},
    {'name': 'chromatic_cancel_request',
     'description': 'Withdraw your write request while it still waits for the owner.',
     'inputSchema': {'type': 'object', 'properties': {'request_id': {'type': 'string'}},
                     'required': ['request_id'], 'additionalProperties': False},
     'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'openWorldHint': False}},
)
TOOL_NAMES = tuple(tool['name'] for tool in TOOLS)


class ToolError(Exception):
    """A call the API will not run (unknown tool or bad arguments)."""


def _check_arguments(tool, arguments):
    schema = tool['inputSchema']
    if not isinstance(arguments, dict):
        raise ToolError('arguments must be an object')
    unknown = set(arguments) - set(schema['properties'])
    if unknown:
        raise ToolError(f'unknown argument(s): {", ".join(sorted(unknown))}')  # no hidden switches
    for name in schema.get('required', ()):
        if name not in arguments:
            raise ToolError(f'missing argument: {name}')
    for name, value in arguments.items():
        spec = schema['properties'][name]
        if spec['type'] == 'string' and not isinstance(value, str):
            raise ToolError(f'{name} must be a string')
        if spec['type'] == 'integer' and (not isinstance(value, int) or isinstance(value, bool)):
            raise ToolError(f'{name} must be an integer')
        if spec['type'] == 'boolean' and not isinstance(value, bool):
            raise ToolError(f'{name} must be true or false')
        if 'enum' in spec and value not in spec['enum']:
            raise ToolError(f'{name} must be one of: {", ".join(spec["enum"])}')


def call_tool(service, name, arguments, requested_by):
    """Run one tool on a ManagerService. The only service methods reachable from here are
    readers, the log panel, start_status, request_action, request_link_update and
    cancel_request."""
    tool = next((t for t in TOOLS if t['name'] == name), None)
    if tool is None:
        raise ToolError(f'unknown tool: {name}')
    arguments = arguments or {}
    _check_arguments(tool, arguments)
    who = api_requester(requested_by)
    if name == 'chromatic_get_state':
        return service.state()
    if name == 'chromatic_get_progress':
        return service.progress_view(arguments.get('lines', 40))
    if name == 'chromatic_get_last_result':
        return service.result()
    if name == 'chromatic_list_backups':
        return service.backups()
    if name == 'chromatic_get_update_guide':
        return service.update_guide()
    if name == 'chromatic_set_log_panel':
        return service.set_log_panel(arguments['open'])
    if name == 'chromatic_read_log':
        return service.read_log(arguments.get('lines', 100))
    if name == 'chromatic_start_status':
        return service.start_status(who, arguments.get('reason', ''))
    if name == 'chromatic_request_action':
        return service.request_action(arguments['action'], who, arguments.get('reason', ''))
    if name == 'chromatic_update_pocket_link':
        return service.request_link_update(who, arguments.get('reason', ''))
    if name == 'chromatic_cancel_request':
        return service.cancel_request(arguments['request_id'], who)
    raise ToolError(f'unknown tool: {name}')


# ---- HTTP on 127.0.0.1 -------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server_version = 'ChromaticManagerAPI/1'

    def log_message(self, *args):  # nothing on stderr; the service keeps its own logs
        pass

    def _send(self, status, body):
        data = json.dumps(body).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _allowed(self):
        api = self.server.api
        if self.headers.get('Origin'):  # a web page: never
            self._send(403, {'error': 'browser requests are refused'})
            return False
        if self.headers.get('Host') not in (f'127.0.0.1:{api.port}', f'localhost:{api.port}'):
            self._send(403, {'error': 'wrong host'})
            return False
        given = self.headers.get('Authorization', '')
        if not secrets.compare_digest(given.encode('utf-8'), f'Bearer {api.token}'.encode('utf-8')):
            self._send(401, {'error': 'token required (see the discovery file)'})
            return False
        return True

    def do_GET(self):
        if not self._allowed():
            return
        if self.path == '/v1/health':
            self._send(200, {'app': 'chromatic-manager', 'api': API_VERSION, 'host': self.server.api.service.host,
                             'pid': os.getpid()})
        elif self.path == '/v1/tools':
            self._send(200, {'tools': list(TOOLS)})
        else:
            self._send(404, {'error': 'not found'})

    def do_POST(self):
        if not self._allowed():
            return
        if self.path != '/v1/call':
            self._send(404, {'error': 'not found'})
            return
        try:
            length = int(self.headers.get('Content-Length') or 0)
            if length > 65536:
                raise ToolError('request too large')
            if length < 0:
                raise ToolError('bad Content-Length')  # never rfile.read(-1), which reads until the client closes
            body = json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(body, dict):
                raise ToolError('body must be an object')
            result = call_tool(self.server.api.service, body.get('tool'), body.get('arguments') or {},
                               self.headers.get('X-Requested-By') or 'a local program')
            self._send(200, {'result': result})
        except (ToolError, ValueError) as error:
            self._send(400, {'error': str(error)})
        except Exception as error:  # never take the window down
            self._send(500, {'error': f'internal error ({type(error).__name__})'})


class LocalApiServer:
    """The window's API: 127.0.0.1, random port, random token, discovery file while open."""

    def __init__(self, service, discovery=DISCOVERY):
        self.service, self.discovery = service, Path(discovery)
        self.token = secrets.token_urlsafe(32)
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.api = self
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name='chromatic-api', daemon=True)

    def start(self):
        self.thread.start()
        self.discovery.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.discovery.with_suffix('.tmp')
        temporary.write_text(json.dumps({'api': API_VERSION, 'url': f'http://127.0.0.1:{self.port}',
                                         'token': self.token, 'pid': os.getpid(), 'host': self.service.host}),
                             encoding='utf-8')
        os.replace(temporary, self.discovery)
        return self

    def stop(self):
        try:
            current = json.loads(self.discovery.read_text(encoding='utf-8'))
            if current.get('token') == self.token:
                self.discovery.unlink()
        except (OSError, ValueError):
            pass
        self.httpd.shutdown()
        self.httpd.server_close()


class RemoteApp:
    """A client for the open window's API (used by the MCP server to attach)."""

    def __init__(self, url, token, timeout=10):
        self.url, self.token, self.timeout = url.rstrip('/'), token, timeout

    @classmethod
    def find(cls, discovery=DISCOVERY):
        """The open window, or None. Never raises."""
        try:
            info = json.loads(Path(discovery).read_text(encoding='utf-8'))
            if not str(info['url']).startswith('http://127.0.0.1:'):
                return None
            app = cls(info['url'], info['token'], timeout=3)
            return app if app.health().get('app') == 'chromatic-manager' else None
        except Exception:
            return None

    def _request(self, method, path, body=None, requested_by=None):
        data = None if body is None else json.dumps(body).encode('utf-8')
        request = urllib.request.Request(self.url + path, data=data, method=method)
        request.add_header('Authorization', f'Bearer {self.token}')
        request.add_header('Content-Type', 'application/json')
        if requested_by:
            request.add_header('X-Requested-By', requester_name(requested_by))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never through a proxy
        try:
            with opener.open(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                message = json.loads(error.read()).get('error', str(error))
            except Exception:
                message = str(error)
            raise ToolError(message) from None

    def health(self):
        return self._request('GET', '/v1/health')

    def call(self, name, arguments, requested_by):
        return self._request('POST', '/v1/call', {'tool': name, 'arguments': arguments}, requested_by)['result']

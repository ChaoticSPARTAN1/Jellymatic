# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Chromatic Manager as an MCP server (stdio), for Claude Code, Codex and other MCP clients.

    ChromaticManagerMCP.exe                (portable build)
    <venv python> scripts/chromatic_manager_mcp.py   (from source; private/chromatic-tools venv)

Each tool call goes to the open Chromatic Manager window when there is one (found
through private/chromatic-manager-gui/api.json), so the owner sees everything a model
does and approves every write there. With no window open, this process runs the same
core headless, under the same device lock: the read tools work, and the read-only status
and every write request are refused until the owner opens the window and approves them
there (the status stops a game and resets the console, so it is approved like a write).

Tools and their limits are in chromatic_manager_api.TOOLS. Nothing returned carries
device identifiers, MACs, flash contents, Wi-Fi details or Jellyfin settings.
Protocol: JSON-RPC 2.0, one message per line on stdin/stdout (MCP stdio transport).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading

SERVER_VERSION = '1.0.0'
PROTOCOLS = ('2025-06-18', '2025-03-26', '2024-11-05')
INSTRUCTIONS = (
    'Chromatic Manager manages the start-up menu on the owner\'s ModRetro Chromatic (ESP32 flash). '
    'Read tools are free. chromatic_start_status asks for the read-only status (about 20 seconds, or about 7 '
    'minutes when the console\'s stock firmware is new to this tool; it stops a game and the console screen '
    'freezes while it runs) and chromatic_request_action asks for a write: both run only '
    'after the owner presses Approve in the Chromatic Manager window (a program cannot), or, when the owner '
    'has allowed AI control in that window, after the hardware usage check (fresh usage snapshot needed); every '
    'safety check still runs and the owner can press Stop. Refused when no window is open. '
    'chromatic_update_pocket_link asks for the one-step Pocket link update: it starts only on the owner\'s Approve '
    'click in the window (allowed AI control never starts it), and writes only after a second Approve the owner '
    'gives with the new image running. '
    'chromatic_set_log_panel shows or hides the window\'s log; chromatic_read_log reads it (redacted). '
    'Never ask for a write unless the owner is present and wants it. Do not poll faster than every few seconds.')


class Backend:
    """The open window if there is one, else this process's own headless core."""

    def __init__(self, discovery=None, make_service=None):
        import chromatic_manager_api as api
        self.api = api
        self.discovery = discovery or api.DISCOVERY
        self.make_service = make_service
        self.headless = None
        self._lock = threading.Lock()

    def _service(self):
        with self._lock:
            if self.headless is None:
                if self.make_service is not None:
                    self.headless = self.make_service()
                else:
                    import chromatic_manager as cm
                    from chromatic_manager_core import ManagerService
                    self.headless = ManagerService(cm, host='headless (MCP server, no window)')
            return self.headless

    def call(self, name, arguments, requested_by):
        # A job this process started keeps being served here until it ends.
        if self.headless is None or not self.headless.busy():
            app = self.api.RemoteApp.find(self.discovery)
            if app is not None:
                return app.call(name, arguments, requested_by)
        return self.api.call_tool(self._service(), name, arguments, requested_by)

    def wait_idle(self):
        if self.headless is not None:
            self.headless.wait()


class McpServer:
    def __init__(self, backend, out):
        self.backend, self.out = backend, out
        self.client = 'an MCP client'

    def send(self, message):
        self.out.write((json.dumps(message, separators=(',', ':')) + '\n').encode('utf-8'))
        self.out.flush()

    def error(self, ident, code, message):
        self.send({'jsonrpc': '2.0', 'id': ident, 'error': {'code': code, 'message': message}})

    def handle(self, message):
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
            self.error(None, -32600, 'Invalid Request')
            return
        method, ident = message.get('method'), message.get('id')
        params = message.get('params') or {}
        if ident is None:  # a notification (initialized, cancelled, ...): nothing to answer
            return
        if method == 'initialize':
            info = params.get('clientInfo') or {}
            from chromatic_manager_core import requester_name
            self.client = requester_name(f'{info.get("name") or "an MCP client"} via MCP')
            asked = params.get('protocolVersion')
            self.send({'jsonrpc': '2.0', 'id': ident, 'result': {
                'protocolVersion': asked if asked in PROTOCOLS else PROTOCOLS[0],
                'capabilities': {'tools': {'listChanged': False}},
                'serverInfo': {'name': 'chromatic-manager', 'title': 'Chromatic Manager', 'version': SERVER_VERSION},
                'instructions': INSTRUCTIONS}})
        elif method == 'ping':
            self.send({'jsonrpc': '2.0', 'id': ident, 'result': {}})
        elif method == 'tools/list':
            self.send({'jsonrpc': '2.0', 'id': ident, 'result': {'tools': list(self.backend.api.TOOLS)}})
        elif method == 'tools/call':
            name, arguments = params.get('name'), params.get('arguments') or {}
            try:
                result = self.backend.call(name, arguments, self.client)
                is_error = False
            except self.backend.api.ToolError as error:
                result, is_error = {'error': str(error)}, True
            except Exception as error:
                result, is_error = {'error': f'internal error ({type(error).__name__})'}, True
            self.send({'jsonrpc': '2.0', 'id': ident, 'result': {
                'content': [{'type': 'text', 'text': json.dumps(result, indent=1)}],
                'structuredContent': result, 'isError': is_error}})
        else:
            self.error(ident, -32601, f'Method not found: {method}')

    def serve(self, stdin):
        for raw in stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                self.error(None, -32700, 'Parse error')
                continue
            for item in message if isinstance(message, list) else [message]:
                self.handle(item)
        self.backend.wait_idle()  # never leave a running status behind with the console in download mode


def main(argv=None, root=None):
    parser = argparse.ArgumentParser(description='Chromatic Manager MCP server (stdio).')
    parser.add_argument('--mcp', action='store_true', help='(accepted for the portable build; always on here)')
    parser.add_argument('--discovery', type=Path, help='Where the open window publishes its local API.')
    args = parser.parse_args(argv)
    sys.dont_write_bytecode = True
    out = sys.stdout.buffer  # the protocol channel; everything else goes to stderr
    sys.stdout = sys.stderr  # esptool prints progress; it must never reach the protocol
    if root is not None:
        sys.path.insert(0, str(Path(root) / 'scripts'))
    McpServer(Backend(args.discovery), out).serve(sys.stdin.buffer)
    return 0


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main(sys.argv[1:]))

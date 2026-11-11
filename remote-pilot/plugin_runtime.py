# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""Local, versioned transport adapter loader for additional game presets."""

import importlib.util
import json
from pathlib import Path
import sys
import threading
import performance_capture


class PluginRuntime:
    """Poll status off the HTTP thread; dispatch bounded requests on workers."""

    def __init__(self, manifest, preset_root, local_root, network_host=None, active=None,
                 on_event=None):
        self.preset_id = manifest["id"]
        self.capabilities = frozenset(manifest.get("capabilities", []))
        self.adapter_local_dir = Path(local_root) / self.preset_id
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._status = self._offline("Adapter has not reported status")
        self.adapter = None
        path = Path(preset_root) / self.preset_id / "adapter.py"
        try:
            module_name = f"remote_pilot_adapter_{self.preset_id.replace('-', '_')}"
            spec = importlib.util.spec_from_file_location(
                module_name, path, submodule_search_locations=[str(path.parent)])
            if spec is None or spec.loader is None:
                raise ValueError("Adapter module could not be loaded")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(module_name, None)
                raise
            factory = getattr(module, "create_adapter", None)
            if not callable(factory):
                raise ValueError("Adapter needs create_adapter(local_dir, manifest)")
            local_dir = Path(local_root) / self.preset_id
            local_dir.mkdir(parents=True, exist_ok=True)
            adapter = factory(local_dir, dict(manifest))
            if not callable(getattr(adapter, "status", None)) or not callable(getattr(adapter, "request", None)):
                raise ValueError("Adapter needs status() and request(kind, payload)")
            set_network_host = getattr(adapter, "set_network_host", None)
            if network_host and callable(set_network_host):
                set_network_host(network_host)
            set_active = getattr(adapter, "set_active", None)
            if active and callable(set_active):
                set_active(active)
            set_event_sink = getattr(adapter, "set_event_sink", None)
            if on_event and callable(set_event_sink):
                set_event_sink(lambda kind, sequence: on_event(self.preset_id, kind, sequence))
            self.adapter = adapter
        except Exception as exc:
            self._status = self._offline(f"Adapter unavailable: {type(exc).__name__}: {exc}")

    @staticmethod
    def _offline(error):
        return {"connected": False, "session": False, "host": "", "discovery": str(error)[:160],
                "meta": None, "perf": None}

    @staticmethod
    def _validate_status(value):
        if not isinstance(value, dict) or type(value.get("connected")) is not bool \
                or type(value.get("session")) is not bool:
            raise ValueError("Adapter status needs boolean connected and session fields")
        host = value.get("host", "")
        discovery = value.get("discovery", "")
        meta = value.get("meta")
        perf = value.get("perf")
        if not isinstance(host, str) or not isinstance(discovery, str) \
                or meta is not None and not isinstance(meta, dict) \
                or perf is not None and not isinstance(perf, dict):
            raise ValueError("Adapter status has invalid host, discovery, metadata, or performance")
        if len(json.dumps({"meta": meta, "perf": perf})) > 8192:
            raise ValueError("Adapter metadata or performance exceeds status limit")
        return {"connected": value["connected"], "session": value["session"],
                "host": host[:64], "discovery": discovery[:160], "meta": meta, "perf": perf}

    def status(self):
        with self._lock:
            value = dict(self._status)
            if not performance_capture.enabled(self.adapter_local_dir):
                value["perf"] = None
            return value

    def run_forever(self):
        if self.adapter is None:
            return
        start = getattr(self.adapter, "start", None)
        if callable(start):
            try:
                start()
            except Exception as exc:
                with self._lock:
                    self._status = self._offline(f"Adapter start failed: {type(exc).__name__}: {exc}")
                return
        while not self._stop.is_set():
            try:
                value = self._validate_status(self.adapter.status())
            except Exception as exc:
                value = self._offline(f"Adapter status failed: {type(exc).__name__}: {exc}")
            with self._lock:
                self._status = value
            self._stop.wait(1.0)

    def request(self, kind, payload):
        if self.adapter is None:
            raise ValueError(self.status()["discovery"])
        if kind not in ("console", "input", "gamepad", "touch", "shot", "stream", "report", "log", "perf"):
            raise ValueError("Unsupported adapter action")
        return self.adapter.request(kind, payload)

    def retry(self):
        action = getattr(self.adapter, "retry", None)
        if not callable(action):
            raise ValueError("This adapter does not support a fresh connection search")
        action()

    def set_network_host(self, host):
        if self.adapter is None:
            return
        action = getattr(self.adapter, "set_network_host", None)
        if callable(action):
            action(host)

    def stop(self):
        self._stop.set()
        close = getattr(self.adapter, "stop", None)
        if callable(close):
            close()

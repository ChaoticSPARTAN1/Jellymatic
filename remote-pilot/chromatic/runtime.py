# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""The Chromatic preset adapter: transports, local config and honest availability.

Loaded by presets/chromatic/adapter.py through Remote Pilot's python-v1 contract.
The shared game actions (shot, stream, input, ...) are refused: the shared viewer
expects dual-screen-sized images and its own button set. Chromatic actions go through
chromatic_request(), reached from /api/chromatic/* (see api.py).

Modes:
  off          nothing is opened
  lan          primary: a paired console speaking CHR1 on the private LAN; the
               address is configured or found by scanning this PC's /24 ("auto").
               Needs local/chromatic/pairing.json and launcher 1.1.2 or newer.
  serial       read-only USB-serial fallback; config file only, and only with
               "serialOpenAllowed": true
  lan-sim      a simulated launcher (what launcher work alone can deliver)
  lan-sim-fpga a simulated future device with an FPGA game bridge (game screen,
               video, sound and buttons). No such FPGA image exists.
  serial-sim   the read-only serial reader over a simulated serial device
  usb          CHR1 over the console's USB serial port (launcher 1.1.2+); config
               file only, and only with "serialOpenAllowed": true
  auto         USB when the port is present and allowed, otherwise Wi-Fi (lan)
  usb-sim      CHR1 over a simulated USB cable to a console with no key yet,
               so USB pairing can be tried (the simulator presses A itself)
  auto-sim     a simulated console on both USB and Wi-Fi; USB is preferred
Default: auto when the config allows the serial port, else lan when a pairing
key exists, otherwise off.

Connect (the shared "Connect to device" button, python-v1 connect()) re-reads the
config and switches to the configured transport. With nothing configured it
lists the serial ports with the console's USB vendor ID (0x374E), never opening
one, and offers "Allow USB on COMx": only that click writes local/chromatic/
config.json with "serialOpenAllowed": true, and only then is the port opened.

USB hand-off (usb_hold.py): while Chromatic Manager holds the console's USB port
(local/chromatic/usb-hold.json, written before its job opens the port), no
transport opens it: every port factory checks that file first. usb-release
closes the port at once; usb-return reopens it. Wi-Fi keeps working throughout.
"""

import json
import re
import threading
import time
from collections import deque
from pathlib import Path

from . import manager_status, protocol, usb_hold
from .auto_transport import AutoTransport
from .lan_transport import LanTransport, discover
from .manager_runner import COMMANDS as MANAGER_COMMANDS, ManagerRunner, required_phrase
from .screen import frame_to_bmp, frame_to_png
from .serial_transport import PySerialLineSource, SerialTransport
from .simulator import SimulatedLauncher, SimulatedSerialDevice
from .usb_sim import SimulatedUsbConsole
from .usb_transport import (CHROMATIC_USB_VID, NO_PYSERIAL, PySerialPort, SerialSupportMissing, UsbTransport,
                            find_consoles, port_present)

MODES = ("off", "lan", "serial", "usb", "auto", "lan-sim", "lan-sim-fpga", "serial-sim", "usb-sim", "auto-sim")
CONFIG_MODES = ("off", "lan", "serial", "usb", "auto")
UI_MODES = ("off", "lan-sim", "lan-sim-fpga", "serial-sim", "usb-sim", "auto-sim", "configured")
CONFIG_FIELDS = {"mode": str, "serialPort": str, "serialOpenAllowed": bool, "lanHost": str,
                 "lanPort": int, "managerProject": str, "managerPython": str, "managerRunAllowed": bool}
# Actions that change the connection, the pairing key or show Manager output: this PC's own browser only.
LOCAL_ONLY_ACTIONS = ("mode", "pair-capture", "pair-usb", "manager-job", "manager-run")
LOCAL_ONLY_COLLECT = ("manager-files", "manager-job")
LOCAL_ONLY_TEXT = "This works only from this PC's own browser, not from a phone or tablet on the network"
MIN_PRESS_GAP_S = 0.15
MIN_HOLD_GAP_S = 0.08
LEASE_S = 1.5
RESCAN_S = 30.0
FPGA_NOTE = ("Needs FPGA update: during games the unmodified stock FPGA owns the picture, sound and buttons. "
             "This lights up when the device offers game access (Gowin tools not installed yet).")
LAUNCHER_NOTE = ("Needs launcher 1.1.2 or newer, which speaks CHR1 over USB serial and Wi-Fi. Install or "
                 "update it through Chromatic Manager with the console in front of you.")
NO_PYSERIAL_HINT = (NO_PYSERIAL + ". Start Remote Pilot with Start-RemotePilot.cmd (it uses local\\venv) "
                    "or run: pip install pyserial")
USB_ID = f"USB vendor ID {CHROMATIC_USB_VID:04X}"

COLLECT_ITEMS = ("device-log", "status", "console", "manager-files", "manager-job")


def load_config(local_dir):
    local_dir = Path(local_dir)
    path = local_dir / "config.json"
    default = "lan" if (local_dir / "pairing.json").is_file() else "off"
    if not path.is_file():
        return {"mode": default, "lanHost": "auto"}
    if path.stat().st_size > 4096:
        raise ValueError("local/chromatic/config.json is too large")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or any(key not in CONFIG_FIELDS or type(value) is not CONFIG_FIELDS[key]
                                         for key, value in data.items()):
        raise ValueError("local/chromatic/config.json has an unknown field or a wrong type")
    if data.get("serialOpenAllowed") is True and data.get("serialPort"):
        default = "auto"  # the user allowed the port: USB first, Wi-Fi otherwise
    if data.get("mode", default) not in CONFIG_MODES:
        raise ValueError("local/chromatic/config.json mode must be " + ", ".join(CONFIG_MODES))
    return {"mode": default, "lanHost": "auto", **data}


def save_config(local_dir, updates):
    """Writes local/chromatic/config.json (git-ignored): the valid fields already there,
    then the updates. Called only from the user's own click (allow-usb)."""
    local_dir = Path(local_dir)
    path = local_dir / "config.json"
    current = {}
    try:
        if path.is_file() and path.stat().st_size <= 4096:
            current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    data = {key: value for key, value in {**current, **updates}.items()
            if key in CONFIG_FIELDS and type(value) is CONFIG_FIELDS[key]}
    if data.get("mode", "off") not in CONFIG_MODES:
        raise ValueError("Config mode must be " + ", ".join(CONFIG_MODES))
    local_dir.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return data


def load_pairing_key(local_dir):
    path = Path(local_dir) / "pairing.json"
    if not path.is_file():
        raise ValueError("Not paired yet: local/chromatic/pairing.json is missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    return protocol.parse_key(data.get("key") if isinstance(data, dict) else None)


def backup_pairing(local_dir):
    """Copies an existing pairing.json to pairing-backup-<time>.json (never overwriting an older
    backup) before it is replaced. Returns the backup's name, or None when there was no key."""
    local_dir = Path(local_dir)
    path = local_dir / "pairing.json"
    if not path.is_file():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for number in range(1, 100):
        backup = local_dir / (f"pairing-backup-{stamp}.json" if number == 1 else f"pairing-backup-{stamp}-{number}.json")
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
            return backup.name
    raise ValueError("Too many pairing backups this second; try again")


def save_pairing_key(local_dir, key_hex, source=None, backup=False):
    """Stores the key. A USB pairing never silently replaces a key Chromatic Manager
    provisioned: that one is kept beside it as pairing-provisioned.json. backup=True
    (pairing capture) first copies the current key to pairing-backup-<time>.json."""
    protocol.parse_key(key_hex)
    path = Path(local_dir) / "pairing.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup:
        backup_pairing(path.parent)
    if source and path.is_file():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            old = {}
        if isinstance(old, dict) and old.get("key") and not old.get("source"):
            (path.parent / "pairing-provisioned.json").write_text(json.dumps(old) + "\n", encoding="utf-8")
    temporary = path.with_suffix(".tmp")
    body = {"key": key_hex, **({"source": source} if source else {})}
    temporary.write_text(json.dumps(body) + "\n", encoding="utf-8")
    temporary.replace(path)


def speaks_chr1(transport):
    return bool(getattr(transport, "speaks_chr1", False))


class ChromaticRuntime:
    def __init__(self, local_dir, manifest, serial_factory=None, discover_fn=None, popen=None,
                 usb_port_factory=None, usb_present=None, usb_list=None):
        if manifest.get("id") != "chromatic":
            raise ValueError("Chromatic adapter requires its own preset")
        self.local_dir = Path(local_dir)
        self.captures = self.local_dir.parent / "captures"
        self._serial_factory = serial_factory or PySerialLineSource
        self._discover = discover_fn or discover
        self._usb_port_factory, self._usb_present = usb_port_factory, usb_present
        self._usb_list = usb_list  # lists the console's ports (find_consoles); never opens one
        self._usb_offer = None
        self._lock = threading.Lock()
        self._press_lock = threading.Lock()
        self._last_press = self._last_hold = 0.0
        self._transport = None
        self._simulator = None
        self._mode = "off"
        self._config_error = ""
        self._manager = None
        self._network_host = None
        self._scan = {"state": "idle", "at": 0.0, "thread": None}
        self._lease = None
        self._last_files = {}
        self._counter = 0
        self._activity = deque(maxlen=60)
        self._perf_history = deque(maxlen=120)
        self._held_mode = None  # a serial-reader mode switched off for Chromatic Manager, restored on return
        runner_args = {"popen": popen} if popen else {}
        self._runner = ManagerRunner(self.local_dir / "manager-jobs", **runner_args)
        self._reload_config()

    def _reload_config(self):
        try:
            self._config, self._config_error = load_config(self.local_dir), ""
        except (OSError, ValueError) as exc:
            self._config, self._config_error = {"mode": "off"}, str(exc)[:160]
        self._manager = None  # managerProject may have changed

    # ---- lifecycle (python-v1) ---------------------------------------------------
    def start(self):
        try:
            self.set_mode(self._config.get("mode", "off"), from_config=True)
        except (OSError, ValueError) as exc:
            self._config_error = f"Configured transport unavailable: {exc}"[:160]
            self._switch(None, None, "off")

    def stop(self):
        self._switch(None, None, "off")

    def set_network_host(self, host):
        self._network_host = host

    def retry(self):
        """Shared 'Retry connection': rescan the LAN, and allow one USB CHR1 OPEN."""
        with self._lock:
            transport = self._transport
        if callable(getattr(transport, "retry", None)):
            transport.retry()
        if self._mode in ("lan", "auto") and self._config.get("lanHost", "auto") == "auto" and self._paired():
            self._start_scan(force=True)

    def connect(self):
        """The shared Connect button: the configured transport, re-read from disk. With
        nothing usable configured, the console's USB port is only listed and offered."""
        self._reload_config()
        offer = self._detect_usb()
        mode = self._config.get("mode", "off")
        if mode != "off":
            if self._mode == mode and self._transport is not None:
                self.retry()
            else:
                try:
                    self.set_mode(mode, from_config=True)
                except (OSError, ValueError) as exc:
                    self._config_error = f"Configured transport unavailable: {exc}"[:160]
                    self._switch(None, None, "off")
            if self._mode != "off":
                via = {"auto": "USB first, then Wi-Fi", "usb": "USB", "lan": "Wi-Fi",
                       "serial": "the read-only USB serial reader"}.get(self._mode, self._mode)
                self._log_activity("connect", True, f"configured transport: {self._mode}")
                return {"state": "connecting", "mode": self._mode, "usbOffer": offer,
                        "message": f"Chromatic: connecting over {via}."}
        found = bool(offer) and offer.get("state") == "found"
        message = (offer or {}).get("message") or self._config_error or "Nothing to connect to."
        if self._config_error and found:
            message = f"{self._config_error}. {message}"
        self._log_activity("connect", found, message)
        return {"state": (offer or {}).get("state", "none"), "mode": self._mode, "usbOffer": offer,
                "message": message[:240]}

    def _list_consoles(self):
        return (self._usb_list or find_consoles)()

    def _detect_usb(self):
        """Lists the console's USB ports (never opens one) and keeps the offer for the panel.
        No offer is needed when USB is already allowed on a listed port."""
        try:
            ports = [entry["port"] for entry in self._list_consoles()]
        except SerialSupportMissing:
            offer = {"state": "no-pyserial", "ports": [], "message": NO_PYSERIAL_HINT}
        except OSError as exc:
            offer = {"state": "error", "ports": [], "message": f"Could not list serial ports: {type(exc).__name__}"}
        else:
            if self._usb_allowed() and self._config.get("serialPort") in ports:
                offer = None
            elif ports:
                offer = {"state": "found", "ports": ports,
                         "message": f"Found the Chromatic on {', '.join(ports)} ({USB_ID}); the port was not opened. "
                                    f"Press Allow USB on {ports[0]} to let Remote Pilot open it."}
            else:
                offer = {"state": "none", "ports": [],
                         "message": f"No Chromatic USB port found ({USB_ID}). Plug the console in and switch it on, "
                                    "then press Connect again." + ("" if self._paired() else
                                                                   " Wi-Fi needs a pairing key first.")}
        self._usb_offer = offer
        return offer

    def allow_usb(self, port, local_client):
        """The user's explicit click: allow opening this port, write the config, connect."""
        if not local_client:
            raise ValueError("Allowing the USB port works only from this PC's own browser")
        if not isinstance(port, str):
            raise ValueError("Allow USB needs the port name")
        try:
            ports = [entry["port"] for entry in self._list_consoles()]
        except SerialSupportMissing as exc:
            raise ValueError(NO_PYSERIAL_HINT) from exc
        if port not in ports:
            raise ValueError(f"{port[:16]} is not listed as a Chromatic ({USB_ID}); press Connect to look again")
        save_config(self.local_dir, {"mode": "auto", "serialPort": port, "serialOpenAllowed": True,
                                     "lanHost": self._config.get("lanHost", "auto")})
        self._reload_config()
        self._usb_offer = None
        self._log_activity("allow-usb", True, f"USB allowed on {port}; local/chromatic/config.json written")
        try:
            mode = self.set_mode("auto", from_config=True)
        except (OSError, ValueError) as exc:
            self._config_error = f"Configured transport unavailable: {exc}"[:160]
            self._switch(None, None, "off")
            raise
        return {"mode": mode, "port": port, "result": f"USB allowed on {port}"}

    def _paired(self):
        return (self.local_dir / "pairing.json").is_file()

    def _switch(self, transport, simulator, mode):
        with self._lock:
            old, old_sim = self._transport, self._simulator
            self._transport, self._simulator, self._mode = transport, simulator, mode
        for item in (old, old_sim):
            if item is not None:
                try:
                    item.stop()
                except Exception:
                    pass
        if transport is not None:
            transport.start()

    def set_mode(self, mode, from_config=False):
        if mode == "configured":
            mode, from_config = self._config.get("mode", "off"), True
        if mode not in MODES or (mode in CONFIG_MODES[1:] and not from_config):
            raise ValueError("The viewer can choose off, a simulator, or the configured transport")
        if mode == "off":
            self._switch(None, None, "off")
        elif mode == "serial-sim":
            self._switch(SerialTransport(self._gated(SimulatedSerialDevice), label="Simulated USB serial",
                                         simulated=True), None, mode)
        elif mode in ("lan-sim", "lan-sim-fpga"):
            key = protocol.new_key()  # in memory only; the simulator is gone on restart
            fpga = mode == "lan-sim-fpga"
            simulator = SimulatedLauncher(key, countdown_s=120.0, game_bridge=fpga).start()
            label = "Simulated future device (FPGA game bridge)" if fpga else "Simulated CHR1 launcher"
            self._switch(LanTransport("127.0.0.1", simulator.port, key, label=label,
                                      simulated=True, allow_loopback=True), simulator, mode)
        elif mode == "serial":
            port, allowed = self._config.get("serialPort", ""), self._config.get("serialOpenAllowed") is True
            factory = self._serial_factory
            self._switch(SerialTransport(self._gated(lambda: factory(port, 115200, allowed)),
                                         label="USB serial (read only)"), None, mode)
        elif mode == "usb-sim":
            console = SimulatedUsbConsole(key=None, countdown_s=120.0, auto_approve_s=3.0)
            self._switch(UsbTransport(self._gated(lambda: console), None, label="Simulated USB console (CHR1)",
                                      simulated=True,
                                      on_paired=lambda key_hex: self._log_activity(
                                          "pair-usb", True, "simulated key kept in memory only")),
                         console, mode)
        elif mode == "auto-sim":
            key = protocol.new_key()  # in memory only
            console = SimulatedUsbConsole(key=key, countdown_s=120.0, lan=True)
            usb = UsbTransport(self._gated(lambda: console), key, label="Simulated USB console (CHR1)",
                               simulated=True)
            lan = LanTransport("127.0.0.1", console.launcher.port, key, label="Simulated Wi-Fi (CHR1)",
                               simulated=True, allow_loopback=True)
            self._switch(AutoTransport(usb, lan, simulated=True), console, mode)
        elif mode == "usb":
            self._switch(self._usb(), None, mode)
        elif mode == "auto":
            usb = self._usb() if self._usb_allowed() else None
            lan = None
            if self._paired() and self._config.get("lanHost", "auto") != "auto":
                lan = self._lan(self._config["lanHost"])
            elif self._paired() and self._cached_host():
                lan = self._lan(self._cached_host())
            if usb is None and not self._paired():
                raise ValueError("Automatic mode needs serialPort and serialOpenAllowed, or a pairing key")
            self._switch(AutoTransport(usb, lan), None, mode)
            if lan is None and self._paired() and self._config.get("lanHost", "auto") == "auto":
                self._start_scan(force=True)
        else:
            load_pairing_key(self.local_dir)  # fail early with a clear message
            host = self._config.get("lanHost", "auto")
            if host == "auto":
                cached = self._cached_host()
                self._switch(self._lan(cached) if cached else None, None, "lan")
                if not cached:
                    self._start_scan(force=True)
            else:
                self._switch(self._lan(host), None, "lan")
        self._log_activity("mode", True, mode)
        return self._mode

    # ---- USB ---------------------------------------------------------------------------
    def _usb_allowed(self):
        return self._config.get("serialOpenAllowed") is True and bool(self._config.get("serialPort"))

    def _usb(self):
        port, allowed = self._config.get("serialPort", ""), self._config.get("serialOpenAllowed") is True
        if not allowed:
            raise ValueError("USB needs \"serialOpenAllowed\": true in local/chromatic/config.json")
        try:
            key = load_pairing_key(self.local_dir)
        except ValueError:
            key = None  # pair over USB

        def paired(key_hex):
            save_pairing_key(self.local_dir, key_hex, source="usb-pairing")
            self._log_activity("pair-usb", True, "key stored in local/chromatic/pairing.json "
                                                 "(valid until the console powers off)")
        factory = self._gated(self._usb_port_factory or (lambda: PySerialPort(port, allowed)))
        present = self._usb_present or (lambda: port_present(port))
        return UsbTransport(factory, key, label="USB (CHR1)", present=present, on_paired=paired)

    # ---- USB hand-off with Chromatic Manager (usb_hold.py) -----------------------------
    def _gated(self, factory):
        """The port factory, refusing while Chromatic Manager holds USB. Checked on every
        open from the file on disk, so a restarted receiver keeps away from the port too."""
        def open_port():
            hold = usb_hold.read_hold(self.local_dir)
            if hold is not None:
                raise usb_hold.UsbHeld(usb_hold.paused_text(hold))
            return factory()
        return open_port

    def _usb_parts(self):
        """(the USB transport or None, the serial reader or None) of the current transport."""
        with self._lock:
            transport = self._transport
        if isinstance(transport, AutoTransport):
            return transport.usb, None
        if isinstance(transport, UsbTransport):
            return transport, None
        if isinstance(transport, SerialTransport):
            return None, transport
        return None, None

    def usb_release(self, payload, local_client):
        """Chromatic Manager is about to use the console's USB port: record the hold on disk
        first, then close the port now. Wi-Fi is left as it is. Returns once the port is shut."""
        if not local_client:
            raise ValueError("Only programs on this PC can pause Remote Pilot's USB link")
        hold_id, holder = payload.get("id"), payload.get("holder", "Chromatic Manager")
        reason, pid = payload.get("reason", ""), payload.get("pid")
        usb_hold.check_fields(hold_id, holder, reason, pid)
        current = usb_hold.read_hold(self.local_dir)
        if current is None or current["id"] != hold_id:
            usb_hold.write_hold(self.local_dir, hold_id, holder, reason, pid)
        usb, serial = self._usb_parts()
        if usb is not None:
            usb.stop()  # under its link lock: an open in progress finishes first and is closed here
        if serial is not None:
            self._held_mode = self._mode
            self._switch(None, None, "off")
        usb, _ = self._usb_parts()
        port_open = bool(usb is not None and usb.port_open)
        self._log_activity("usb-release", not port_open, f"USB paused for {holder}" + (f": {reason}" if reason else ""))
        mode, _, snap = self._snapshot()
        alternate = snap.get("alternate") or {}
        wifi = (snap.get("kind") == "lan" and bool(snap.get("session"))) or \
            (alternate.get("kind") == "lan" and bool(alternate.get("session")))
        return {"held": True, "portOpen": port_open, "wifiSession": bool(wifi), "mode": mode}

    def usb_return(self, payload, local_client):
        """Chromatic Manager has finished: end its hold and open USB again. `force` (this
        PC's browser, "Use USB again") ends a hold only when its program is no longer running
        and no Manager job is waiting to be finished."""
        if not local_client:
            raise ValueError("Only programs on this PC can give Remote Pilot's USB link back")
        hold = usb_hold.read_hold(self.local_dir)
        if hold is None:
            return self._usb_back(False)
        if payload.get("force") is True:
            if not hold["unreadable"] and usb_hold.process_alive(hold["pid"]):
                raise ValueError(f"{hold['holder']} is still running and holds the USB cable; it gives it back "
                                 "when its job ends")
            self._manager = manager_status.read(self._config.get("managerProject"))
            if self._manager.get("journalPresent"):
                raise ValueError("A Chromatic Manager job did not finish: open Chromatic Manager and press Finish "
                                 "it. USB stays paused until then")
            usb_hold.remove_any(self.local_dir)
            self._log_activity("usb-return", True, "USB taken back (the holder had closed)")
            return self._usb_back(True)
        if not isinstance(payload.get("id"), str) or not usb_hold.clear_hold(self.local_dir, payload["id"]):
            raise ValueError("That is not the current USB hold")
        self._log_activity("usb-return", True, f"USB back from {hold['holder']}")
        return self._usb_back(True)

    def _usb_back(self, changed):
        usb, _ = self._usb_parts()
        if usb is not None and changed:
            usb.start()  # opens at once (the gate is clear now)
        if self._held_mode is not None and self._transport is None:
            mode, self._held_mode = self._held_mode, None
            try:
                self.set_mode(mode, from_config=mode in CONFIG_MODES)
            except (OSError, ValueError) as exc:
                self._config_error = f"Configured transport unavailable: {exc}"[:160]
        usb, _ = self._usb_parts()
        return {"held": False, "portOpen": bool(usb is not None and usb.port_open)}

    # ---- Wi-Fi discovery -------------------------------------------------------------
    def _lan(self, host):
        return LanTransport(host, self._config.get("lanPort", protocol.PORT), load_pairing_key(self.local_dir))

    def _cached_host(self):
        try:
            host = json.loads((self.local_dir / "last-device.json").read_text(encoding="utf-8")).get("host")
            return host if protocol.allowed_host(host) else None
        except (OSError, ValueError, AttributeError):
            return None

    def _start_scan(self, force=False):
        with self._lock:
            running = self._scan["thread"] is not None and self._scan["thread"].is_alive()
            if running or not force and time.monotonic() - self._scan["at"] < RESCAN_S:
                return
            self._scan["at"] = time.monotonic()
            self._scan["state"] = "scanning"
            thread = threading.Thread(target=self._scan_once, name="chromatic-scan", daemon=True)
            self._scan["thread"] = thread
        thread.start()

    def _scan_once(self):
        try:
            if not self._network_host:
                raise ValueError("this PC has no private LAN address yet")
            host = self._discover(self._network_host, self._config.get("lanPort", protocol.PORT),
                                  load_pairing_key(self.local_dir))
        except (OSError, ValueError) as exc:
            self._scan["state"] = f"scan failed: {exc}"[:120]
            return
        if not host:
            self._scan["state"] = "no paired console answered on this network"
            return
        self._scan["state"] = "found"
        (self.local_dir / "last-device.json").write_text(json.dumps({"host": host}) + "\n", encoding="utf-8")
        if self._mode == "lan":
            self._switch(self._lan(host), None, "lan")
        elif self._mode == "auto" and isinstance(self._transport, AutoTransport):
            self._transport.set_lan(self._lan(host))

    # ---- status (python-v1, one-second background poll) ------------------------------
    def _snapshot(self):
        with self._lock:
            transport, simulator, mode = self._transport, self._simulator, self._mode
        if simulator is not None:
            simulator.tick()
        if transport is None:
            label = "Wi-Fi (CHR1)" if mode == "lan" else "Off"
            error = self._config_error or (self._scan["state"] if mode == "lan" else "")
            return mode, None, {"kind": "lan" if mode == "lan" else "none", "label": label, "simulated": False,
                                "open": False, "connected": False, "session": False, "phase": "unknown",
                                "target": "launcher", "launcherVersion": None, "deviceCapabilities": [],
                                "lines": 0, "error": error}
        return mode, transport, transport.snapshot()

    def status(self):
        with self._lock:
            transport, mode = self._transport, self._mode
        if speaks_chr1(transport):
            transport.poll()
            if mode in ("lan", "auto") and not transport.snapshot()["session"] and self._paired() \
                    and self._config.get("lanHost", "auto") == "auto":
                self._start_scan()  # the console may have a new address
        mode, _, snap = self._snapshot()
        hold = usb_hold.read_hold(self.local_dir)
        perf = transport.perf() if speaks_chr1(transport) else None
        if perf:
            self._perf_history.append(perf)
        phase = snap.get("phase")
        prefix = "SIMULATOR · " if snap.get("simulated") else ""
        discovery = ("Chromatic link off" if mode == "off" else
                     f"{prefix}{snap['label']}: " + (
                         f"launcher {snap.get('launcherVersion') or '?'} running" if phase == "launcher" else
                         "game running (FPGA game bridge)" if phase == "game" else
                         "stock firmware running (launcher handed over)" if phase == "stock" else
                         "console booting" if phase == "booting" else
                         snap.get("error") or "waiting for the console"))
        if hold is not None and not snap.get("connected"):
            discovery = usb_hold.paused_text(hold)
        elif snap.get("usbNote") and phase == "launcher":
            discovery = f"{discovery}. {snap['usbNote']}"  # measured: a launcher before 1.3.2 with Jellyfin open
        return {"connected": bool(snap.get("connected")), "session": mode != "off",
                "host": snap.get("host", "") if snap.get("kind") == "lan" else "",
                "discovery": discovery[:160],
                "meta": {"chromatic": True, "mode": mode, "phase": phase, "target": snap.get("target"),
                         "launcherVersion": snap.get("launcherVersion"), "simulated": bool(snap.get("simulated")),
                         "nextStep": self.next_step(mode, snap, hold), "usbHold": usb_hold.public(hold)},
                "perf": perf}

    def next_step(self, mode, snap, hold=None):
        """A short badge text saying what to do next (the shared viewer's connection badge)."""
        if hold is not None and not (snap.get("connected") and snap.get("session")):
            return f"USB paused for {hold['holder']}; back when it finishes"
        if mode == "off":
            offer = self._usb_offer or {}
            return {"found": f"Press Allow USB on {(offer.get('ports') or ['?'])[0]}",
                    "no-pyserial": "Install pyserial (Start-RemotePilot.cmd)"}.get(offer.get("state"),
                                                                                    "Press Connect")
        if snap.get("connected") and snap.get("session"):
            return "Connected"
        pairing = snap.get("pairing") or {}
        usb_state = snap.get("usbState") or (snap.get("alternate") or {}).get("usbState")
        if pairing.get("state") == "waiting":
            return f"Press A on the console (code {pairing.get('code')})"
        if usb_state in ("nokey", "denied"):
            return "Pair over USB, then press A on the console"
        return {"no-pyserial": "Install pyserial (Start-RemotePilot.cmd)",
                "unplugged": "Plug in the console's USB cable",
                "closed": "Plug in the console, then press Retry",
                "listening": "Switch the console on (launcher starts)",
                "no-answer": "Switch the console off and on",
                "old-launcher": "Update the launcher (1.1.2 or newer)",
                "stock": "Stock firmware running: power-cycle for the launcher"}.get(usb_state) or (
            "Stock firmware running: power-cycle for the launcher" if snap.get("phase") == "stock" else
            "Searching Wi-Fi for the paired console" if mode == "lan" and self._scan["state"] == "scanning" else
            "Waiting for the console")

    def request(self, kind, payload):
        raise ValueError("Chromatic actions use the Chromatic panel (/api/chromatic/*), "
                         "not the shared dual-screen actions")

    # ---- availability -----------------------------------------------------------------
    def features(self, mode, snap):
        connected, phase = bool(snap.get("connected")), snap.get("phase")
        caps = set(snap.get("deviceCapabilities") or [])
        kind, alternate = snap.get("kind"), snap.get("alternate") or {}
        lan, serial = kind == "lan", kind == "serial"
        chr1 = kind in ("lan", "usb")
        usb_on = kind == "usb" or alternate.get("kind") == "usb"
        lan_on = lan or alternate.get("kind") == "lan" or mode == "auto" and self._paired()
        target = snap.get("target", "launcher")
        sim = " (simulator)" if snap.get("simulated") else ""
        stock = "Stock firmware is running; the launcher returns at the next power-on."

        def entry(ident, group, label, state, detail):
            return {"id": ident, "group": group, "label": label, "state": state, "detail": detail}

        def launcher(ident, label, cap, screen_bound=True):
            if mode == "off":
                return entry(ident, "launcher", label, "off", "Chromatic link is off.")
            if phase == "stock" and (chr1 or cap in caps):
                return entry(ident, "launcher", label, "waiting", stock)
            if cap not in caps:
                if chr1 and not snap.get("session"):
                    return entry(ident, "launcher", label, "waiting",
                                 snap.get("error") or "Waiting for a CHR1 session.")
                return entry(ident, "launcher", label, "needs-launcher", LAUNCHER_NOTE if not chr1 else
                             f"This device did not offer {cap!r} over CHR1.")
            if target == "game" and screen_bound:
                return entry(ident, "launcher", label, "waiting", "A game owns the screen now.")
            if connected and screen_bound and cap in ("screen", "video") and snap.get("usbNote"):
                return entry(ident, "launcher", label, "available", f"Ready. {snap['usbNote']}." + sim)
            return entry(ident, "launcher", label, "available" if connected else "waiting",
                         ("Ready" + sim) if connected else snap.get("error") or "Waiting for the launcher.")

        def game(ident, label, cap):
            if cap not in caps:
                return entry(ident, "game", label, "needs-fpga", FPGA_NOTE)
            if not connected or target != "game":
                return entry(ident, "game", label, "waiting", "The device offers this; start a game." + sim)
            return entry(ident, "game", label, "available", "Ready" + sim)

        if self._manager is None:  # read once; the Refresh button re-reads
            self._manager = manager_status.read(self._config.get("managerProject"))
        manager = self._manager
        paired = (self.local_dir / "pairing.json").is_file() or bool(snap.get("pcHasKey"))
        log_ok = serial or "log" in caps or kind == "usb"
        usb_state = snap.get("usbState") or alternate.get("usbState")
        usb_session = kind == "usb" and bool(snap.get("session"))
        lan_session = lan and bool(snap.get("session"))
        hold = usb_hold.read_hold(self.local_dir)
        usb_detail = {
            "closed": "The console's USB port is not open.",
            "unplugged": "The console's USB serial port is not connected.",
            "no-pyserial": NO_PYSERIAL_HINT + ".",
            "listening": "Port open; waiting for the launcher (it runs from power-on until the hand-over).",
            "diag-seen": "Launcher lines seen; opening a session.",
            "launcher": "Launcher 1.1.2+ seen; opening a session.",
            "old-launcher": "This launcher has no CHR1. " + LAUNCHER_NOTE,
            "no-answer": "No CHR1 answer: the launcher is older than 1.1.2 or not running. Nothing more is "
                         "written until the next launcher start (switch the console off and on).",
            "nokey": "The console has no key yet: use Pair over USB, then press A on the console.",
            "busy": "A Wi-Fi session holds the console.",
            "wait": "The console refused a handshake a moment ago; retrying.",
            "denied": "The console refused this PC's key: pair again over USB.",
            "stock": stock,
            "session": "CHR1 session up over USB" + sim,
        }.get(usb_state, "Waiting for the console.")
        if hold is not None:
            usb_detail = usb_hold.paused_text(hold)
        pairing = snap.get("pairing") or {}
        return [
            entry("usb-link", "connection", "USB link (CHR1, paired; launcher 1.1.2+)",
                  "available" if usb_session and connected else "waiting" if usb_on or hold else "off",
                  usb_detail if usb_on or hold else (self._usb_offer or {}).get("message") or
                  "Press Connect: Remote Pilot lists the console's USB port and asks before opening it "
                  "(or set serialPort and \"serialOpenAllowed\": true in local/chromatic/config.json)."),
            entry("wifi-link", "connection", "Wi-Fi link (paired, local network only; launcher 1.1.2+)",
                  "available" if lan_session and connected else "waiting" if lan_on else "needs-launcher",
                  ("CHR1 session up over Wi-Fi" + sim) if lan_session and connected else
                  (snap.get("error") if lan else alternate.get("error") if alternate.get("kind") == "lan" else "")
                  or ("The launcher joins Wi-Fi after you open Jellyfin on the console and press A; it shows "
                      "its address there." if lan_on else
                      "Pair first (USB pairing or Chromatic Manager's provisioning). " + LAUNCHER_NOTE)),
            entry("pairing", "connection", "Pairing key (private, in local/)",
                  "available" if paired else "waiting" if usb_state == "nokey" or pairing else "off",
                  pairing.get("message") if pairing.get("state") == "waiting" else
                  "Paired." if paired else "Not paired. Pair over USB (an A press on the console approves), or "
                                           "provision a key with Chromatic Manager. Simulators pair themselves."),
            entry("usb-serial", "connection", "USB serial log reader (read only, any launcher)",
                  ("available" if connected else "waiting") if serial else "off",
                  ("Reading launcher lines" + sim) if serial and connected else
                  "Configure mode \"serial\" in local/chromatic/config.json (serialOpenAllowed) with the console "
                  "in front of you. USB with CHR1 also keeps these lines."),
            entry("launcher-status", "launcher", "Status: start marker, version, hand-over",
                  "available" if connected and (serial or "status" in caps) or kind == "usb" and snap.get("portOpen")
                  else "off" if mode == "off" else "waiting",
                  ("Ready" + sim) if connected or kind == "usb" and snap.get("portOpen") else "Waiting for the console."),
            launcher("launcher-ping", "Ping (round trip)", "status", screen_bound=False),
            entry("launcher-log", "launcher", "Log retrieval",
                  "available" if (connected or kind == "usb" and snap.get("portOpen")) and log_ok else
                  "off" if mode == "off" else "waiting" if log_ok or chr1 else "needs-launcher",
                  ("Ready" + sim) if (connected or kind == "usb" and snap.get("portOpen")) and log_ok else
                  "Waiting for the console."),
            launcher("launcher-screen", "Launcher screenshots", "screen"),
            launcher("launcher-video", "Launcher live video (CHV1)", "video"),
            launcher("launcher-input", "Launcher buttons: press, chords", "input"),
            launcher("launcher-hold", "Launcher buttons: hold and release, controller", "hold"),
            launcher("launcher-console", "Read-only device console", "console", screen_bound=False),
            launcher("launcher-perf", "Timing telemetry", "perf", screen_bound=False),
            game("game-screen", "Game screenshots", "game-screen"),
            game("game-video", "Game live video", "game-video"),
            game("game-input", "Game buttons: press, chords, hold", "game-input"),
            game("game-audio", "Game sound", "game-audio"),
            entry("capture-bundle", "captures", "Capture bundle (screen, log, manifest)",
                  "available" if mode != "off" else "off", "Saves what the current transport can provide."),
            entry("report", "captures", "QA report", "available", "Status, availability, timing and log tail."),
            entry("diagnostics", "captures", "Diagnostics collection", "available",
                  "Device log, status, console output, Manager files and the last Manager job."),
            entry("manager-status", "firmware", "Chromatic Manager files (read only)",
                  "available" if manager.get("projectFound") else "off", manager.get("detail", "")),
            entry("firmware-patch", "firmware", "Firmware patching through the Chromatic Manager",
                  "available" if manager.get("projectFound") and self._config.get("managerRunAllowed") is True
                  else "off",
                  ("Runs the Manager as a separate process after a typed confirmation; this PC's browser only."
                   if self._config.get("managerRunAllowed") is True else
                   "Off: add \"managerRunAllowed\": true to local/chromatic/config.json to allow running it.")
                  if manager.get("projectFound") else "Set managerProject in local/chromatic/config.json."),
            entry("ota-launcher", "firmware", "Over-the-air launcher-slot update", "needs-launcher",
                  "Design only (LAUNCHER-SIDE.md): launcher slot 0x260000 only, never bootloader, table, "
                  "settings or stock firmware."),
            # No "touch screen: not applicable" row: the Chromatic has none, so it is not listed.
        ]

    # ---- helpers ----------------------------------------------------------------------
    def _log_activity(self, action, ok, detail):
        self._activity.append({"time": time.strftime("%H:%M:%S"), "action": action, "ok": ok,
                               "detail": str(detail)[:160]})

    def _transport_or_fail(self):
        with self._lock:
            transport = self._transport
        if transport is None:
            raise ValueError("No Chromatic connection. Choose a transport first.")
        return transport

    def _lan_or_fail(self, what):
        """The CHR1 transport (Wi-Fi, USB or automatic), for device actions."""
        transport = self._transport_or_fail()
        if not speaks_chr1(transport):
            raise ValueError(f"{what} needs a CHR1 link (USB or Wi-Fi); the read-only serial reader is text only")
        return transport

    def _screen_allowed(self, transport):
        snap = transport.snapshot()
        caps, target = set(snap["deviceCapabilities"]), snap.get("target")
        return ("game-screen" if target == "game" else "screen") in caps, snap

    def _require_lease(self, owner):
        lease = self._lease
        if not lease or lease[0] != owner or time.monotonic() > lease[1]:
            raise ValueError("Enable remote controls in this focused tab first")

    def _video(self, transport):
        allowed, snap = self._screen_allowed(transport)
        if not allowed:
            raise ValueError(FPGA_NOTE if snap.get("target") == "game" else "The device offers no screen mirror")
        caps = set(snap["deviceCapabilities"])
        wants_video = ("game-video" if snap.get("target") == "game" else "video") in caps
        return transport.video_frame() if wants_video else transport.screen()

    def _save(self, kind, extension, data):
        self.captures.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._counter += 1
            name = f"chromatic-{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{self._counter}.{extension}"
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name):
            raise ValueError("Invalid capture name")
        if isinstance(data, str):
            data = data.encode("utf-8")
        temporary = self.captures / (name + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(self.captures / name)
        self._last_files[kind] = name
        return name

    def _status_payload(self):
        mode, transport, snap = self._snapshot()
        return {"mode": mode, "modes": list(UI_MODES), "configuredMode": self._config.get("mode", "off"),
                "configError": self._config_error, "transport": snap, "scan": self._scan["state"],
                "features": self.features(mode, snap),
                "tail": transport.tail(40) if transport is not None else [],
                "perf": list(self._perf_history)[-60:], "manager": self._manager,
                "managerJob": self._runner.snapshot(), "managerCommands": list(MANAGER_COMMANDS),
                "pairingArmed": bool(getattr(transport, "pairing_armed", False)),
                "usbOffer": self._usb_offer if mode == "off" or (self._usb_offer or {}).get("ports") else None,
                "usbPairing": transport.pairing_state() if callable(getattr(transport, "pairing_state", None))
                else None,
                "activity": list(self._activity)[-20:], "files": dict(self._last_files),
                "usbHold": usb_hold.public(usb_hold.read_hold(self.local_dir)),
                "controlsArmed": bool(self._lease and time.monotonic() <= self._lease[1])}

    def _report_text(self):
        status = self._status_payload()
        lines = [f"Remote Pilot Chromatic report {time.strftime('%Y-%m-%d %H:%M:%S')}",
                 f"mode: {status['mode']}  transport: {status['transport'].get('label')}"
                 f"{'  [SIMULATOR]' if status['transport'].get('simulated') else ''}",
                 f"phase: {status['transport'].get('phase')}  target: {status['transport'].get('target')}"
                 f"  launcher: {status['transport'].get('launcherVersion') or '-'}", "", "Availability:"]
        lines += [f"  {item['state']:>14}  {item['label']} - {item['detail']}" for item in status["features"]]
        if status["perf"]:
            last = status["perf"][-1]
            lines += ["", f"Timing: fps {last.get('fps')} frame {last.get('frameMs')} ms work {last.get('workMs')} ms"]
        lines += ["", "Recent activity:"] + [f"  {a['time']} {a['action']} {'ok' if a['ok'] else 'failed'} {a['detail']}"
                                             for a in status["activity"]]
        lines += ["", "Log tail:"] + [f"  {line}" for line in status["tail"]]
        return "\n".join(lines) + "\n"

    # ---- Chromatic API --------------------------------------------------------------------
    def chromatic_request(self, action, payload, local_client=True):
        try:
            result = self._dispatch(action, payload, local_client)
        except ValueError as exc:
            if action not in ("status", "frame", "audio", "hold", "arm", "pair-usb"):
                self._log_activity(action, False, exc)
            raise
        if action not in ("status", "frame", "audio", "hold", "arm", "manager-job", "manager-phrase", "mode",
                          "pair-usb", "ping", "perf", "connect", "allow-usb", "usb-release", "usb-return", "usb-hold"):
            self._log_activity(action, True, result.get("file") or result.get("result") or "ok"
                               if isinstance(result, dict) else "ok")
        return result

    def _dispatch(self, action, payload, local_client):
        if not local_client and (action in LOCAL_ONLY_ACTIONS or
                                 action == "collect" and payload.get("item") in LOCAL_ONLY_COLLECT):
            raise ValueError(LOCAL_ONLY_TEXT)
        if action == "status":
            value = self._status_payload()
            if not local_client:
                value["managerJob"] = None  # Manager output stays on this PC's own browser
            return value
        if action == "mode":
            mode = payload.get("mode")
            if mode not in UI_MODES:
                raise ValueError("Mode must be one of " + ", ".join(UI_MODES))
            return {"mode": self.set_mode(mode)}
        if action == "retry":
            self.retry()
            return {"scan": self._scan["state"]}
        if action == "connect":
            return self.connect()
        if action == "allow-usb":
            return self.allow_usb(payload.get("port"), local_client)
        if action == "usb-release":
            return self.usb_release(payload, local_client)
        if action == "usb-return":
            return self.usb_return(payload, local_client)
        if action == "usb-hold":
            return {"usbHold": usb_hold.public(usb_hold.read_hold(self.local_dir))}
        if action == "manager":
            self._manager = manager_status.read(self._config.get("managerProject"))
            return self._manager
        if action == "manager-job":
            return {"job": self._runner.snapshot()}
        if action == "manager-run":
            command, phrase = payload.get("command"), payload.get("phrase")
            if not isinstance(command, str) or not isinstance(phrase, str):
                raise ValueError("Manager run needs a command and the typed phrase")
            with self._lock:
                transport = self._transport
            serial_open = self._mode == "serial" or bool(getattr(transport, "port_open", False))
            job = self._runner.start(self._config, command, phrase, local_client, serial_open)
            return {"job": job}
        if action == "manager-phrase":
            command = payload.get("command")
            self._manager = manager_status.read(self._config.get("managerProject"))
            return {"command": command, "phrase": required_phrase(command, bool(self._manager.get("journalPresent")))}
        if action == "report":
            return {"file": self._save("report", "txt", self._report_text())}
        if action == "arm":
            owner, enabled = payload.get("owner"), payload.get("enabled")
            if not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{32}", owner) or type(enabled) is not bool:
                raise ValueError("Arm needs a 32-hex owner and enabled true/false")
            if enabled:
                if self._lease and self._lease[0] != owner and time.monotonic() <= self._lease[1]:
                    raise ValueError("Remote controls are armed in another tab")
                self._lease = (owner, time.monotonic() + LEASE_S)
            else:
                if self._lease and self._lease[0] == owner:
                    self._lease = None
                    transport = self._transport
                    if speaks_chr1(transport) and transport.snapshot()["session"]:
                        try:
                            transport.hold(())  # best effort; the device lease releases anyway
                        except (OSError, ValueError):
                            pass
            return {"armed": bool(self._lease)}
        if action == "collect":
            item = payload.get("item")
            if item not in COLLECT_ITEMS:
                raise ValueError("Collect one of " + ", ".join(COLLECT_ITEMS))
            return self._collect(item)
        if action == "pair-capture":
            transport = self._transport_or_fail()
            if not isinstance(transport, SerialTransport):
                raise ValueError("Pairing capture reads the USB-serial line; choose the serial transport")
            enabled = payload.get("enabled") is True

            def adopt(key_hex):
                save_pairing_key(self.local_dir, key_hex, backup=True)
                self._log_activity("pair-capture", True, "pairing key stored in local/chromatic/pairing.json "
                                                         "(any previous key kept as pairing-backup-*.json)")
            transport.arm_pairing(adopt if enabled else None)
            return {"armed": enabled}
        if action == "pair-usb":
            transport = self._transport_or_fail()
            if not callable(getattr(transport, "start_pairing", None)):
                raise ValueError("USB pairing needs the USB (CHR1) or automatic transport")
            if payload.get("start") is True:
                state = transport.start_pairing()
                self._log_activity("pair-usb", True, "asked the console to pair; an A press on it approves")
                return {"pairing": state}
            return {"pairing": transport.pairing_state()}

        transport = self._transport_or_fail()
        if action == "log":
            text = transport.log_text(65536)
            if not text:
                raise ValueError("No device lines received yet")
            name = self._save("log", "txt", text)
            return {"file": name, "bytes": len(text.encode("utf-8"))}
        if action == "clear-log":
            transport.clear()
            return {"cleared": True}
        if action == "frame":  # live video: returned to the viewer, never written to disk
            return frame_to_bmp(self._video(self._lan_or_fail("Live video")))
        if action == "shot":
            lan = self._lan_or_fail("Screenshots")
            allowed, snap = self._screen_allowed(lan)
            if not allowed:
                raise ValueError(FPGA_NOTE if snap.get("target") == "game" else "The device offers no screenshots")
            image = payload.get("format", "bmp")
            if image not in ("bmp", "png"):
                raise ValueError("Screenshot format is bmp or png")
            frame = lan.screen()
            name = self._save("screen", image, frame_to_png(frame) if image == "png" else frame_to_bmp(frame))
            # The bare file name only (no folder path); it is served from /captures/<name>.
            return {"file": name, "width": protocol.SCREEN_WIDTH,
                    "height": protocol.SCREEN_HEIGHT, "target": snap.get("target"), "via": snap.get("kind")}
        if action == "ping":
            lan = self._lan_or_fail("Ping")
            started = time.monotonic()
            result = lan.ping()
            return {"result": result, "ms": round((time.monotonic() - started) * 1000, 1),
                    "via": lan.snapshot().get("kind")}
        if action == "perf":
            lan = self._lan_or_fail("Timing")
            sample = lan.perf()
            if sample is None:
                raise ValueError("No timing sample yet (the device must offer perf in a session)")
            return {"perf": sample, "via": lan.snapshot().get("kind")}
        if action == "bundle":
            return self._bundle(transport)
        if action == "console":
            return {"command": payload.get("command"),
                    "result": self._lan_or_fail("The console").console(payload.get("command"))}
        if action == "audio":
            lan = self._lan_or_fail("Sound")
            if "game-audio" not in lan.capabilities:
                raise ValueError(FPGA_NOTE)
            return lan.audio()
        if action in ("input", "hold"):
            return self._buttons(action, payload)
        raise ValueError("Unknown Chromatic action")

    def _buttons(self, action, payload):
        self._require_lease(payload.get("owner"))
        lan = self._lan_or_fail("Button input")
        buttons = payload.get("buttons", [payload["button"]] if "button" in payload else None)
        if not isinstance(buttons, list) or any(not isinstance(name, str) for name in buttons):
            raise ValueError("Buttons must be a list of names")
        snap = lan.snapshot()
        caps, target = set(snap["deviceCapabilities"]), snap.get("target")
        needed = "game-input" if target == "game" else ("input" if action == "input" else "hold")
        if needed not in caps:
            raise ValueError(FPGA_NOTE if target == "game" else "The device offers no remote buttons")
        if not self._press_lock.acquire(blocking=False):
            raise ValueError("Busy: one button request at a time")
        try:
            now = time.monotonic()
            if action == "input":
                ms = payload.get("ms", 100)
                if type(ms) is not int or not protocol.MIN_PRESS_MS <= ms <= protocol.MAX_PRESS_MS:
                    raise ValueError(f"ms must be {protocol.MIN_PRESS_MS}..{protocol.MAX_PRESS_MS}")
                if now - self._last_press < MIN_PRESS_GAP_S:
                    raise ValueError("Busy: wait between button presses")
                names = protocol.parse_buttons("+".join(buttons))
                result = lan.press(names, ms)
                self._last_press = time.monotonic()
                return {"buttons": list(names), "ms": ms, "result": result, "target": target}
            if now - self._last_hold < MIN_HOLD_GAP_S and buttons:
                raise ValueError("Busy: hold refreshes are limited to about 10 a second")
            names = protocol.parse_buttons(protocol.buttons_arg(tuple(buttons)), allow_none=True)
            result = lan.hold(names)
            self._last_hold = time.monotonic()
            return {"buttons": list(names), "result": result, "target": target}
        except protocol.ProtocolError as exc:
            raise ValueError(str(exc)) from exc
        finally:
            self._press_lock.release()

    def _bundle(self, transport):
        files, skipped = {}, {}
        if speaks_chr1(transport) and self._screen_allowed(transport)[0]:
            try:
                files["screen"] = self._save("screen", "bmp", frame_to_bmp(transport.screen()))
            except (OSError, ValueError) as exc:
                skipped["screen"] = str(exc)[:120]
        else:
            skipped["screen"] = "No screen from this transport or device state"
        try:
            text = transport.log_text(65536)
            if text:
                files["log"] = self._save("log", "txt", text)
            else:
                skipped["log"] = "No device lines yet"
        except (OSError, ValueError) as exc:
            skipped["log"] = str(exc)[:120]
        files["report"] = self._save("report", "txt", self._report_text())
        manifest = {"preset": "chromatic", "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "simulated": bool(transport.snapshot().get("simulated")), "files": files, "skipped": skipped}
        files["manifest"] = self._save("capture", "json", json.dumps(manifest, indent=2) + "\n")
        return {"file": files["manifest"], "files": files, "skipped": skipped}

    def _collect(self, item):
        if item == "status":
            return {"file": self._save("status", "json", json.dumps(self._status_payload(), indent=2) + "\n")}
        if item == "manager-files":
            self._manager = manager_status.read(self._config.get("managerProject"))
            return {"file": self._save("manager-files", "json", json.dumps(self._manager, indent=2) + "\n")}
        if item == "manager-job":
            job = self._runner.snapshot()
            if not job:
                raise ValueError("No Manager job has run in this session")
            return {"file": self._save("manager-job", "txt", "\n".join(job["lines"]) + "\n")}
        transport = self._transport_or_fail()
        if item == "console":
            lan = self._lan_or_fail("The console")
            text = "\n".join(f"> {command}\n{lan.console(command)}" for command in protocol.CONSOLE_COMMANDS)
            return {"file": self._save("console", "txt", text + "\n")}
        text = transport.log_text(65536)
        if not text:
            raise ValueError("No device lines received yet")
        return {"file": self._save("log", "txt", text)}

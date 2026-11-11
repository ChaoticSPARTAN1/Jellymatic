# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""Automatic transport choice: USB when connected and allowed, otherwise Wi-Fi.

As CHROMATIC-REMOTE-CONTROL.md says: the console allows one CHR1 session across
both transports. Each one-second poll:
  1. a Wi-Fi session that is already up is kept until it ends (a USB OPEN would
     only get `CHR1 BUSY`);
  2. otherwise USB is tried (it writes nothing unless a CHR1 launcher shows on
     the port, see usb_transport.py);
  3. if USB has no session, Wi-Fi is tried with the paired key.
Every device action goes to whichever transport holds the session.
"""

ACTIONS = ("perf", "log_text", "tail", "clear", "screen", "video_frame", "press", "hold", "console",
           "audio", "ping")


class AutoTransport:
    kind = "auto"
    speaks_chr1 = True

    def __init__(self, usb=None, lan=None, label="Automatic: USB, else Wi-Fi", simulated=False):
        if usb is None and lan is None:
            raise ValueError("Automatic mode needs USB (serialPort, serialOpenAllowed) or a Wi-Fi pairing key")
        self.usb, self.lan = usb, lan
        self.label, self.simulated = label, simulated
        self._route = usb or lan

    @staticmethod
    def _up(transport):
        return transport is not None and transport.snapshot()["session"]

    @property
    def route(self):
        if self._up(self.usb):
            return self.usb
        if self._up(self.lan):
            return self.lan
        return self._route

    @property
    def capabilities(self):
        return self.route.capabilities

    @property
    def port_open(self):
        return bool(self.usb is not None and self.usb.port_open)

    @property
    def video_bytes(self):
        return self.route.video_bytes

    def set_lan(self, lan):
        old, self.lan = self.lan, lan
        if old is not None and old is not lan:
            old.stop()

    def start(self):
        for transport in (self.usb, self.lan):
            if transport is not None:
                transport.start()

    def stop(self):
        for transport in (self.usb, self.lan):
            if transport is not None:
                transport.stop()

    def retry(self):
        if self.usb is not None:
            self.usb.retry()

    def poll(self):
        if self._up(self.lan):
            self.lan.poll()
            if self._up(self.lan):
                self._route = self.lan
                return
        if self.usb is not None:
            self.usb.poll()
            if self._up(self.usb):
                self._route = self.usb
                return
        if self.lan is not None:
            self.lan.poll()
            self._route = self.lan if self._up(self.lan) or self.usb is None else self.usb

    def snapshot(self):
        route = self.route
        snap = dict(route.snapshot())
        other = self.lan if route is self.usb else self.usb
        snap.update({"label": f"Automatic: {snap['label']}", "route": route.kind, "auto": True,
                     "simulated": self.simulated or snap.get("simulated", False)})
        if other is not None:
            peer = other.snapshot()
            snap["alternate"] = {"kind": peer["kind"], "session": peer["session"], "error": peer.get("error", ""),
                                 "portOpen": peer.get("portOpen"), "usbState": peer.get("usbState"),
                                 "host": peer.get("host")}
        if self.usb is not None:
            usb = self.usb.snapshot() if route is not self.usb else snap
            snap["portOpen"], snap["usbState"], snap["pairing"] = usb.get("portOpen"), usb.get("usbState"), \
                usb.get("pairing")
            snap["pcHasKey"] = usb.get("pcHasKey")
        return snap

    def pairing_state(self):
        return self.usb.pairing_state() if self.usb is not None else None

    def start_pairing(self):
        if self.usb is None:
            raise ValueError("USB pairing needs the console's USB port (serialPort and serialOpenAllowed)")
        if self._up(self.lan):
            raise ValueError("A Wi-Fi session is open: this PC is already paired with the console")
        return self.usb.start_pairing()

    def __getattr__(self, name):
        if name in ACTIONS:
            return getattr(self.route, name)
        raise AttributeError(name)

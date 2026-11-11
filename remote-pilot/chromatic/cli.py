# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""Drive the Chromatic preset of a running Remote Pilot viewer from the command line.

Meant for people and for AI test agents. It talks only to the local viewer
(127.0.0.1), which owns the connection; it never opens a serial port itself.

  python -m chromatic.cli status
  python -m chromatic.cli mode lan-sim            # or lan-sim-fpga, serial-sim, usb-sim, auto-sim, configured, off
  python -m chromatic.cli wait --seconds 30       # until a CHR1 session is up (USB or Wi-Fi)
  python -m chromatic.cli ping | perf | tail      # round trip, timing sample, last device lines
  python -m chromatic.cli shot --png              # prints the saved file's full path
  python -m chromatic.cli pair-usb                # USB pairing: shows the code, press A on the console
  python -m chromatic.cli press DOWN              # one 100 ms press
  python -m chromatic.cli press A B               # a chord
  python -m chromatic.cli hold RIGHT --seconds 1.5
  python -m chromatic.cli shot | bundle | report | log
  python -m chromatic.cli collect console
  python -m chromatic.cli console wifi
  python -m chromatic.cli pair --key-file key.txt # stores the key in local/chromatic/pairing.json
  python -m chromatic.cli manager                 # re-read the Manager's files (never runs it)
  python -m chromatic.cli manager-run status --phrase "READ CHROMATIC"

`manager-run` starts the Chromatic Manager, which can write the console's flash
for install, remove and restore-stock. Use it only with the console in front of
you; it needs the same typed phrase as the viewer.
"""

import argparse
import json
import secrets
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .protocol import BUTTONS, CONSOLE_COMMANDS, MAX_PRESS_MS, MIN_PRESS_MS, parse_key
from .manager_runner import COMMANDS as MANAGER_COMMANDS

ROOT = Path(__file__).resolve().parent.parent


def call(action, payload=None, viewer_port=38764):
    if not 1 <= viewer_port <= 65535:
        raise ValueError("Viewer port must be 1..65535")
    request = Request(f"http://127.0.0.1:{viewer_port}/api/chromatic/{action}",
                      json.dumps(payload or {}).encode("utf-8"),
                      {"Content-Type": "application/json", "X-Remote-Pilot-Viewer": "1"}, method="POST")
    try:
        with urlopen(request, timeout=10) as response:
            return json.load(response)
    except HTTPError as exc:
        raise ValueError(exc.read(300).decode("utf-8", "replace") or f"Viewer rejected ({exc.code})") from exc
    except (URLError, TimeoutError) as exc:
        raise ValueError("Start the viewer first: python receiver.py serve") from exc


def summary(status):
    transport = status["transport"]
    lines = [f"mode: {status['mode']}  connection: {transport.get('label')}"
             f"{'  [SIMULATOR]' if transport.get('simulated') else ''}",
             f"phase: {transport.get('phase')}  screen owner: {transport.get('target')}"
             f"  launcher: {transport.get('launcherVersion') or '-'}  connected: {transport.get('connected')}"]
    lines += [f"  {item['state']:>14}  {item['label']}" for item in status["features"]]
    lines += ["last lines:"] + [f"  {line}" for line in status.get("tail", [])[-10:]]
    return "\n".join(lines)


def store_key(path):
    key = Path(path).read_text(encoding="ascii").strip().lower()
    parse_key(key)
    folder = ROOT / "local" / "chromatic"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "pairing.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps({"key": key}) + "\n", encoding="utf-8")
    temporary.replace(target)
    return {"stored": "local/chromatic/pairing.json"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--viewer-port", type=int, default=38764)
    parser.add_argument("--json", action="store_true", help="print the raw JSON result")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "log", "bundle", "report", "manager", "retry"):
        sub.add_parser(name)
    sub.add_parser("shot").add_argument("--png", action="store_true", help="save a PNG instead of a BMP")
    sub.add_parser("mode").add_argument("mode", choices=("off", "lan-sim", "lan-sim-fpga", "serial-sim", "usb-sim",
                                                         "auto-sim", "configured"))
    for name in ("ping", "perf", "pair-usb"):
        sub.add_parser(name)
    sub.add_parser("tail").add_argument("--lines", type=int, default=20)
    sub.add_parser("wait", help="wait until a CHR1 session is up").add_argument("--seconds", type=float, default=30.0)
    press = sub.add_parser("press", help="press one button or a chord of up to four")
    press.add_argument("buttons", nargs="+", choices=BUTTONS)
    press.add_argument("--ms", type=int, default=100, help=f"{MIN_PRESS_MS}..{MAX_PRESS_MS}")
    hold = sub.add_parser("hold", help="hold buttons, refreshing the device lease, then release")
    hold.add_argument("buttons", nargs="+", choices=[name for name in BUTTONS if name != "MENU"])
    hold.add_argument("--seconds", type=float, default=1.0)
    sub.add_parser("collect").add_argument("item", choices=("device-log", "status", "console", "manager-files", "manager-job"))
    sub.add_parser("console").add_argument("console_command", choices=CONSOLE_COMMANDS)
    sub.add_parser("pair").add_argument("--key-file", required=True)
    run = sub.add_parser("manager-run", help="start the Chromatic Manager (can write flash)")
    run.add_argument("manager_command", choices=tuple(MANAGER_COMMANDS))
    run.add_argument("--phrase", required=True)
    args = parser.parse_args(argv)
    port = args.viewer_port
    try:
        if args.command == "pair":
            result = store_key(args.key_file)
        elif args.command == "pair-usb":
            result = pair_usb(port)
        elif args.command == "wait":
            result = wait_session(port, args.seconds)
        elif args.command == "tail":
            result = {"tail": call("status", {}, port).get("tail", [])[-max(1, min(args.lines, 40)):]}
        elif args.command in ("press", "hold"):
            owner = secrets.token_hex(16)
            call("arm", {"owner": owner, "enabled": True}, port)
            try:
                if args.command == "press":
                    result = call("input", {"owner": owner, "buttons": args.buttons, "ms": args.ms}, port)
                else:
                    end = time.monotonic() + max(0.0, min(args.seconds, 30.0))
                    result = call("hold", {"owner": owner, "buttons": args.buttons}, port)
                    while time.monotonic() < end:
                        time.sleep(0.2)
                        call("arm", {"owner": owner, "enabled": True}, port)
                        call("hold", {"owner": owner, "buttons": args.buttons}, port)
                    result = call("hold", {"owner": owner, "buttons": []}, port)
            finally:
                call("arm", {"owner": owner, "enabled": False}, port)
        else:
            payload = {"mode": {"mode": getattr(args, "mode", None)},
                       "collect": {"item": getattr(args, "item", None)},
                       "shot": {"format": "png" if getattr(args, "png", False) else "bmp"},
                       "console": {"command": getattr(args, "console_command", None)},
                       "manager-run": {"command": getattr(args, "manager_command", None),
                                       "phrase": getattr(args, "phrase", None)}}.get(args.command, {})
            result = call(args.command, payload, port)
            if args.command == "shot" and isinstance(result.get("file"), str):
                # The viewer replies with the bare file name; this PC's own folder gives the full path.
                result["path"] = str(ROOT / "local" / "captures" / Path(result["file"]).name)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    if args.command == "status" and not args.json:
        print(summary(result))
    elif args.command == "tail" and not args.json:
        print("\n".join(result["tail"]))
    else:
        print(json.dumps(result, indent=1))
    return 0


def wait_session(port, seconds):
    """Polls the viewer until the Chromatic transport has a CHR1 session."""
    end = time.monotonic() + max(0.0, min(seconds, 600.0))
    while True:
        transport = call("status", {}, port)["transport"]
        if transport.get("session"):
            return {"session": True, "via": transport.get("kind"), "phase": transport.get("phase"),
                    "launcherVersion": transport.get("launcherVersion")}
        if time.monotonic() >= end:
            raise ValueError("No CHR1 session yet: " + (transport.get("error") or transport.get("usbState") or
                                                         "waiting for the console"))
        time.sleep(0.5)


def pair_usb(port):
    """USB pairing. The console must approve with a physical A press; this only asks."""
    state = call("pair-usb", {"start": True}, port)["pairing"]
    shown = False
    end = time.monotonic() + 70
    while time.monotonic() < end:
        if state and state.get("state") == "waiting" and not shown:
            print(f"Pairing code {state['code']}: check the console shows the same code, then press A on it "
                  "(B refuses). With no cartridge the screen stays dark: press A anyway.", flush=True)
            shown = True
        if state and state.get("state") not in ("asking", "waiting"):
            return state
        time.sleep(0.5)
        state = call("pair-usb", {}, port)["pairing"]
    raise ValueError("Pairing did not finish in time")


if __name__ == "__main__":
    raise SystemExit(main())

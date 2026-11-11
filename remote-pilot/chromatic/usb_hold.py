# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""USB hand-off between Remote Pilot and Chromatic Manager.

The console has one USB serial port and only one program can hold it. Chromatic
Manager needs it for every job (install, update, restore, console chip jobs,
Wi-Fi and checks); Remote Pilot uses it for its USB link. Before a job the Manager
asks Remote Pilot to let go of USB, and gives it back afterwards. Remote Pilot's
Wi-Fi link keeps working the whole time.

The hold is a small file, local/chromatic/usb-hold.json (git-ignored):

  {"format": 1, "holder": "Chromatic Manager", "id": "<32 hex>", "pid": 1234,
   "reason": "Install the start-up menu", "since": "2026-11-08 12:00:00"}

Crash safety comes from the order of steps and from the file living on disk:
  1. the Manager writes the hold file (flushed to disk) before its job touches the
     port, then asks a running receiver to close USB (POST usb-release) and waits
     until it says the port is closed;
  2. Remote Pilot checks the file before EVERY attempt to open the port, not a flag
     in memory, so a receiver that restarts, or one started during the job, also
     keeps away from the port;
  3. a file that cannot be read or makes no sense counts as a hold (fail safe);
  4. only the Manager's "give back" (POST usb-return with the hold's id, or the
     Manager deleting its own file) ends a hold. If the Manager dies mid-job the
     file stays and USB stays off: a half-written console is never touched. The
     next Manager start clears a hold whose process has gone, but only when no
     job is left unfinished; Remote Pilot's own "Use USB again" does the same
     (the holder's process must be gone and no Manager journal may be waiting).
"""

import json
import os
import re
import time
from pathlib import Path

HOLD_FILE = "usb-hold.json"
FORMAT = 1
MAX_BYTES = 2048
_ID = re.compile(r"[0-9a-f]{32}")
_HOLDER = re.compile(r"[A-Za-z0-9 ._()-]{1,40}")
_REASON = re.compile(r"[A-Za-z0-9 ,.()'/:+&-]{0,80}")


class UsbHeld(PermissionError):
    """Raised instead of opening the port while Chromatic Manager holds USB."""


def hold_path(local_dir):
    return Path(local_dir) / HOLD_FILE


def check_fields(hold_id, holder, reason, pid):
    if not isinstance(hold_id, str) or not _ID.fullmatch(hold_id):
        raise ValueError("The USB hold needs a 32-hex id")
    if not isinstance(holder, str) or not _HOLDER.fullmatch(holder):
        raise ValueError("The USB hold needs a short holder name")
    if not isinstance(reason, str) or not _REASON.fullmatch(reason):
        raise ValueError("The USB hold reason must be short plain text")
    if type(pid) is not int or not 0 < pid < 2 ** 32:
        raise ValueError("The USB hold needs the holder's process id")


def read_hold(local_dir):
    """None when USB is free, else the hold. Anything unreadable counts as held."""
    path = hold_path(local_dir)
    try:
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("too large")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise ValueError("unknown format")
        check_fields(data.get("id"), data.get("holder"), data.get("reason", ""), data.get("pid"))
        since = data.get("since", "")
        return {"holder": data["holder"], "id": data["id"], "pid": data["pid"], "reason": data.get("reason", ""),
                "since": since if isinstance(since, str) else "", "unreadable": False}
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, UnicodeDecodeError):
        return {"holder": "unknown", "id": None, "pid": None, "reason": "", "since": "", "unreadable": True}


def write_hold(local_dir, hold_id, holder, reason, pid):
    """Writes the hold atomically and flushes it to disk before returning."""
    check_fields(hold_id, holder, reason, pid)
    path = hold_path(local_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"format": FORMAT, "holder": holder, "id": hold_id, "pid": pid, "reason": reason,
            "since": time.strftime("%Y-%m-%d %H:%M:%S")}
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(body) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return read_hold(local_dir)


def clear_hold(local_dir, hold_id):
    """Removes the hold if it is still the one with this id. True when USB is free now."""
    hold = read_hold(local_dir)
    if hold is None:
        return True
    if hold["id"] != hold_id:
        return False
    try:
        hold_path(local_dir).unlink()
    except FileNotFoundError:
        pass
    return True


def remove_any(local_dir):
    try:
        hold_path(local_dir).unlink()
    except FileNotFoundError:
        pass


def process_alive(pid):
    """Is that process still running? Never signals it. Unknown counts as alive."""
    if type(pid) is not int or pid <= 0:
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # POSIX: signal 0 only checks
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def public(hold):
    """What the status shows: no id."""
    if hold is None:
        return None
    return {"held": True, "holder": hold["holder"], "reason": hold["reason"], "since": hold["since"],
            "unreadable": hold["unreadable"], "holderRunning": process_alive(hold["pid"])}


def paused_text(hold):
    if hold is None:
        return ""
    what = f" ({hold['reason']})" if hold.get("reason") else ""
    return (f"USB paused: {hold['holder']} is using the cable{what}. Wi-Fi keeps working; USB comes back when it "
            "finishes.")

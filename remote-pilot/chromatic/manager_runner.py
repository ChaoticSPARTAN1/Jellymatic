# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""Firmware patching for the Chromatic, only through the Chromatic Manager.

Remote Pilot never writes flash itself. Every write goes through
`scripts/chromatic_manager.py` in the Jellymatic project, run as a
separate process: journalled, identity-checked, backup first, automatic undo,
bootloader/settings protected (see the Jellymatic documentation). This module
only starts it with an allow-listed command after a typed confirmation, and
shows its (redacted) output.

Guards, all checked before a process is started:
  * the request comes from this PC's own browser (127.0.0.1), never a LAN viewer;
  * the user opted in with `"managerRunAllowed": true` in local/chromatic/config.json
    (off by default);
  * the user configured `managerProject` in local/chromatic/config.json; the Manager's
    Python is `managerPython` if set, else the venv that Setup-Jellymatic.ps1 makes in the
    per-user Jellymatic folder, else the older private/chromatic-tools/venv (manager_home.py);
  * Remote Pilot's own USB-serial mode is off, because the Manager needs the port;
  * the exact confirmation phrase for that command was typed;
  * when a Manager journal is present the Manager will first finish or undo that
    job, so a different phrase is required that says so;
  * one Manager process at a time; there is deliberately no cancel, because
    stopping a flash write half way is what the journal exists to avoid.
"""

import os
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from . import manager_home
from .serial_transport import sanitize

COMMANDS = {
    # command: (writes flash, typed phrase)
    "status": (False, "READ CHROMATIC"),
    "backup": (False, "BACKUP CHROMATIC"),
    "install": (True, "INSTALL CHROMATIC LAUNCHER"),
    "remove": (True, "REMOVE CHROMATIC LAUNCHER"),
    "restore-stock": (True, "RESTORE CHROMATIC STOCK"),
}
JOURNAL_PHRASE = "FINISH CHROMATIC JOB"
# Chromatic Manager exit codes
EXIT_STATES = {0: "done", 1: "refused", 2: "recovered", 3: "manual-recovery-needed", 4: "error"}
MAX_LINES = 2000


def manager_paths(config, environ=None, platform=None):
    project = config.get("managerProject")
    if not project:
        raise ValueError("Set managerProject in local/chromatic/config.json first")
    root = Path(project)
    script = root / "scripts" / "chromatic_manager.py"
    if not script.is_file():
        raise ValueError("managerProject has no scripts/chromatic_manager.py")
    # managerPython if set; else Setup-Jellymatic.ps1's venv in the per-user Jellymatic folder;
    # else the older <project>/private/chromatic-tools/venv (manager_home.py).
    python = manager_home.manager_python(config, root, environ, platform)
    if python is None:
        raise ValueError("The Manager's Python was not found: run Setup-Jellymatic.ps1, or set managerPython "
                         "in local/chromatic/config.json")
    return root, script, python


def required_phrase(command, journal_present):
    if command not in COMMANDS:
        raise ValueError("Manager command must be one of " + ", ".join(COMMANDS))
    return JOURNAL_PHRASE if journal_present else COMMANDS[command][1]


class ManagerRunner:
    def __init__(self, jobs_dir, popen=subprocess.Popen, clock=time.time):
        self.jobs_dir = Path(jobs_dir)
        self._popen, self._clock = popen, clock
        self._lock = threading.Lock()
        self._job = None

    def busy(self):
        with self._lock:
            return bool(self._job and self._job["state"] == "running")

    def start(self, config, command, phrase, local_client, serial_open):
        if not local_client:
            raise ValueError("Firmware patching is only available from this PC's own browser")
        if config.get("managerRunAllowed") is not True:
            raise ValueError('Running the Chromatic Manager is off: add "managerRunAllowed": true to '
                             "local/chromatic/config.json to allow it")
        if serial_open:
            raise ValueError("Turn off Remote Pilot's USB-serial mode first: the Manager needs the port")
        root, script, python = manager_paths(config)
        journal = manager_home.journal_present(root)
        expected = required_phrase(command, journal)
        if phrase != expected:
            raise ValueError(f'Type "{expected}" exactly to run the Manager\'s {command}'
                             + (" (an unfinished Manager job will be finished or undone first)" if journal else ""))
        writes = COMMANDS[command][0]
        with self._lock:
            if self._job and self._job["state"] == "running":
                raise ValueError("A Manager job is already running")
            ident = time.strftime("%Y%m%d-%H%M%S")
            job = {"id": ident, "command": command, "writes": writes, "journalFirst": journal,
                   "state": "running", "exitCode": None, "started": self._clock(), "ended": None,
                   "lines": deque(maxlen=MAX_LINES)}
            args = [str(python), "-B", str(script), command] + (["--yes"] if writes else [])
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = self._popen(args, cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, env=env, creationflags=flags)
            job["process"] = process
            self._job = job
        threading.Thread(target=self._pump, args=(job,), daemon=True).start()
        return self.snapshot()

    def _pump(self, job):
        process = job["process"]
        try:
            for raw in iter(process.stdout.readline, b""):
                text = sanitize(raw)
                if text:
                    with self._lock:  # snapshot() copies the job under the same lock
                        job["lines"].append(text)
        finally:
            process.stdout.close()
            code = process.wait()
            with self._lock:
                job["exitCode"], job["ended"] = code, self._clock()
            try:
                self._save(job)  # the job file exists before anyone can see the job as finished
            finally:
                with self._lock:
                    job["state"] = EXIT_STATES.get(code, "failed")

    def _save(self, job):
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        name = f"manager-{job['id']}-{job['command']}.txt"
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name):
            return
        with self._lock:
            body = f"Chromatic Manager {job['command']} via Remote Pilot, exit {job['exitCode']}\n" + \
                "\n".join(job["lines"]) + "\n"
        (self.jobs_dir / name).write_text(body, encoding="utf-8")
        with self._lock:
            job["file"] = name

    def snapshot(self):
        with self._lock:
            job = self._job
            if job is None:
                return None
            return {key: (list(value)[-200:] if key == "lines" else value)
                    for key, value in job.items() if key != "process"}

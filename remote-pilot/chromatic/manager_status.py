# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""Read-only, file-only view of the Chromatic Manager's state.

This never runs, imports or links the Manager, and never touches the console.
Reason: `chromatic_manager.py status` reads the console over the ROM download
mode (which resets it), and the Manager's entry point first resumes any
unfinished journalled job, which can write flash. Neither is acceptable from a
viewer, so Remote Pilot only reports what the Manager's own files say and
tells the user to run the Manager themselves.

It reads:
  * <managerProject>/chromatic/launcher/main/launcher_main.c - the launcher source version
  * chromatic-manager/journal.jsonl (and chromatic-manager/fpga/) - existence only
  * chromatic-manager-backups/*/ - a count of folders that hold a record.json (contents never read)
The last two are looked for in the Manager's per-user folder (JELLYMATIC_HOME, else
%LOCALAPPDATA%\\Jellymatic) first, then in <managerProject>/private/ for older setups
(see manager_home.py).
"""

from pathlib import Path
import re

from . import manager_home

_VERSION = re.compile(rb'#define\s+PJ_LAUNCHER_VERSION\s+"([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3})"')
LIVE_STATUS_NOTE = ("Live console status is not run from Remote Pilot: the Manager resets the console to read "
                    "it, and it first finishes any unfinished job, which may write flash. Run "
                    "Chromatic-Manager.cmd yourself, with the console in front of you.")


def read(project, environ=None, platform=None):
    if not project:
        return {"configured": False, "detail": "Set managerProject in local/chromatic/config.json to the "
                                                "Jellymatic folder to see Manager files here.",
                "liveStatus": LIVE_STATUS_NOTE}
    root = Path(project)
    if not (root / "scripts" / "chromatic_manager.py").is_file():
        return {"configured": True, "projectFound": False,
                "detail": "The configured folder has no scripts/chromatic_manager.py.",
                "liveStatus": LIVE_STATUS_NOTE}
    version = None
    source = root / "chromatic" / "launcher" / "main" / "launcher_main.c"
    try:
        if source.is_file() and source.stat().st_size <= 262144:
            match = _VERSION.search(source.read_bytes())
            version = match.group(1).decode("ascii") if match else None
    except OSError:
        version = None
    # The main job journal, or the console-chip (FPGA) job journal: either one unfinished means the Manager
    # may still write the console, so USB stays paused (an FPGA journal is moved to closed/ when its job ends).
    # Both the per-user Jellymatic folder and the older <project>/private layout are checked.
    journal = manager_home.journal_present(root, environ, platform)
    backups = manager_home.backup_folders(root, environ, platform)
    detail = ("A Manager journal is present. The Manager finishes or undoes that job the next time it runs, "
              "which can write flash. Run it yourself with the console attached and in front of you."
              if journal else "No Manager job is waiting.")
    return {"configured": True, "projectFound": True, "launcherSourceVersion": version,
            "journalPresent": journal, "backupFolders": backups,
            "secondCopyBackupFolders": manager_home.second_copy_folders(environ), "detail": detail,
            "liveStatus": LIVE_STATUS_NOTE}

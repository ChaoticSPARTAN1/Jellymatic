# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""Where the Chromatic Manager (Jellymatic) keeps its files and its Python. Paths only: nothing
here opens, runs or changes anything.

The published Manager keeps everything personal in one per-user folder:
  * JELLYMATIC_HOME, if set; otherwise
  * Windows: %LOCALAPPDATA%\\Jellymatic; macOS / Linux: $XDG_DATA_HOME/jellymatic
    (usually ~/.local/share/jellymatic).
In it: chromatic-manager/ (job journals; chromatic-manager/fpga/ for console-chip jobs),
chromatic-manager-backups/ (one folder per backup), chromatic-manager-gui/ (logs, status files)
and venv/ (the Manager's Python, made by Setup-Jellymatic.ps1). A second copy of every backup
goes to JELLYMATIC_SECOND_COPY, or a Jellymatic-Backups folder in the home folder.

Older setups kept the same folders in <managerProject>/private/ and the Python in
<managerProject>/private/chromatic-tools/venv/. Remote Pilot looks in the per-user folder first,
then in that older place, so both keep working.
"""

import os
from pathlib import Path
import sys


def jellymatic_home(environ=None, platform=None):
    env = os.environ if environ is None else environ
    if env.get("JELLYMATIC_HOME"):
        return Path(env["JELLYMATIC_HOME"])
    if (platform or sys.platform) == "win32":
        return Path(env.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Jellymatic"
    return Path(env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "jellymatic"


def second_copy(environ=None):
    env = os.environ if environ is None else environ
    return Path(env.get("JELLYMATIC_SECOND_COPY") or Path.home() / "Jellymatic-Backups")


def data_roots(project, environ=None, platform=None):
    """The Manager's data folders, newest layout first: the per-user folder, then <project>/private."""
    roots = [jellymatic_home(environ, platform)]
    if project:
        roots.append(Path(project) / "private")
    return roots


def journal_present(project, environ=None, platform=None):
    """True when any Manager job journal (main or console-chip) is waiting in any known location:
    the Manager would finish or undo that job the next time it runs, which can write flash."""
    return any((root / "chromatic-manager" / "journal.jsonl").exists()
               or (root / "chromatic-manager" / "fpga" / "journal.jsonl").exists()
               for root in data_roots(project, environ, platform))


def _count_backups(folder):
    try:
        return sum(1 for item in folder.iterdir()
                   if item.is_dir() and (item / "record.json").is_file()) if folder.is_dir() else 0
    except OSError:
        return 0


def backup_folders(project, environ=None, platform=None):
    """Folders that hold a backup record.json (contents never read), across the known locations."""
    seen, total = set(), 0
    for root in data_roots(project, environ, platform):
        folder = root / "chromatic-manager-backups"
        try:
            key = os.path.normcase(str(folder.resolve()))
        except OSError:
            key = os.path.normcase(str(folder))
        if key not in seen:
            seen.add(key)
            total += _count_backups(folder)
    return total


def second_copy_folders(environ=None):
    return _count_backups(second_copy(environ) / "manager")


def manager_python_candidates(config, project, environ=None, platform=None):
    """`managerPython` from the config when set; otherwise Setup-Jellymatic.ps1's venv in the
    per-user folder, then the older <project>/private/chromatic-tools/venv."""
    if config.get("managerPython"):
        return [Path(config["managerPython"])]
    windows = (platform or sys.platform) == "win32"
    tail = Path("Scripts") / "python.exe" if windows else Path("bin") / "python"
    candidates = [jellymatic_home(environ, platform) / "venv" / tail]
    if project:
        candidates.append(Path(project) / "private" / "chromatic-tools" / "venv" / tail)
    return candidates


def manager_python(config, project, environ=None, platform=None):
    """The first Manager Python that exists, or None."""
    return next((path for path in manager_python_candidates(config, project, environ, platform)
                 if path.is_file()), None)

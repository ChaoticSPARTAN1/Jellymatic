# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Entry point of the portable ChromaticManager.exe.

The exe carries Python, Qt and the libraries (esptool, pyserial, ...), but not the
manager itself: it finds this project folder and runs scripts/chromatic_manager_gui.py
and scripts/chromatic_manager.py from there. So the window always runs the same,
tested manager code as Chromatic-Manager.cmd, with the same journal, lock, backups and
pinned launcher release (your per-user Jellymatic folder and its second backup copy; see user_paths.py).

Where the project folder comes from, first match wins:
  1. --project <folder>
  2. project-root.txt next to the exe (written by the build)
  3. the folders above the exe
"""
import sys
from pathlib import Path


def is_project(folder):
    return (Path(folder) / 'scripts/chromatic_manager.py').is_file() and \
           (Path(folder) / 'scripts/chromatic_manager_gui.py').is_file()


def find_project(argv, here):
    if '--project' in argv:
        at = argv.index('--project')
        if at + 1 < len(argv):
            folder = Path(argv[at + 1])
            del argv[at:at + 2]
            return folder.resolve() if is_project(folder) else None
    note = here / 'project-root.txt'
    if note.is_file():
        folder = Path(note.read_text(encoding='utf-8').strip())
        if is_project(folder):
            return folder.resolve()
    for folder in (here, *here.parents):
        if is_project(folder):
            return folder
    return None


def main():
    argv = sys.argv[1:]
    executable = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve()
    here = executable.parent
    root = find_project(argv, here)
    if '--mcp' in argv or executable.stem.lower().endswith('mcp'):
        # ChromaticManagerMCP.exe: the MCP stdio server, no window and no Qt.
        if root is None:
            print('Chromatic Manager MCP: the project folder was not found (keep the exe in '
                  'the Jellymatic folder, or pass --project <folder>).', file=sys.stderr)
            return 1
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(root / 'scripts'))
        import chromatic_manager_mcp
        return chromatic_manager_mcp.main([a for a in argv if a != '--mcp'])
    if root is None:
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication(sys.argv[:1])
        QMessageBox.critical(None, 'Chromatic Manager',
                             'The Chromatic project folder was not found.\n\nKeep this program inside the project '
                             'folder, or start it with --project <folder>.')
        return 1
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root / 'scripts'))
    import chromatic_manager_gui
    return chromatic_manager_gui.main(argv, root)


if __name__ == '__main__':
    raise SystemExit(main())

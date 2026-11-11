# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
# PyInstaller spec for the portable Chromatic Manager (one folder, no installer).
# Build with scripts/Build-ChromaticManagerApp.ps1; output in dist/ (Git-ignored).
#
# The project's own modules are analysed (so every library they use is bundled) and then
# left OUT of the bundle: the exe loads them from the project's scripts folder at run
# time, so it always runs the same manager code, journal, lock and backups as the .cmd.
# One folder rather than one file keeps the Qt libraries (LGPL-3.0) replaceable.
#
# Remote Pilot is NOT bundled: the Remote Pilot tab (remote_pilot_link.py) finds Remote Pilot's own folder
# and starts its receiver with Remote Pilot's own Python (local\venv), so both stay one install each.
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

SCRIPTS = Path(SPECPATH).resolve()
OWN = {p.stem for p in SCRIPTS.glob('*.py')}

datas, binaries, hidden = [], [], []
# Open-licensed pixel fonts, if the owner added any (chromatic_theme loads them; their licence
# texts are copied to licenses/fonts by the build script).
FONTS = SCRIPTS / 'chromatic_gui_fonts'
if FONTS.is_dir() and any(FONTS.iterdir()):
    datas.append((str(FONTS), 'chromatic_gui_fonts'))
for package in ('esptool', 'serial'):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hidden += h

a = Analysis(
    [str(SCRIPTS / 'chromatic_manager_app.py')],
    pathex=[str(SCRIPTS)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden + ['chromatic_manager_gui', 'chromatic_manager', 'chromatic_manager_core',
                            'chromatic_manager_api', 'chromatic_manager_mcp', 'chromatic_theme', 'chromatic_lock',
                            'chromatic_jellyfin_link', 'connection_config', 'jellyfin_client',
                            'chromatic_trial_runner', 'chromatic_provision', 'wifi_gateway',
                            'compose_chromatic_image', 'verify_chromatic_backup', 'hardware_preflight',
                            'remote_pilot_link', 'jellyfin_discovery', 'user_paths', 'getpass', 'secrets', 'ctypes',
                            'http.server', 'http.client', 'socketserver', 'urllib.request'],
    excludes=['tkinter', 'PySide6.QtNetwork', 'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtPdf',
              'PySide6.QtOpenGL', 'PySide6.QtSvg', 'PySide6.QtVirtualKeyboard'],
    noarchive=False,
)
# Drop the project's own modules (but keep the entry script, which is not in a.pure).
a.pure = [entry for entry in a.pure if entry[0].split('.')[0] not in OWN]

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ChromaticManager',
    console=False,
    icon=os.environ.get('CHROMATIC_ICON') or None,  # drawn by chromatic_theme.icon_pixmap at build time
    upx=False,
)
# The same program as a console exe for MCP clients (Claude Code, Codex): they talk to it over
# stdin/stdout, which a windowed exe does not have. It runs chromatic_manager_mcp from scripts/.
mcp_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ChromaticManagerMCP',
    console=True,
    icon=os.environ.get('CHROMATIC_ICON') or None,
    upx=False,
)
coll = COLLECT(exe, mcp_exe, a.binaries, a.datas, name='ChromaticManager', upx=False)

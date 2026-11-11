@echo off
rem SPDX-License-Identifier: GPL-3.0-or-later
rem Additional terms: ADDITIONAL-TERMS.md
rem Opens the Chromatic Manager window from source, with the Python environment that
rem Setup-Jellymatic.ps1 made in your per-user Jellymatic folder.
setlocal
if defined JELLYMATIC_HOME (set "JM=%JELLYMATIC_HOME%") else (set "JM=%LOCALAPPDATA%\Jellymatic")
set "PY=%JM%\venv\Scripts\pythonw.exe"
if not exist "%PY%" (
    echo Chromatic Manager is not set up yet. Run this once:
    echo   powershell -ExecutionPolicy Bypass -File "%~dp0Setup-Jellymatic.ps1"
    pause
    exit /b 1
)
start "" "%PY%" -B "%~dp0scripts\chromatic_manager_gui.py" %*

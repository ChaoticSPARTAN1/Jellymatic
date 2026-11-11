@echo off
rem SPDX-License-Identifier: GPL-3.0-or-later
rem Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
rem Starts the Remote Pilot receiver (http://127.0.0.1:38764/).
rem Uses the private venv made by Setup-RemotePilot.ps1 when present: it has
rem pyserial, which the Chromatic preset needs for USB. Otherwise falls back to
rem the py launcher. The viewer listens on this PC only (127.0.0.1).
setlocal
cd /d "%~dp0"
if exist "local\venv\Scripts\python.exe" (
    "local\venv\Scripts\python.exe" receiver.py serve %*
    exit /b %errorlevel%
)
where py >nul 2>nul
if not errorlevel 1 (
    echo local\venv not found: using py -3. Chromatic USB needs pyserial in that Python ^(py -3 -m pip install pyserial^).
    py -3 receiver.py serve %*
    exit /b %errorlevel%
)
echo local\venv not found: using python. Chromatic USB needs pyserial in that Python ^(python -m pip install pyserial^).
python receiver.py serve %*

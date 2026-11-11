@echo off
rem SPDX-License-Identifier: GPL-3.0-or-later
rem Additional terms: ADDITIONAL-TERMS.md
rem Chromatic Manager, text version: status, backup, install/repair/remove the start-up menu,
rem restore stock, guided official update, Wi-Fi setup. No arguments: the text menu.
rem With arguments, one command, for example:  Chromatic-Manager.cmd status
setlocal
if defined JELLYMATIC_HOME (set "JM=%JELLYMATIC_HOME%") else (set "JM=%LOCALAPPDATA%\Jellymatic")
set "PY=%JM%\venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Chromatic Manager is not set up yet. Run this once:
    echo   powershell -ExecutionPolicy Bypass -File "%~dp0Setup-Jellymatic.ps1"
    pause
    exit /b 1
)
cd /d "%~dp0scripts"
if "%~1"=="" (
    "%PY%" -B chromatic_manager.py menu
) else (
    "%PY%" -B chromatic_manager.py %*
)
echo.
pause

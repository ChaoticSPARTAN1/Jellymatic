# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
# Setup-RemotePilot.ps1: one-time setup for Remote Pilot on Windows.
#
# What it does (and nothing else):
#   1. Checks that Python 3.11 or newer is installed.
#   2. Creates a private Python virtual environment in this folder's local\venv (Git ignores local\).
#   3. Installs requirements.txt (pyserial, for the USB link to the Chromatic) into it from PyPI with pip.
# It needs no admin rights, changes no system settings, never opens a USB device, never contacts a
# console, never uploads anything and asks for no password, key or token.
# Run it again at any time; it only adds what is missing.
#
# Usage:  right-click > Run with PowerShell, or
#         powershell -ExecutionPolicy Bypass -File Setup-RemotePilot.ps1
#         -Python <path>   use a specific python.exe
#         -SkipSerial      create the environment but do not install pyserial (Wi-Fi and simulators only)
param([string]$Python = '', [switch]$SkipSerial)
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot

if (-not $Python) {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { $Python = 'py'; $PyArgs = @('-3') } else { $Python = 'python'; $PyArgs = @() }
} else { $PyArgs = @() }
& $Python @PyArgs -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11 or newer is needed'"
if ($LASTEXITCODE) { throw 'Python 3.11 or newer is needed (https://www.python.org/downloads/).' }

$Local = Join-Path $Root 'local'
New-Item -ItemType Directory -Force $Local | Out-Null
$Venv = Join-Path $Local 'venv'
if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) {
    Write-Host 'Creating Remote Pilot''s private Python environment in local\venv ...'
    & $Python @PyArgs -m venv $Venv
    if ($LASTEXITCODE) { throw 'Could not create the virtual environment.' }
}
$VenvPy = Join-Path $Venv 'Scripts\python.exe'
if (-not $SkipSerial) {
    Write-Host 'Installing pyserial (USB support) from PyPI into local\venv ...'
    & $VenvPy -m pip install --disable-pip-version-check -r (Join-Path $Root 'requirements.txt')
    if ($LASTEXITCODE) { throw 'pip could not install requirements.txt. Wi-Fi and the simulators still work without it.' }
}

Write-Host ''
Write-Host 'Done. Start Remote Pilot with Start-RemotePilot.cmd, then open http://127.0.0.1:38764/ in your browser.'
Write-Host 'Your pairing key, settings and captures will stay in the local\ folder on this PC. Never share or commit it.'

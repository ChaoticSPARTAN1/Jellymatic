# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
# Setup-Jellymatic.ps1: one-time setup for Chromatic Manager on Windows.
#
# What it does (and nothing else):
#   1. Creates your per-user Jellymatic folder (%LOCALAPPDATA%\Jellymatic, or $env:JELLYMATIC_HOME).
#   2. Creates a Python virtual environment in it (venv\) and installs requirements.txt
#      (PySide6-Essentials, esptool, pyserial) from PyPI with pip.
#   3. Creates a second virtual environment for the bundled Remote Pilot (remote-pilot\local\venv),
#      which needs only pyserial.
# It never opens a USB device, never writes to a console, never uploads anything and asks for no
# password or token. Run it again at any time; it only adds what is missing.
#
# Usage:  powershell -ExecutionPolicy Bypass -File Setup-Jellymatic.ps1
param([string]$Python = '')
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$Home_ = if ($env:JELLYMATIC_HOME) { $env:JELLYMATIC_HOME } else { Join-Path $env:LOCALAPPDATA 'Jellymatic' }
New-Item -ItemType Directory -Force $Home_ | Out-Null
Write-Host "Your Jellymatic folder: $Home_"

if (-not $Python) {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { $Python = 'py'; $PyArgs = @('-3') } else { $Python = 'python'; $PyArgs = @() }
} else { $PyArgs = @() }
& $Python @PyArgs -c "import sys; assert sys.version_info >= (3, 12), 'Python 3.12 or newer is needed'"
if ($LASTEXITCODE) { throw 'Python 3.12 or newer is needed (https://www.python.org/downloads/).' }

$Venv = Join-Path $Home_ 'venv'
if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) {
    Write-Host 'Creating the Manager''s Python environment...'
    & $Python @PyArgs -m venv $Venv
    if ($LASTEXITCODE) { throw 'Could not create the virtual environment.' }
}
$VenvPy = Join-Path $Venv 'Scripts\python.exe'
& $VenvPy -m pip install --disable-pip-version-check -r (Join-Path $Root 'requirements.txt')
if ($LASTEXITCODE) { throw 'pip could not install requirements.txt.' }

$Rp = Join-Path $Root 'remote-pilot\local\venv'
if (-not (Test-Path (Join-Path $Rp 'Scripts\python.exe'))) {
    Write-Host 'Creating Remote Pilot''s Python environment...'
    & $Python @PyArgs -m venv $Rp
    if ($LASTEXITCODE) { throw 'Could not create Remote Pilot''s virtual environment.' }
}
& (Join-Path $Rp 'Scripts\python.exe') -m pip install --disable-pip-version-check -r (Join-Path $Root 'remote-pilot\requirements.txt')
if ($LASTEXITCODE) { throw 'pip could not install pyserial for Remote Pilot.' }

Write-Host ''
Write-Host 'Done. Start Chromatic Manager with Chromatic-Manager-Window.cmd (window) or Chromatic-Manager.cmd (text menu).'
Write-Host 'The start-up menu image must be built first, or copied from a release: see docs/Install-with-Chromatic-Manager.md.'

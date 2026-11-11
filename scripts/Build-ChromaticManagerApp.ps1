# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
# Build-ChromaticManagerApp.ps1 (optional): makes a portable Chromatic Manager for YOUR OWN use:
# dist\ChromaticManager\ChromaticManager.exe and ChromaticManagerMCP.exe (one folder, no installer).
# The exe carries Python, Qt and the libraries; the Manager's own code still runs from this folder's
# scripts\ (project-root.txt next to the exe says where), so fixes need no rebuild.
#
# Uses the venv made by Setup-Jellymatic.ps1 and adds PyInstaller to it. Touches no device.
# If you SHARE the result, you take on the bundled libraries' licence duties (Qt/PySide6 LGPL-3.0:
# keep the Qt DLLs replaceable, ship their licences and say where their source is; esptool GPL-2.0+;
# Python PSF). The licences are copied to dist\ChromaticManager\licenses for you.
param([string]$PyInstallerVersion = '6.22.3')
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$JmHome = if ($env:JELLYMATIC_HOME) { $env:JELLYMATIC_HOME } else { Join-Path $env:LOCALAPPDATA 'Jellymatic' }
$Python = Join-Path $JmHome 'venv\Scripts\python.exe'
if (-not (Test-Path $Python)) { throw "Run Setup-Jellymatic.ps1 first ($Python is missing)." }
& $Python -m pip install --disable-pip-version-check "pyinstaller==$PyInstallerVersion"
if ($LASTEXITCODE) { throw 'pip could not install PyInstaller.' }

$Out = Join-Path $Root 'dist'
$App = Join-Path $Out 'ChromaticManager'
if (Get-Process -Name ChromaticManager, ChromaticManagerMCP -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path.StartsWith($Out, [StringComparison]::OrdinalIgnoreCase) }) {
    throw 'Chromatic Manager is running from dist. Close it first.'
}
New-Item -ItemType Directory -Force $Out | Out-Null
$Icon = Join-Path $Out 'chromatic_manager.ico'
$env:QT_QPA_PLATFORM = 'offscreen'
& $Python -B -c "import sys; sys.path.insert(0, r'$PSScriptRoot'); from PySide6.QtGui import QGuiApplication; app = QGuiApplication([]); import chromatic_theme; assert chromatic_theme.icon_pixmap().save(r'$Icon', 'ICO')"
if ($LASTEXITCODE) { throw 'Icon drawing failed.' }
Remove-Item Env:QT_QPA_PLATFORM
$env:CHROMATIC_ICON = $Icon
& $Python -m PyInstaller --noconfirm --clean --log-level WARN --distpath $Out --workpath (Join-Path $Out 'build') (Join-Path $PSScriptRoot 'chromatic_manager_app.spec')
if ($LASTEXITCODE) { throw "PyInstaller failed ($LASTEXITCODE)." }

$Licenses = Join-Path $App 'licenses'
New-Item -ItemType Directory -Force $Licenses | Out-Null
Copy-Item (Join-Path $PSScriptRoot 'chromatic_gui_licenses\*') $Licenses -Force
Copy-Item (Join-Path $Root 'LICENSE') (Join-Path $Licenses 'Jellymatic-LICENSE.txt') -Force
Copy-Item (Join-Path $Root 'ADDITIONAL-TERMS.md') $Licenses -Force
$Fonts = Join-Path $PSScriptRoot 'chromatic_gui_fonts'
New-Item -ItemType Directory -Force (Join-Path $Licenses 'fonts') | Out-Null
Get-ChildItem $Fonts -File | Where-Object { $_.Name -match '^(OFL|README)' } | Copy-Item -Destination (Join-Path $Licenses 'fonts') -Force
$Site = (& $Python -c "import sysconfig; print(sysconfig.get_paths()['purelib'])").Trim()
foreach ($info in Get-ChildItem $Site -Directory -Filter '*.dist-info') {
    $name = ($info.Name -replace '-[^-]+\.dist-info$', '').ToLower()
    if ($name -in @('pip', 'setuptools')) { continue }
    $files = Get-ChildItem $info.FullName -Recurse -File | Where-Object { $_.Name -match '^(LICEN[CS]E|COPYING|NOTICE|AUTHORS)' -or $_.Name -like 'LicenseRef-*' }
    if ($files) {
        $target = Join-Path $Licenses $name
        New-Item -ItemType Directory -Force $target | Out-Null
        foreach ($file in $files) { Copy-Item $file.FullName (Join-Path $target $file.Name) -Force }
    }
}
$PythonHome = (& $Python -c "import sys; print(sys.base_prefix)").Trim()
New-Item -ItemType Directory -Force (Join-Path $Licenses 'python') | Out-Null
Copy-Item (Join-Path $PythonHome 'LICENSE.txt') (Join-Path $Licenses 'python\LICENSE.txt') -Force
# Where this folder is, for when the exe is started from a shortcut (local only; dist\ is ignored by Git).
Set-Content -Path (Join-Path $App 'project-root.txt') -Value $Root -Encoding UTF8
Write-Host "Built $App\ChromaticManager.exe (for your own use; see the note at the top before sharing it)."

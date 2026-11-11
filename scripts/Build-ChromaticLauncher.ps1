# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
# Build-ChromaticLauncher.ps1: compiles the start-up menu (custom bootloader, partition table and
# launcher app) with ESP-IDF v5.3 and puts the three files where Chromatic Manager installs them
# from: release\launcher\ (bootloader\bootloader.bin, partition_table\partition-table.bin,
# chromatic_launcher.bin).
#
# Nothing here opens a serial port or touches a device: idf.py is only ever asked to `build`.
# (The project also makes idf.py's own flash targets fail on purpose: they would overwrite the
# console's stock firmware.)
#
# Needs: ESP-IDF v5.3 (commit in upstream.lock.json) at -IdfPath (default upstream\esp-idf, made by
# scripts\Get-Upstreams.ps1), and its tools installed with ESP-IDF's own install.ps1 into
# -IdfToolsPath (default <your Jellymatic folder>\tools\esp32-tools).
param(
    [string]$IdfPath = '',
    [string]$IdfToolsPath = '',
    [string]$Python = '',
    [string]$OutDir = ''
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Lock = Get-Content -LiteralPath (Join-Path $Root 'upstream.lock.json') -Raw | ConvertFrom-Json
if (-not $IdfPath) { $IdfPath = Join-Path $Root 'upstream\esp-idf' }
$JmHome = if ($env:JELLYMATIC_HOME) { $env:JELLYMATIC_HOME } else { Join-Path $env:LOCALAPPDATA 'Jellymatic' }
$JmTools = if ($env:JELLYMATIC_TOOLS) { $env:JELLYMATIC_TOOLS } else { Join-Path $JmHome 'tools' }
if (-not $IdfToolsPath) { $IdfToolsPath = Join-Path $JmTools 'esp32-tools' }
if (-not (Test-Path (Join-Path $IdfPath 'tools\idf.py'))) { throw "ESP-IDF not found at $IdfPath (run scripts\Get-Upstreams.ps1 -EspIdf)." }
$actual = & git -C $IdfPath rev-parse HEAD
if ($LASTEXITCODE -or $actual -ne $Lock.esp_idf.commit) { throw "ESP-IDF at $IdfPath is not the pinned commit $($Lock.esp_idf.commit)." }

$env:IDF_PATH = $IdfPath
$env:IDF_TOOLS_PATH = $IdfToolsPath
if (-not $Python) { $Python = (Get-Command python -ErrorAction Stop).Source }
$originalPath = $env:PATH
$exports = & $Python "$IdfPath\tools\idf_tools.py" export --format key-value
if ($LASTEXITCODE) { throw "ESP-IDF tools are not installed in $IdfToolsPath (run $IdfPath\install.ps1 esp32 with IDF_TOOLS_PATH set)." }
foreach ($line in $exports) {
    if ($line -match '^([A-Z_]+)=(.*)$') {
        $name = $Matches[1]; $value = $Matches[2].Replace('%PATH%', $originalPath)
        if ($name -in @('PATH', 'IDF_PYTHON_ENV_PATH', 'ESP_ROM_ELF_DIR', 'IDF_CCACHE_ENABLE', 'ESP_IDF_VERSION')) {
            [Environment]::SetEnvironmentVariable($name, $value, 'Process')
        }
    }
}
$Project = Join-Path $Root 'chromatic\launcher'
$Work = if ($OutDir) { [IO.Path]::GetFullPath($OutDir) } else { Join-Path $Root 'staging\chromatic-launcher' }
New-Item -ItemType Directory -Force $Work | Out-Null
$IdfPython = Join-Path $env:IDF_PYTHON_ENV_PATH 'Scripts\python.exe'
$Log = Join-Path $Work 'compile.log'
cmd /c "`"$IdfPython`" `"$IdfPath\tools\idf.py`" -C `"$Project`" -B `"$Work\build`" -D SDKCONFIG=`"$Work\sdkconfig`" -D IDF_TARGET=esp32 build > `"$Log`" 2>&1"
if ($LASTEXITCODE) { throw "Launcher build failed; see $Log" }
# IDF leaves esptool argument files that would write this app over the stock firmware: make them unusable.
& $Python -B -c "import sys; sys.path.insert(0, r'$Root\scripts'); from compose_chromatic_image import poison_flash_args; poison_flash_args(r'$Work\build')"
if ($LASTEXITCODE) { throw 'Could not neutralise the esptool argument files.' }

$Release = Join-Path $Root 'release\launcher'
New-Item -ItemType Directory -Force (Join-Path $Release 'bootloader'), (Join-Path $Release 'partition_table') | Out-Null
Copy-Item (Join-Path $Work 'build\bootloader\bootloader.bin') (Join-Path $Release 'bootloader\bootloader.bin') -Force
Copy-Item (Join-Path $Work 'build\partition_table\partition-table.bin') (Join-Path $Release 'partition_table\partition-table.bin') -Force
Copy-Item (Join-Path $Work 'build\chromatic_launcher.bin') (Join-Path $Release 'chromatic_launcher.bin') -Force
Write-Output "Built. Chromatic Manager will install the start-up menu from $Release. No device was touched."

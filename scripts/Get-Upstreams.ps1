# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
# Get-Upstreams.ps1: downloads (git clone) the exact upstream sources Jellymatic builds on, at the
# commits pinned in upstream.lock.json, into upstream\ (ignored by Git). Read-only use: the build
# scripts copy from them and never edit them.
#
#   -ChromaticFpga  ModRetro's Chromatic FPGA design (GPL-3.0) with its MiSTer core submodule.
#                   Needed to build the console chip (FPGA) images 18.40 / 18.41.
#   -EspIdf         Espressif ESP-IDF v5.3 (Apache-2.0) with submodules. Needed to build the start-up menu.
#
# It only runs `git clone` / `git checkout` against github.com. Nothing is uploaded, no device is touched.
param([switch]$ChromaticFpga, [switch]$EspIdf)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Lock = Get-Content -LiteralPath (Join-Path $Root 'upstream.lock.json') -Raw | ConvertFrom-Json
$Up = Join-Path $Root 'upstream'
New-Item -ItemType Directory -Force $Up | Out-Null
if (-not ($ChromaticFpga -or $EspIdf)) { $ChromaticFpga = $true; $EspIdf = $true }

function Get-Pinned($Url, $Commit, $Folder, $Recursive) {
    if (-not (Test-Path (Join-Path $Folder '.git'))) {
        git clone $Url $Folder
        if ($LASTEXITCODE) { throw "git clone $Url failed" }
    }
    git -C $Folder fetch --tags origin
    git -C $Folder checkout --detach $Commit
    if ($LASTEXITCODE) { throw "checkout of $Commit failed in $Folder" }
    if ($Recursive) {
        git -C $Folder submodule update --init --recursive
        if ($LASTEXITCODE) { throw "submodules failed in $Folder" }
    }
    $head = git -C $Folder rev-parse HEAD
    if ($head -ne $Commit) { throw "$Folder is at $head, expected $Commit" }
    Write-Host "OK  $Folder at $Commit"
}
if ($ChromaticFpga) { Get-Pinned $Lock.chromatic_fpga.repository $Lock.chromatic_fpga.commit (Join-Path $Up 'chromatic-fpga') $true }
if ($EspIdf) { Get-Pinned $Lock.esp_idf.repository $Lock.esp_idf.commit (Join-Path $Up 'esp-idf') $true }

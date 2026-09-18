# ===========================================================================
#  Create the private QQ copy that NapCat runs on
# ===========================================================================
#  Why this exists: Windows tells applications apart by executable PATH, so a
#  second QQ instance needs a QQ at a different path. Copying the whole
#  installation would waste ~1.1 GB - QQ's versions\<ver>\ folder is written
#  once at install time and essentially never again.
#
#  So we build a tiny copy: the ~8 MB of top-level files are real, and
#  versions\<ver>\ is a junction into the QQ you already have installed.
#
#  That also means **this script is the whole reason we never ship QQ in the
#  release package** - the copy is generated locally from your own install,
#  which is both legal and 8 MB instead of 1.1 GB.
#
#  Usage:
#      cd app
#      powershell -ExecutionPolicy Bypass -File _setup-qq-copy.ps1
#
#  Safe to re-run. It only ever reads from your QQ installation; the junction
#  points into it, it never writes there.
#
#  KEEP THIS FILE PURE ASCII - see "PowerShell script encoding" in the
#  developer docs (docs/, section 3.7).
# ===========================================================================

param(
    [string]$Target = 'qq-napcat-private',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$dest = Join-Path $here $Target

Write-Host '=== locating your QQ installation ==='

$qqRoot = $null
try {
    $key = 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ'
    $raw = (Get-ItemProperty -Path $key -ErrorAction Stop).UninstallString
    if ($raw) { $qqRoot = Split-Path -Parent ($raw.Trim('"')) }
} catch { }
if (-not $qqRoot -or -not (Test-Path $qqRoot)) {
    throw 'QQ not found in the registry. Install QQ first, or copy the private QQ folder from someone else.'
}
Write-Host "    QQ root : $qqRoot"

$srcVers = Join-Path $qqRoot 'versions'
if (-not (Test-Path $srcVers)) { throw "No versions folder under $qqRoot" }

$newest = Get-ChildItem -Path $srcVers -Directory -ErrorAction SilentlyContinue |
          Sort-Object -Property Name -Descending | Select-Object -First 1
if (-not $newest) { throw "No version folder under $srcVers" }
Write-Host "    version : $($newest.Name)"

# --- refuse to clobber a working copy unless asked -------------------------
# Note the junction lives at versions\<ver>, NOT at the top level - checking
# only the top level would never find it and we would silently rebuild every
# time.
$oldVers = Join-Path $dest 'versions'
if (Test-Path $dest) {
    $hasJunction = @(Get-ChildItem -Path $oldVers -Force -ErrorAction SilentlyContinue |
                     Where-Object { $_.LinkType -eq 'Junction' })
    if ($hasJunction.Count -gt 0 -and -not $Force) {
        Write-Host ''
        Write-Host "$Target already exists and looks set up. Nothing to do."
        Write-Host 'Re-run with -Force to rebuild it.'
        exit 0
    }

    Write-Host "    removing existing $Target ..."
    # CRITICAL: pull the junctions out with rmdir BEFORE Remove-Item -Recurse.
    # rmdir on a junction removes only the link. Remove-Item -Recurse can walk
    # INTO a junction on older PowerShell and delete the target's contents -
    # which here would be your real QQ installation.
    if (Test-Path $oldVers) {
        Get-ChildItem $oldVers -Force | Where-Object { $_.LinkType -eq 'Junction' } |
            ForEach-Object { cmd /c rmdir "$($_.FullName)" 2>&1 | Out-Null }
    }
    Remove-Item $dest -Recurse -Force
}

# --- 1. the small top-level files are real copies --------------------------
Write-Host '=== copying top-level files (~8 MB) ==='
New-Item -ItemType Directory -Path $dest -Force | Out-Null

$copied = 0
foreach ($item in (Get-ChildItem -Path $qqRoot -Force)) {
    if ($item.Name -eq 'versions') { continue }
    # Skip anything that is not part of running QQ
    if ($item.Name -match '^(Uninstall|QQUninstall)') { continue }
    try {
        Copy-Item $item.FullName -Destination $dest -Recurse -Force -ErrorAction Stop
        $copied++
    } catch {
        Write-Host "    (skipped $($item.Name): $($_.Exception.Message))"
    }
}
Write-Host "    $copied items copied"

# --- 2. versions\ keeps the tiny mutable json files, junction for the rest --
Write-Host '=== linking versions ==='
$destVers = Join-Path $dest 'versions'
New-Item -ItemType Directory -Path $destVers -Force | Out-Null

# Only the tiny json files. QQ keeps a downloaded update package in here too
# (a ~79 MB .zip) and copying that would bloat the "8 MB" copy tenfold.
foreach ($json in (Get-ChildItem -Path $srcVers -File -Filter '*.json' -ErrorAction SilentlyContinue)) {
    Copy-Item $json.FullName -Destination $destVers -Force
}

$linkPath = Join-Path $destVers $newest.Name
New-Item -ItemType Junction -Path $linkPath -Target $newest.FullName | Out-Null
Write-Host "    junction: $($newest.Name) -> $($newest.FullName)"

# --- 3. verify -------------------------------------------------------------
Write-Host '=== verifying ==='
$ok = $true
foreach ($f in @('QQ.exe', "versions\$($newest.Name)\QQNT.dll")) {
    $p = Join-Path $dest $f
    $exists = Test-Path $p
    Write-Host ("    {0,-46} {1}" -f $f, $(if ($exists) { 'ok' } else { 'MISSING' }))
    if (-not $exists) { $ok = $false }
}

$size = (Get-ChildItem $dest -Recurse -File -Force -ErrorAction SilentlyContinue |
         Measure-Object Length -Sum).Sum
Write-Host ''
Write-Host ("size: {0:N1} MB  (a full copy would be about 1100 MB)" -f ($size / 1MB))

if ($ok) {
    Write-Host 'Done. Your own QQ keeps running normally - NapCat gets its own instance.'
} else {
    Write-Host 'Something is missing. Check the QQ installation and re-run with -Force.'
    exit 1
}

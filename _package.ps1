# ===========================================================================
#  Build the release zip that gets attached to a GitHub Release
# ===========================================================================
#  What goes in:
#      <name>.exe           the PyInstaller build
#      <quickstart>.txt     offline quick start (Chinese filename)
#      app/                 this repo's sources, docs and icon assets
#
#  What NEVER goes in (and why):
#      app/qq-napcat-private/  Tencent's QQ binaries. Uploading them is
#                              copyright infringement and gets the repo taken
#                              down. The recipient generates this locally by
#                              running _setup-qq-copy.ps1 against their own QQ.
#      app/napcat/             NapCat is ~90 MB and updates often; the user
#                              downloads it from the official Releases.
#      app/config.json         contains real group IDs
#      app/_account.txt        contains the bot's QQ number
#      app/logs/, *.bak        runtime junk and private data
#      app/_privacy.txt        the pre-commit needle list ITSELF - real room ID,
#                              bot QQ numbers, control-port tokens, local paths.
#                              Shipping it publishes exactly what it exists to
#                              keep out. It did once: the v1.7.5 asset carried
#                              it. Note the netdisk script's blanket "_*" rule
#                              cannot be copied here - the standard package
#                              needs _setup-qq-copy.ps1, _fix-qq-link.ps1 and
#                              _find-python.bat at run time.
#
#  Usage:
#      powershell -ExecutionPolicy Bypass -File _package.ps1 -Exe <path to exe>
#
#  KEEP THIS FILE PURE ASCII - see "PowerShell script encoding" in the docs.
# ===========================================================================

param(
    [string]$Version,
    [string]$Exe,
    [string]$Out
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

# --- version ---------------------------------------------------------------
if (-not $Version) {
    $src = Get-Content (Join-Path $root 'live_notify.py') -Raw -Encoding UTF8
    $m = [regex]::Match($src, 'VERSION\s*=\s*"([^"]+)"')
    if (-not $m.Success) { throw 'VERSION not found in live_notify.py' }
    $Version = $m.Groups[1].Value
}

if (-not $Exe) {
    throw 'Pass the built exe: -Exe "..\..\path\to\build.exe"'
}
if (-not (Test-Path $Exe)) { throw "exe not found: $Exe" }
$Exe = (Resolve-Path $Exe).Path
$exeDir = Split-Path -Parent $Exe
$exeName = Split-Path -Leaf $Exe

if (-not $Out) {
    $Out = Join-Path $root ("dist\dafeiyu-live-notify-v{0}.zip" -f $Version)
}
$Out = [System.IO.Path]::GetFullPath($Out)

# The root folder inside the zip carries NO version number, on purpose:
# Scoop's extract_dir does not do $version substitution, so a versioned folder
# would force a manifest edit on every release. The version still lives in the
# zip filename, the Release page and the CHANGELOG.
#
# Do NOT put Chinese in this file. PowerShell 5.1 decodes BOM-less scripts as
# ANSI; an odd number of high bytes on a line swallows the trailing newline and
# the next line of real code turns into a comment. That already happened once
# here, silently.
$folderName = 'dafeiyu-live-notify'
Write-Host "version : $Version"
Write-Host "exe     : $Exe"
Write-Host "output  : $Out"

# --- stage -----------------------------------------------------------------
$stage = Join-Path ([System.IO.Path]::GetTempPath()) ("dfypkg_" + [guid]::NewGuid().ToString('N'))
$pkgRoot = Join-Path $stage $folderName
$pkgApp = Join-Path $pkgRoot 'app'
New-Item -ItemType Directory -Path $pkgApp -Force | Out-Null

try {
    # the exe sits NEXT TO app\ - _resolve_data_dir() looks for it there
    Copy-Item $Exe -Destination $pkgRoot -Force
    # The quick-start file has a Chinese name. Build it from code points so
    # this script stays pure ASCII and immune to the BOM-stripping problem.
    $quickStart = (([string][char]0x4F7F + [char]0x7528 + [char]0x8BF4 + [char]0x660E) + '.txt')
    foreach ($extra in @($quickStart)) {
        $p = Join-Path $exeDir $extra
        if (Test-Path $p) { Copy-Item $p -Destination $pkgRoot -Force }
    }

    # app\ = this repo, minus everything private or third-party.
    # 'build' is PyInstaller's work directory - it contains a copy of the whole
    # app (.pkg, .pyz, .toc) and adds ~12 MB to the zip for no reason. It is
    # gitignored, so it only ever lands here when the exe was built inside the
    # repo, which is exactly what happened once.
    $denyDirs = @('.git', '.github', 'dist', 'build', 'napcat', 'qq-napcat',
                  'qq-napcat-private', 'logs', '__pycache__', '_qqcopy_test')
    $denyFiles = @('config.json', '_account.txt', 'gui-error.log',
                   '.gitignore', '.gitattributes', '_privacy.txt')
    $denyExt = @('.bak', '.pyc', '.exe', '.zip')

    $copied = 0
    foreach ($item in (Get-ChildItem -Path $root -Force)) {
        if ($denyDirs -contains $item.Name) { continue }
        if ($denyFiles -contains $item.Name) { continue }
        if ($denyExt -contains $item.Extension) { continue }
        Copy-Item $item.FullName -Destination $pkgApp -Recurse -Force
        $copied++
    }
    Write-Host "staged $copied top-level items into app\"

    # Repo-facing assets the end user has no use for.
    foreach ($junk in @('social-preview.png', 'icon-preview.png', 'banner-art.png')) {
        Remove-Item (Join-Path $pkgApp "_build\$junk") -Force -ErrorAction SilentlyContinue
    }

    # defensive: make sure nothing forbidden slipped through
    $bad = @()
    foreach ($name in $denyDirs + $denyFiles) {
        $p = Join-Path $pkgApp $name
        if (Test-Path $p) { $bad += $name }
    }
    $strayExe = Get-ChildItem $pkgApp -Recurse -File -Filter '*.exe' -ErrorAction SilentlyContinue
    if ($strayExe) { $bad += ($strayExe | ForEach-Object { $_.Name }) }
    # Deny-listing _privacy.txt is not enough on its own - a rename would walk
    # straight past a name check. Match the shape as well.
    $strayPrivate = Get-ChildItem $pkgApp -Recurse -File -Filter '_privacy*' -ErrorAction SilentlyContinue
    if ($strayPrivate) { $bad += ($strayPrivate | ForEach-Object { $_.Name }) }
    # Same reasoning for the login cookie (subscribe.sessdata). config.json is
    # deny-listed, but a hand-made copy ("config - copy.json") would sail past
    # a name check - and that file is a live Bilibili credential, not a preference.
    # Match a FILLED value only: the bare key name also appears in
    # config.example.json, and a case-insensitive match on "sessdata" would
    # refuse to package the example - which it did, once.
    $sessShape = '("sessdata"\s*:\s*"[^"]{8,}"|SESSDATA%3D|SESSDATA=[A-Za-z0-9%])'
    $straySess = Get-ChildItem $pkgApp -Recurse -File -Include '*.json', '*.txt' -ErrorAction SilentlyContinue |
        Where-Object { (Get-Content $_.FullName -Raw -ErrorAction SilentlyContinue) -cmatch $sessShape }
    if ($straySess) { $bad += ($straySess | ForEach-Object { $_.Name + ' (contains SESSDATA)' }) }
    if ($bad.Count -gt 0) {
        throw ("refusing to package - these must not ship: " + ($bad -join ', '))
    }

    # --- zip ---------------------------------------------------------------
    New-Item -ItemType Directory -Path (Split-Path -Parent $Out) -Force | Out-Null
    if (Test-Path $Out) { Remove-Item $Out -Force }
    Compress-Archive -Path $pkgRoot -DestinationPath $Out -CompressionLevel Optimal

    $size = (Get-Item $Out).Length
    Write-Host ''
    Write-Host ("done: {0:N2} MB" -f ($size / 1MB))
    Write-Host "      $Out"
}
finally {
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
}

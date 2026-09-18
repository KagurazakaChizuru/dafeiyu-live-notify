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

$folderName = "dafeiyu-live-notify-v$Version"
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

    # app\ = this repo, minus everything private or third-party
    $denyDirs = @('.git', '.github', 'dist', 'napcat', 'qq-napcat',
                  'qq-napcat-private', 'logs', '__pycache__', '_qqcopy_test')
    $denyFiles = @('config.json', '_account.txt', 'gui-error.log',
                   '.gitignore', '.gitattributes')
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
    foreach ($junk in @('social-preview.png', 'icon-preview.png')) {
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

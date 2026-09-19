# ===========================================================================
#  Build the netdisk (portable share) package
# ===========================================================================
#  Difference from _package.ps1:
#
#    _package.ps1   -> GitHub Release. NapCat is NOT included; the user
#                      downloads it themselves. Small, but two manual steps.
#
#    this script    -> netdisk share. NapCat IS included, so the recipient
#                      only runs one file. Bigger, but "extract and go".
#
#  Two things are deliberately NEVER included, and the reasons matter:
#
#    qq-napcat-private\   It is built from the recipient's own QQ install.
#                         Shipping it would mean shipping Tencent's binaries.
#                         _setup-qq-copy.ps1 exists precisely to avoid that.
#
#    napcat\config,       These carry the packager's own account numbers as
#    napcat\cache         file names, the WebUI token, and a login QR code.
#                         Sharing them would leak the packager's account.
#
#  NapCat's LICENSE must travel with it - its redistribution terms require
#  the license text to be included. The script checks for it and refuses to
#  build without it.
#
#  Keep this file PURE ASCII - see _find-python.bat for the reason.
# ===========================================================================
param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [string]$Out  = (Join-Path (Split-Path -Parent $PSScriptRoot) 'dist\dafeiyu-netdisk.zip')
)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path $Root).Path
$App  = Join-Path $Root 'app'
$Exe  = Join-Path $Root '*.exe'

$exeFile = Get-ChildItem $Exe -File | Select-Object -First 1
if (-not $exeFile) { throw "no .exe found in $Root" }
Write-Host "exe     : $($exeFile.FullName)"

$napcatLicense = Join-Path $App 'napcat\LICENSE'
if (-not (Test-Path $napcatLicense)) {
    throw "app\napcat\LICENSE is missing. NapCat's license requires the text to be redistributed with it."
}

$stage = Join-Path $env:TEMP 'dfy-netdisk\大肥鱼直播姬'
if (Test-Path (Split-Path $stage -Parent)) { Remove-Item (Split-Path $stage -Parent) -Recurse -Force }
New-Item (Join-Path $stage 'app\_build') -ItemType Directory -Force | Out-Null

# --- top level -------------------------------------------------------------
Copy-Item $exeFile.FullName $stage -Force
foreach ($f in @('首次使用-点我.bat', '使用说明.txt', 'LICENSE', 'README.md')) {
    $p = Join-Path $App $f
    if (Test-Path $p) { Copy-Item $p $stage -Force }
}

# --- NapCat (minus the packager's own account data) ------------------------
$napDst = Join-Path $stage 'app\napcat'
New-Item $napDst -ItemType Directory -Force | Out-Null
Get-ChildItem (Join-Path $App 'napcat') -Force |
    Where-Object { $_.Name -notin @('config', 'cache', 'logs') } |
    ForEach-Object {
        if ($_.PSIsContainer) { Copy-Item $_.FullName $napDst -Recurse -Force }
        else { Copy-Item $_.FullName $napDst -Force }
    }

# --- sources and assets ----------------------------------------------------
$skip = @('config.json', 'config.json.bak', '_account.txt', 'gui-error.log',
          'gui.py.bak', 'header-light.png', 'header-dark.png')
Get-ChildItem $App -File |
    Where-Object { $_.Name -notin $skip -and $_.Extension -notin @('.bak', '.log', '.spec') } |
    ForEach-Object { Copy-Item $_.FullName (Join-Path $stage 'app') -Force }

# only what the app actually needs at run time - not the build scripts or spec
foreach ($f in @('app.ico', 'header-light.png', 'header-dark.png')) {
    $p = Join-Path $App "_build\$f"
    if (Test-Path $p) { Copy-Item $p (Join-Path $stage "app\_build\$f") -Force }
}
if (Test-Path (Join-Path $App 'docs')) {
    Copy-Item (Join-Path $App 'docs') (Join-Path $stage 'app\docs') -Recurse -Force
}

# --- zip -------------------------------------------------------------------
$outDir = Split-Path $Out -Parent
if (-not (Test-Path $outDir)) { New-Item $outDir -ItemType Directory -Force | Out-Null }
if (Test-Path $Out) { Remove-Item $Out -Force }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    (Split-Path $stage -Parent), $Out,
    [System.IO.Compression.CompressionLevel]::Optimal, $false)

$size = [math]::Round((Get-Item $Out).Length / 1MB, 2)
Write-Host ""
Write-Host "done: $size MB"
Write-Host "      $Out"

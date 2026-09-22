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
    [string]$Out  = (Join-Path ([Environment]::GetFolderPath('Desktop')) 'dafeiyu-netdisk.zip')
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
# Skip backup files. **config.json.bak especially** - it is a copy of the
# packager's own config and carries a real room id. It shipped once already.
$skip = @('config.json', '_account.txt', 'gui-error.log',
          'header-light.png', 'header-dark.png')

# Underscore files fall into TWO groups and must NOT be filtered with one "_*"
# wildcard:
#
#   private / dev-only   _privacy.txt (real room id + control token),
#                        _account.txt, _selftest.py, _watchtest.py,
#                        _mock_napcat.py, _package*.ps1, _release.ps1,
#                        _publish-netdisk.ps1, _submit-winget.ps1,
#                        _ui_text.py                       -> never shipped
#
#   needed at run time   the four below. Without them the recipient cannot even
#                        take the first step:
#                        "first-run" bat calls app\_setup-qq-copy.ps1;
#                        every other bat starts with "call _find-python.bat";
#                        0-one-click.bat also calls _ensure-napcat.bat.
#
# The previous netdisk package used a blanket "_*" rule and dropped both groups.
# A user double-clicked the first-run bat and got "-File argument does not
# exist: _setup-qq-copy.ps1" - reported with a screenshot.
#
# KEEP THE COMMENTS IN THIS FILE ASCII - see "PowerShell script encoding" in
# the docs. Chinese comments in a BOM-less .ps1 get decoded as ANSI by
# PowerShell 5.1 and the parser dies. I did exactly that once while fixing this.
$keepUnder = @('_setup-qq-copy.ps1', '_fix-qq-link.ps1', '_find-python.bat',
               '_ensure-napcat.bat', '_account.txt.example')
Get-ChildItem $App -File |
    Where-Object {
        $_.Name -notin $skip -and
        ($_.Name -notlike '_*' -or $_.Name -in $keepUnder) -and
        $_.Name -notlike 'config.json*' -and
        $_.Extension -notin @('.bak', '.log', '.spec')
    } |
    ForEach-Object { Copy-Item $_.FullName (Join-Path $stage 'app') -Force }

# Fail the build if a run-time script is missing, or a private file got in.
# "User finds out after downloading" is too late to notice.
foreach ($need in @('_setup-qq-copy.ps1', '_fix-qq-link.ps1', '_find-python.bat',
                    '_ensure-napcat.bat')) {
    if (-not (Test-Path (Join-Path $stage "app\$need"))) {
        throw "netdisk package would ship without app\$need - the recipient cannot set it up."
    }
}
foreach ($bad in @('_privacy.txt', '_account.txt', 'config.json')) {
    if (Test-Path (Join-Path $stage "app\$bad")) {
        throw "netdisk package must not contain app\$bad"
    }
}

# only what the app actually needs at run time - not the build scripts or spec
foreach ($f in @('app.ico', 'header-light.png', 'header-dark.png')) {
    $p = Join-Path $App "_build\$f"
    if (Test-Path $p) { Copy-Item $p (Join-Path $stage "app\_build\$f") -Force }
}
if (Test-Path (Join-Path $App 'docs')) {
    Copy-Item (Join-Path $App 'docs') (Join-Path $stage 'app\docs') -Recurse -Force
}

# --- loadNapCat.js：打包时重写成相对路径 ------------------------------------
#
# **不能只改一次就算完。** 那个文件里原本是打包者的绝对路径（含用户名），
# 既泄露又让收包的人跑不起来。但 NapCat 每次启动都会按当前路径把它重写
# 回去 —— 所以每次打包都必须重写一遍，靠"上次改过了"是不行的。
$loadJs = Join-Path $napDst 'loadNapCat.js'
if (Test-Path $loadJs) {
    $relative = @'
// NapCat entry script. Rewritten at package time.
//
// The original bakes in an absolute path (with the packager's user name) -
// that both leaks it and stops working on anyone else's machine.
// Resolving relative to __dirname runs wherever it is extracted.
(async () => {
  const path = require("node:path");
  const { pathToFileURL } = require("node:url");
  await import(pathToFileURL(path.join(__dirname, "napcat.mjs")).href);
})();
'@
    [System.IO.File]::WriteAllText($loadJs, $relative, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "  loadNapCat.js rewritten to a relative path"
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

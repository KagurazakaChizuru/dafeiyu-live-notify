# ===========================================================================
#  Rebuild the netdisk package and put it where you can grab it
# ===========================================================================
#  Why this exists: the GitHub release package and the netdisk package are
#  different builds (_package.ps1 vs _package-netdisk.ps1), and the netdisk one
#  is the easy path for non-technical users - NapCat is already inside, so the
#  recipient extracts and double-clicks one file. Forgetting to rebuild it means
#  the netdisk copy stays a version behind while GitHub moves on.
#
#  Usage:
#      .\_publish-netdisk.ps1
#      .\_publish-netdisk.ps1 -Root "D:\somewhere\app" -Out "D:\share\dfy.zip"
#      .\_publish-netdisk.ps1 -Sync "D:\QuarkSync"      # copy into a synced dir
#      .\_publish-netdisk.ps1 -NoReveal                 # do not open Explorer
#
#  About "-Sync": it only COPIES the zip into a folder. Whether that folder
#  uploads anything is the netdisk client's business. Quark's PC client has no
#  documented upload API and no local sync folder on this machine (checked
#  2026-09-21), so in practice this stays a copy - the upload is still a drag
#  into the client. The switch is here for the day a synced folder exists.
#
#  Keep this file PURE ASCII - see "PowerShell script encoding" in the docs.
#  That is also why the default -Root is assembled from code points: the
#  deployment folder has a Chinese name and this file may not contain it.
# ===========================================================================

param(
    [string]$Root,
    [string]$Out,
    [string]$Sync,
    [switch]$NoReveal
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot

# Desktop\<deployment folder>, spelled without embedding Chinese in this file.
if (-not $Root) {
    $leaf = -join ([char]0x5927, [char]0x80A5, [char]0x9C7C,   # big fat fish
                   [char]0x76F4, [char]0x64AD, [char]0x59EC)  # live streaming girl
    $Root = Join-Path ([Environment]::GetFolderPath('Desktop')) $leaf
}
if (-not (Test-Path $Root)) { throw "deployment folder not found: $Root" }

if (-not $Out) { $Out = Join-Path $here 'dist\dafeiyu-netdisk.zip' }

Write-Host "building the netdisk package..."
& (Join-Path $here '_package-netdisk.ps1') -Root $Root -Out $Out

$size = [math]::Round((Get-Item $Out).Length / 1MB, 2)
$hash = (Get-FileHash $Out -Algorithm SHA256).Hash
Write-Host ""
Write-Host ("ready : {0} MB" -f $size)
Write-Host ("sha256: {0}" -f $hash)
Write-Host ("path  : {0}" -f $Out)

if ($Sync) {
    if (-not (Test-Path $Sync)) { throw "sync folder not found: $Sync" }
    Copy-Item $Out (Join-Path $Sync (Split-Path -Leaf $Out)) -Force
    Write-Host ("copied to: {0}" -f $Sync)
}

# Opening the folder with the file already selected saves the step people
# actually fumble - finding which folder the zip landed in.
if (-not $NoReveal) { Start-Process explorer.exe -ArgumentList "/select,`"$Out`"" }

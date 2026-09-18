# ===========================================================================
#  Keep the private QQ copy's version junction pointing at the real QQ install
# ===========================================================================
#  app\qq-napcat-private holds only QQ's ~8 MB of top-level files. Its
#  versions\<ver> directory is a junction into the QQ you already installed,
#  which is what keeps this folder around 8 MB instead of duplicating ~1.1 GB.
#
#  QQ updates itself: an update installs a NEW versions\<ver> directory and
#  eventually drops the old one. That leaves our junction dangling and NapCat
#  would then fail to boot QQ at all. This script re-points it before every
#  launch, so a QQ update is a non-event.
#
#  It also retires app\qq-napcat, the old full copy left behind on machines
#  that were slimmed down. Deleting it can fail while your own QQ still holds
#  a handle on a file inside, and that is fine - we simply try again next time.
#
#  Keep this file PURE ASCII. Windows PowerShell 5.1 decodes BOM-less script
#  files using the ANSI codepage; UTF-8 Chinese comments get mangled and the
#  parser dies on the first stray byte ("Missing ')' in function parameter
#  list" is the usual symptom).
# ===========================================================================

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$vers = Join-Path $here 'qq-napcat-private\versions'

# Only act on installs that have been migrated to the shared copy. Anything
# else is a layout we do not know about, so leave it alone.
if (-not (Test-Path $vers)) { exit 0 }

# --- retire the old full copy, if the slimming left one behind ---------------
$old = Join-Path $here 'qq-napcat'
if (Test-Path $old) {
    Remove-Item $old -Recurse -Force -ErrorAction SilentlyContinue
}

# --- 1. locate the real QQ installation -------------------------------------
# Same registry value NapCat's stock launcher-user.bat reads.
$qqRoot = $null
try {
    $key = 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ'
    $raw = (Get-ItemProperty -Path $key -ErrorAction Stop).UninstallString
    if ($raw) { $qqRoot = Split-Path -Parent ($raw.Trim('"')) }
} catch { }
if (-not $qqRoot) { exit 0 }

$srcVers = Join-Path $qqRoot 'versions'
if (-not (Test-Path $srcVers)) { exit 0 }

# --- 2. which version is current? -------------------------------------------
$newest = Get-ChildItem -Path $srcVers -Directory -ErrorAction SilentlyContinue |
          Sort-Object -Property Name -Descending | Select-Object -First 1
if (-not $newest) { exit 0 }

# --- 3. collect the junctions we currently have ------------------------------
$links = @(Get-ChildItem -Path $vers -Force -ErrorAction SilentlyContinue |
           Where-Object { $_.LinkType -eq 'Junction' })

# --- 4. already correct? nothing more to do ----------------------------------
foreach ($l in $links) {
    if (@($l.Target)[0] -eq $newest.FullName) { exit 0 }
}

# --- 5. stale: drop the LINKS and make a fresh one ---------------------------
# rmdir removes a junction itself; it never walks into the target's contents,
# so the real QQ installation is untouched.
foreach ($l in $links) {
    cmd /c rmdir "$($l.FullName)" 2>&1 | Out-Null
}

$dest = Join-Path $vers $newest.Name
if (-not (Test-Path $dest)) {
    try {
        New-Item -ItemType Junction -Path $dest -Target $newest.FullName -ErrorAction Stop | Out-Null
    } catch {
        # A dangling link still occupies the directory entry; clear it and retry.
        cmd /c rmdir "$dest" 2>&1 | Out-Null
        New-Item -ItemType Junction -Path $dest -Target $newest.FullName | Out-Null
    }
}
exit 0
